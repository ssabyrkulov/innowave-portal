"""Перемещение товаров между складами — файл «…_Перемещение товаров».

Построчный формат 1С: Дата, Номер, Организация, СкладОтправитель,
СкладПолучатель, Номенклатура, НоменклатураGUID, ЕдИзм, Количество,
СуммаПеремещения, Комментарий, ДокументGUID.

На итог по фирме перемещение не влияет: сколько ушло с одного склада,
столько пришло на другой. Ценность в другом — без него остаток отдельного
склада посчитать нечем, и сверка велась только по фирме целиком.
"""

from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import models, onec
from ..database import get_db
from ..deps import get_current_user

router = APIRouter(prefix="/stock-transfers", tags=["stock-transfers"])

HEADERS = {
    "Дата": "date",
    "Номер": "doc_number",
    "СкладОтправитель": "from_warehouse",
    "СкладПолучатель": "to_warehouse",
    "Номенклатура": "product",
    "НоменклатураНаименование": "product",
    "НоменклатураGUID": "product_guid",
    "Количество": "qty",
    "ЕдИзм": "unit",
    "ЕдиницаИзмерения": "unit",
    "СуммаПеремещения": "amount",
    "Комментарий": "comment",
    "ДокументGUID": "doc_guid",
    # Непроведённые и помеченные на удаление — не операции.
    **onec.header_map(),
}


def import_stock_transfers_workbook(db: Session, content: bytes, filename: str,
                                    user_id: int,
                                    org: str = models.DEFAULT_ORG) -> dict:
    """Импорт перемещений заменой данных организации целиком."""
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
                "СкладОтправитель" in names or "СкладПолучатель" in names):
            header_idx = i
            col = {HEADERS[k]: j for k, j in names.items() if k in HEADERS}
            break
    if header_idx is None:
        found = ", ".join(str(c).strip() for c in (rows[0] if rows else [])
                          if c is not None)[:300]
        raise HTTPException(
            status_code=400,
            detail="Не найдены колонки перемещений (ожидаются Дата, "
                   f"СкладОтправитель, Количество). В файле: {found or 'пусто'}")

    def cell(row, key):
        j = col.get(key)
        return row[j] if j is not None and j < len(row) else None

    def text(row, key):
        return str(cell(row, key) or "").strip() or None

    parsed: list[models.StockTransfer] = []
    not_posted = 0
    for row in rows[header_idx + 1:]:
        if onec.skip_reason({k: cell(row, k) for k in ("_posted", "_deleted")}):
            not_posted += 1
            continue
        d = _day(cell(row, "date"))
        qty = _num(cell(row, "qty"))
        product = text(row, "product")
        if d is None or qty is None or not product:
            continue  # пустой хвост файла или итоговая строка
        parsed.append(models.StockTransfer(
            organization=org,
            date=d,
            doc_number=text(row, "doc_number"),
            doc_guid=text(row, "doc_guid"),
            from_warehouse=text(row, "from_warehouse"),
            to_warehouse=text(row, "to_warehouse"),
            product=product,
            product_guid=(text(row, "product_guid") or "").lower() or None,
            qty=qty,
            unit=text(row, "unit"),
            amount=_num(cell(row, "amount")),
            comment=text(row, "comment"),
        ))
    if not parsed:
        # Перемещений может не быть вовсе — это не сбой. Молча выходим,
        # оставив прежние данные: пустой файл не повод их стереть.
        return {"added": 0, "skipped_not_posted": not_posted, "empty": True}

    db.query(models.StockTransfer).filter(
        models.StockTransfer.organization == org).delete(synchronize_session=False)
    db.bulk_save_objects(parsed)
    db.add(models.ImportLog(
        filename=f"[перемещения:{org}] {filename}",
        user_id=user_id, added=len(parsed), skipped=0, errors_count=0,
    ))
    db.commit()
    routes = {(t.from_warehouse, t.to_warehouse) for t in parsed}
    return {"added": len(parsed), "skipped_not_posted": not_posted,
            "routes": len(routes)}


@router.get("/lines")
def transfer_lines(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    org: str = "all",
    limit: int = Query(default=300, le=2000),
):
    """Строки перемещений, свежие сверху."""
    rows = (models.org_scope(db.query(models.StockTransfer),
                             models.StockTransfer, org)
            .order_by(models.StockTransfer.date.desc(),
                      models.StockTransfer.id.desc())
            .limit(limit).all())
    return [{
        "date": r.date.isoformat(),
        "doc_number": r.doc_number,
        "from_warehouse": r.from_warehouse,
        "to_warehouse": r.to_warehouse,
        "product": r.product,
        "qty": float(r.qty or 0),
        "amount": float(r.amount) if r.amount is not None else None,
        "comment": r.comment,
    } for r in rows]


@router.get("/by-warehouse")
def stock_by_warehouse(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    org: str = "all",
):
    """Остаток по каждому складу: наш расчёт против снапшота 1С.

    Считается так же, как остаток по фирме, но каждое движение относится к
    своему складу, а перемещение делится надвое: минус отправителю, плюс
    получателю.

    Одна честная дыра: возврат покупателя приходит без склада. Товар на
    склад вернулся, а на какой — 1С в этой выгрузке не говорит. Такие строки
    собраны отдельной позицией «без склада», и разносить их по складам
    наугад portal не станет: это исказило бы каждый склад тихо, тогда как
    отдельная строка видна и понятна."""
    calc: dict[str, float] = defaultdict(float)

    def scope(model):
        return models.org_scope(db.query(model), model, org).all()

    for p in scope(models.Purchase):
        calc[p.warehouse or ""] += float(p.qty or 0)
    for r in scope(models.StockReceipt):
        calc[r.warehouse or ""] += float(r.qty or 0)
    for s in scope(models.Sale):
        calc[s.warehouse or ""] -= float(s.qty or 0)
    for w in scope(models.WriteOff):
        calc[w.warehouse or ""] -= float(w.qty or 0)
    for t in scope(models.StockTransfer):
        q = float(t.qty or 0)
        calc[t.from_warehouse or ""] -= q
        calc[t.to_warehouse or ""] += q
    returns_no_wh = sum(float(r.qty or 0) for r in scope(models.ReturnLine))
    calc[""] += returns_no_wh

    onec: dict[str, float] = defaultdict(float)
    for b in scope(models.StockBalance):
        onec[b.warehouse or ""] += float(b.qty or 0)

    rows = []
    for wh in sorted(set(calc) | set(onec), key=lambda w: (w == "", w)):
        c, o = round(calc.get(wh, 0.0), 1), round(onec.get(wh, 0.0), 1)
        rows.append({
            "warehouse": wh or "(без склада)",
            "calc_qty": c,
            "onec_qty": o if wh in onec else None,
            "diff": round(c - o, 1) if wh in onec else None,
        })
    return {
        "rows": rows,
        # Сколько штук возвратов не удалось отнести ни к одному складу —
        # ровно на столько «(без склада)» завышен, а какой-то реальный склад
        # занижен. Названо цифрой, чтобы масштаб был виден сразу.
        "returns_without_warehouse": round(returns_no_wh, 1),
        "transfers": db.query(models.StockTransfer).count(),
    }
