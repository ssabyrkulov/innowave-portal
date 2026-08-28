"""Журнал проводок — файл «…_Журнал проводок».

Полная двойная запись фирмы: каждая строка — проводка дебет/кредит с суммой.
Из журнала считается оборотно-сальдовая ведомость: по каждому счёту обороты
и сальдо. Это первый отчёт портала, который видит фирму целиком, а не по
кускам, — и последняя инстанция при расхождениях: если документная выгрузка
и проводки говорят разное, врут выгрузки.

Строки с «Активность: Нет» отбрасываются при импорте: 1С помечает так
проводки выключенных операций, в итогах базы их нет.
"""

from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import get_current_user

router = APIRouter(prefix="/ledger", tags=["ledger"])

HEADERS = {
    "Период": "date",
    "Документ": "doc",
    "ДокументGUID": "doc_guid",
    "НомерСтроки": "line_no",
    "СчетДт": "debit_account",
    "НаименованиеСчетаДт": "debit_name",
    "СубконтоДт1": "debit_sub",
    "СчетКт": "credit_account",
    "НаименованиеСчетаКт": "credit_name",
    "СубконтоКт1": "credit_sub",
    "Сумма": "amount",
    "Содержание": "content",
    "Активность": "active",
}


def import_ledger_workbook(db: Session, content: bytes, filename: str,
                           user_id: int,
                           org: str = models.DEFAULT_ORG) -> dict:
    """Импорт журнала проводок заменой данных организации целиком."""
    from ..services import xlsx  # читалка с починкой архива 1С ред. 1.7
    from .tax import _day, _num

    org = models.normalize_org(org)
    wb = xlsx.load_workbook(content)
    ws = wb[wb.sheetnames[0]]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]

    header_idx, col = None, {}
    for i, row in enumerate(rows[:10]):
        names = {str(c).strip(): j for j, c in enumerate(row) if c}
        if "Период" in names and "СчетДт" in names and "СчетКт" in names:
            header_idx = i
            col = {HEADERS[k]: j for k, j in names.items() if k in HEADERS}
            break
    if header_idx is None:
        found = ", ".join(str(c).strip() for c in (rows[0] if rows else [])
                          if c is not None)[:300]
        raise HTTPException(
            status_code=400,
            detail="Не найдены колонки журнала проводок (ожидаются Период, "
                   f"СчетДт, СчетКт). В файле: {found or 'пусто'}")

    def cell(row, key):
        j = col.get(key)
        return row[j] if j is not None and j < len(row) else None

    def text(row, key):
        return str(cell(row, key) or "").strip() or None

    parsed: list[models.LedgerEntry] = []
    inactive = 0
    for row in rows[header_idx + 1:]:
        d = _day(cell(row, "date"))
        if d is None:
            continue  # пустой хвост файла или итоговая строка
        if str(cell(row, "active") or "").strip().lower() in ("нет", "false"):
            inactive += 1
            continue
        parsed.append(models.LedgerEntry(
            organization=org,
            date=d,
            doc=text(row, "doc"),
            doc_guid=(text(row, "doc_guid") or "").lower() or None,
            line_no=int(_num(cell(row, "line_no")) or 0) or None,
            debit_account=text(row, "debit_account"),
            debit_name=text(row, "debit_name"),
            debit_sub=text(row, "debit_sub"),
            credit_account=text(row, "credit_account"),
            credit_name=text(row, "credit_name"),
            credit_sub=text(row, "credit_sub"),
            amount=_num(cell(row, "amount")),
            content=text(row, "content"),
        ))
    if not parsed:
        # Пустой журнал не бывает у живой базы — сбой выгрузки, прежние
        # данные не трогаем.
        return {"added": 0, "skipped_inactive": inactive, "empty": True}

    db.query(models.LedgerEntry).filter(
        models.LedgerEntry.organization == org).delete(synchronize_session=False)
    # Журнал — самый большой файл выгрузки (у Хайджина ~20 тыс. строк).
    # Пишем порциями: разовый bulk на весь список держит все объекты в
    # памяти одновременно, и на маленьком инстансе Render этот пик ронял
    # сервис — соседние запросы автосинка ловили 502.
    CHUNK = 2000
    for i in range(0, len(parsed), CHUNK):
        db.bulk_save_objects(parsed[i:i + CHUNK])
        db.flush()
    db.add(models.ImportLog(filename=f"[проводки:{org}] {filename}",
                            user_id=user_id, added=len(parsed),
                            skipped=inactive, errors_count=0))
    db.commit()
    return {"added": len(parsed), "skipped_inactive": inactive}


