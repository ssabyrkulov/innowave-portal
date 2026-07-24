import os
import threading
import time
import traceback
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text

from .config import settings
from .database import Base, engine
from .routers import (
    agents,
    auth,
    balances,
    budget,
    checks,
    dashboard,
    expenses,
    integrations,
    operations,
    payments,
    receipts,
    returns,
    sales,
    salesdoc,
    users,
)
from .seed import seed_initial_admin


def run_mini_migrations() -> None:
    """Добавляет недостающие колонки в существующие таблицы.

    Base.metadata.create_all создаёт только новые таблицы; при добавлении
    полей в модель уже развёрнутая база их не получит — досоздаём вручную.
    """
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                ddl_type = column.type.compile(engine.dialect)
                conn.execute(text(
                    f'ALTER TABLE {table.name} ADD COLUMN {column.name} {ddl_type}'
                ))

app = FastAPI(
    title="InnoWave Group — Corporate Portal API",
    description="Корпоративный портал: платёжный календарь и другие модули, "
    "пользователи и права доступа",
    version="1.0.0",
)

# Auth uses Bearer tokens (not cookies), so credentials are not required and a
# wildcard origin is safe. Restrict via the CORS_ORIGINS env var in production.
origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def db_target() -> str:
    """Хост и имя БД без пароля — чтобы в логах было видно, к какой именно базе
    подключается сервис (при смене DATABASE_URL сразу ясно, подхватилась ли)."""
    try:
        from .database import database_url
        rest = database_url.split("://", 1)[-1]
        creds, _, addr = rest.rpartition("@")
        user = creds.split(":", 1)[0] if creds else ""
        return f"{user + '@' if user else ''}{addr}"
    except Exception:  # noqa: BLE001 — диагностика не должна ронять старт
        return "(не удалось определить)"


def wait_for_db(attempts: int = 40, base_delay: float = 2.0) -> None:
    """Ждёт готовности БД перед инициализацией.

    PostgreSQL на Render бывает недоступен несколько минут — при плановом
    обслуживании внутренний хост перестаёт резолвиться. Раньше мы ждали ~2
    минуты и выходили: Render перезапускал контейнер, тот падал снова —
    крэш-луп и 502 на всё время работ. Теперь ждём ~10 минут, чтобы спокойно
    пережить обслуживание, а если и это не помогло — сервис всё равно
    стартует (см. on_startup) и не уходит в бесконечный перезапуск.
    """
    print(f"[startup] Подключаюсь к БД: {db_target()}", flush=True)
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except Exception as err:  # noqa: BLE001 — ждём любую ошибку соединения
            last_err = err
            delay = min(base_delay * (i + 1), 15.0)
            print(
                f"[startup] БД пока недоступна (попытка {i + 1}/{attempts}): "
                f"{err.__class__.__name__}. Повтор через {delay:.0f}s…",
                flush=True,
            )
            time.sleep(delay)
    raise RuntimeError(
        f"Не удалось подключиться к БД после {attempts} попыток"
    ) from last_err


def backfill_organization() -> None:
    """Проставляет organization='hygiene' существующим строкам (данные,
    загруженные до появления мультиучёта). Идемпотентно."""
    tables = ["sales", "receipts", "expenses", "return_docs",
              "cash_balances", "stock_balances"]
    inspector = inspect(engine)
    with engine.begin() as conn:
        for t in tables:
            if not inspector.has_table(t):
                continue
            cols = {c["name"] for c in inspector.get_columns(t)}
            if "organization" not in cols:
                continue
            conn.execute(text(
                f"UPDATE {t} SET organization='hygiene' WHERE organization IS NULL"
            ))
        # Категория помеченных контрагентов: старые записи = безнадёжные.
        if inspector.has_table("bad_debt_clients"):
            cols = {c["name"] for c in inspector.get_columns("bad_debt_clients")}
            if "kind" in cols:
                conn.execute(text(
                    "UPDATE bad_debt_clients SET kind='bad_debt' WHERE kind IS NULL"
                ))


# Готовность БД: пока False — API отвечает понятной ошибкой, а не падает.
db_state: dict = {"ready": False, "error": None}


def init_database() -> None:
    """Подготовка схемы и первичных данных (после того, как БД доступна)."""
    Base.metadata.create_all(bind=engine)
    run_mini_migrations()
    backfill_organization()
    seed_initial_admin()
    # Зеркало SalesDoc: держим копию журналов в базе и обновляем в фоне
    # (дельта каждые 5 минут, полная выгрузка раз в сутки). Благодаря этому
    # карточка клиента и сверка открываются мгновенно, без запросов к SalesDoc.
    from .services import salesdoc_mirror
    salesdoc_mirror.start_background_sync()


