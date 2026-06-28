from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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


@app.get("/", tags=["health"])
def health():
    return {"status": "ok", "service": "payment-calendar"}


app.include_router(auth.router)
app.include_router(users.router)
app.include_router(payments.router)
