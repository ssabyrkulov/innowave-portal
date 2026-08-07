"""Снапшоты остатков: деньги по кассам/счетам и товары по складам.

Файлы 1С — это «остатки на сейчас», поэтому каждый импорт полностью
заменяет предыдущий снимок.
"""

import io
from collections import Counter
from datetime import datetime

import openpyxl
from fastapi import APIRouter, Depends, HTTPException, Query
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
    """Строки первого листа книги.

    Под защитой весь разбор, а не только открытие: на некоторых файлах 1С
    ошибка возникала уже при чтении листа и уходила наружу «сырым» 500.
    Читаем без read_only — этот режим разбирает XML лениво и на файлах с
    нестандартной разметкой ведёт себя капризно."""
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
        ws = wb[wb.sheetnames[0]]
        return list(ws.iter_rows(values_only=True))
    except HTTPException:
        raise
    except Exception as err:  # noqa: BLE001 — важен понятный ответ, не тип
        raise HTTPException(
            status_code=400,
            detail=f"Не удалось прочитать файл Excel: {type(err).__name__}: {err}",
        ) from err


def import_cash_balances_workbook(
    db: Session, content: bytes, filename: str, user_id: int,
    org: str = models.DEFAULT_ORG,
) -> dict:
    org = models.normalize_org(org)
    rows = _load_rows(content)
    header_idx = None
    for i, row in enumerate(rows[:20]):
        cells = {str(c).strip() for c in row if c is not None}
        if "Касса_Банк" in cells and "СуммаОстаток" in cells:
            header_idx = i
            break
    if header_idx is None:
        raise HTTPException(status_code=400, detail="Не найдены колонки остатков денег")

    # Как и с товарами: сначала разбираем, потом заменяем — иначе пустая
    # выгрузка стёрла бы остатки денег.
    parsed: list[tuple[str, float]] = []
    for row in rows[header_idx + 1:]:
        if not row or row[0] is None:
            continue
        amount = _num(row[1] if len(row) > 1 else None)
        if amount is None:
            continue
        parsed.append((str(row[0]).strip(), amount))

    if not parsed:
        db.add(models.ImportLog(
            filename=f"[остатки денег, пусто] {filename}", user_id=user_id,
            added=0, skipped=0, errors_count=0,
        ))
        db.commit()
        return {
            "added": 0, "snapshot": True, "empty": True,
            "detail": "В файле нет строк остатков — прежние данные сохранены.",
        }

    db.query(models.CashBalance).filter(models.CashBalance.organization == org).delete()
    now = datetime.utcnow()
    for account, amount in parsed:
        db.add(models.CashBalance(
            account=account, amount=amount,
            organization=org, updated_at=now,
        ))

    db.add(models.ImportLog(
        filename=f"[остатки денег] {filename}", user_id=user_id,
        added=len(parsed), skipped=0, errors_count=0,
    ))
    db.commit()
    return {"added": len(parsed), "snapshot": True}


def _import_stock_flat(db: Session, data_rows, header_cells: dict,
                       col_product: int, col_store: int,
                       filename: str, user_id: int, org: str) -> dict:
    """Плоская выгрузка остатков (наша обработка 1С): строка = товар на складе.

    Колонки ищутся по именам, лишние не мешают. НоменклатураGUID — по желанию:
    с ним товар связывается с SalesDoc по идентификатору, а не по названию."""
    col_qty = header_cells.get("КоличествоОстаток")
    col_amount = header_cells.get("СуммаОстаток")
    col_guid = header_cells.get("НоменклатураGUID")

    def cell(row, j):
        return row[j] if j is not None and j < len(row) else None

    parsed = []
    for row in data_rows:
        if not row:
            continue
        product = str(cell(row, col_product) or "").strip()
        if not product or product == "Итог":
            continue
        qty = _num(cell(row, col_qty))
        amount = _num(cell(row, col_amount))
        if qty is None and amount is None:
            continue
        parsed.append(models.StockBalance(
            organization=org,
            product=product,
            product_guid=str(cell(row, col_guid) or "").strip() or None,
            warehouse=str(cell(row, col_store) or "").strip() or None,
            qty=qty or 0,
            amount=amount or 0,
        ))

    if not parsed:
        db.add(models.ImportLog(
            filename=f"[остатки товаров, пусто] {filename}", user_id=user_id,
            added=0, skipped=0, errors_count=0,
        ))
        db.commit()
        return {"added": 0, "snapshot": True, "empty": True,
                "detail": "В файле нет строк остатков — прежние данные сохранены."}

    # Снимок: замена целиком в рамках организации (как у иерархического).
    db.query(models.StockBalance).filter(
        models.StockBalance.organization == org).delete(synchronize_session=False)
    now = datetime.utcnow()
    for p in parsed:
        p.updated_at = now
        db.add(p)
    db.add(models.ImportLog(
        filename=f"[остатки товаров] {filename}", user_id=user_id,
        added=len(parsed), skipped=0, errors_count=0,
    ))
    db.commit()
    return {"added": len(parsed), "snapshot": True, "flat": True}


