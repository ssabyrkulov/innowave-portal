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
from .balances import (
    import_cash_balances_workbook,
    import_stock_balances_workbook,
)
from .receipts import import_receipts_workbook
from .returns import import_returns_workbook
from .sales import _row_hash, import_sales_workbook, parse_sales_workbook

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
    first_rows: list[set] = []
    all_text = ""
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i >= 20:
            break
        cells = {str(c).strip() for c in row if c is not None}
        first_rows.append(cells)
        all_text += " " + " ".join(cells)

    # Возвраты выгружаются с теми же колонками, что продажи — отличаем по
    # служебной строке 1С «Запрос: Документ.ВозвратТоваров…» или по слову
    # «Возврат» в шапке. Это спасает выручку от чужих строк.
    is_return_marked = "Возврат" in all_text

    for cells in first_rows:
        if "НоменклатураНаименование" in cells and "Сумма" in cells:
            return "return_lines" if is_return_marked else "sales"
        if {"Дата", "Сумма", "Контрагент", "ВидОперации"} <= cells:
            return "receipts"
        if "Основание" in cells or {"Документ", "Номер"} <= cells:
            return "expense"  # РКО/платёжки — импортёр в разработке
        if "Касса_Банк" in cells and "СуммаОстаток" in cells:
            return "cash_balances"
        if "СуммаОстаток" in cells and "КоличествоОстаток" in cells:
            return "stock_balances"
        if {"Дата", "Сумма", "Валюта", "Контрагент"} <= cells:
            return "return_docs"
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

    # Один и тот же файл (по содержимому) не обрабатываем повторно.
    # Хэш пишется только при успешной обработке, поэтому «отложенные»
    # форматы обработаются, как только появится их импортёр.
    file_hash = hashlib.sha256(content).hexdigest()
    already = (
        db.query(models.ImportLog)
        .filter(models.ImportLog.file_hash == file_hash)
        .first()
    )
    if already:
        return {"type": kind, "status": "unchanged", "detail": "Файл уже обработан"}

    auto_name = f"[авто] {filename}"
    if kind == "sales":
        result = import_sales_workbook(db, content, auto_name, robot.id)
    elif kind == "receipts":
        result = import_receipts_workbook(db, content, auto_name, robot.id)
    elif kind == "return_docs":
        result = import_returns_workbook(db, content, auto_name, robot.id)
    elif kind == "cash_balances":
        result = import_cash_balances_workbook(db, content, auto_name, robot.id)
    elif kind == "stock_balances":
        result = import_stock_balances_workbook(db, content, auto_name, robot.id)
    elif kind == "return_lines":
        # Возвраты в формате продаж. Финансово их несёт документный файл;
        # здесь главное — самолечение: удаляем строки этого файла, если они
        # когда-то ошибочно попали в продажи.
        parsed, _errs = parse_sales_workbook(content)
        hashes = [_row_hash(p) for p in parsed]
        removed = 0
        if hashes:
            removed = (
                db.query(models.Sale)
                .filter(models.Sale.row_hash.in_(hashes))
                .delete(synchronize_session=False)
            )
        db.add(models.ImportLog(
            filename=f"[возвраты, очистка продаж: −{removed}] {filename}",
            user_id=robot.id,
            added=0,
            skipped=len(hashes),
            errors_count=0,
            file_hash=file_hash,
        ))
        db.commit()
        return {
            "type": kind,
            "status": "cleaned",
            "removed_from_sales": removed,
        }
    else:
        db.add(models.ImportLog(
            filename=f"[авто, не распознан] {filename}",
            user_id=robot.id,
            added=0,
            skipped=0,
            errors_count=0,
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
