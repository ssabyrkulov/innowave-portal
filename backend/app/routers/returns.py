"""Возвраты товаров от покупателей (документный уровень, файл ВыгрузкаВозв:
Дата / Сумма / Валюта / Контрагент). Уменьшают долг клиента в дебиторке."""

import hashlib
import io
from collections import defaultdict
from datetime import date, datetime

import openpyxl
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import get_current_user

router = APIRouter(prefix="/returns", tags=["returns"])

HEADERS = {"Дата": "date", "Сумма": "amount", "Валюта": "currency",
           "Контрагент": "client"}


def _parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value.split()[0], "%d.%m.%Y").date()
        except ValueError:
            return None
    return None


def import_returns_workbook(
    db: Session,
    content: bytes,
    filename: str,
    user_id: int,
) -> dict:
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except Exception:
        raise HTTPException(status_code=400, detail="Не удалось открыть файл Excel")

    ws = wb[wb.sheetnames[0]]
    rows_iter = ws.iter_rows(values_only=True)

    header_idx, columns, buffered = None, {}, []
    for i, row in enumerate(rows_iter):
        buffered.append(row)
        if i >= 20:
            break
        matched = {
            j: HEADERS[str(c).strip()]
            for j, c in enumerate(row)
            if c is not None and str(c).strip() in HEADERS
        }
        if {"date", "amount", "client"} <= set(matched.values()):
            header_idx, columns = i, matched
            break
    if header_idx is None:
        raise HTTPException(
            status_code=400,
            detail="Не найдены колонки возвратов (Дата, Сумма, Валюта, Контрагент)",
        )

    parsed, errors = [], []

    def process(row, line_no):
        data = {f: (row[j] if j < len(row) else None) for j, f in columns.items()}
        if all(v is None for v in data.values()):
            return
        dt = _parse_date(data.get("date"))
        try:
            amount = float(str(data.get("amount")).replace(" ", "").replace(",", "."))
        except (TypeError, ValueError):
            amount = None
        client = str(data.get("client") or "").strip()
        if not dt or amount is None or not client:
            if len(errors) < 20:
                errors.append(f"Строка {line_no}: нет даты, суммы или контрагента")
            return
        parsed.append({
            "src_dt": str(data.get("date")),
            "date": dt,
            "amount": amount,
            "currency": (str(data.get("currency") or "KGS").strip() or "KGS")[:3],
            "client": client,
        })

    line_no = header_idx + 1
    for line_no, row in enumerate(buffered[header_idx + 1:], start=header_idx + 2):
        process(row, line_no)
    for line_no, row in enumerate(rows_iter, start=line_no + 1):
        process(row, line_no)

    # Возвраты — полная выгрузка за всю историю, поэтому грузим «заменой
    # целиком»: это самоисцеляет таблицу, если в неё раньше по ошибке попал
    # чужой файл (напр. исходящие платежи).
    db.query(models.ReturnDoc).delete()
    db.flush()
    existing: set[str] = set()
    seen: set[str] = set()
    occurrences: dict[str, int] = defaultdict(int)
    added = skipped = 0
    for p in parsed:
        base = "|".join(str(p[f]) for f in ("src_dt", "amount", "currency", "client"))
        occurrences[base] += 1
        h = hashlib.sha256(f"{base}#{occurrences[base]}".encode()).hexdigest()
        p = {k: v for k, v in p.items() if k != "src_dt"}
        if h in existing or h in seen:
            skipped += 1
            continue
        seen.add(h)
        db.add(models.ReturnDoc(**p, row_hash=h))
        added += 1

    db.add(models.ImportLog(
        filename=f"[возвраты] {filename}",
        user_id=user_id,
        added=added,
        skipped=skipped,
        errors_count=len(errors),
    ))
    db.commit()
    return {
        "added": added,
        "skipped_duplicates": skipped,
        "errors": errors,
        "total_in_db": db.query(models.ReturnDoc).count(),
    }


@router.get("")
def list_returns(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    limit: int = Query(default=300, le=1000),
):
    rows = (
        db.query(models.ReturnDoc)
        .order_by(models.ReturnDoc.date.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "date": r.date.isoformat(),
            "amount": float(r.amount),
            "currency": r.currency,
            "client": r.client,
        }
        for r in rows
    ]
