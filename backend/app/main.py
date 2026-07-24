import os
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


def wait_for_db(attempts: int = 12, base_delay: float = 2.0) -> None:
    """Ждёт готовности БД перед инициализацией.

    Бесплатный PostgreSQL на Render иногда не успевает принять соединение к
    моменту старта контейнера — раньше одно такое «моргание» роняло весь
    деплой (Application startup failed → Render откатывался на старую версию).
    Повторяем подключение с нарастающей паузой, чтобы транзиентный таймаут
    не срывал выкладку.
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


@app.on_event("startup")
def on_startup() -> None:
    wait_for_db()
    Base.metadata.create_all(bind=engine)
    run_mini_migrations()
    backfill_organization()
    seed_initial_admin()
    # Фоновый прогрев кэша SalesDoc по умолчанию ВЫКЛЮЧЕН: он держал в памяти
    # сырые выгрузки за 3 года и на инстансе 512 МБ приводил к падению по
    # памяти (перезапуск → снова прогрев → 502). Включается осознанно через
    # SALESDOC_WARM_CACHE=1 на инстансе с достаточной памятью.
    if os.environ.get("SALESDOC_WARM_CACHE") == "1":
        from .services import salesdoc
        salesdoc.start_background_warmer()


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