@router.get("/accounts")
def trial_balance(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    org: str = "all",
):
    """Оборотно-сальдовая ведомость: обороты и сальдо по каждому счёту.

    Сальдо считается разницей оборотов с начала журнала — журнал
    выгружается целиком, входящих остатков у него нет. Знак не
    навязывается: активный счёт покажет плюс, пассивный минус, и это
    нормально — портал не изображает бухгалтерию, он показывает цифры."""
    turn: dict[str, dict] = {}

    def acc(code, name):
        a = turn.get(code)
        if a is None:
            a = turn[code] = {"account": code, "name": name,
                              "debit": 0.0, "credit": 0.0}
        elif name and len(name) > len(a["name"] or ""):
            a["name"] = name
        return a

    rows = models.org_scope(
        db.query(models.LedgerEntry.debit_account,
                 models.LedgerEntry.debit_name,
                 models.LedgerEntry.credit_account,
                 models.LedgerEntry.credit_name,
                 models.LedgerEntry.amount),
        models.LedgerEntry, org).all()
    for da, dn, ca, cn, amount in rows:
        amt = float(amount or 0)
        if da:
            acc(da, dn)["debit"] += amt
        if ca:
            acc(ca, cn)["credit"] += amt

    out = []
    for a in turn.values():
        out.append({
            "account": a["account"], "name": a["name"],
            "debit": round(a["debit"], 2), "credit": round(a["credit"], 2),
            "balance": round(a["debit"] - a["credit"], 2),
        })
    out.sort(key=lambda a: a["account"])
    total_d = round(sum(a["debit"] for a in out), 2)
    total_c = round(sum(a["credit"] for a in out), 2)
    return {
        "rows": out,
        "entries": len(rows),
        "total_debit": total_d,
        "total_credit": total_c,
        # В двойной записи обороты обязаны сходиться копейка в копейку.
        # Расхождение значит, что часть проводок потерялась при выгрузке.
        "balanced": abs(total_d - total_c) < 0.01,
    }


@router.get("/entries")
def entries(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    org: str = "all",
    account: str = "",
    q: str = "",
    limit: int = Query(default=200, le=1000),
):
    """Проводки: по счёту (в дебете или кредите) и поиску, свежие сверху."""
    query = models.org_scope(db.query(models.LedgerEntry),
                             models.LedgerEntry, org)
    if account.strip():
        a = account.strip()
        query = query.filter((models.LedgerEntry.debit_account == a)
                             | (models.LedgerEntry.credit_account == a))
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(models.LedgerEntry.doc.ilike(like)
                             | models.LedgerEntry.content.ilike(like)
                             | models.LedgerEntry.debit_sub.ilike(like)
                             | models.LedgerEntry.credit_sub.ilike(like))
    rows = (query.order_by(models.LedgerEntry.date.desc(),
                           models.LedgerEntry.id.desc())
            .limit(limit).all())
    return [{
        "date": r.date.isoformat(),
        "doc": r.doc,
        "debit_account": r.debit_account,
        "debit_sub": r.debit_sub,
        "credit_account": r.credit_account,
        "credit_sub": r.credit_sub,
        "amount": float(r.amount or 0),
        "content": r.content,
        "organization": r.organization,
    } for r in rows]
