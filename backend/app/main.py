from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .database import Base, engine
from .routers import auth, payments, users
from .seed import seed_initial_admin

app = FastAPI(
    title="InnoWave Group — Payment Calendar API",
    description="Платёжный календарь с пользователями и правами доступа",
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
    seed_initial_admin()


@app.get("/healthz", tags=["health"])
def health():
    return {"status": "ok", "service": "payment-calendar"}


app.include_router(auth.router)
app.include_router(users.router)
app.include_router(payments.router)

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
        return {"status": "ok", "service": "payment-calendar", "docs": "/docs"}
