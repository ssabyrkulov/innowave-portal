"""Расход денежных средств: исходящие платёжки банка (ВыгрузкаППИсход) и
РКО кассы (ВыгрузкаРКО). Гибкий разбор: понимает и «плоский» формат
Дата/Сумма/Валюта/Контрагент, и расширенный с Документ/Номер/Основание.
"""

import hashlib
from collections import defaultdict
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import models
from ..services import xlsx
from ..database import get_db
from ..deps import get_current_user, require_roles
from .receipts import DEFAULT_RATES

router = APIRouter(prefix="/expenses", tags=["expenses"])

admin_only = require_roles(models.Role.admin)

HEADERS = {
    # дата
    "Дата": "date",
    "ДатаДокумента": "date",
    # сумма
    "Сумма": "amount",
    "СуммаДокумента": "amount",
    "СуммаПлатежа": "amount",
    "СуммаРасхода": "amount",
    # валюта
    "Валюта": "currency",
    "ВалютаДокумента": "currency",
    "ВалютаДокументаНаименование": "currency",
    # GUID документа 1С — в новом формате есть у всех денежных документов
    "ДокументGUID": "doc_guid",
    # контрагент/получатель
    "Контрагент": "counterparty",
    "КонтрагентНаименование": "counterparty",
    "Получатель": "counterparty",
    "ПолучательНаименование": "counterparty",
    # основание/назначение/статья
    "Основание": "basis",
    "ВидОперации": "basis",
    "Назначение": "basis",
    "НазначениеПлатежа": "basis",
    "СтатьяДвиженияДенежныхСредств": "basis",
    "СтатьяДДС": "basis",
    "Статья": "basis",
    # номер
    "Номер": "doc_number",
    "НомерДокумента": "doc_number",
}


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


def _drop_legacy_twins(db: Session, org: str) -> int:
    """Убирает строки старого формата, у которых уже появился двойник нового.

    Ключ дедупликации какое-то время включал номер документа, которого в
    старой выгрузке не было. Пока это не исправили, один и тот же платёж
    успел лечь в базу дважды: строка без номера и она же с номером. Пары
    ищем по деловым значениям и удаляем ровно столько безномерных строк,
    сколько нашлось номерных — если старый платёж пары не получил, он
    остаётся нетронутым.
    """
    groups: dict[tuple, dict] = defaultdict(lambda: {"old": [], "new": []})
    for r in db.query(models.Expense).filter(models.Expense.organization == org).all():
        key = (r.date, round(float(r.amount or 0), 2), (r.currency or "").strip(),
               (r.counterparty or "").strip(), (r.kind or "").strip())
        groups[key]["new" if (r.doc_number or r.doc_guid) else "old"].append(r)
    removed = 0
    for g in groups.values():
        for row in g["old"][:len(g["new"])]:
            db.delete(row)
            removed += 1
    if removed:
        db.flush()
    return removed


