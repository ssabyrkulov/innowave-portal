from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text

from .config import settings
from .database import Base, engine
from .routers import (
    agents,
    auth,
    balances,
    checks,
    dashboard,
    integrations,
    payments,
    receipts,
    returns,
    sales,
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


@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(bind=engine)
    run_mini_migrations()
    seed_initial_admin()


@app.get("/healthz", tags=["health"])
def health():
    return {"status": "ok", "service": "innowave-portal"}


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
