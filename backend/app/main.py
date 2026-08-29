import os
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text

from .config import settings
from . import models
from .database import Base, SessionLocal, database_url, engine
from .routers import (
    advances,
    agents,
    auth,
    balances,
    budget,
    checks,
    counterparties,
    dashboard,
    expenses,
    integrations,
    landed_cost,
    ledger,
    manual_entries,
    operations,
    payments,
    payroll,
    problem_docs,
    products,
    receipts,
    returns,
    purchases,
    writeoffs,
    sales,
    salesdoc,
    stock_receipts,
    stock_transfers,
    tax,
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
    if database_url.startswith("sqlite"):
        print("[startup] ВНИМАНИЕ: база — SQLite внутри контейнера. Она "
              "пересоздаётся при каждом деплое, все данные портала теряются. "
              "Задайте DATABASE_URL на сервере.", flush=True)
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


def unify_clients_by_guid() -> None:
    """Сводит переименованного в 1С контрагента обратно в одного. Разово.

    До перехода на GUID ключ строки реализации включал название клиента.
    Переименовали точку в 1С — та же самая реализация приходила с другим
    ключом и ложилась рядом со старой: один магазин становился двумя
    контрагентами, отгрузки оставались на одном, оплаты и возвраты уезжали
    на другого, и сверка показывала расхождение на пустом месте.

    Здесь это разбирается по GUID — он при переименовании не меняется:
    строкам клиента ставится его нынешнее название, ключи пересчитываются
    по GUID, а копии, которые после этого совпали, удаляются.

    Строке переписывается ключ только тогда, когда значения из базы дают в
    точности тот ключ, под которым она лежит (проверка старым хэшем). Не
    совпало — значит запись в базу что-то округлила, пересчитанный ключ
    разошёлся бы с тем, что посчитает импорт по файлу; такую строку не
    трогаем и считаем отдельно.
    """
    from sqlalchemy.orm import Session as _Session

    from . import models
    from .database import SessionLocal
    from .routers.sales import _legacy_row_hash, _row_hash

    NAME = "clients_by_guid_v1"
    db: _Session = SessionLocal()
    try:
        if db.query(models.AppMigration).filter_by(name=NAME).first():
            return
        pairs = (db.query(models.Sale.organization, models.Sale.client_guid)
                 .filter(models.Sale.client_guid.isnot(None))
                 .distinct().all())
        renamed = merged = skipped = 0
        for org, guid in pairs:
            # По одному клиенту за раз: у продаж бывают сотни тысяч строк, и
            # разовая правка не должна поднимать их в память целиком.
            rows = (db.query(models.Sale)
                    .filter(models.Sale.organization == org,
                            models.Sale.client_guid == guid)
                    .order_by(models.Sale.id).all())
            if not rows:
                continue

            def as_dict(r) -> dict:
                num = lambda v: float(v) if v is not None else None  # noqa: E731
                return {"date": r.date, "client": r.client,
                        "client_guid": r.client_guid, "product": r.product,
                        "qty": num(r.qty), "price": num(r.price),
                        "amount": num(r.amount), "doc_number": r.doc_number,
                        "warehouse": r.warehouse, "doc_total": num(r.doc_total)}

            by_key: dict[str, models.Sale] = {}
            survivors: list[models.Sale] = []
            for r in rows:
                d = as_dict(r)
                if _legacy_row_hash(d, org) != r.row_hash:
                    skipped += 1  # округление при записи — ключ не пересчитать
                    survivors.append(r)
                    continue
                key = _row_hash(d, org)
                if key not in by_key:
                    by_key[key] = r
                    r.row_hash = key
                    survivors.append(r)
                else:
                    db.delete(r)  # копия под другим названием того же клиента
                    merged += 1

            # Нынешнее название — из самой свежей строки клиента: последняя
            # выгрузка знает, как он называется сегодня. Удаляемых строк не
            # касаемся: их уже нет.
            fresh = max(rows, key=lambda r: (r.date, r.id)).client
            for r in survivors:
                if r.client != fresh:
                    r.client = fresh
                    renamed += 1
            db.flush()

        db.add(models.AppMigration(
            name=NAME,
            note=f"склеено {merged}, переименовано строк {renamed}, "
                 f"не тронуто {skipped}"))
        db.commit()
        if merged or renamed:
            print(f"[startup] Клиенты 1С сведены по GUID: склеено дублей "
                  f"{merged}, переименовано строк {renamed}, "
                  f"не тронуто {skipped}", flush=True)
    except Exception as err:  # noqa: BLE001 — портал обязан подняться и без этого
        db.rollback()
        print(f"[startup] Склейка клиентов по GUID не прошла: {err}", flush=True)
    finally:
        db.close()


def dedupe_renamed_money() -> None:
    """Убирает вторые экземпляры оплат и расходов после переименований 1С.

    Денежный документ портал узнаёт по дате, сумме и контрагенту: времени
    платежа в базе нет, пересчитать ключ нельзя. Пока контрагент входил в
    ключ, переименование делало уже загруженный платёж «незнакомым», и он
    ложился вторым экземпляром — долг клиента уезжал в минус на всю сумму его
    оплат. Импорт это больше не допускает (см. защиту по ДокументGUID), но
    накопившиеся копии надо убрать.

    Копия узнаётся по ДокументGUID: один документ 1С — одна запись. Если у
    документа несколько строк с одинаковой суммой (так бывает у расшифровок),
    они пришли одним импортом — это видно по времени загрузки, и такие строки
    остаются все. Удаляются только приехавшие позже первой загрузки документа.
    """
    from collections import defaultdict

    from sqlalchemy import func

    from . import models
    from .database import SessionLocal

    NAME = "money_dupes_v1"
    db = SessionLocal()
    try:
        if db.query(models.AppMigration).filter_by(name=NAME).first():
            return
        notes = []
        for model, label in ((models.Receipt, "оплат"),
                             (models.Expense, "расходов")):
            pairs = (db.query(model.organization, model.doc_guid)
                     .filter(model.doc_guid.isnot(None))
                     .group_by(model.organization, model.doc_guid)
                     .having(func.count(model.id) > 1).all())
            removed_rows, removed = 0, 0.0
            for org, guid in pairs:
                rows = (db.query(model)
                        .filter(model.organization == org,
                                model.doc_guid == guid)
                        .order_by(model.imported_at, model.id).all())
                same: dict[tuple, list] = defaultdict(list)
                for r in rows:
                    same[(r.date, float(r.amount or 0), r.currency,
                          r.kind)].append(r)
                for group in same.values():
                    if len(group) < 2:
                        continue
                    first = group[0].imported_at
                    for r in group[1:]:
                        late = (r.imported_at and first
                                and (r.imported_at - first).total_seconds() > 60)
                        if late:
                            removed += float(r.amount or 0)
                            removed_rows += 1
                            db.delete(r)
                db.flush()
            notes.append(f"{label}: {removed_rows} на {removed:.2f}")
            if removed_rows:
                print(f"[startup] Убраны копии {label} после переименований: "
                      f"{removed_rows} шт. на {removed:.2f}", flush=True)
        db.add(models.AppMigration(name=NAME, note="; ".join(notes)))
        db.commit()
    except Exception as err:  # noqa: BLE001 — портал обязан подняться и без этого
        db.rollback()
        print(f"[startup] Чистка денежных копий не прошла: {err}", flush=True)
    finally:
        db.close()


# Готовность БД: пока False — API отвечает понятной ошибкой, а не падает.
db_state: dict = {"ready": False, "error": None}


def init_database() -> None:
    """Подготовка схемы и первичных данных (после того, как БД доступна)."""
    Base.metadata.create_all(bind=engine)
    run_mini_migrations()
    backfill_organization()
    unify_clients_by_guid()
    dedupe_renamed_money()
    seed_initial_admin()
    # Зеркало SalesDoc: держим копию журналов в базе и обновляем в фоне
    # (дельта каждые 5 минут, полная выгрузка раз в сутки). Благодаря этому
    # карточка клиента и сверка открываются мгновенно, без запросов к SalesDoc.
    from .services import salesdoc_mirror
    salesdoc_mirror.start_background_sync()


def log_row_counts() -> None:
    """Печатает количество строк в основных таблицах при каждом запуске.

    Данные портала уже дважды обнулялись между прогонами автоприёма, и понять
    это удалось только по счётчикам в чужом логе Google Apps Script — сам
    сервис молчал. Со строкой в логе запуска видно, с чем сервис поднялся, и
    момент пропажи можно привязать к конкретной выкладке или перезапуску.
    """
    tables = [
        ("продажи", models.Sale), ("оплаты", models.Receipt),
        ("расходы", models.Expense), ("возвраты", models.ReturnDoc),
        ("закупки", models.Purchase), ("списания", models.WriteOff),
        ("налоговая", models.TaxOperation), ("пользователи", models.User),
        ("связки НАЛ↔УПР", models.TaxClientLink),
        ("алиасы плательщиков", models.ClientAlias),
    ]
    parts = []
    with SessionLocal() as db:
        for label, model in tables:
            try:
                parts.append(f"{label} {db.query(model).count()}")
            except Exception as err:  # noqa: BLE001 — диагностика не роняет старт
                parts.append(f"{label} ? ({type(err).__name__})")
    print("[startup] В базе: " + " · ".join(parts), flush=True)


def startup_sequence() -> None:
    wait_for_db()
    init_database()
    db_state["ready"] = True
    db_state["error"] = None
    log_row_counts()
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


def _frontend_build() -> dict:
    """Имя главного JS-бандла и время сборки статики — отпечаток версии."""
    try:
        assets = STATIC_DIR / "assets"
        js = sorted(f.name for f in assets.glob("index-*.js"))
        index = STATIC_DIR / "index.html"
        return {
            "bundle": js[0] if js else None,
            "built_at": datetime.fromtimestamp(
                index.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
        }
    except Exception:  # статики нет — локальный запуск только API
        return {"bundle": None, "built_at": None}


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
        # Отпечаток собранного фронтенда: имя главного бандла хешируется по
        # содержимому, поэтому оно меняется от сборки к сборке. Если в браузере
        # загружен другой файл — открыта старая версия из кэша, а не новый
        # деплой. Это единственный способ отличить «не задеплоилось» от
        # «задеплоилось, но браузер показывает старое».
        "frontend": _frontend_build(),
        # Видно, поднялась ли база: при обслуживании сервис жив, db=false.
        "db": db_state["ready"],
        "db_error": db_state["error"],
        # К какой именно базе подключён сервис и переживёт ли она деплой.
        # Без DATABASE_URL приложение молча берёт SQLite-файл внутри
        # контейнера, а контейнер пересоздаётся при каждой выкладке: данные
        # обнуляются, и понять это можно было только по обнулившимся счётчикам
        # в журнале загрузок.
        "db_target": db_target(),
        "db_persistent": not database_url.startswith("sqlite"),
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
app.include_router(tax.router)
app.include_router(purchases.router)
app.include_router(writeoffs.router)
app.include_router(stock_receipts.router)
app.include_router(products.router)
app.include_router(counterparties.router)
app.include_router(stock_transfers.router)
app.include_router(landed_cost.router)
app.include_router(problem_docs.router)
app.include_router(manual_entries.router)
app.include_router(advances.router)
app.include_router(payroll.router)
app.include_router(ledger.router)

# --- Frontend (single-service deploy) -------------------------------------
# When the built React app is present in backend/static (created by the
# Docker build), serve it from the same origin: assets under /assets and
# index.html for every non-API path so client-side routing works.
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

if STATIC_DIR.is_dir():
    app.mount(
        "/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets"
    )

    # index.html имя не хеширует, поэтому браузер по умолчанию держит его в
    # кэше и после нового деплоя открывает старую сборку — со ссылками на
    # старые файлы /assets. Внешне это выглядит как «изменений нет».
    # Ссылки внутри index.html хешированные, так что перепроверять его на
    # каждый заход дёшево, а сами /assets остаются кэшируемыми навсегда.
    _NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        candidate = (STATIC_DIR / full_path).resolve()
        if (
            full_path
            and candidate.is_file()
            and candidate.is_relative_to(STATIC_DIR)
        ):
            return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html", headers=_NO_CACHE)

else:

    @app.get("/", tags=["health"], include_in_schema=False)
    def root():
        return {"status": "ok", "service": "innowave-portal", "docs": "/docs"}
