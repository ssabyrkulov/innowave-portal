"""Подотчёт: авансовые отчёты — файл «…_Авансовый отчет».

Сотрудник берёт деньги под отчёт: брокеру на таможню, водителю на дорогу,
директору на билеты. Пока он не отчитался чеками, это долг перед фирмой —
такой же настоящий, как долг магазина, только его нигде не видно. В выгрузках
оплат виден лишь уход денег из кассы, а куда они делись дальше — нет.

1С отдаёт отчёт построчно, и строки разного смысла (колонка ТипСтроки):

* «Документ выдачи» — чем выдали (РКО, платёжное поручение);
* «Оплата» и «Прочее» — на что потратил, с контрагентом и содержанием;
* «Сводка» — итог: остаток на начало, аванс, израсходовано, остаток на конец.

Сводка накопительная: её «остаток на конец» — это и есть сумма на руках у
человека на дату отчёта. Поэтому долг подотчётника берётся из последней по
дате сводки, а не суммированием: сложить остатки всех отчётов значило бы
посчитать одни и те же деньги столько раз, сколько было отчётов.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import get_current_user

router = APIRouter(prefix="/advances", tags=["advances"])

HEADERS = {
    "Дата": "date",
    "Номер": "doc_number",
    "Сотрудник": "employee",
    "СчетПодотчета": "account",
    "ВалютаДокумента": "currency",
    "СуммаДокумента": "doc_amount",
    "Назначение": "purpose",
    "ТипСтроки": "line_type",
    "ОстатокНаНачало": "opening",
    "ИтогоАванс": "advance",
    "Израсходовано": "spent",
    "ОстатокНаКонец": "closing",
    "ДокументВыдачи": "issue_doc",
    "ДатаВыдачи": "issue_date",
    "СуммаВыдачи": "issue_amount",
    "ОстатокПоДокументу": "issue_left",
    "Контрагент": "counterparty",
    "Номенклатура": "product",
    "Количество": "qty",
    "Сумма": "amount",
    "Содержание": "content",
    "Комментарий": "comment",
    "Автор": "author",
    "ДокументGUID": "doc_guid",
}

SUMMARY = "сводка"
SPENT_TYPES = ("оплата", "прочее")


def import_advances_workbook(db: Session, content: bytes, filename: str,
                             user_id: int,
                             org: str = models.DEFAULT_ORG) -> dict:
    """Импорт авансовых отчётов заменой данных организации целиком."""
    from ..services import xlsx  # читалка с починкой архива 1С ред. 1.7
    from .tax import _day, _num

    org = models.normalize_org(org)
    wb = xlsx.load_workbook(content)
    ws = wb[wb.sheetnames[0]]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]

    header_idx, col = None, {}
    for i, row in enumerate(rows[:10]):
        names = {str(c).strip(): j for j, c in enumerate(row) if c}
        if "Дата" in names and "Сотрудник" in names and "ТипСтроки" in names:
            header_idx = i
            col = {HEADERS[k]: j for k, j in names.items() if k in HEADERS}
            break
    if header_idx is None:
        found = ", ".join(str(c).strip() for c in (rows[0] if rows else [])
                          if c is not None)[:300]
        raise HTTPException(
            status_code=400,
            detail="Не найдены колонки авансовых отчётов (ожидаются Дата, "
                   f"Сотрудник, ТипСтроки). В файле: {found or 'пусто'}")

    def cell(row, key):
        j = col.get(key)
        return row[j] if j is not None and j < len(row) else None

    def text(row, key):
        return str(cell(row, key) or "").strip() or None

    parsed: list[models.AdvanceLine] = []
    for row in rows[header_idx + 1:]:
        d = _day(cell(row, "date"))
        if d is None:
            continue  # пустой хвост файла или итоговая строка
        parsed.append(models.AdvanceLine(
            organization=org,
            date=d,
            doc_number=text(row, "doc_number"),
            doc_guid=(text(row, "doc_guid") or "").lower() or None,
            employee=text(row, "employee"),
            account=text(row, "account"),
            currency=text(row, "currency"),
            doc_amount=_num(cell(row, "doc_amount")),
            line_type=text(row, "line_type"),
            opening=_num(cell(row, "opening")),
            advance=_num(cell(row, "advance")),
            spent=_num(cell(row, "spent")),
            closing=_num(cell(row, "closing")),
            issue_doc=text(row, "issue_doc"),
            issue_date=_day(cell(row, "issue_date")),
            issue_amount=_num(cell(row, "issue_amount")),
            issue_left=_num(cell(row, "issue_left")),
            counterparty=text(row, "counterparty"),
            product=text(row, "product"),
            qty=_num(cell(row, "qty")),
            amount=_num(cell(row, "amount")),
            content=text(row, "content"),
            purpose=text(row, "purpose"),
            comment=text(row, "comment"),
            author=text(row, "author"),
        ))

    db.query(models.AdvanceLine).filter(
        models.AdvanceLine.organization == org).delete(synchronize_session=False)
    if parsed:
        db.bulk_save_objects(parsed)
    db.add(models.ImportLog(filename=f"[подотчёт:{org}] {filename}",
                            user_id=user_id, added=len(parsed),
                            skipped=0, errors_count=0))
    db.commit()
    return {"added": len(parsed),
            "employees": len({p.employee for p in parsed if p.employee}),
            "reports": len({(p.doc_guid or p.doc_number) for p in parsed})}


@router.get("")
def advances(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    org: str = "all",
    limit: int = Query(default=200, le=1000),
):
    """Сколько денег на руках у подотчётных и на что они потрачены."""
    lines = (models.org_scope(db.query(models.AdvanceLine),
                              models.AdvanceLine, org)
             .order_by(models.AdvanceLine.date, models.AdvanceLine.id)
             .all())
    if not lines:
        return {"employees": [], "reports": [], "spending": [],
                "on_hand": 0.0, "owed_back": 0.0, "reports_total": 0,
                "spending_total": 0.0, "spending_shown": 0}

    def kind(line):
        return (line.line_type or "").strip().lower()

    # Долг подотчётника — остаток последней по дате сводки, а не сумма
    # остатков: сводка накопительная, и складывать их значило бы посчитать
    # одни и те же деньги по разу на каждый отчёт. Строки отсортированы по
    # (дата, id), поэтому последняя запись сотрудника и есть свежая — так же
    # разрешается и случай двух отчётов одним днём.
    latest: dict[tuple[str, str], models.AdvanceLine] = {}
    spent_total: dict[tuple[str, str], float] = {}
    reports: list[dict] = []
    for line in lines:
        if kind(line) != SUMMARY or not line.employee:
            continue
        key = (line.organization, line.employee)
        latest[key] = line
        spent_total[key] = spent_total.get(key, 0.0) + float(line.spent or 0)
        reports.append({
            "date": line.date.isoformat(),
            "doc_number": line.doc_number,
            "employee": line.employee,
            "organization": line.organization,
            "opening": float(line.opening or 0),
            "advance": float(line.advance or 0),
            "spent": float(line.spent or 0),
            "closing": float(line.closing or 0),
        })
    reports.sort(key=lambda r: r["date"], reverse=True)

    employees = []
    for (org_name, name), line in latest.items():
        employees.append({
            "employee": name,
            "organization": org_name,
            "last_date": line.date.isoformat(),
            "last_doc": line.doc_number,
            "advance": float(line.advance or 0),
            "spent": float(line.spent or 0),
            # Остаток на конец: плюс — деньги у человека, минус — человек
            # потратил больше выданного, и теперь фирма должна ему.
            "balance": float(line.closing or 0),
            "spent_total": round(spent_total.get((org_name, name), 0.0), 2),
            "reports": sum(1 for r in reports
                           if r["employee"] == name
                           and r["organization"] == org_name),
        })
    employees.sort(key=lambda e: -e["balance"])

    spending = [{
        "date": line.date.isoformat(),
        "employee": line.employee,
        "counterparty": line.counterparty,
        "content": line.content or line.product,
        "amount": float(line.amount or 0),
        "doc_number": line.doc_number,
    } for line in reversed(lines) if kind(line) in SPENT_TYPES]

    return {
        "employees": employees,
        "reports": reports[:limit],
        "spending": spending[:limit],
        "spending_shown": min(len(spending), limit),
        "spending_total": round(sum(s["amount"] for s in spending), 2),
        # Деньги, за которые ещё не отчитались.
        "on_hand": round(sum(e["balance"] for e in employees
                             if e["balance"] > 0), 2),
        # Перерасход: сотрудник потратил своё, фирма должна вернуть.
        "owed_back": round(-sum(e["balance"] for e in employees
                                if e["balance"] < 0), 2),
        "reports_total": len(reports),
    }
