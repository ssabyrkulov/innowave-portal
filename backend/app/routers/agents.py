from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import get_current_user
from .receipts import CUSTOMER_PAYMENT_PREFIX, _normalize

router = APIRouter(prefix="/agents", tags=["agents"])

NO_AGENT = "— без агента —"


@router.get("/summary")
def agents_summary(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    sales = db.query(models.Sale).all()
    receipts = db.query(models.Receipt).all()
    aliases = {a.payer: a.client for a in db.query(models.ClientAlias).all()}

    # --- Оплаты по клиентам (та же логика сопоставления, что в дебиторке) ---
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
    for r in receipts:
        if not r.operation.startswith(CUSTOMER_PAYMENT_PREFIX):
            continue
        client = aliases.get(r.payer)
        if client is None:
            client = r.payer if r.payer in shipped_by_client else norm_clients.get(_normalize(r.payer))
        if client is not None:
            paid_by_client[client] += float(r.amount_kgs)

    debt_by_client = {
        c: round(shipped_by_client[c] - paid_by_client.get(c, 0.0), 2)
        for c in shipped_by_client
    }

    # --- Агрегация по агентам ---
    agents: dict[str, dict] = {}
    client_agent_rev: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

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

    # Клиент закрепляется за агентом с наибольшей выручкой по нему —
    # долг клиента попадает в строку этого агента.
    primary_agent = {
        client: max(rev_map.items(), key=lambda kv: kv[1])[0]
        for client, rev_map in client_agent_rev.items()
    }
    debt_by_agent: dict[str, float] = defaultdict(float)
    debtors_by_agent: dict[str, int] = defaultdict(int)
    for client, agent in primary_agent.items():
        debt = debt_by_client.get(client, 0.0)
        if debt > 0.01:
            debt_by_agent[agent] += debt
            debtors_by_agent[agent] += 1

    # Все месяцы периода — чтобы спарклайны были в одной шкале
    all_months = sorted({m for a in agents.values() for m in a["monthly"]})
    months = all_months[-12:]

    out = []
    for a in agents.values():
        docs_count = len(a["docs"])
        top_clients = sorted(
            (
                {
                    "name": c,
                    "revenue": round(client_agent_rev[c][a["name"]], 2),
                    "debt": debt_by_client.get(c, 0.0)
                    if primary_agent.get(c) == a["name"]
                    else 0.0,
                }
                for c in a["clients"]
            ),
            key=lambda x: -x["revenue"],
        )[:5]
        out.append({
            "name": a["name"],
            "revenue": round(a["revenue"], 2),
            "docs": docs_count,
            "clients": len(a["clients"]),
            "avg_doc": round(a["revenue"] / docs_count, 2) if docs_count else 0,
            "debt": round(debt_by_agent.get(a["name"], 0.0), 2),
            "debtors": debtors_by_agent.get(a["name"], 0),
            "last_shipment": a["last_shipment"].isoformat() if a["last_shipment"] else None,
            "monthly": [
                {"month": m, "revenue": round(a["monthly"].get(m, 0.0), 2)}
                for m in months
            ],
            "top_clients": top_clients,
        })

    no_agent = next((x for x in out if x["name"] == NO_AGENT), None)
    out = [x for x in out if x["name"] != NO_AGENT]
    out.sort(key=lambda x: -x["revenue"])

    return {
        "agents": out,
        "no_agent": no_agent,
        "months": months,
        "total_revenue": round(sum(x["revenue"] for x in out), 2),
    }
