import calendar
from collections import defaultdict
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import get_current_user, require_roles
from .receipts import CUSTOMER_PAYMENT_PREFIX, _normalize

router = APIRouter(prefix="/agents", tags=["agents"])

can_edit = require_roles(models.Role.admin, models.Role.accountant)

NO_AGENT = "— без агента —"
SLEEPING_DAYS = 45      # клиент «уснул», если нет отгрузок дольше
SLEEPING_MIN_REVENUE = 5000  # мелкие разовые покупки не считаем потерей


@router.get("/summary")
def agents_summary(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    sales = db.query(models.Sale).all()
    receipts = db.query(models.Receipt).all()
    aliases = {a.payer: a.client for a in db.query(models.ClientAlias).all()}
    today = date.today()
    cur_month = today.strftime("%Y-%m")
    prev_month = (
        date(today.year - 1, 12, 1) if today.month == 1
        else date(today.year, today.month - 1, 1)
    ).strftime("%Y-%m")
    targets = {
        (t.agent, t.month): float(t.amount)
        for t in db.query(models.AgentTarget).all()
    }

    # --- Отгрузки/оплаты по клиентам (та же логика, что в дебиторке) ---
    shipped_by_client: dict[str, float] = defaultdict(float)
    seen_docs: set = set()
    for s in sales:
        client = s.client
        if s.doc_number and s.doc_total is not None:
            key = (s.doc_number, s.date, client)
            if key in seen_docs:
                continue
            seen_docs.add(key)
            shipped_by_client[client] += float(s.doc_total)
        else:
            shipped_by_client[client] += float(s.amount) * (
                1 - float(s.discount_pct or 0) / 100
            )

    norm_clients = {_normalize(c): c for c in shipped_by_client}
    paid_by_client: dict[str, float] = defaultdict(float)
    last_payment_by_client: dict[str, date] = {}
    for r in receipts:
        if not r.operation.startswith(CUSTOMER_PAYMENT_PREFIX):
            continue
        client = aliases.get(r.payer)
        if client is None:
            client = r.payer if r.payer in shipped_by_client else norm_clients.get(_normalize(r.payer))
        if client is None:
            continue
        paid_by_client[client] += float(r.amount_kgs)
        if client not in last_payment_by_client or r.date > last_payment_by_client[client]:
            last_payment_by_client[client] = r.date

    debt_by_client = {
        c: round(shipped_by_client[c] - paid_by_client.get(c, 0.0), 2)
        for c in shipped_by_client
    }

    # --- Агрегация по агентам ---
    agents: dict[str, dict] = {}
    client_agent_rev: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    last_shipment_by_client: dict[str, date] = {}

    for s in sales:
        name = s.agent or s.responsible or NO_AGENT
        amt = float(s.amount)
        a = agents.setdefault(name, {
            "name": name,
            "revenue": 0.0,
            "docs": set(),
            "clients": set(),
            "monthly": defaultdict(float),
            "last_shipment": None,
        })
        a["revenue"] += amt
        if s.doc_number:
            a["docs"].add(f"{s.doc_number}|{s.date}")
        a["clients"].add(s.client)
        a["monthly"][s.date.strftime("%Y-%m")] += amt
        if a["last_shipment"] is None or s.date > a["last_shipment"]:
            a["last_shipment"] = s.date
        client_agent_rev[s.client][name] += amt
        if s.client not in last_shipment_by_client or s.date > last_shipment_by_client[s.client]:
            last_shipment_by_client[s.client] = s.date

    primary_agent = {
        client: max(rev_map.items(), key=lambda kv: kv[1])[0]
        for client, rev_map in client_agent_rev.items()
    }
    agent_clients: dict[str, list[str]] = defaultdict(list)
    for client, agent in primary_agent.items():
        agent_clients[agent].append(client)

    all_months = sorted({m for a in agents.values() for m in a["monthly"]})
    months = all_months[-12:]

    days_in_month = calendar.monthrange(today.year, today.month)[1]

    out = []
    for a in agents.values():
        name = a["name"]
        docs_count = len(a["docs"])
        month_rev = a["monthly"].get(cur_month, 0.0)
        prev_rev = a["monthly"].get(prev_month, 0.0)
        forecast = (
            month_rev / max(today.day, 1) * days_in_month
            if cur_month in a["monthly"]
            else 0.0
        )
        target = targets.get((name, cur_month))

        attributed = agent_clients.get(name, [])
        debtors = sorted(
            (
                {
                    "name": c,
                    "debt": debt_by_client.get(c, 0.0),
                    "last_payment": lp.isoformat() if (lp := last_payment_by_client.get(c)) else None,
                    "days_no_payment": (today - lp).days if lp else None,
                }
                for c in attributed
                if debt_by_client.get(c, 0.0) > 0.01
            ),
            key=lambda x: -x["debt"],
        )
        sleeping = sorted(
            (
                {
                    "name": c,
                    "revenue": round(sum(client_agent_rev[c].values()), 2),
                    "last_shipment": last_shipment_by_client[c].isoformat(),
                    "days": (today - last_shipment_by_client[c]).days,
                }
                for c in attributed
                if (today - last_shipment_by_client[c]).days > SLEEPING_DAYS
                and sum(client_agent_rev[c].values()) >= SLEEPING_MIN_REVENUE
            ),
            key=lambda x: -x["revenue"],
        )
        top_clients = sorted(
            (
                {
                    "name": c,
                    "revenue": round(client_agent_rev[c][name], 2),
                    "debt": debt_by_client.get(c, 0.0)
                    if primary_agent.get(c) == name
                    else 0.0,
                }
                for c in a["clients"]
            ),
            key=lambda x: -x["revenue"],
        )[:5]

        out.append({
            "name": name,
            "revenue": round(a["revenue"], 2),
            "docs": docs_count,
            "clients": len(a["clients"]),
            "avg_doc": round(a["revenue"] / docs_count, 2) if docs_count else 0,
            "debt": round(sum(d["debt"] for d in debtors), 2),
            "debtors_count": len(debtors),
            "last_shipment": a["last_shipment"].isoformat() if a["last_shipment"] else None,
            "month_revenue": round(month_rev, 2),
            "prev_month_revenue": round(prev_rev, 2),
            "forecast": round(forecast, 2),
            "target": target,
            "target_pct": round(month_rev / target * 100, 1) if target else None,
            "monthly": [
                {"month": m, "revenue": round(a["monthly"].get(m, 0.0), 2)}
                for m in months
            ],
            "debtors": debtors[:12],
            "sleeping": sleeping[:12],
            "top_clients": top_clients,
        })

    no_agent = next((x for x in out if x["name"] == NO_AGENT), None)
    out = [x for x in out if x["name"] != NO_AGENT]
    out.sort(key=lambda x: -x["revenue"])

    return {
        "agents": out,
        "no_agent": no_agent,
        "months": months,
        "current_month": cur_month,
        "prev_month": prev_month,
        "total_revenue": round(sum(x["revenue"] for x in out), 2),
        "total_month_revenue": round(sum(x["month_revenue"] for x in out), 2),
    }


class TargetSet(BaseModel):
    agent: str
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    amount: float = Field(ge=0)


@router.post("/target")
def set_target(
    payload: TargetSet,
    db: Session = Depends(get_db),
    _: models.User = Depends(can_edit),
):
    row = (
        db.query(models.AgentTarget)
        .filter_by(agent=payload.agent, month=payload.month)
        .first()
    )
    if payload.amount == 0:
        if row:
            db.delete(row)
            db.commit()
        return {"status": "deleted"}
    if row:
        row.amount = payload.amount
    else:
        db.add(models.AgentTarget(
            agent=payload.agent, month=payload.month, amount=payload.amount
        ))
    db.commit()
    return {"status": "ok"}