def import_stock_balances_workbook(
    db: Session, content: bytes, filename: str, user_id: int,
    org: str = models.DEFAULT_ORG,
) -> dict:
    org = models.normalize_org(org)
    rows = _load_rows(content)
    header_idx = None
    for i, row in enumerate(rows[:20]):
        cells = {str(c).strip() for c in row if c is not None}
        if "СуммаОстаток" in cells and "КоличествоОстаток" in cells:
            header_idx = i
            break
    if header_idx is None:
        raise HTTPException(status_code=400, detail="Не найдены колонки остатков товаров")

    # Плоский формат нашей обработки: склад и номенклатура — отдельными
    # колонками, одна строка = товар на складе. Надёжнее иерархического
    # отчёта: не нужно угадывать, что в первой колонке — товар или склад.
    header_cells = {str(c).strip(): j for j, c in enumerate(rows[header_idx])
                    if c is not None}
    flat_product = next((header_cells[k] for k in
                         ("НоменклатураНаименование", "Номенклатура")
                         if k in header_cells), None)
    flat_store = next((header_cells[k] for k in
                       ("СкладНаименование", "Склад") if k in header_cells), None)
    if flat_product is not None and flat_store is not None:
        return _import_stock_flat(db, rows[header_idx + 1:], header_cells,
                                  flat_product, flat_store, filename, user_id, org)

    # Файл иерархический: строка товара, затем строки его складов.
    # Склад узнаём по справочнику складов из продаж этой организации.
    known_warehouses = {
        w for (w,) in db.query(models.Sale.warehouse)
        .filter(models.Sale.organization == org).distinct() if w
    }
    if not known_warehouses:  # продажи этой фирмы ещё не загружены — берём все
        known_warehouses = {w for (w,) in db.query(models.Sale.warehouse).distinct() if w}

    # Структурный запас: в иерархии склад повторяется под многими товарами, а
    # товар уникален. Имена, встречающиеся ≥2 раз, считаем складами — тогда
    # разбор не зависит от того, загружены ли уже продажи.
    data_rows = rows[header_idx + 1:]
    counts = Counter(
        str(r[0]).strip() for r in data_rows
        if r and r[0] is not None and str(r[0]).strip() not in ("", "Итог")
    )
    known_warehouses |= {n for n, c in counts.items() if c >= 2}

    # Сначала разбираем, и только потом трогаем базу: снимок заменяет данные
    # целиком, поэтому пустая выгрузка стёрла бы остатки подчистую.
    parsed: list[tuple[str, str, float, float]] = []
    current_product = None
    for row in data_rows:
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
            parsed.append((current_product, name, amount or 0, qty or 0))
        else:
            current_product = name

    if not parsed:
        # Файл без строк (в 1С выгрузили пустой отчёт) — прежние остатки
        # оставляем нетронутыми и честно говорим, что грузить было нечего.
        db.add(models.ImportLog(
            filename=f"[остатки товаров, пусто] {filename}", user_id=user_id,
            added=0, skipped=0, errors_count=0,
        ))
        db.commit()
        return {
            "added": 0, "snapshot": True, "empty": True,
            "detail": "В файле нет строк остатков — прежние данные сохранены. "
                      "Проверьте выгрузку в 1С (отчёт выгрузился пустым).",
        }

    db.query(models.StockBalance).filter(models.StockBalance.organization == org).delete()
    now = datetime.utcnow()
    for product, warehouse, amount, qty in parsed:
        db.add(models.StockBalance(
            product=product, warehouse=warehouse,
            amount=amount, qty=qty,
            organization=org, updated_at=now,
        ))
    added = len(parsed)

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
    org: str = Query(default="all"),
):
    rows = models.org_scope(db.query(models.CashBalance), models.CashBalance, org).all()
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
    org: str = Query(default="all"),
):
    rows = models.org_scope(db.query(models.StockBalance), models.StockBalance, org).all()
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
