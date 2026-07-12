"""Автоприём выгрузок 1С, которые Google Apps Script пушит из папки Drive.

Скрипт (docs/AUTOSYNC.md) раз в N минут отправляет новые/изменённые файлы
на POST /integrations/inbox с токеном. Тип файла определяется по заголовкам
колонок, дубли отсекает построчная дедупликация импортёров.
"""

import hashlib
import io
import secrets

import openpyxl
from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..database import get_db
from ..security import hash_password
from .receipts import import_receipts_workbook
from .sales import import_sales_workbook

router = APIRouter(prefix="/integrations", tags=["integrations"])

ROBOT_EMAIL = "robot@innowave.portal"


def _require_token(authorization: str | None) -> None:
    if not settings.inbox_token:
        raise HTTPException(
            status_code=503,
            detail="Автоприём выключен: не задан INBOX_TOKEN на сервере",
        )
    expected = f"Bearer {settings.inbox_token}"
    if not authorization or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Неверный токен")


def _robot_user(db: Session) -> models.User:
    """Служебный пользователь для журнала автозагрузок (вход запрещён)."""
    user = db.query(models.User).filter_by(email=ROBOT_EMAIL).first()
    if user is None:
        user = models.User(
            email=ROBOT_EMAIL,
            full_name="Автозагрузка (Drive)",
            role=models.Role.viewer,
            is_active=False,
            hashed_password=hash_password(secrets.token_urlsafe(24)),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def sniff_kind(content: bytes) -> str:
    """Определяет тип выгрузки по заголовкам колонок в первых 20 строках."""
    try:
        wb = openpyxl.load_workbook(
            io.BytesIO(content), data_only=True, read_only=True
        )
    except Exception:
        return "not_excel"
    ws = wb[wb.sheetnames[0]]
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i >= 20:
            break
        cells = {str(c).strip() for c in row if c is not None}
        if "НоменклатураНаименование" in cells and "Сумма" in cells:
            return "sales"
        if {"Дата", "Сумма", "Контрагент", "ВидОперации"} <= cells:
            return "receipts"
    return "unknown"


@router.post("/inbox")
async def inbox(
    file: UploadFile,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    _require_token(authorization)

    content = await file.read()
    filename = file.filename or "file.xlsx"
    kind = sniff_kind(content)
    robot = _robot_user(db)

    # Один и тот же файл (по содержимому) не обрабатываем повторно —
    # экономим работу, даже с учётом построчной дедупликации.
    file_hash = hashlib.sha256(content).hexdigest()
    already = (
        db.query(models.ImportLog)
        .filter(models.ImportLog.file_hash == file_hash)
        .first()
    )
    if already:
        return {"type": kind, "status": "unchanged", "detail": "Файл уже обработан"}

    if kind == "sales":
        result = import_sales_workbook(db, content, f"[авто] {filename}", robot.id)
    elif kind == "receipts":
        result = import_receipts_workbook(db, content, f"[авто] {filename}", robot.id)
    else:
        db.add(models.ImportLog(
            filename=f"[авто, не распознан] {filename}",
            user_id=robot.id,
            added=0,
            skipped=0,
            errors_count=0,
            file_hash=file_hash,
        ))
        db.commit()
        return {
            "type": kind,
            "status": "skipped",
            "detail": "Формат пока не поддерживается — файл записан в журнал",
        }

    # Проставляем хэш файла в свежую запись журнала
    log = (
        db.query(models.ImportLog)
        .order_by(models.ImportLog.id.desc())
        .first()
    )
    if log and log.file_hash is None:
        log.file_hash = file_hash
        db.commit()

    return {"type": kind, "status": "imported", **result}
