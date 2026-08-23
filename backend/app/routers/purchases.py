"""Поступления товаров (закупки у поставщиков) — файл ВыгрузкаПост.

Построчный формат 1С: Дата, Номер, Контрагент, Склад, Номенклатура,
Количество, Цена, Сумма, Валюта, СуммаДокумента, СчетУчета, Единица.
Цена и итог документа — в валюте закупки (импорт обычно в USD), сумма строки
приходит уже в сомах. Открывает кредиторку, себестоимость и товарный баланс.
"""

from collections import defaultdict
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, onec
from ..database import get_db
from ..deps import get_current_user

router = APIRouter(prefix="/purchases", tags=["purchases"])

HEADERS = {
    "Дата": "date",
    "Номер": "doc_number",
    "КонтрагентНаименование": "supplier",
    "Контрагент": "supplier",
    "СкладНаименование": "warehouse",
    "Склад": "warehouse",
    "НоменклатураНаименование": "product",
    "Номенклатура": "product",
    "ЕдИзм": "unit",
    "Валюта": "currency",
    "Количество": "qty",
    "Цена": "price",
    "Сумма": "amount_kgs",
    "ВалютаДокументаНаименование": "currency",
    "СуммаДокумента": "doc_total",
    "СчетУчета": "account",
    "НоменклатураЕдиницаИзмеренияНаименование": "unit",
    "ДокументGUID": "doc_guid",
    # Непроведённые и помеченные на удаление — не операции.
    **onec.header_map(),
}


def import_purchases_workbook(db: Session, content: bytes, filename: str,
                              user_id: int, org: str = models.DEFAULT_ORG) -> dict:
    """Импорт закупок. Файл выгружается за всю историю, поэтому загрузка
    заменяет данные организации целиком; сначала разбор, потом замена —
    битый файл не может стереть данные."""
    from ..services import xlsx  # читалка с починкой архива 1С ред. 1.7
    from .tax import _day, _num

    org = models.normalize_org(org)
    wb = xlsx.load_workbook(content)
    ws = wb[wb.sheetnames[0]]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]

    header_idx, col = None, {}
    for i, row in enumerate(rows[:10]):
        names = {str(c).strip(): j for j, c in enumerate(row) if c}
        # Имена колонок различаются в старом и новом формате выгрузок —
        # ищем шапку по полям, а не по конкретным названиям.
        got = {HEADERS[k] for k in names if k in HEADERS}
        if {"supplier", "product", "date"} <= got:
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
    not_posted = 0
    for row in rows[header_idx + 1:]:
        if onec.skip_reason({k: cell(row, k) for k in ("_posted", "_deleted")}):
            not_posted += 1
            continue
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
            doc_guid=str(cell(row, "doc_guid") or "").strip() or None,
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
    return {"added": len(parsed), "skipped_not_posted": not_posted}


import re


