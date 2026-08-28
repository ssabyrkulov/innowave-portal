from collections import defaultdict
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import models
from ..checks import run_checks
from ..database import get_db
from ..deps import get_current_user
from .counterparties import client_matcher
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
    # Тот же сопоставитель, что в /receipts/receivables: дашборд и дебиторка
    # обязаны давать одну цифру, а два одинаковых куска логики рано или
    # поздно расходятся.
    guid_clients: dict[str, str] = {}
    for s_row in sales:
        if s_row.client_guid:
            guid_clients.setdefault(s_row.client_guid, s_row.client)
    resolve_client = client_matcher(db, norm_clients, guid_clients, aliases)

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
        client = resolve_client(r.payer, r.payer_guid)
        if client is not None:
            paid[client] += float(r.amount_kgs)

    returned: dict[str, float] = defaultdict(float)
    for rd in return_docs:
        client = resolve_client(rd.client, rd.client_guid)
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


# Товарные группы для графиков продаж. Порядок — как в голове у владельца, а
# Порядок и склейка резервных групп — если справочник номенклатуры ещё не
# загружен, товары раскладываются по ключевым словам, как раньше.
_SALES_GROUPS = (
    ("one", "Подгузники ONE", (1,)),
    ("one_mini", "Подгузники ONE mini", (2,)),
    ("starkid", "Подгузники StarKid", (3,)),
    # Туалетная бумага, полотенца и салфетки — один товарный поток, смотреть
    # их по отдельности смысла нет.
    ("paper", "Бумага, полотенца, салфетки", (4, 5)),
    ("splash", "Шампуни SPLASH", (6,)),
    ("other", "Прочее", (7,)),
)


def _catalog_groups(db):
    """product_guid → название группы из справочника номенклатуры 1С.

    Группа товара — его родитель в дереве справочника («Подгузники ONE»,
    «Шампунь SPLASH»), то есть ровно то, как товары сгруппировал сам 1С.
    Раньше портал угадывал группу по ключевым словам в названии, и каждая
    новая группа требовала правки кода; теперь новая группа в 1С появляется
    на графике сама. Возвращает пары (guid → группа) и мост имя → guid для
    строк, загруженных до появления GUID."""
    from .purchases import _norm_product, _norm_product_exact

    products = db.query(models.Product).all()
    name_of = {p.guid: p.name for p in products}
    group_of: dict = {}
    by_name: dict = {}
    for p in products:
        if p.is_group:
            continue
        group_of[p.guid] = name_of.get(p.parent_guid)
        for candidate in (p.name, p.name_full):
            if candidate:
                by_name.setdefault(_norm_product_exact(candidate), p.guid)
                by_name.setdefault(_norm_product(candidate), p.guid)
    return group_of, by_name


@router.get("/sales-groups")
def sales_groups(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    org: str = Query(default="all"),
    months: int = Query(default=18, le=60),
):
    """Продажи по месяцам в разрезе товарных групп 1С.

    Группы берутся из справочника номенклатуры — так, как их завёл сам 1С.
    Ключевые слова остаются запасным ходом для строк, которые справочник не
    знает: угадывание хуже справочника, но лучше свалки в «Прочее».

    Каждая группа отдаётся двумя рядами: со всеми клиентами и без Байго
    Трейд. Байго — это больше половины оборота, и на общем графике остальные
    товары превращаются в плоскую линию у нуля: понять по нему, растут ли
    продажи шампуня, невозможно.
    """
    from .purchases import _group_of, _norm_product, _norm_product_exact

    big = "байго"
    group_of, by_name = _catalog_groups(db)
    fallback = {c: label for _k, label, codes in _SALES_GROUPS for c in codes}

    def label_of(product, guid):
        g = (guid or "").strip().lower()
        if g not in group_of:
            g = (by_name.get(_norm_product_exact(product or ""))
                 or by_name.get(_norm_product(product or ""), ""))
        label = group_of.get(g)
        return label or fallback.get(_group_of(product), "Прочее")

    sales = models.org_scope(db.query(models.Sale), models.Sale, org).all()
    by_group: dict = {}
    seen_months: set = set()
    for s in sales:
        label = label_of(s.product, s.product_guid)
        g = by_group.setdefault(label, defaultdict(lambda: [0.0, 0.0, 0.0, 0.0]))
        month = s.date.strftime("%Y-%m")
        seen_months.add(month)
        cell = g[month]
        amount, qty = float(s.amount or 0), float(s.qty or 0)
        cell[0] += amount
        cell[1] += qty
        if big not in (s.client or "").lower():
            cell[2] += amount
            cell[3] += qty

    period = sorted(seen_months)[-months:]
    out = []
    for label, g in by_group.items():
        series = [
            {"month": m,
             "revenue": round(g[m][0], 2), "qty": round(g[m][1], 1),
             "revenue_ex": round(g[m][2], 2), "qty_ex": round(g[m][3], 1)}
            for m in period
        ]
        total = round(sum(p["revenue"] for p in series), 2)
        total_ex = round(sum(p["revenue_ex"] for p in series), 2)
        # Пустые группы не показываем: карточка с нулевым графиком только
        # занимает место.
        if total or total_ex:
            out.append({"key": label, "label": label, "monthly": series,
                        "total": total, "total_ex": total_ex,
                        "qty": round(sum(p["qty"] for p in series), 1),
                        "qty_ex": round(sum(p["qty_ex"] for p in series), 1)})
    out.sort(key=lambda g: -g["total"])
    return {"months": period, "groups": out, "excluded": "Байго Трейд"}
