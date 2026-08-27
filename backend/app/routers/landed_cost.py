"""Себестоимость импорта: ГТД и дополнительные расходы.

Товар приходит по инвойсу, а на склад ложится дороже — пошлина, таможенный
сбор, акциз, сопровождение, стоянка, доставка, услуги брокера. Портал считал
маржу по сумме поступления, то есть завышал её на всю эту разницу.

Два файла, два разных источника:

* «ГТД по импорту» — таможенная часть, разложенная 1С по строкам поступления;
* «Дополнительные расходы» — всё, что отнесли на партию после поступления.

В ГТД есть своя колонка «ДопРасходы», и она соблазняет взять всё из одного
файла. Так нельзя: это снимок на момент проведения ГТД, а расходы приходят и
позже. По данным Хайджина ГТД знает 13.79 млн, а документы расходов —
14.87 млн. Берём таможню из ГТД, расходы из их собственного документа, и
ничего не складываем дважды.

Отдельно стоит НДС. В документах расходов он проведён на счёт товара (1610)
и по Хайджину даёт 11.4 млн из 14.9 — если молча вложить его в
себестоимость, товар «подорожает» на четверть, хотя у плательщика НДС он
зачётный. Поэтому НДС считается, но своей колонкой: полная себестоимость
показана без него, рядом — сколько получится с ним. Что из двух верно,
решает не портал, а бухгалтер; дело портала — показать обе цифры, а не
выбрать одну втихую.

Портал ничего не распределяет заново: 1С уже разложила суммы по строкам, мы
только читаем результат и связываем его с товаром по НоменклатураGUID.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import get_current_user

router = APIRouter(prefix="/landed-cost", tags=["landed-cost"])

GTD_HEADERS = {
    "Дата": "date",
    "Номер": "doc_number",
    "НомерГТД": "gtd_number",
    "Таможня": "customs",
    "ПоставщикТовара": "supplier",
    "Номенклатура": "product",
    "НоменклатураGUID": "product_guid",
    "Количество": "qty",
    "ФактурнаяСтоимость": "invoice_amount",
    "Пошлина": "duty",
    "ТаможенныйСбор": "customs_fee",
    "Акциз": "excise",
    "Сопровождение": "escort",
    "ДопРасходы": "extra_at_gtd",
    "ДокументПоступления": "receipt_doc",
    "ПоступлениеGUID": "receipt_guid",
    "Курс": "rate",
    "ДокументGUID": "doc_guid",
}

EXTRA_HEADERS = {
    "Дата": "date",
    "Номер": "doc_number",
    "Контрагент": "counterparty",
    "ВидРасхода": "kind",
    "Номенклатура": "product",
    "НоменклатураGUID": "product_guid",
    "Количество": "qty",
    "СуммаТовара": "goods_amount",
    "СуммаРасходов": "amount",
    "ДокументПоступления": "receipt_doc",
    "ПоступлениеGUID": "receipt_guid",
    "Комментарий": "comment",
    "ДокументGUID": "doc_guid",
}


def _is_vat(row: "models.ExtraCost") -> bool:
    """Строка дополнительных расходов — это НДС при ввозе?

    Вид расхода 1С не заполняет (по обеим фирмам колонка пустая), поэтому
    судим по комментарию — там бухгалтер пишет «НДС» и рядом сумму. Правило
    простое нарочно: расширять его догадками значило бы тихо перекладывать
    миллионы из себестоимости в зачётный налог и обратно."""
    text = f"{row.comment or ''} {row.kind or ''}".lower()
    return "ндс" in text or "nds" in text


def _read(content: bytes, headers: dict, required: tuple[str, ...], what: str):
    """Общий разбор построчной выгрузки: шапка, колонки, строки."""
    from ..services import xlsx  # читалка с починкой архива 1С ред. 1.7

    wb = xlsx.load_workbook(content)
    ws = wb[wb.sheetnames[0]]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    for i, row in enumerate(rows[:10]):
        names = {str(c).strip(): j for j, c in enumerate(row) if c}
        if all(k in names for k in required):
            col = {headers[k]: j for k, j in names.items() if k in headers}
            return rows[i + 1:], col
    found = ", ".join(str(c).strip() for c in (rows[0] if rows else [])
                      if c is not None)[:300]
    raise HTTPException(
        status_code=400,
        detail=f"Не найдены колонки {what} (ожидаются {', '.join(required)}). "
               f"В файле: {found or 'пусто'}")


def _cellreader(col: dict):
    from .tax import _num

    def cell(row, key):
        j = col.get(key)
        return row[j] if j is not None and j < len(row) else None

    def text(row, key):
        return str(cell(row, key) or "").strip() or None

    def guid(row, key):
        return (text(row, key) or "").lower() or None

    def num(row, key):
        return _num(cell(row, key))

    return cell, text, guid, num


def import_gtd_workbook(db: Session, content: bytes, filename: str,
                        user_id: int, org: str = models.DEFAULT_ORG) -> dict:
    """Импорт ГТД по импорту заменой данных организации целиком."""
    from .tax import _day

    org = models.normalize_org(org)
    rows, col = _read(content, GTD_HEADERS,
                      ("Дата", "НомерГТД", "Номенклатура"), "ГТД по импорту")
    cell, text, guid, num = _cellreader(col)

    parsed: list[models.ImportCost] = []
    for row in rows:
        d = _day(cell(row, "date"))
        product = text(row, "product")
        if d is None or not product:
            continue  # хвост файла или итоговая строка
        parsed.append(models.ImportCost(
            organization=org, date=d,
            doc_number=text(row, "doc_number"),
            doc_guid=guid(row, "doc_guid"),
            gtd_number=text(row, "gtd_number"),
            customs=text(row, "customs"),
            supplier=text(row, "supplier"),
            receipt_guid=guid(row, "receipt_guid"),
            receipt_doc=text(row, "receipt_doc"),
            product=product, product_guid=guid(row, "product_guid"),
            qty=num(row, "qty"),
            invoice_amount=num(row, "invoice_amount"),
            duty=num(row, "duty"),
            customs_fee=num(row, "customs_fee"),
            excise=num(row, "excise"),
            escort=num(row, "escort"),
            extra_at_gtd=num(row, "extra_at_gtd"),
            rate=num(row, "rate"),
        ))
    if not parsed:
        return {"added": 0, "empty": True}

    db.query(models.ImportCost).filter(
        models.ImportCost.organization == org).delete(synchronize_session=False)
    db.bulk_save_objects(parsed)
    db.add(models.ImportLog(filename=f"[ГТД:{org}] {filename}",
                            user_id=user_id, added=len(parsed),
                            skipped=0, errors_count=0))
    db.commit()
    return {"added": len(parsed),
            "declarations": len({p.gtd_number for p in parsed if p.gtd_number})}


def import_extra_costs_workbook(db: Session, content: bytes, filename: str,
                                user_id: int,
                                org: str = models.DEFAULT_ORG) -> dict:
    """Импорт дополнительных расходов заменой данных организации целиком."""
    from .tax import _day

    org = models.normalize_org(org)
    rows, col = _read(content, EXTRA_HEADERS,
                      ("Дата", "СуммаРасходов", "Номенклатура"),
                      "дополнительных расходов")
    cell, text, guid, num = _cellreader(col)

    parsed: list[models.ExtraCost] = []
    for row in rows:
        d = _day(cell(row, "date"))
        product = text(row, "product")
        if d is None or not product:
            continue
        parsed.append(models.ExtraCost(
            organization=org, date=d,
            doc_number=text(row, "doc_number"),
            doc_guid=guid(row, "doc_guid"),
            counterparty=text(row, "counterparty"),
            kind=text(row, "kind"),
            receipt_guid=guid(row, "receipt_guid"),
            receipt_doc=text(row, "receipt_doc"),
            product=product, product_guid=guid(row, "product_guid"),
            qty=num(row, "qty"),
            goods_amount=num(row, "goods_amount"),
            amount=num(row, "amount"),
            comment=text(row, "comment"),
        ))
    if not parsed:
        return {"added": 0, "empty": True}

    db.query(models.ExtraCost).filter(
        models.ExtraCost.organization == org).delete(synchronize_session=False)
    db.bulk_save_objects(parsed)
    db.add(models.ImportLog(filename=f"[доп.расходы:{org}] {filename}",
                            user_id=user_id, added=len(parsed),
                            skipped=0, errors_count=0))
    db.commit()
    return {"added": len(parsed),
            "documents": len({p.doc_guid for p in parsed if p.doc_guid})}


@router.get("")
def landed_cost(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    org: str = "all",
    limit: int = Query(default=200, le=1000),
):
    """Полная себестоимость импорта по товару: инвойс, таможня, расходы.

    Ничего не меняет в существующих расчётах — это отдельный отчёт. Маржа
    по-прежнему считается по сумме поступления: переводить её на полную
    себестоимость — решение про деньги, и принимать его молча неправильно.
    Сначала цифра должна быть видна."""
    from .purchases import _product_keys

    key_of, canon, _ = _product_keys(db)

    agg: dict[str, dict] = {}

    def entry(product, guid):
        k = key_of(product, guid)
        e = agg.get(k)
        if e is None:
            e = agg[k] = {"product": canon.get(k) or product or "(без названия)",
                          "qty": 0.0, "invoice": 0.0, "customs": 0.0,
                          "extra": 0.0, "vat": 0.0, "declarations": set()}
        return e

    for r in models.org_scope(db.query(models.ImportCost),
                              models.ImportCost, org).all():
        e = entry(r.product, r.product_guid)
        e["qty"] += float(r.qty or 0)
        e["invoice"] += float(r.invoice_amount or 0)
        e["customs"] += (float(r.duty or 0) + float(r.customs_fee or 0)
                         + float(r.excise or 0) + float(r.escort or 0))
        if r.gtd_number:
            e["declarations"].add(r.gtd_number)

    for r in models.org_scope(db.query(models.ExtraCost),
                              models.ExtraCost, org).all():
        e = entry(r.product, r.product_guid)
        e["vat" if _is_vat(r) else "extra"] += float(r.amount or 0)

    rows = []
    for e in agg.values():
        total = e["invoice"] + e["customs"] + e["extra"]
        qty = e["qty"]
        rows.append({
            "product": e["product"],
            "qty": round(qty, 1),
            "invoice": round(e["invoice"], 2),
            "customs": round(e["customs"], 2),
            "extra": round(e["extra"], 2),
            "vat": round(e["vat"], 2),
            "total": round(total, 2),
            "total_with_vat": round(total + e["vat"], 2),
            # Себестоимость единицы — то, ради чего всё считалось.
            "unit_invoice": round(e["invoice"] / qty, 2) if qty else None,
            "unit_total": round(total / qty, 2) if qty else None,
            "unit_total_with_vat": (round((total + e["vat"]) / qty, 2)
                                    if qty else None),
            # На сколько процентов инвойс дешевле правды. Именно на столько
            # завышена маржа, пока она считается по сумме поступления.
            "markup_pct": (round((total / e["invoice"] - 1) * 100, 1)
                           if e["invoice"] else None),
            "markup_with_vat_pct": (
                round(((total + e["vat"]) / e["invoice"] - 1) * 100, 1)
                if e["invoice"] else None),
            "declarations": len(e["declarations"]),
        })
    rows.sort(key=lambda r: -r["total"])

    inv = sum(r["invoice"] for r in rows)
    cus = sum(r["customs"] for r in rows)
    ext = sum(r["extra"] for r in rows)
    vat = sum(r["vat"] for r in rows)
    return {
        "rows": rows[:limit],
        "shown": min(len(rows), limit),
        "total_rows": len(rows),
        "totals": {
            "invoice": round(inv, 2), "customs": round(cus, 2),
            "extra": round(ext, 2), "vat": round(vat, 2),
            "total": round(inv + cus + ext, 2),
            "total_with_vat": round(inv + cus + ext + vat, 2),
            "markup_pct": (round(((inv + cus + ext) / inv - 1) * 100, 1)
                           if inv else None),
            "markup_with_vat_pct": (
                round(((inv + cus + ext + vat) / inv - 1) * 100, 1)
                if inv else None),
        },
    }
