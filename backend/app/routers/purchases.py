"""Поступления товаров (закупки у поставщиков) — файл ВыгрузкаПост.

Построчный формат 1С: Дата, Номер, Контрагент, Склад, Номенклатура,
Количество, Цена, Сумма, Валюта, СуммаДокумента, СчетУчета, Единица.
Цена и итог документа — в валюте закупки (импорт обычно в USD), сумма строки
приходит уже в сомах. Открывает кредиторку, себестоимость и товарный баланс.
"""

from collections import defaultdict
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import get_current_user

router = APIRouter(prefix="/purchases", tags=["purchases"])

HEADERS = {
    "Дата": "date",
    "Номер": "doc_number",
    "КонтрагентНаименование": "supplier",
    "СкладНаименование": "warehouse",
    "НоменклатураНаименование": "product",
    "Количество": "qty",
    "Цена": "price",
    "Сумма": "amount_kgs",
    "ВалютаДокументаНаименование": "currency",
    "СуммаДокумента": "doc_total",
    "СчетУчета": "account",
    "НоменклатураЕдиницаИзмеренияНаименование": "unit",
}


def import_purchases_workbook(db: Session, content: bytes, filename: str,
                              user_id: int, org: str = models.DEFAULT_ORG) -> dict:
    """Импорт закупок. Файл выгружается за всю историю, поэтому загрузка
    заменяет данные организации целиком; сначала разбор, потом замена —
    битый файл не может стереть данные."""
    from .tax import _load_wb, _day, _num  # та же читалка с починкой архива 1С

    org = models.normalize_org(org)
    wb = _load_wb(content)
    ws = wb[wb.sheetnames[0]]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]

    header_idx, col = None, {}
    for i, row in enumerate(rows[:10]):
        names = {str(c).strip(): j for j, c in enumerate(row) if c}
        if "КонтрагентНаименование" in names and "НоменклатураНаименование" in names \
                and "Дата" in names:
            header_idx = i
            col = {HEADERS[k]: j for k, j in names.items() if k in HEADERS}
            break
    if header_idx is None:
        raise HTTPException(status_code=400,
                            detail="Не найдены колонки поступлений товаров")

    def cell(row, key):
        j = col.get(key)
        return row[j] if j is not None and j < len(row) else None

    parsed: list[models.Purchase] = []
    for row in rows[header_idx + 1:]:
        d = _day(cell(row, "date"))
        amount = _num(cell(row, "amount_kgs"))
        supplier = str(cell(row, "supplier") or "").strip()
        if d is None or amount is None or not supplier:
            continue
        parsed.append(models.Purchase(
            organization=org,
            date=d,
            supplier=supplier,
            warehouse=str(cell(row, "warehouse") or "").strip() or None,
            product=str(cell(row, "product") or "").strip() or None,
            qty=_num(cell(row, "qty")),
            price=_num(cell(row, "price")),
            amount_kgs=amount,
            currency=str(cell(row, "currency") or "KGS").strip() or "KGS",
            doc_number=str(cell(row, "doc_number") or "").strip() or None,
            doc_total=_num(cell(row, "doc_total")),
            unit=str(cell(row, "unit") or "").strip() or None,
            account=str(cell(row, "account") or "").strip() or None,
        ))
    if not parsed:
        raise HTTPException(status_code=400,
                            detail="В файле поступлений не нашлось ни одной строки")

    db.query(models.Purchase).filter(
        models.Purchase.organization == org).delete(synchronize_session=False)
    db.bulk_save_objects(parsed)
    db.add(models.ImportLog(
        filename=f"[закупки:{org}] {filename}",
        user_id=user_id, added=len(parsed), skipped=0, errors_count=0,
    ))
    db.commit()
    return {"added": len(parsed)}


LINES_CAP = 3000


@router.get("/lines")
def purchases_lines(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    org: str = "all",
):
    """Построчная детализация закупок — как в файле, без группировок.

    Фильтр и сортировка делаются на клиенте: строк немного (сотни), а живой
    поиск без походов на сервер удобнее."""
    q = db.query(models.Purchase)
    o = models.normalize_org(org) if (org or "").lower() in models.ORGS else None
    if o:
        q = q.filter(models.Purchase.organization == o)
    rows = q.order_by(models.Purchase.date.desc(),
                      models.Purchase.doc_number.desc()).limit(LINES_CAP).all()
    return {
        "total": q.count(),
        "cap": LINES_CAP,
        "items": [
            {
                "date": p.date.isoformat(),
                "doc_number": p.doc_number,
                "supplier": p.supplier,
                "warehouse": p.warehouse,
                "product": p.product,
                "qty": float(p.qty) if p.qty is not None else None,
                "unit": p.unit,
                "price": float(p.price) if p.price is not None else None,
                "amount_kgs": float(p.amount_kgs),
                "currency": p.currency,
                "doc_total": float(p.doc_total) if p.doc_total is not None else None,
                "account": p.account,
            }
            for p in rows
        ],
    }


@router.get("/summary")
def purchases_summary(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    org: str = "all",
):
    """Закупки по поставщикам и годам. Суммы — в сомах (строки 1С уже
    пересчитаны); валюта показывается отдельно, чтобы видеть импорт."""
    q = db.query(models.Purchase)
    o = models.normalize_org(org) if (org or "").lower() in models.ORGS else None
    if o:
        q = q.filter(models.Purchase.organization == o)
    rows = q.all()

    by_supplier: dict[str, dict] = {}
    by_year: dict[int, float] = defaultdict(float)
    docs_seen: set = set()
    for p in rows:
        amt = float(p.amount_kgs)
        by_year[p.date.year] += amt
        s = by_supplier.setdefault(p.supplier, {
            "amount_kgs": 0.0, "docs": set(), "currencies": set(),
            "last_date": None,
        })
        s["amount_kgs"] += amt
        s["currencies"].add(p.currency)
        if p.doc_number:
            s["docs"].add((p.doc_number, p.date))
        if s["last_date"] is None or p.date > s["last_date"]:
            s["last_date"] = p.date
        docs_seen.add((p.doc_number, p.date, p.supplier))

    return {
        "org": o or "all",
        "rows_total": len(rows),
        "docs_total": len(docs_seen),
        "amount_kgs": round(sum(float(p.amount_kgs) for p in rows), 2),
        "by_year": [{"year": y, "amount_kgs": round(a, 2)}
                    for y, a in sorted(by_year.items())],
        "suppliers": sorted(
            ({"supplier": name,
              "amount_kgs": round(v["amount_kgs"], 2),
              "docs": len(v["docs"]),
              "currencies": sorted(v["currencies"]),
              "last_date": v["last_date"] and v["last_date"].isoformat()}
             for name, v in by_supplier.items()),
            key=lambda x: -x["amount_kgs"],
        ),
    }
