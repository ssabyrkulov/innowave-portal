from collections import defaultdict
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import models
from ..checks import run_checks
from ..database import get_db
from ..deps import get_current_user
from .receipts import CUSTOMER_PAYMENT_PREFIX, _normalize

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("")
def dashboard(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    org: str = Query(default="all"),
):
    today = date.today()
    cur_month = today.strftime("%Y-%m")
    prev_month = (
        date(today.year - 1, 12, 1) if today.month == 1
        else date(today.year, today.month - 1, 1)
    ).strftime("%Y-%m")

    # --- Продажи ---
    sales = models.org_scope(db.query(models.Sale), models.Sale, org).all()
    monthly_sales: dict[str, float] = defaultdict(float)
    for s in sales:
        monthly_sales[s.date.strftime("%Y-%m")] += float(s.amount)
    months = sorted(monthly_sales)[-12:]

    # --- Дебиторка (та же логика, что в /receipts/receivables) ---
    receipts = models.org_scope(db.query(models.Receipt), models.Receipt, org).all()
    return_docs = models.org_scope(db.query(models.ReturnDoc), models.ReturnDoc, org).all()
    aliases = {a.payer: a.client for a in db.query(models.ClientAlias).all()}

    shipped: dict[str, float] = defaultdict(float)
    seen_docs: set = set()
    for s in sales:
        if s.doc_number and s.doc_total is not None:
            key = (s.doc_number, s.date, s.client)
            if key in seen_docs:
                continue
            seen_docs.add(key)
            shipped[s.client] += float(s.doc_total)
        else:
            shipped[s.client] += float(s.amount) * (
                1 - float(s.discount_pct or 0) / 100
            )

    norm_clients = {_normalize(c): c for c in shipped}
    paid: dict[str, float] = defaultdict(float)
    month_in = 0.0
    prev_month_in = 0.0
    for r in receipts:
        if not r.operation.startswith(CUSTOMER_PAYMENT_PREFIX):
            continue
        rm = r.date.strftime("%Y-%m")
        if rm == cur_month:
            month_in += float(r.amount_kgs)
        elif rm == prev_month:
            prev_month_in += float(r.amount_kgs)
        client = aliases.get(r.payer)
        if client is None:
            client = r.payer if r.payer in shipped else norm_clients.get(_normalize(r.payer))
        if client is not None:
            paid[client] += float(r.amount_kgs)

    returned: dict[str, float] = defaultdict(float)
    for rd in return_docs:
        client = aliases.get(rd.client)
        if client is None:
            client = rd.client if rd.client in shipped else norm_clients.get(_normalize(rd.client))
        returned[client or rd.client] += float(rd.amount)

    debts = sorted(
        (
            {"name": c, "debt": round(shipped[c] - returned.get(c, 0.0) - paid.get(c, 0.0), 2)}
            for c in shipped
            if shipped[c] - returned.get(c, 0.0) - paid.get(c, 0.0) > 0.01
        ),
        key=lambda x: -x["debt"],
    )
    total_debt = round(sum(d["debt"] for d in debts), 2)

    # --- Расходы текущего/прошлого месяца (в сомах) ---
    expense_month = 0.0
    expense_prev = 0.0
    for e in models.org_scope(db.query(models.Expense), models.Expense, org).all():
        em = e.date.strftime("%Y-%m")
        if em == cur_month:
            expense_month += float(e.amount_kgs)
        elif em == prev_month:
            expense_prev += float(e.amount_kgs)

    # --- Деньги на счетах (снапшот из 1С) ---
    cash_rows = models.org_scope(db.query(models.CashBalance), models.CashBalance, org).all()
    cash = {
        "total": round(sum(float(r.amount) for r in cash_rows), 2),
        "updated_at": cash_rows[0].updated_at.isoformat() if cash_rows else None,
        "accounts": len(cash_rows),
        "items": sorted(
            ({"account": r.account, "amount": round(float(r.amount), 2)}
             for r in cash_rows),
            key=lambda x: -x["amount"],
        ),
    } if cash_rows else None

    # --- Контроль ---
    acked = {a.vhash for a in db.query(models.ViolationAck).all()}
    critical = warning = 0
    for v in run_checks(db, org=org):
        if v["vhash"] in acked:
            continue
        if v["severity"] == "critical":
            critical += 1
        else:
            warning += 1

    # --- Платёжный календарь ---
    payments = db.query(models.Payment).all()
    horizon = today + timedelta(days=30)
    upcoming = sorted(
        (
            p for p in payments
            if p.status == models.Status.planned and today <= p.due_date <= horizon
        ),
        key=lambda p: p.due_date,
    )
    overdue = [
        p for p in payments
        if p.status == models.Status.overdue
        or (p.status == models.Status.planned and p.due_date < today)
    ]
    out_30 = sum(
        float(p.amount) for p in upcoming
        if p.direction == models.Direction.outgoing
    )
    in_30 = sum(
        float(p.amount) for p in upcoming
        if p.direction == models.Direction.incoming
    )

    return {
        "today": today.isoformat(),
        "current_month": cur_month,
        "sales": {
            "month": round(monthly_sales.get(cur_month, 0.0), 2),
            "prev_month": round(monthly_sales.get(prev_month, 0.0), 2),
            "monthly": [
                {"month": m, "revenue": round(monthly_sales[m], 2)} for m in months
            ],
        },
        "money": {
            "month_in": round(month_in, 2),
            "prev_month_in": round(prev_month_in, 2),
            "month_out": round(expense_month, 2),
            "prev_month_out": round(expense_prev, 2),
        },
        "cash": cash,
        "debt": {
            "total": total_debt,
            "debtors": len(debts),
            "top": debts[:4],
        },
        "checks": {"critical": critical, "warning": warning},
        "payments": {
            "upcoming": [
                {
                    "title": p.title,
                    "amount": float(p.amount),
                    "currency": p.currency,
                    "direction": p.direction.value,
                    "due_date": p.due_date.isoformat(),
                }
                for p in upcoming[:6]
            ],
            "overdue_count": len(overdue),
            "out_30": round(out_30, 2),
            "in_30": round(in_30, 2),
        },
    }
