"""ФОТ: начисление зарплаты — файл «…_Начисление зарплаты».

Построчная выгрузка 1С: один сотрудник в одном документе начисления — оклад,
подразделение, должность, норма и отработанные дни, результат. Это
персональные данные, поэтому отчёт отдаётся только администратору: агенту
или наблюдателю чужие оклады видеть незачем.

Сумма берётся из «Результат» — это начисленное с учётом отработанного, а не
оклад по штатке. «СуммаНачисленийВсего» повторяется в каждой строке документа
и суммированием задваивалась бы, поэтому в расчёт не идёт.
"""

from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import require_roles

router = APIRouter(prefix="/payroll", tags=["payroll"])

admin_only = require_roles(models.Role.admin)

HEADERS = {
    "Дата": "date",
    "Номер": "doc_number",
    "Сотрудник": "employee",
    "Должность": "position",
    "Подразделение": "department",
    "ВидРасчета": "calc_kind",
    "ДатаНачала": "period_start",
    "ДатаОкончания": "period_end",
    "НормаДней": "days_norm",
    "ОтработаноДней": "days_worked",
    "Результат": "amount",
    "РучнаяКорректировка": "manual",
    "Комментарий": "comment",
    "Автор": "author",
    "ДокументGUID": "doc_guid",
}


def import_payroll_workbook(db: Session, content: bytes, filename: str,
                            user_id: int,
                            org: str = models.DEFAULT_ORG) -> dict:
    """Импорт начислений заменой данных организации целиком."""
    from ..services import xlsx  # читалка с починкой архива 1С ред. 1.7
    from .tax import _day, _num

    org = models.normalize_org(org)
    wb = xlsx.load_workbook(content)
    ws = wb[wb.sheetnames[0]]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]

    header_idx, col = None, {}
    for i, row in enumerate(rows[:10]):
        names = {str(c).strip(): j for j, c in enumerate(row) if c}
        if "Дата" in names and "Сотрудник" in names and "Результат" in names:
            header_idx = i
            col = {HEADERS[k]: j for k, j in names.items() if k in HEADERS}
            break
    if header_idx is None:
        found = ", ".join(str(c).strip() for c in (rows[0] if rows else [])
                          if c is not None)[:300]
        raise HTTPException(
            status_code=400,
            detail="Не найдены колонки начисления зарплаты (ожидаются Дата, "
                   f"Сотрудник, Результат). В файле: {found or 'пусто'}")

    def cell(row, key):
        j = col.get(key)
        return row[j] if j is not None and j < len(row) else None

    def text(row, key):
        return str(cell(row, key) or "").strip() or None

    parsed: list[models.PayrollLine] = []
    for row in rows[header_idx + 1:]:
        d = _day(cell(row, "date"))
        employee = text(row, "employee")
        if d is None or not employee:
            continue  # пустой хвост файла или итоговая строка
        parsed.append(models.PayrollLine(
            organization=org,
            date=d,
            doc_number=text(row, "doc_number"),
            doc_guid=(text(row, "doc_guid") or "").lower() or None,
            employee=employee,
            position=text(row, "position"),
            department=text(row, "department"),
            calc_kind=text(row, "calc_kind"),
            period_start=_day(cell(row, "period_start")),
            period_end=_day(cell(row, "period_end")),
            days_norm=_num(cell(row, "days_norm")),
            days_worked=_num(cell(row, "days_worked")),
            amount=_num(cell(row, "amount")),
            manual=str(cell(row, "manual") or "").strip().lower()
            in ("да", "истина", "true", "1"),
            comment=text(row, "comment"),
            author=text(row, "author"),
        ))
    if not parsed:
        # Пустой файл начислений — сбой выгрузки, а не «зарплат больше нет»:
        # прежние данные не трогаем.
        return {"added": 0, "empty": True}

    db.query(models.PayrollLine).filter(
        models.PayrollLine.organization == org).delete(synchronize_session=False)
    db.bulk_save_objects(parsed)
    db.add(models.ImportLog(filename=f"[ФОТ:{org}] {filename}",
                            user_id=user_id, added=len(parsed),
                            skipped=0, errors_count=0))
    db.commit()
    return {"added": len(parsed),
            "employees": len({p.employee for p in parsed})}


@router.get("")
def payroll(
    db: Session = Depends(get_db),
    _: models.User = Depends(admin_only),
    org: str = "all",
    months: int = Query(default=18, le=60),
):
    """ФОТ по месяцам, подразделениям и сотрудникам."""
    lines = (models.org_scope(db.query(models.PayrollLine),
                              models.PayrollLine, org)
             .order_by(models.PayrollLine.date).all())
    if not lines:
        return {"months": [], "by_department": [], "employees": [],
                "total": 0.0, "last_month": None}

    def month_of(line):
        # Месяц начисления — период, за который платят, а не дата документа:
        # ноябрьскую зарплату могут провести декабрём.
        return (line.period_start or line.date).strftime("%Y-%m")

    by_month: dict[str, float] = defaultdict(float)
    by_dep: dict[str, dict] = {}
    by_emp: dict[tuple, dict] = {}
    for l in lines:
        amt = float(l.amount or 0)
        m = month_of(l)
        by_month[m] += amt
        dep = l.department or "(без подразделения)"
        d = by_dep.setdefault(dep, {"department": dep, "amount": 0.0,
                                    "employees": set()})
        d["amount"] += amt
        d["employees"].add(l.employee)
        key = (l.organization, l.employee)
        e = by_emp.setdefault(key, {
            "employee": l.employee, "organization": l.organization,
            "position": l.position, "department": dep,
            "total": 0.0, "accruals": 0, "last_month": None, "last_amount": 0.0,
        })
        e["total"] += amt
        e["accruals"] += 1
        # Строки отсортированы по дате — последняя запись и есть свежая.
        e["position"] = l.position or e["position"]
        e["department"] = dep
        e["last_month"] = m
        e["last_amount"] = amt

    period = sorted(by_month)[-months:]
    last = period[-1] if period else None
    return {
        "months": [{"month": m, "amount": round(by_month[m], 2)}
                   for m in period],
        "by_department": sorted(
            ({"department": d["department"],
              "amount": round(d["amount"], 2),
              "employees": len(d["employees"])} for d in by_dep.values()),
            key=lambda x: -x["amount"]),
        "employees": sorted(
            ({**e, "total": round(e["total"], 2),
              "last_amount": round(e["last_amount"], 2)}
             for e in by_emp.values()),
            key=lambda x: -x["last_amount"]),
        "total": round(sum(by_month.values()), 2),
        "last_month": last,
        "last_month_amount": round(by_month[last], 2) if last else 0.0,
    }