def import_expenses_workbook(
    db: Session, content: bytes, filename: str, user_id: int, kind: str = "bank",
    org: str = models.DEFAULT_ORG,
) -> dict:
    org = models.normalize_org(org)
    try:
        wb = xlsx.load_workbook(content)
    except Exception:
        raise HTTPException(status_code=400, detail="Не удалось открыть файл Excel")

    ws = wb[wb.sheetnames[0]]
    rows_iter = ws.iter_rows(values_only=True)

    header_idx, columns, buffered = None, {}, []
    seen_headers: list[str] = []
    for i, row in enumerate(rows_iter):
        buffered.append(row)
        if i >= 20:
            break
        cells = [str(c).strip() for c in row if c is not None]
        if cells and not seen_headers:
            seen_headers = cells
        matched = {
            j: HEADERS[str(c).strip()]
            for j, c in enumerate(row)
            if c is not None and str(c).strip() in HEADERS
        }
        # Минимум — дата и сумма; контрагент желателен, но не обязателен.
        if {"date", "amount"} <= set(matched.values()):
            header_idx, columns = i, matched
            seen_headers = cells
            break
    if header_idx is None:
        raise HTTPException(
            status_code=400,
            detail="Не найдены колонки расхода (нужны минимум Дата и Сумма). "
                   f"Заголовки в файле: {', '.join(seen_headers) or '—'}",
        )

    parsed, errors = [], []

    def process(row, line_no):
        data = {}
        for j, field in columns.items():
            # basis/doc_number могут встречаться в нескольких колонках — не
            # перетираем уже найденное непустое значение
            val = row[j] if j < len(row) else None
            if field in data and data[field]:
                continue
            data[field] = val
        if all(v is None for v in data.values()):
            return
        dt = _parse_date(data.get("date"))
        try:
            amount = float(str(data.get("amount")).replace(" ", "").replace(",", "."))
        except (TypeError, ValueError):
            amount = None
        cp = str(data.get("counterparty") or "").strip()
        if not dt or amount is None:
            if len(errors) < 20:
                errors.append(f"Строка {line_no}: нет даты или суммы")
            return
        currency = (str(data.get("currency") or "KGS").strip() or "KGS")[:3]
        rate = DEFAULT_RATES.get(currency, 1.0)
        parsed.append({
            "src_dt": str(data.get("date")),
            "date": dt,
            "amount": amount,
            "currency": currency,
            "rate": rate,
            "amount_kgs": round(amount * rate, 2),
            "counterparty": cp or "Не указан",
            "kind": kind,
            "basis": str(data.get("basis") or "").strip() or None,
            "doc_number": str(data.get("doc_number") or "").strip() or None,
            "doc_guid": str(data.get("doc_guid") or "").strip() or None,
        })

    line_no = header_idx + 1
    for line_no, row in enumerate(buffered[header_idx + 1:], start=header_idx + 2):
        process(row, line_no)
    for line_no, row in enumerate(rows_iter, start=line_no + 1):
        process(row, line_no)

    # Ключ по деловым значениям — тот же, что и в поступлениях. Хеш строки
    # включает дату со временем прямо из файла, а форматы пишут время
    # по-разному («4:36:57» против «04:36:57»), поэтому одного хеша мало.
    #
    # Номера документа в ключе НЕТ, и это принципиально: старая выгрузка
    # расходов состояла из «Дата, Сумма, Валюта, Контрагент, ВидОперации» —
    # колонки «Номер» в ней не было вовсе. С номером в ключе ни одна старая
    # строка не сходилась с новой, и переход задваивал расходы: РКО Хайджина
    # дал 611 новых строк из 821 вместо трёх десятков.
    def _bk(date_, amount, currency, counterparty, kind_):
        return (date_, round(float(amount or 0), 2), (currency or "").strip(),
                (counterparty or "").strip(), (kind_ or "").strip())

    removed_twins = _drop_legacy_twins(db, org)

    existing = {h for (h,) in db.query(models.Expense.row_hash).all()}
    prior: dict[tuple, list] = defaultdict(list)
    for r in db.query(models.Expense).filter(models.Expense.organization == org).all():
        prior[_bk(r.date, r.amount, r.currency, r.counterparty, r.kind)].append(r)

    # Уже загруженные документы — по ДокументGUID. Ключ по контрагенту
    # ломается от переименования в 1С (у поставщика правят название, и весь
    # его платёж приезжает вторым экземпляром), а GUID документа не меняется.
    # Считаем количество строк документа: у расшифровки их бывает несколько.
    doc_rows: dict[tuple, int] = defaultdict(int)
    for r in (db.query(models.Expense)
              .filter(models.Expense.organization == org,
                      models.Expense.doc_guid.isnot(None)).all()):
        doc_rows[(r.doc_guid, r.date, float(r.amount or 0), r.kind)] += 1
    doc_used: dict[tuple, int] = defaultdict(int)

    seen: set[str] = set()
    occurrences: dict[str, int] = defaultdict(int)
    biz_used: dict[tuple, int] = defaultdict(int)
    added = skipped = adopted = 0
    for p in parsed:
        base = "|".join(str(p[f]) for f in
                        ("src_dt", "amount", "currency", "counterparty", "kind", "doc_number"))
        occurrences[base] += 1
        h = hashlib.sha256(f"{org}|{base}#{occurrences[base]}".encode()).hexdigest()
        p = {k: v for k, v in p.items() if k != "src_dt"}
        if h in existing or h in seen:
            skipped += 1
            continue
        dk = (p.get("doc_guid"), p["date"], p["amount"], p["kind"])
        if p.get("doc_guid") and doc_used[dk] < doc_rows.get(dk, 0):
            doc_used[dk] += 1  # документ уже загружен, пусть под другим именем
            skipped += 1
            continue
        bk = _bk(p["date"], p["amount"], p["currency"], p["counterparty"], p["kind"])
        same = prior.get(bk) or []
        if biz_used[bk] < len(same):
            row = same[biz_used[bk]]
            biz_used[bk] += 1
            if p.get("doc_guid") and not getattr(row, "doc_guid", None):
                row.doc_guid = p["doc_guid"]
                adopted += 1
            skipped += 1
            continue
        seen.add(h)
        db.add(models.Expense(**p, organization=org, row_hash=h))
        added += 1

    db.add(models.ImportLog(
        filename=f"[расход {kind}] {filename}",
        user_id=user_id,
        added=added,
        skipped=skipped,
        errors_count=len(errors),
    ))
    db.commit()
    return {
        "added": added,
        "skipped_duplicates": skipped,
        "guid_backfilled": adopted,
        "legacy_twins_removed": removed_twins,
        "errors": errors,
        "total_in_db": db.query(models.Expense).count(),
    }


@router.get("")
def list_expenses(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    kind: str | None = Query(default=None),
    limit: int = Query(default=300, le=1000),
    org: str = Query(default="all"),
):
    q = models.org_scope(db.query(models.Expense), models.Expense, org).order_by(models.Expense.date.desc())
    if kind:
        q = q.filter(models.Expense.kind == kind)
    return [
        {
            "id": e.id,
            "date": e.date.isoformat(),
            "amount": float(e.amount),
            "currency": e.currency,
            "amount_kgs": float(e.amount_kgs),
            "counterparty": e.counterparty,
            "kind": e.kind,
            "basis": e.basis,
        }
        for e in q.limit(limit).all()
    ]


@router.get("/summary")
def expenses_summary(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    org: str = Query(default="all"),
):
    q = models.org_scope(db.query(models.Expense), models.Expense, org)
    if date_from:
        q = q.filter(models.Expense.date >= date_from)
    if date_to:
        q = q.filter(models.Expense.date <= date_to)
    rows = q.all()
    by_cp: dict[str, float] = defaultdict(float)
    by_month: dict[str, float] = defaultdict(float)
    total = 0.0
    for e in rows:
        amt = float(e.amount_kgs)
        total += amt
        by_cp[e.counterparty] += amt
        by_month[e.date.strftime("%Y-%m")] += amt
    return {
        "total": round(total, 2),
        "count": len(rows),
        "monthly": [
            {"month": m, "amount": round(v, 2)} for m, v in sorted(by_month.items())
        ],
        "top_counterparties": sorted(
            ({"name": k, "amount": round(v, 2)} for k, v in by_cp.items()),
            key=lambda x: -x["amount"],
        )[:15],
    }


@router.delete("", status_code=204)
def clear_expenses(
    db: Session = Depends(get_db),
    _: models.User = Depends(admin_only),
):
    db.query(models.Expense).delete()
    db.commit()
