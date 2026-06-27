from sqlalchemy.orm import Session

from . import models
from .config import settings
from .database import SessionLocal
from .security import hash_password


def seed_initial_admin() -> None:
    """Create the first admin account if there are no users yet."""
    db: Session = SessionLocal()
    try:
        has_users = db.query(models.User).first() is not None
        if has_users:
            return
        admin = models.User(
            email=settings.first_admin_email,
            full_name=settings.first_admin_name,
            role=models.Role.admin,
            is_active=True,
            hashed_password=hash_password(settings.first_admin_password),
        )
        db.add(admin)
        db.commit()
        print(f"[seed] Created initial admin: {settings.first_admin_email}")
    finally:
        db.close()