def startup_sequence() -> None:
    wait_for_db()
    init_database()
    db_state["ready"] = True
    db_state["error"] = None
    print("[startup] БД готова, сервис поднят", flush=True)


def retry_startup_in_background() -> None:
    """Продолжаем поднимать БД в фоне, не роняя процесс.

    Важно: сервис уже слушает порт, поэтому пользователь видит понятное
    сообщение вместо 502, а Render не крутит бесконечные перезапуски.
    Как только база вернётся (например, после обслуживания) — портал
    заработает сам, без ручного вмешательства."""
    def loop():
        while not db_state["ready"]:
            try:
                startup_sequence()
            except Exception as err:  # noqa: BLE001 — фон не должен падать
                db_state["error"] = f"{type(err).__name__}: {err}"
                print(f"[startup] БД всё ещё недоступна: {err}. Повтор через 30s…",
                      flush=True)
                time.sleep(30)

    threading.Thread(target=loop, name="db-init-retry", daemon=True).start()


@app.on_event("startup")
def on_startup() -> None:
    try:
        startup_sequence()
    except Exception as err:  # noqa: BLE001 — стартуем даже без БД
        db_state["error"] = f"{type(err).__name__}: {err}"
        print(
            "[startup] Не удалось подключиться к БД. Сервис стартует без неё и "
            "продолжит попытки в фоне (API вернёт 503 до восстановления).",
            flush=True,
        )
        retry_startup_in_background()


@app.middleware("http")
async def guard_db(request: Request, call_next):
    """Пока БД не поднялась — отдаём понятное 503 на API вместо 500/502.
    Статика и /healthz работают, чтобы страница открывалась и было видно
    состояние сервиса."""
    path = request.url.path
    is_api = path.startswith("/api/") or any(
        path.startswith(f"/{p}/") or path == f"/{p}"
        for p in ("auth", "users", "payments", "sales", "checks", "receipts",
                  "agents", "dashboard", "integrations", "returns", "balances",
                  "expenses", "budget", "operations", "salesdoc")
    )
    if is_api and not db_state["ready"]:
        return JSONResponse(
            status_code=503,
            content={"detail": "База данных временно недоступна (идёт "
                               "обслуживание). Сервис восстановится "
                               "автоматически — обновите страницу через "
                               "несколько минут."},
        )
    return await call_next(request)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Необработанные исключения отдаём с текстом, а не «пустым» 500 — чтобы на
    фронте вместо безликого «Ошибка запроса» была видна реальная причина."""
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={"detail": f"Внутренняя ошибка: {type(exc).__name__}: {exc}"},
    )


@app.get("/healthz", tags=["health"])
def health():
    # modules — быстрая проверка, какая версия развёрнута: если в списке
    # есть "expenses", значит модуль расходов уже задеплоен.
    modules = sorted(
        {
            r.path.split("/")[1]
            for r in app.routes
            if getattr(r, "path", "").count("/") >= 1 and r.path != "/"
        }
        - {"", "healthz", "docs", "openapi.json", "redoc", "assets", "{full_path:path}"}
    )
    # Render кладёт короткий SHA коммита в RENDER_GIT_COMMIT — по нему видно,
    # какая именно версия развёрнута.
    commit = os.environ.get("RENDER_GIT_COMMIT", "")[:7] or "dev"
    return {
        "status": "ok",
        "service": "innowave-portal",
        "commit": commit,
        # Видно, поднялась ли база: при обслуживании сервис жив, db=false.
        "db": db_state["ready"],
        "db_error": db_state["error"],
        "modules": modules,
    }


app.include_router(auth.router)
app.include_router(users.router)
app.include_router(payments.router)
app.include_router(sales.router)
app.include_router(checks.router)
app.include_router(receipts.router)
app.include_router(agents.router)
app.include_router(dashboard.router)
app.include_router(integrations.router)
app.include_router(returns.router)
app.include_router(balances.router)
app.include_router(expenses.router)
app.include_router(budget.router)
app.include_router(operations.router)
app.include_router(salesdoc.router)

# --- Frontend (single-service deploy) -------------------------------------
# When the built React app is present in backend/static (created by the
# Docker build), serve it from the same origin: assets under /assets and
# index.html for every non-API path so client-side routing works.
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

if STATIC_DIR.is_dir():
    app.mount(
        "/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets"
    )

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        candidate = (STATIC_DIR / full_path).resolve()
        if (
            full_path
            and candidate.is_file()
            and candidate.is_relative_to(STATIC_DIR)
        ):
            return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html")

else:

    @app.get("/", tags=["health"], include_in_schema=False)
    def root():
        return {"status": "ok", "service": "innowave-portal", "docs": "/docs"}