def _norm_product(s: str | None) -> str:
    """Ключ сопоставления номенклатуры между закупками и продажами.

    Названия пишутся по-разному: закупка «Подгузники StarKid размер L*4»,
    продажа «Детские подгузники StarKid размер L». Убираем фасовку «*N»,
    слово «детские», пунктуацию и регистр — остальное должно совпасть."""
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"\*\s*\d+", " ", s)
    s = re.sub(r"\bдетские\b", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# Порядок как в голове у владельца, а не по алфавиту или объёму:
# подгузники ONE (обычные, затем mini), StarKid, туалетная бумага,
# салфетки, Splash, остальное.
def _group_of(name: str) -> int:
    s = (name or "").lower()
    one = "one" in s
    if "подгуз" in s and one:
        return 1 if "mini" not in s and "мини" not in s else 2
    if "подгуз" in s and "starkid" in s:
        return 3
    if "бумаг" in s and one:
        return 4
    # Салфетки и бумажные полотенца — одна группа: это один вид товара.
    if ("салфет" in s or "полотенц" in s) and one:
        return 5
    if "splash" in s or "сплэш" in s or "сплеш" in s:
        return 6
    return 7


# Внутри группы — по размеру, а не по алфавиту: NB, S, M, L, XL, XXL.
# Границы слова обязательны, иначе «L» находится внутри «XL», а «S» —
# внутри «StarKid».
_SIZE_RANK = {"nb": 0, "s": 1, "m": 2, "l": 3, "xl": 4, "xxl": 5}


def _size_of(name: str) -> int:
    s = (name or "").lower()
    for tok in ("xxl", "xl", "nb", "s", "m", "l"):  # от длинных к коротким
        if re.search(rf"\b{tok}\b", s):
            return _SIZE_RANK[tok]
    return 99  # без размера — после размерных


def _calc_stock(db: Session, org: str, until: date | None = None) -> dict[str, dict]:
    """Расчётные остатки по нормализованной номенклатуре — «как должно быть».

    Считается по фирме ЦЕЛИКОМ, по всем складам сразу: перемещения между
    складами внутри фирмы взаимно сокращаются и на итог не влияют. Разрез по
    отдельному складу потребовал бы склада во всех движениях, а возвраты
    покупателей приходят без него.

    until — считать остаток НА ДАТУ, отбросив более поздние движения. Нужно
    для честной сверки со снапшотом: в выгрузках встречаются документы,
    датированные завтрашним днём, и без отсечки они выглядят расхождением."""
    def scope(q, model):
        q = models.org_scope(q, model, org)
        if until is not None:
            q = q.filter(model.date <= until)
        return q

    agg: dict[str, dict] = {}

    def entry(product):
        key = _norm_product(product)
        e = agg.get(key)
        if e is None:
            e = agg[key] = {"name": product or "(без названия)",
                            "purchased": 0.0, "sold": 0.0, "returned": 0.0,
                            "written_off": 0.0, "has_purchase": False}
        return e

    for p in scope(db.query(models.Purchase), models.Purchase).all():
        e = entry(p.product)
        e["purchased"] += float(p.qty or 0)
        e["has_purchase"] = True
        e["name"] = p.product or e["name"]  # имя из закупки — каноничное
    for s in scope(db.query(models.Sale), models.Sale).all():
        entry(s.product)["sold"] += float(s.qty or 0)
    for r in scope(db.query(models.ReturnLine), models.ReturnLine).all():
        entry(r.product)["returned"] += float(r.qty or 0)
    for w in scope(db.query(models.WriteOff), models.WriteOff).all():
        entry(w.product)["written_off"] += float(w.qty or 0)
    return agg


@router.get("/stock-calc")
def stock_calc(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    org: str = "all",
):
    """Расчётные остатки: поступило − продано + возвраты − списано.

    Списания теперь выгружаются (ВыгрузкаСпис) и вычитаются наравне с
    продажами — раньше их приходилось считать невидимой погрешностью, и
    расчёт был заведомо верхней оценкой. Инвентаризации и пересорт всё ещё
    не выгружаются, так что расхождение с фактическими остатками теперь
    показывает именно их. Продажи Innowave выгружаются документами без
    товарных строк — расчёт работает там, где продажи построчные."""
    agg = _calc_stock(db, org)

    # Правая сторона сверки — отчёт 1С по остаткам (снапшот ВыгрузкаОст).
    # Слева наша математика из движений, справа то, что говорит учёт.
    onec: dict[str, dict] = {}
    onec_at = None
    for r in models.org_scope(db.query(models.StockBalance),
                              models.StockBalance, org).all():
        e = onec.setdefault(_norm_product(r.product),
                            {"qty": 0.0, "amount": 0.0, "name": r.product})
        e["qty"] += float(r.qty or 0)
        e["amount"] += float(r.amount or 0)
        if onec_at is None or (r.updated_at and r.updated_at > onec_at):
            onec_at = r.updated_at

    # Δ против 1С считаем НА ДАТУ снапшота: движения позже него (в т.ч.
    # документы завтрашней датой) — не расхождение, а разные даты среза.
    cut = onec_at.date() if onec_at else None
    agg_at = _calc_stock(db, org, until=cut) if cut else agg

    def qty_of(e):
        return e["purchased"] - e["sold"] + e["returned"] - e["written_off"]

    future_excluded = round(sum(qty_of(e) for e in agg.values())
                            - sum(qty_of(e) for e in agg_at.values()), 1) \
        if cut else 0.0

    rows = []
    for key, e in agg.items():
        calc = qty_of(e)
        at = agg_at.get(key)
        calc_at = round(qty_of(at), 1) if at is not None else None
        o = onec.pop(key, None)
        rows.append({
            "product": e["name"],
            "purchased": round(e["purchased"], 1),
            "sold": round(e["sold"], 1),
            "returned": round(e["returned"], 1),
            "written_off": round(e["written_off"], 1),
            "onec_qty": round(o["qty"], 1) if o else None,
            "onec_amount": round(o["amount"], 2) if o else None,
            "diff_onec": (round(calc_at - o["qty"], 1)
                          if o and calc_at is not None else None),
            "calc_qty": round(calc, 1),
            # Продано то, чего не закупали (по имени) — почти всегда значит,
            # что имена не склеились; честно помечаем вместо тихого минуса.
            "unmatched": not e["has_purchase"] and e["sold"] > 0,
        })
    # Позиции, которые есть в отчёте 1С, но не встречались в движениях
    # (например, канцтовары до первой закупки в выгрузке) — тоже показываем:
    # «в учёте есть, в математике нет» — это находка, а не мусор.
    for o in onec.values():
        rows.append({
            "product": o["name"], "purchased": 0, "sold": 0, "returned": 0,
            "written_off": 0, "calc_qty": None,
            "onec_qty": round(o["qty"], 1), "onec_amount": round(o["amount"], 2),
            "diff_onec": None, "unmatched": False,
        })
    rows.sort(key=lambda x: (_group_of(x["product"]),
                             _size_of(x["product"]),
                             x["product"] or ""))
    return {
        "org": (org or "all"),
        "has_onec": onec_at is not None,
        "onec_updated_at": onec_at.isoformat() if onec_at else None,
        # Сколько штук движений позже снапшота исключено из Δ — чтобы человек
        # видел, почему «расчётный остаток» и Δ могут не биться напрямую.
        "future_excluded_qty": future_excluded,
        "rows": rows,
        "totals": {
            "purchased": round(sum(r["purchased"] for r in rows), 1),
            "sold": round(sum(r["sold"] for r in rows), 1),
            "returned": round(sum(r["returned"] for r in rows), 1),
            "written_off": round(sum(r["written_off"] for r in rows), 1),
            "calc_qty": round(sum(r["calc_qty"] or 0 for r in rows), 1),
            "onec_qty": round(sum(r["onec_qty"] or 0 for r in rows), 1),
            "onec_amount": round(sum(r["onec_amount"] or 0 for r in rows), 2),
            "diff_onec": round(sum(r["diff_onec"] or 0 for r in rows), 1),
        },
        "unmatched_count": sum(1 for r in rows if r["unmatched"]),
    }


@router.get("/stock-compare")
def stock_compare(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    org: str = "all",
):
    """Остатки тремя путями рядом: расчёт из движений · факт 1С · SalesDoc.

    Это сверка «как должно быть ↔ как на самом деле» для товара. Расчёт —
    из загруженных движений (закупки − продажи + возвраты − списания), факт
    1С — из ВыгрузкаОст, SalesDoc — из зеркала остатков. Все три считаются по
    фирме целиком, по всем складам: перемещения внутри фирмы на итог не
    влияют.

    Знак расхождения — диагноз: расчёт больше факта — недостача или
    неучтённое списание; меньше — неоприходованный приход или пересорт."""
    calc = _calc_stock(db, org)

    # Факт 1С (снапшот ВыгрузкаОст). Пустая таблица — не «нулевые остатки»,
    # а «выгрузки ещё не было»: эти состояния различаем флагом has_onec.
    onec: dict[str, dict] = {}
    onec_names: dict[str, str] = {}
    # GUID номенклатуры из плоской выгрузки: по нему строка SalesDoc позже
    # склеится с этой же позицией через getProduct.code_1C — точнее, чем по
    # названию, которое в системах пишется по-разному.
    guid_to_key: dict[str, str] = {}
    for r in models.org_scope(db.query(models.StockBalance),
                              models.StockBalance, org).all():
        key = _norm_product(r.product)
        e = onec.setdefault(key, {"qty": 0.0, "amount": 0.0})
        e["qty"] += float(r.qty or 0)
        e["amount"] += float(r.amount or 0)
        onec_names.setdefault(key, r.product)
        if r.product_guid:
            guid_to_key.setdefault(r.product_guid.lower(), key)

    # SalesDoc: склады выбранной фирмы плюс не привязанные к фирме — их
    # исключать нельзя, иначе товар «пропадёт» из сверки только из-за того,
    # что складу не назначили организацию.
    o = models.normalize_org(org) if (org or "").lower() in models.ORGS else None
    allowed = None
    if o:
        allowed = {s.store_id for s in db.query(models.SalesDocStore).all()
                   if s.store_id and (s.organization or None) in (o, None)}
    # Товар SalesDoc несёт GUID номенклатуры 1С (getProduct.code_1C) — если
    # этот GUID пришёл и в выгрузке остатков, ключом становится он.
    sd_guid = {p.sd_id: (p.code_1c or "").lower()
               for p in db.query(models.SalesDocProduct).all() if p.code_1c}
    sd: dict[str, float] = {}
    sd_names: dict[str, str] = {}
    sd_rows = 0
    for r in db.query(models.SalesDocStock).all():
        if allowed is not None and r.store_sd_id not in allowed:
            continue
        sd_rows += 1
        key = (guid_to_key.get(sd_guid.get(r.product_sd_id or "", ""))
               or _norm_product(r.product_name))
        sd[key] = sd.get(key, 0.0) + float(r.quantity or 0)
        sd_names.setdefault(key, r.product_name)

    rows = []
    for key in set(calc) | set(onec) | set(sd):
        c = calc.get(key)
        calc_qty = (round(c["purchased"] - c["sold"] + c["returned"]
                          - c["written_off"], 1) if c else None)
        name = (c and c["name"]) or onec_names.get(key) or sd_names.get(key) or "—"
        onec_qty = round(onec[key]["qty"], 1) if key in onec else None
        sd_qty = round(sd[key], 1) if key in sd else None
        rows.append({
            "product": name,
            "calc_qty": calc_qty,
            "onec_qty": onec_qty,
            "onec_amount": round(onec[key]["amount"], 2) if key in onec else None,
            "sd_qty": sd_qty,
            # Разница считается только там, где есть обе стороны: «нет данных»
            # и «ноль» — разные вещи, и превращать первое во второе нельзя.
            "diff_sd": (round(calc_qty - sd_qty, 1)
                        if calc_qty is not None and sd_qty is not None else None),
            "diff_onec": (round(calc_qty - onec_qty, 1)
                          if calc_qty is not None and onec_qty is not None else None),
        })
    rows.sort(key=lambda x: (_group_of(x["product"]), _size_of(x["product"]),
                             x["product"] or ""))

    def total(field):
        vals = [r[field] for r in rows if r[field] is not None]
        return round(sum(vals), 1) if vals else None

    return {
        "org": (org or "all"),
        "has_onec": bool(onec),
        "has_sd": sd_rows > 0,
        "rows": rows,
        "totals": {f: total(f) for f in
                   ("calc_qty", "onec_qty", "sd_qty", "diff_sd", "diff_onec")},
    }


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
