from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings

# Some providers (e.g. Render, Heroku) hand out a legacy "postgres://" URL,
# but SQLAlchemy requires the "postgresql://" scheme.
database_url = settings.database_url
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

connect_args = (
    {"check_same_thread": False} if database_url.startswith("sqlite") else {}
)

engine = create_engine(database_url, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
