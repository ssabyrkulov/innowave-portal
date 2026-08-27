"""Оприходование товаров — файл «…_Оприходование товаров».

Построчный формат 1С: Дата, Номер, Организация, Склад, Основание,
Номенклатура, ЕдИзм, Количество, Цена, Сумма, СчетУчета, СчетОприходования,
Комментарий, ДокументGUID.

Это приход на склад без поставщика: излишки инвентаризации, возврат из
эксплуатации, ввод остатков. До появления этого импортёра расчёт остатков
видел только половину инвентаризации — недостачи через списания были, а
излишки нет, и расчётный остаток систематически занижался против 1С.

Сама «Инвентаризация товаров» сюда не входит намеренно: в 1С этот документ
проводок не делает, он лишь фиксирует отклонение. Товар двигают созданные
на его основании оприходование (излишек) и списание (недостача) — оба уже
загружаются. Считать ещё и инвентаризацию значило бы задвоить движение.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import models, onec
from ..database import get_db
from ..deps import get_current_user

router = APIRouter(prefix="/stock-receipts", tags=["stock-receipts"])

HEADERS = {
    "Дата": "date",
    "Номер": "doc_number",
    "Склад": "warehouse",
    "СкладНаименование": "warehouse",
    "Номенклатура": "product",
    "НоменклатураНаименование": "product",
    "НоменклатураGUID": "product_guid",
    "Количество": "qty",
    "ЕдИзм": "unit",
    "ЕдиницаИзмерения": "unit",
    "Цена": "price",
    "Сумма": "amount",
    "Основание": "basis",
    "СчетОприходования": "account",
    "СчетУчета": "account_fallback",
    "Комментарий": "comment",
    "ДокументGUID": "doc_guid",
    # Непроведённые и помеченные на удаление — не операции.
    **onec.header_map(),
}


def import_stock_receipts_workbook(db: Session, content: bytes, filename: str,
                                   user_id: int,
                                   org: str = models.DEFAULT_ORG) -> dict:
    """Импорт оприходований. Файл выгружается за всю историю, поэтому загрузка
    заменяет данные организации целиком; сначала разбор, потом замена — битый
    файл не может стереть уже загруженное."""
    from ..services import xlsx  # читалка с починкой архива 1С ред. 1.7
    from .tax import _day, _num

    org = models.normalize_org(org)
    wb = xlsx.load_workbook(content)
    ws = wb[wb.sheetnames[0]]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]

    header_idx, col = None, {}
    for i, row in enumerate(rows[:10]):
        names = {str(c).strip(): j for j, c in enumerate(row) if c}
        if "Дата" in names and "Количество" in names and (
                "Номенклатура" in names or "НоменклатураНаименование" in names):
            header_idx = i
            col = {HEADERS[k]: j for k, j in names.items() if k in HEADERS}
            break
    if header_idx is None:
        # Называем то, что реально нашли: следующая правка обработки 1С
        # переименует колонку, и по этому тексту сразу видно, какую именно.
        found = ", ".join(str(c).strip() for c in (rows[0] if rows else [])
                          if c is not None)[:300]
        raise HTTPException(
            status_code=400,
            detail="Не найдены колонки оприходования (ожидаются Дата, "
                   f"Номенклатура, Количество). В файле: {found or 'пусто'}")

    def cell(row, key):
        j = col.get(key)
        return row[j] if j is not None and j < len(row) else None

    def text(row, key):
        return str(cell(row, key) or "").strip() or None

    parsed: list[models.StockReceipt] = []
    not_posted = 0
    for row in rows[header_idx + 1:]:
        if onec.skip_reason({k: cell(row, k) for k in ("_posted", "_deleted")}):
            not_posted += 1
            continue
        d = _day(cell(row, "date"))
        qty = _num(cell(row, "qty"))
        product = text(row, "product")
        # Оприходование основного средства идёт тем же документом, но без
        # номенклатуры — товарного движения в нём нет. Пустой хвост файла и
        # итоговая строка выглядят так же; молча пропускаем и те и другие.
        if d is None or qty is None or not product:
            continue
        parsed.append(models.StockReceipt(
            organization=org,
            date=d,
            doc_number=text(row, "doc_number"),
            doc_guid=text(row, "doc_guid"),
            warehouse=text(row, "warehouse"),
            product=product,
            product_guid=text(row, "product_guid"),
            qty=qty,
            unit=text(row, "unit"),
            price=_num(cell(row, "price")),
            amount=_num(cell(row, "amount")),
            basis=text(row, "basis"),
            account=text(row, "account") or text(row, "account_fallback"),
            comment=text(row, "comment"),
        ))
    if not parsed:
        # Пустая выгрузка — обычное дело: оприходований может не быть вовсе.
        # Ронять автосинк из-за этого нельзя, но и стирать ранее загруженное
        # по пустому файлу тоже: молча выходим, оставив данные как были.
        return {"added": 0, "skipped_not_posted": not_posted, "empty": True}

    db.query(models.StockReceipt).filter(
        models.StockReceipt.organization == org).delete(synchronize_session=False)
    db.bulk_save_objects(parsed)
    db.add(models.ImportLog(
        filename=f"[оприходование:{org}] {filename}",
        user_id=user_id, added=len(parsed), skipped=0, errors_count=0,
    ))
    db.commit()
    return {"added": len(parsed), "skipped_not_posted": not_posted}


@router.get("/lines")
def stock_receipt_lines(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    org: str = "all",
    limit: int = Query(default=300, le=2000),
):
    """Строки оприходований — свежие сверху. Нужны, чтобы глазами проверить,
    что именно портал засчитал приходом помимо закупок."""
    rows = (models.org_scope(db.query(models.StockReceipt),
                             models.StockReceipt, org)
            .order_by(models.StockReceipt.date.desc(),
                      models.StockReceipt.id.desc())
            .limit(limit).all())
    return [{
        "date": r.date.isoformat(),
        "doc_number": r.doc_number,
        "warehouse": r.warehouse,
        "product": r.product,
        "qty": float(r.qty or 0),
        "unit": r.unit,
        "amount": float(r.amount) if r.amount is not None else None,
        "basis": r.basis,
        "comment": r.comment,
    } for r in rows]
