"""Снапшоты остатков: деньги по кассам/счетам и товары по складам.

Файлы 1С — это «остатки на сейчас», поэтому каждый импорт полностью
заменяет предыдущий снимок.
"""

import io
from datetime import datetime

import openpyxl
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import get_current_user

router = APIRouter(prefix="/balances", tags=["balances"])


def _num(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(" ", "").replace(",", ""))
    except ValueError:
        try:
            return float(str(value).replace(" ", "").replace(",", "."))
        except ValueError:
            return None


def _load_rows(content: bytes):
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except Exception:
        raise HTTPException(status_code=400, detail="Не удалось открыть файл Excel")
    ws = wb[wb.sheetnames[0]]
    return list(ws.iter_rows(values_only=True))


def import_cash_balances_workbook(
    db: Session, content: bytes, filename: str, user_id: int
) -> dict:
    rows = _load_rows(content)
    header_idx = None
    for i, row in enumerate(rows[:20]):
        cells = {str(c).strip() for c in row if c is not None}
        if "Касса_Банк" in cells and "СуммаОстаток" in cells:
            header_idx = i
            break
    if header_idx is None:
        raise HTTPException(status_code=400, detail="Не найдены колонки остатков денег")

    db.query(models.CashBalance).delete()
    added = 0
    now = datetime.utcnow()
    for row in rows[header_idx + 1:]:
        if not row or row[0] is None:
            continue
        amount = _num(row[1] if len(row) > 1 else None)
        if amount is None:
            continue
        db.add(models.CashBalance(
            account=str(row[0]).strip(), amount=amount, updated_at=now
        ))
        added += 1

    db.add(models.ImportLog(
        filename=f"[остатки денег] {filename}", user_id=user_id,
        added=added, skipped=0, errors_count=0,
    ))
    db.commit()
    return {"added": added, "snapshot": True}


def import_stock_balances_workbook(
    db: Session, content: bytes, filename: str, user_id: int
) -> dict:
    rows = _load_rows(content)
    header_idx = None
    for i, row in enumerate(rows[:20]):
        cells = {str(c).strip() for c in row if c is not None}
        if "СуммаОстаток" in cells and "КоличествоОстаток" in cells:
            header_idx = i
            break
    if header_idx is None:
        raise HTTPException(status_code=400, detail="Не найдены колонки остатков товаров")

    # Файл иерархический: строка товара, затем строки его складов.
    # Склад узнаём по справочнику складов из продаж.
    known_warehouses = {
        w for (w,) in db.query(models.Sale.warehouse).distinct() if w
    }

    db.query(models.StockBalance).delete()
    added = 0
    now = datetime.utcnow()
    current_product = None
    for row in rows[header_idx + 1:]:
        if not row or row[0] is None:
            continue
        name = str(row[0]).strip()
        if name == "Итог":
            continue
        amount = _num(row[1] if len(row) > 1 else None)
        qty = _num(row[2] if len(row) > 2 else None)
        if amount is None and qty is None:
            continue
        if name in known_warehouses and current_product:
            db.add(models.StockBalance(
                product=current_product, warehouse=name,
                amount=amount or 0, qty=qty or 0, updated_at=now,
            ))
            added += 1
        else:
            current_product = name

    db.add(models.ImportLog(
        filename=f"[остатки товаров] {filename}", user_id=user_id,
        added=added, skipped=0, errors_count=0,
    ))
    db.commit()
    return {"added": added, "snapshot": True}


@router.get("/cash")
def cash_balances(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    rows = db.query(models.CashBalance).all()
    return {
        "total": round(sum(float(r.amount) for r in rows), 2),
        "updated_at": rows[0].updated_at.isoformat() if rows else None,
        "accounts": [
            {"account": r.account, "amount": float(r.amount)} for r in rows
        ],
    }


@router.get("/stock")
def stock_balances(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    rows = db.query(models.StockBalance).all()
    return {
        "total_amount": round(sum(float(r.amount) for r in rows), 2),
        "total_qty": round(sum(float(r.qty) for r in rows), 3),
        "updated_at": rows[0].updated_at.isoformat() if rows else None,
        "items": [
            {
                "product": r.product,
                "warehouse": r.warehouse,
                "amount": float(r.amount),
                "qty": float(r.qty),
            }
            for r in rows
        ],
    }
