"""БДДС (бюджет движения денежных средств) — план-факт по статьям.

План вводится вручную (BudgetItem) по статьям на месяц; факт берётся из
загруженных из 1С поступлений (Receipt) и расходов (Expense) и сводится по
той же статье. Отчёт показывает план, факт и отклонение по каждой статье,
итоги по поступлениям/выплатам и чистый денежный поток.
"""

from collections import defaultdict
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import get_current_user, require_roles

router = APIRouter(prefix="/budget", tags=["budget"])

can_edit = require_roles(models.Role.admin, models.Role.accountant)

NO_ARTICLE = "Без статьи"


def _month_bounds(period: str) -> tuple[date, date]:
    year, month = int(period[:4]), int(period[5:7])
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def _fact_maps(db: Session, period: str, org: str = "all") -> tuple[dict, dict]:
    """Факт по статьям за месяц: (поступления, выплаты) — в сомах."""
    start, end = _month_bounds(period)

    incoming: dict[str, float] = defaultdict(float)
    for r in (
        models.org_scope(db.query(models.Receipt), models.Receipt, org)
        .filter(models.Receipt.date >= start, models.Receipt.date < end)
        .all()
    ):
        incoming[r.operation or NO_ARTICLE] += float(r.amount_kgs)

    outgoing: dict[str, float] = defaultdict(float)
    for e in (
        models.org_scope(db.query(models.Expense), models.Expense, org)
        .filter(models.Expense.date >= start, models.Expense.date < end)
        .all()
    ):
        outgoing[e.basis or NO_ARTICLE] += float(e.amount_kgs)

    return incoming, outgoing


class PlanUpsert(BaseModel):
    period: str
    direction: str  # in|out
    article: str
    amount: float
    note: str | None = None


@router.get("")
def list_plan(
    period: str = Query(..., description="Месяц YYYY-MM"),
    org: str = Query(default="all"),
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    items = (
        models.org_scope(db.query(models.BudgetItem), models.BudgetItem, org)
        .filter(models.BudgetItem.period == period)
        .order_by(models.BudgetItem.direction, models.BudgetItem.article)
        .all()
    )
    return [
        {
            "id": i.id,
            "period": i.period,
            "direction": i.direction,
            "article": i.article,
            "amount": float(i.amount),
            "note": i.note,
        }
        for i in items
    ]


@router.put("")
def upsert_plan(
    body: PlanUpsert,
    org: str = Query(default=models.DEFAULT_ORG),
    db: Session = Depends(get_db),
    _: models.User = Depends(can_edit),
):
    if body.direction not in ("in", "out"):
        raise HTTPException(status_code=400, detail="direction должен быть in|out")
    article = body.article.strip()
    if not article:
        raise HTTPException(status_code=400, detail="Пустая статья")

    org = models.normalize_org(org)  # план привязан к конкретной фирме
    item = (
        db.query(models.BudgetItem)
        .filter(
            models.BudgetItem.organization == org,
            models.BudgetItem.period == body.period,
            models.BudgetItem.direction == body.direction,
            models.BudgetItem.article == article,
        )
        .first()
    )
    if item is None:
        item = models.BudgetItem(
            period=body.period, direction=body.direction, article=article,
            organization=org,
        )
        db.add(item)
    item.amount = round(body.amount, 2)
    if body.note is not None:
        item.note = body.note
    db.commit()
    db.refresh(item)
    return {"id": item.id, "article": item.article, "amount": float(item.amount)}


@router.delete("/{item_id}", status_code=204)
def delete_plan(
    item_id: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(can_edit),
):
    db.query(models.BudgetItem).filter(models.BudgetItem.id == item_id).delete()
    db.commit()


@router.get("/fact-articles")
def fact_articles(
    period: str = Query(...),
    org: str = Query(default="all"),
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    """Статьи, реально встречающиеся в факте за месяц — для подсказки при
    заполнении плана."""
    incoming, outgoing = _fact_maps(db, period, org)
    return {
        "in": sorted(incoming.keys()),
        "out": sorted(outgoing.keys()),
    }


@router.get("/plan-fact")
def plan_fact(
    period: str = Query(..., description="Месяц YYYY-MM"),
    org: str = Query(default="all"),
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    incoming, outgoing = _fact_maps(db, period, org)

    plan_in: dict[str, float] = defaultdict(float)
    plan_out: dict[str, float] = defaultdict(float)
    for i in (
        models.org_scope(db.query(models.BudgetItem), models.BudgetItem, org)
        .filter(models.BudgetItem.period == period)
        .all()
    ):
        (plan_in if i.direction == "in" else plan_out)[i.article] += float(i.amount)

    def build(plan: dict, fact: dict) -> list[dict]:
        rows = []
        for article in sorted(set(plan) | set(fact)):
            p = round(plan.get(article, 0.0), 2)
            f = round(fact.get(article, 0.0), 2)
            rows.append({
                "article": article,
                "plan": p,
                "fact": f,
                "variance": round(f - p, 2),
                "pct": round(f / p * 100, 1) if p else None,
            })
        return sorted(rows, key=lambda r: -max(r["plan"], r["fact"]))

    rows_in = build(plan_in, incoming)
    rows_out = build(plan_out, outgoing)

    total_in_plan = round(sum(plan_in.values()), 2)
    total_in_fact = round(sum(incoming.values()), 2)
    total_out_plan = round(sum(plan_out.values()), 2)
    total_out_fact = round(sum(outgoing.values()), 2)

    return {
        "period": period,
        "incoming": rows_in,
        "outgoing": rows_out,
        "totals": {
            "in_plan": total_in_plan,
            "in_fact": total_in_fact,
            "out_plan": total_out_plan,
            "out_fact": total_out_fact,
            "flow_plan": round(total_in_plan - total_out_plan, 2),
            "flow_fact": round(total_in_fact - total_out_fact, 2),
        },
    }
