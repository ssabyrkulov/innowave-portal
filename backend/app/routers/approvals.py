"""Согласование платежей.

Ни один платёж не уходит из банка или кассы без заявки, прошедшей цепочку:

    черновик → проверка бухгалтера → согласование → утверждена → оплачена → сверена

Кто что может (роли портала: admin, accountant, viewer):
  • создать заявку — любой активный пользователь (инициатор);
  • проверить, вернуть на доработку, отметить оплату — бухгалтер и админ;
  • утвердить/отклонить — админ без ограничений; бухгалтер до лимита фирмы
    (ApprovalSettings.accountant_limit); пользователь с личным лимитом
    (User.approve_limit) — до этого лимита. Заявка сверх плана БДДС
    утверждается только админом, каким бы ни был лимит;
  • отменить — инициатор, пока заявка не утверждена, и админ до оплаты.

История хранится в PaymentRequestEvent и никогда не удаляется. После
оплаты заявка сама ищет свою платёжку в выгрузке 1С (Expense) и, найдя,
переходит в «сверена»: так видно, что оплачено, но не проведено в учёте.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import RequestStatus as S
from ..services.notify import notify

router = APIRouter(prefix="/approvals", tags=["approvals"])

admin_only = require_roles(models.Role.admin)
staff = require_roles(models.Role.admin, models.Role.accountant)

MAX_FILE_BYTES = 10 * 1024 * 1024
ACTIVE = (S.review.value, S.approval.value, S.approved.value)
FINAL = (S.paid.value, S.reconciled.value, S.rejected.value, S.cancelled.value)

STATUS_LABELS = {
    S.draft.value: "черновик",
    S.review.value: "на проверке",
    S.approval.value: "на согласовании",
    S.approved.value: "утверждена, ждёт оплаты",
    S.paid.value: "оплачена",
    S.reconciled.value: "оплачена и сверена с 1С",
    S.rejected.value: "отклонена",
    S.cancelled.value: "отменена",
}

ORG_LABELS = {
    "hygiene": "Innowave Hygiene",
    "innowave": "Innowave",
    "bluecarbon": "Blue Carbon",
}


# ---------- схемы ----------

class RequestIn(BaseModel):
    organization: str
    title: str
    amount: Decimal = Field(gt=0)
    currency: str = "KGS"
    due_date: date
    counterparty: str
    counterparty_guid: str | None = None
    article: str | None = None
    method: str = "bank"
    basis: str | None = None
    purpose: str | None = None
    note: str | None = None
    submit: bool = False  # сразу отправить, минуя черновик


class RequestPatch(BaseModel):
    organization: str | None = None
    title: str | None = None
    amount: Decimal | None = Field(default=None, gt=0)
    currency: str | None = None
    due_date: date | None = None
    counterparty: str | None = None
    counterparty_guid: str | None = None
    article: str | None = None
    method: str | None = None
    basis: str | None = None
    purpose: str | None = None
    note: str | None = None


class Decision(BaseModel):
    comment: str | None = None


class PayIn(BaseModel):
    paid_date: date
    paid_amount: Decimal | None = Field(default=None, gt=0)
    paid_doc: str | None = None
    comment: str | None = None


class SettingsIn(BaseModel):
    organization: str
    accountant_limit: Decimal = Field(ge=0)
    cash_cushion: Decimal = Field(ge=0)
    require_basis_file: bool = True


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    action: str
    from_status: str | None
    to_status: str | None
    comment: str | None
    created_at: datetime
    user_name: str | None = None


class FileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    filename: str
    content_type: str
    size: int
    kind: str
    created_at: datetime


class RequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    organization: str
    title: str
    amount: Decimal
    currency: str
    due_date: date
    counterparty: str
    counterparty_guid: str | None
    article: str | None
    method: str
    basis: str | None
    purpose: str | None
    note: str | None
    status: str
    created_by: int
    creator_name: str | None = None
    checker_name: str | None = None
    approver_name: str | None = None
    payer_name: str | None = None
    submitted_at: datetime | None
    checked_at: datetime | None
    decided_at: datetime | None
    paid_at: datetime | None
    paid_date: date | None
    paid_amount: Decimal | None
    paid_doc: str | None
    decision_comment: str | None
    expense_id: int | None
    created_at: datetime
    updated_at: datetime
    files_count: int = 0
    warnings: list[dict] = []
    can: dict = {}


class RequestDetail(RequestOut):
    events: list[EventOut] = []
    files: list[FileOut] = []
    budget: dict | None = None
    cash: dict | None = None


# ---------- вспомогательное ----------

def _norm(name: str | None) -> str:
    s = (name or "").lower().replace("ё", "е")
    s = re.sub(r"[\"«»'().,/\\-]", " ", s)
    s = re.sub(r"\b(осоо|ип|оао|зао|ооо|тоо|ltd|inc|co|llc)\b", " ", s)
    return " ".join(s.split())


def _same_party(a: str | None, b: str | None) -> bool:
    x, y = _norm(a), _norm(b)
    if not x or not y:
        return False
    if x == y or x in y or y in x:
        return True
    wa, wb = set(x.split()), set(y.split())
    common = {w for w in wa & wb if len(w) > 2}
    return len(common) >= 2 or (len(common) == 1 and (len(wa) == 1 or len(wb) == 1))


def _month_bounds(d: date) -> tuple[date, date]:
    start = date(d.year, d.month, 1)
    end = date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)
    return start, end


def _settings_for(db: Session, org: str) -> models.ApprovalSettings:
    row = db.query(models.ApprovalSettings).filter_by(organization=org).first()
    if row is None:
        row = db.query(models.ApprovalSettings).filter_by(organization="all").first()
    if row is None:
        row = models.ApprovalSettings(organization="all", accountant_limit=0,
                                      cash_cushion=0, require_basis_file=True)
    return row


def _amount_kgs(req: models.PaymentRequest) -> float:
    # Валютные заявки в бюджет и остатки считаем по номиналу: курс на дату
    # оплаты неизвестен, а грубая оценка хуже честного «в валюте».
    return float(req.amount)


def _budget_info(db: Session, req: models.PaymentRequest) -> dict | None:
    """План БДДС по статье заявки за месяц срока оплаты против того, что уже
    оплачено (факт 1С) и утверждено, но ещё не оплачено (заявки)."""
    if not req.article:
        return None
    start, end = _month_bounds(req.due_date)
    period = start.strftime("%Y-%m")
    plan_row = (
        db.query(models.BudgetItem)
        .filter(models.BudgetItem.organization == req.organization,
                models.BudgetItem.period == period,
                models.BudgetItem.direction == "out",
                models.BudgetItem.article == req.article)
        .first()
    )
    plan = float(plan_row.amount) if plan_row else None
    fact = sum(
        float(e.amount_kgs)
        for e in db.query(models.Expense)
        .filter(models.Expense.organization == req.organization,
                models.Expense.date >= start, models.Expense.date < end,
                models.Expense.basis == req.article)
        .all()
    )
    pending = sum(
        _amount_kgs(r)
        for r in db.query(models.PaymentRequest)
        .filter(models.PaymentRequest.organization == req.organization,
                models.PaymentRequest.article == req.article,
                models.PaymentRequest.due_date >= start,
                models.PaymentRequest.due_date < end,
                models.PaymentRequest.status == S.approved.value,
                models.PaymentRequest.id != req.id)
        .all()
    )
    used = fact + pending
    left = (plan - used) if plan is not None else None
    return {
        "period": period,
        "article": req.article,
        "plan": plan,
        "fact": round(fact, 2),
        "pending": round(pending, 2),
        "left": round(left, 2) if left is not None else None,
        "over": bool(plan is not None and _amount_kgs(req) > left),
        "no_plan": plan is None,
    }


def _cash_info(db: Session, req: models.PaymentRequest) -> dict:
    rows = (db.query(models.CashBalance)
            .filter(models.CashBalance.organization == req.organization).all())
    total = sum(float(r.amount) for r in rows)
    committed = sum(
        _amount_kgs(r)
        for r in db.query(models.PaymentRequest)
        .filter(models.PaymentRequest.organization == req.organization,
                models.PaymentRequest.status == S.approved.value,
                models.PaymentRequest.id != req.id)
        .all()
    )
    cushion = float(_settings_for(db, req.organization).cash_cushion)
    after = total - committed - _amount_kgs(req)
    return {
        "total": round(total, 2),
        "committed": round(committed, 2),
        "after": round(after, 2),
        "cushion": cushion,
        "updated_at": rows[0].updated_at.isoformat() if rows else None,
        "below_cushion": bool(rows) and after < cushion,
        "negative": bool(rows) and after < 0,
    }


def _warnings(db: Session, req: models.PaymentRequest,
              budget: dict | None = None, cash: dict | None = None) -> list[dict]:
    out: list[dict] = []
    budget = budget if budget is not None else _budget_info(db, req)
    if budget:
        if budget["no_plan"]:
            out.append({"code": "no_plan", "level": "info",
                        "text": f"По статье «{req.article}» нет плана на {budget['period']}"})
        elif budget["over"]:
            out.append({"code": "over_budget", "level": "warn",
                        "text": f"Сверх плана по статье: остаток {budget['left']:,.0f}, "
                                f"заявка {float(req.amount):,.0f}. Утверждает только руководитель"})
    elif not req.article:
        out.append({"code": "no_article", "level": "info", "text": "Не указана статья бюджета"})

    cash = cash if cash is not None else _cash_info(db, req)
    if cash["negative"]:
        out.append({"code": "cash_negative", "level": "warn",
                    "text": f"Денег на счетах не хватает: после оплаты остаток {cash['after']:,.0f}"})
    elif cash["below_cushion"]:
        out.append({"code": "cash_cushion", "level": "warn",
                    "text": f"После оплаты остаток {cash['after']:,.0f} ниже подушки {cash['cushion']:,.0f}"})

    # Дубль: та же сумма тому же контрагенту за последние 30 дней — среди
    # заявок и среди уже проведённых в 1С оплат.
    since = req.due_date - timedelta(days=30)
    until = req.due_date + timedelta(days=30)
    amt = float(req.amount)
    dup_req = [
        r for r in db.query(models.PaymentRequest)
        .filter(models.PaymentRequest.organization == req.organization,
                models.PaymentRequest.id != req.id,
                models.PaymentRequest.due_date >= since,
                models.PaymentRequest.due_date <= until,
                models.PaymentRequest.status.notin_((S.rejected.value, S.cancelled.value)))
        .all()
        if abs(float(r.amount) - amt) < 0.5 and _same_party(r.counterparty, req.counterparty)
    ]
    if dup_req:
        out.append({"code": "duplicate_request", "level": "warn",
                    "text": "Похожая заявка уже есть: №" + ", №".join(str(r.id) for r in dup_req)})
    dup_exp = [
        e for e in db.query(models.Expense)
        .filter(models.Expense.organization == req.organization,
                models.Expense.date >= since, models.Expense.date <= until)
        .all()
        if abs(float(e.amount) - amt) < 0.5 and _same_party(e.counterparty, req.counterparty)
    ]
    if dup_exp and req.status not in (S.paid.value, S.reconciled.value):
        e = dup_exp[0]
        out.append({"code": "duplicate_expense", "level": "warn",
                    "text": f"Такая оплата уже есть в 1С: {e.date.isoformat()} "
                            f"{e.counterparty} {float(e.amount):,.0f}"})

    # Контрагент не из справочника 1С — до оплаты его надо завести.
    if not req.counterparty_guid:
        known = db.query(models.Counterparty).filter(
            models.Counterparty.deleted.is_(False)).all()
        if not any(_same_party(c.name, req.counterparty) for c in known):
            out.append({"code": "new_counterparty", "level": "info",
                        "text": "Контрагента нет в справочнике 1С"})
    return out


def _can(req: models.PaymentRequest, user: models.User, db: Session,
         over_budget: bool | None = None) -> dict:
    is_admin = user.role == models.Role.admin
    is_acc = user.role == models.Role.accountant
    is_staff = is_admin or is_acc
    own = req.created_by == user.id
    st = req.status
    if over_budget is None:
        b = _budget_info(db, req)
        over_budget = bool(b and b["over"])

    limit = None
    if is_admin:
        limit = float("inf")
    else:
        conf = _settings_for(db, req.organization)
        if is_acc and float(conf.accountant_limit) > 0:
            limit = float(conf.accountant_limit)
        if user.approve_limit is not None and float(user.approve_limit) > 0:
            limit = max(limit or 0, float(user.approve_limit))
    may_approve = (
        st == S.approval.value
        and limit is not None
        and float(req.amount) <= limit
        and (is_admin or not over_budget)
    )
    return {
        "edit": st in (S.draft.value, S.review.value) and (own or is_staff)
                or st == S.approval.value and is_staff,
        "submit": st == S.draft.value and (own or is_staff),
        "check": st == S.review.value and is_staff,
        "return": st in (S.review.value, S.approval.value) and is_staff,
        "approve": may_approve,
        "reject": st in (S.review.value, S.approval.value) and (is_admin or may_approve),
        "pay": st == S.approved.value and is_staff,
        "cancel": (st in (S.draft.value, S.review.value, S.approval.value) and own)
                  or (st in (S.draft.value, S.review.value, S.approval.value, S.approved.value)
                      and is_admin),
        "upload": st not in (S.rejected.value, S.cancelled.value) and (own or is_staff),
    }


def _event(db: Session, req: models.PaymentRequest, user: models.User | None,
           action: str, to_status: str | None, comment: str | None = None) -> None:
    db.add(models.PaymentRequestEvent(
        request_id=req.id, user_id=user.id if user else None, action=action,
        from_status=req.status, to_status=to_status, comment=(comment or "").strip() or None,
    ))
    if to_status:
        req.status = to_status


def _link(req: models.PaymentRequest) -> str:
    base = settings.portal_url.rstrip("/")
    return f"{base}/approvals?id={req.id}" if base else f"заявка №{req.id}"


def _tg(req: models.PaymentRequest, head: str, who: models.User, extra: str = "") -> None:
    notify(
        f"<b>{head}</b>\n"
        f"№{req.id} · {ORG_LABELS.get(req.organization, req.organization)}\n"
        f"{req.title}\n{req.counterparty}: <b>{float(req.amount):,.0f} {req.currency}</b>"
        f" · срок {req.due_date.strftime('%d.%m')}\n"
        f"{extra}{who.full_name}\n{_link(req)}"
    )


def _try_reconcile(db: Session, req: models.PaymentRequest) -> bool:
    """Ищет оплату заявки в выгрузке 1С: та же фирма, сумма (±1 сом), дата в
    пределах трёх дней от даты оплаты, контрагент похож. Нашла — заявка
    «сверена»."""
    if req.status != S.paid.value or not req.paid_date:
        return False
    amt = float(req.paid_amount or req.amount)
    lo, hi = req.paid_date - timedelta(days=3), req.paid_date + timedelta(days=3)
    taken = {r.expense_id for r in db.query(models.PaymentRequest.expense_id)
             .filter(models.PaymentRequest.expense_id.isnot(None)).all()}
    cands = (db.query(models.Expense)
             .filter(models.Expense.organization == req.organization,
                     models.Expense.date >= lo, models.Expense.date <= hi)
             .all())
    for e in cands:
        if e.id in taken:
            continue
        value = float(e.amount) if req.currency != "KGS" else float(e.amount_kgs)
        if abs(value - amt) > 1.0:
            continue
        if not _same_party(e.counterparty, req.counterparty):
            continue
        req.expense_id = e.id
        _event(db, req, None, "reconcile", S.reconciled.value,
               f"Найдена в 1С: {e.kind} {e.doc_number or ''} от {e.date.isoformat()}")
        return True
    return False


def _serialize(db: Session, req: models.PaymentRequest, user: models.User,
               detail: bool = False) -> RequestOut | RequestDetail:
    budget = _budget_info(db, req) if detail or req.status in ACTIVE else None
    cash = _cash_info(db, req) if (detail or req.status in ACTIVE) else None
    warnings = _warnings(db, req, budget, cash) if req.status in ACTIVE or detail else []
    can = _can(req, user, db, over_budget=bool(budget and budget["over"]))
    base = dict(
        id=req.id, organization=req.organization, title=req.title, amount=req.amount,
        currency=req.currency, due_date=req.due_date, counterparty=req.counterparty,
        counterparty_guid=req.counterparty_guid, article=req.article, method=req.method,
        basis=req.basis, purpose=req.purpose, note=req.note, status=req.status,
        created_by=req.created_by,
        creator_name=req.creator.full_name if req.creator else None,
        checker_name=req.checker.full_name if req.checker else None,
        approver_name=req.approver.full_name if req.approver else None,
        payer_name=req.payer.full_name if req.payer else None,
        submitted_at=req.submitted_at, checked_at=req.checked_at,
        decided_at=req.decided_at, paid_at=req.paid_at, paid_date=req.paid_date,
        paid_amount=req.paid_amount, paid_doc=req.paid_doc,
        decision_comment=req.decision_comment, expense_id=req.expense_id,
        created_at=req.created_at, updated_at=req.updated_at,
        files_count=len(req.files), warnings=warnings, can=can,
    )
    if not detail:
        return RequestOut(**base)
    return RequestDetail(
        **base,
        events=[EventOut(id=e.id, action=e.action, from_status=e.from_status,
                         to_status=e.to_status, comment=e.comment, created_at=e.created_at,
                         user_name=e.user.full_name if e.user else "портал")
                for e in req.events],
        files=[FileOut.model_validate(f) for f in req.files],
        budget=budget, cash=cash,
    )


def _get(db: Session, request_id: int) -> models.PaymentRequest:
    req = db.get(models.PaymentRequest, request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Заявка не найдена")
    return req


def _forbid(cond: bool, msg: str = "Это действие вам недоступно") -> None:
    if not cond:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=msg)


def _check_org(org: str) -> str:
    o = (org or "").strip().lower()
    if o not in models.ORGS:
        raise HTTPException(status_code=422, detail="Укажите фирму: hygiene, innowave или bluecarbon")
    return o


# ---------- список и сводка ----------

@router.get("", response_model=list[RequestOut])
def list_requests(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
    org: str = Query(default="all"),
    scope: str = Query(default="all"),      # all | inbox | mine
    status_filter: str | None = Query(default=None, alias="status"),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
):
    q = db.query(models.PaymentRequest)
    o = (org or "").strip().lower()
    if o in models.ORGS:
        q = q.filter(models.PaymentRequest.organization == o)
    if status_filter:
        if status_filter == "active":
            q = q.filter(models.PaymentRequest.status.in_(ACTIVE))
        elif status_filter == "done":
            q = q.filter(models.PaymentRequest.status.in_(FINAL))
        else:
            q = q.filter(models.PaymentRequest.status == status_filter)
    if date_from:
        q = q.filter(models.PaymentRequest.due_date >= date_from)
    if date_to:
        q = q.filter(models.PaymentRequest.due_date <= date_to)
    if scope == "mine":
        q = q.filter(models.PaymentRequest.created_by == user.id)
    rows = q.order_by(models.PaymentRequest.due_date.asc(),
                      models.PaymentRequest.id.asc()).all()

    # Оплаченные заявки при каждом просмотре пробуют найти себя в 1С.
    changed = False
    for r in rows:
        if r.status == S.paid.value and _try_reconcile(db, r):
            changed = True
    if changed:
        db.commit()

    out = [_serialize(db, r, user) for r in rows]
    if scope == "inbox":
        out = [r for r in out if r.can["check"] or r.can["approve"] or r.can["pay"]
               or (r.status == S.draft.value and r.created_by == user.id)]
    return out


@router.get("/summary")
def summary(
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
    org: str = Query(default="all"),
):
    """Счётчики для меню и главной: сколько заявок ждёт именно этого
    пользователя, сколько всего в работе, сколько просрочено."""
    q = db.query(models.PaymentRequest).filter(
        models.PaymentRequest.status.in_(ACTIVE + (S.draft.value,)))
    o = (org or "").strip().lower()
    if o in models.ORGS:
        q = q.filter(models.PaymentRequest.organization == o)
    rows = q.all()
    today = date.today()
    inbox = inbox_sum = 0.0
    by_status: dict[str, dict] = defaultdict(lambda: {"count": 0, "sum": 0.0})
    overdue = overdue_sum = 0.0
    for r in rows:
        can = _can(r, user, db)
        if can["check"] or can["approve"] or can["pay"] or (
                r.status == S.draft.value and r.created_by == user.id):
            inbox += 1
            inbox_sum += float(r.amount)
        by_status[r.status]["count"] += 1
        by_status[r.status]["sum"] += float(r.amount)
        if r.status == S.approved.value and r.due_date < today:
            overdue += 1
            overdue_sum += float(r.amount)
    return {
        "inbox": int(inbox), "inbox_sum": round(inbox_sum, 2),
        "overdue": int(overdue), "overdue_sum": round(overdue_sum, 2),
        "by_status": {k: {"count": v["count"], "sum": round(v["sum"], 2)}
                      for k, v in by_status.items()},
        "labels": STATUS_LABELS,
    }


@router.get("/articles")
def articles(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    org: str = Query(default="all"),
):
    """Статьи выплат для подсказки: из плана БДДС и из факта 1С."""
    names: set[str] = set()
    bq = db.query(models.BudgetItem.article).filter(models.BudgetItem.direction == "out")
    eq = db.query(models.Expense.basis)
    o = (org or "").strip().lower()
    if o in models.ORGS:
        bq = bq.filter(models.BudgetItem.organization == o)
        eq = eq.filter(models.Expense.organization == o)
    names.update(a for (a,) in bq.distinct().all() if a)
    names.update(b for (b,) in eq.distinct().all() if b)
    return sorted(names)


@router.get("/counterparties")
def counterparties(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    q: str = Query(default="", alias="q"),
):
    """Подсказка контрагента из справочника 1С по части названия."""
    needle = _norm(q)
    if len(needle) < 2:
        return []
    rows = (db.query(models.Counterparty)
            .filter(models.Counterparty.deleted.is_(False),
                    models.Counterparty.is_group.is_(False))
            .all())
    hits = [c for c in rows if needle in _norm(c.name) or needle in _norm(c.name_full)]
    hits.sort(key=lambda c: (not _norm(c.name).startswith(needle), _norm(c.name)))
    return [{"guid": c.guid, "name": c.name, "inn": c.inn} for c in hits[:15]]


# ---------- настройки ----------

@router.get("/settings")
def get_settings(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    rows = db.query(models.ApprovalSettings).all()
    out = {r.organization: {
        "accountant_limit": float(r.accountant_limit),
        "cash_cushion": float(r.cash_cushion),
        "require_basis_file": r.require_basis_file,
    } for r in rows}
    out.setdefault("all", {"accountant_limit": 0.0, "cash_cushion": 0.0,
                           "require_basis_file": True})
    return out


@router.put("/settings")
def put_settings(
    payload: SettingsIn,
    db: Session = Depends(get_db),
    _: models.User = Depends(admin_only),
):
    org = payload.organization.strip().lower()
    if org != "all" and org not in models.ORGS:
        raise HTTPException(status_code=422, detail="Неизвестная фирма")
    row = db.query(models.ApprovalSettings).filter_by(organization=org).first()
    if row is None:
        row = models.ApprovalSettings(organization=org)
        db.add(row)
    row.accountant_limit = payload.accountant_limit
    row.cash_cushion = payload.cash_cushion
    row.require_basis_file = payload.require_basis_file
    db.commit()
    return get_settings(db, _)


# ---------- заявка ----------

@router.post("", response_model=RequestDetail, status_code=status.HTTP_201_CREATED)
def create_request(
    payload: RequestIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    data = payload.model_dump()
    do_submit = data.pop("submit")
    data["organization"] = _check_org(data["organization"])
    if data["method"] not in ("bank", "cash", "card"):
        raise HTTPException(status_code=422, detail="Способ оплаты: bank, cash или card")
    data["counterparty"] = data["counterparty"].strip()
    if not data["counterparty"]:
        raise HTTPException(status_code=422, detail="Укажите контрагента")
    req = models.PaymentRequest(**data, created_by=user.id, status=S.draft.value)
    db.add(req)
    db.flush()
    _event(db, req, user, "create", None)
    if do_submit:
        _submit(db, req, user)
    db.commit()
    db.refresh(req)
    return _serialize(db, req, user, detail=True)


@router.get("/{request_id}", response_model=RequestDetail)
def get_request(
    request_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    req = _get(db, request_id)
    if req.status == S.paid.value and _try_reconcile(db, req):
        db.commit()
    return _serialize(db, req, user, detail=True)


@router.patch("/{request_id}", response_model=RequestDetail)
def update_request(
    request_id: int,
    payload: RequestPatch,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    req = _get(db, request_id)
    _forbid(_can(req, user, db)["edit"], "Заявку уже нельзя редактировать")
    data = payload.model_dump(exclude_unset=True)
    if "organization" in data:
        data["organization"] = _check_org(data["organization"])
    if "method" in data and data["method"] not in ("bank", "cash", "card"):
        raise HTTPException(status_code=422, detail="Способ оплаты: bank, cash или card")
    changed = []
    for k, v in data.items():
        old = getattr(req, k)
        if isinstance(v, str):
            v = v.strip() or None if k != "counterparty" and k != "title" else v.strip()
        if str(old) != str(v):
            changed.append(k)
            setattr(req, k, v)
    if changed:
        _event(db, req, user, "edit", None, "изменено: " + ", ".join(changed))
    db.commit()
    db.refresh(req)
    return _serialize(db, req, user, detail=True)


def _submit(db: Session, req: models.PaymentRequest, user: models.User) -> None:
    conf = _settings_for(db, req.organization)
    if conf.require_basis_file and not req.files and req.method != "cash":
        # Зарплата и налоги идут без счёта — их отмечаем статьёй.
        art = (req.article or "").lower()
        if not any(w in art for w in ("зарплат", "налог", "взнос", "фот", "оплата труда")):
            raise HTTPException(
                status_code=422,
                detail="Приложите счёт, договор или другое основание платежа",
            )
    req.submitted_at = datetime.utcnow()
    if user.role in (models.Role.admin, models.Role.accountant):
        # Бухгалтер сам и проверяющий: его заявка сразу идёт на согласование.
        req.checked_by = user.id
        req.checked_at = datetime.utcnow()
        _event(db, req, user, "submit", S.approval.value)
        _tg(req, "Заявка на согласование", user, "Отправил: ")
    else:
        _event(db, req, user, "submit", S.review.value)
        _tg(req, "Новая заявка на проверку", user, "Инициатор: ")


@router.post("/{request_id}/submit", response_model=RequestDetail)
def submit_request(
    request_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    req = _get(db, request_id)
    _forbid(_can(req, user, db)["submit"], "Отправить можно только черновик")
    _submit(db, req, user)
    db.commit()
    db.refresh(req)
    return _serialize(db, req, user, detail=True)


@router.post("/{request_id}/check", response_model=RequestDetail)
def check_request(
    request_id: int,
    payload: Decision,
    db: Session = Depends(get_db),
    user: models.User = Depends(staff),
):
    req = _get(db, request_id)
    _forbid(_can(req, user, db)["check"], "Заявка не на проверке")
    req.checked_by = user.id
    req.checked_at = datetime.utcnow()
    _event(db, req, user, "check", S.approval.value, payload.comment)
    db.commit()
    db.refresh(req)
    _tg(req, "Заявка проверена, ждёт согласования", user, "Проверил: ")
    return _serialize(db, req, user, detail=True)


@router.post("/{request_id}/return", response_model=RequestDetail)
def return_request(
    request_id: int,
    payload: Decision,
    db: Session = Depends(get_db),
    user: models.User = Depends(staff),
):
    req = _get(db, request_id)
    _forbid(_can(req, user, db)["return"], "Вернуть можно заявку на проверке или согласовании")
    if not (payload.comment or "").strip():
        raise HTTPException(status_code=422, detail="Напишите, что нужно исправить")
    _event(db, req, user, "return", S.draft.value, payload.comment)
    db.commit()
    db.refresh(req)
    _tg(req, "Заявка возвращена на доработку", user, f"{payload.comment}\nВернул: ")
    return _serialize(db, req, user, detail=True)


@router.post("/{request_id}/approve", response_model=RequestDetail)
def approve_request(
    request_id: int,
    payload: Decision,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    req = _get(db, request_id)
    can = _can(req, user, db)
    if not can["approve"]:
        if req.status != S.approval.value:
            raise HTTPException(status_code=403, detail="Заявка не на согласовании")
        raise HTTPException(
            status_code=403,
            detail="Сумма выше вашего лимита или заявка сверх бюджета: утверждает руководитель",
        )
    req.approved_by = user.id
    req.decided_at = datetime.utcnow()
    req.decision_comment = (payload.comment or "").strip() or None
    _event(db, req, user, "approve", S.approved.value, payload.comment)
    db.commit()
    db.refresh(req)
    _tg(req, "Заявка утверждена, можно оплачивать", user, "Утвердил: ")
    return _serialize(db, req, user, detail=True)


@router.post("/{request_id}/reject", response_model=RequestDetail)
def reject_request(
    request_id: int,
    payload: Decision,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    req = _get(db, request_id)
    _forbid(_can(req, user, db)["reject"], "Отклонить эту заявку вы не можете")
    if not (payload.comment or "").strip():
        raise HTTPException(status_code=422, detail="Укажите причину отклонения")
    req.approved_by = user.id
    req.decided_at = datetime.utcnow()
    req.decision_comment = payload.comment.strip()
    _event(db, req, user, "reject", S.rejected.value, payload.comment)
    db.commit()
    db.refresh(req)
    _tg(req, "Заявка отклонена", user, f"{payload.comment}\nОтклонил: ")
    return _serialize(db, req, user, detail=True)


@router.post("/{request_id}/pay", response_model=RequestDetail)
def pay_request(
    request_id: int,
    payload: PayIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(staff),
):
    req = _get(db, request_id)
    _forbid(_can(req, user, db)["pay"], "Оплатить можно только утверждённую заявку")
    req.paid_by = user.id
    req.paid_at = datetime.utcnow()
    req.paid_date = payload.paid_date
    req.paid_amount = payload.paid_amount or req.amount
    req.paid_doc = (payload.paid_doc or "").strip() or None
    note = f"оплачено {float(req.paid_amount):,.2f} {req.currency} {payload.paid_date.isoformat()}"
    if req.paid_doc:
        note += f", документ {req.paid_doc}"
    if payload.comment:
        note += f". {payload.comment.strip()}"
    _event(db, req, user, "pay", S.paid.value, note)
    db.flush()
    _try_reconcile(db, req)
    db.commit()
    db.refresh(req)
    _tg(req, "Заявка оплачена", user, "Оплатил: ")
    return _serialize(db, req, user, detail=True)


@router.post("/{request_id}/cancel", response_model=RequestDetail)
def cancel_request(
    request_id: int,
    payload: Decision,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    req = _get(db, request_id)
    _forbid(_can(req, user, db)["cancel"], "Отменить эту заявку вы не можете")
    _event(db, req, user, "cancel", S.cancelled.value, payload.comment)
    db.commit()
    db.refresh(req)
    return _serialize(db, req, user, detail=True)


# ---------- вложения ----------

@router.post("/{request_id}/files", response_model=RequestDetail)
async def upload_file(
    request_id: int,
    file: UploadFile = File(...),
    kind: str = Query(default="basis"),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    req = _get(db, request_id)
    _forbid(_can(req, user, db)["upload"], "К этой заявке уже нельзя добавить файл")
    if kind not in ("basis", "payment"):
        raise HTTPException(status_code=422, detail="kind: basis или payment")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="Пустой файл")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="Файл больше 10 МБ")
    db.add(models.PaymentRequestFile(
        request_id=req.id, filename=file.filename or "файл",
        content_type=file.content_type or "application/octet-stream",
        size=len(data), kind=kind, data=data, uploaded_by=user.id,
    ))
    _event(db, req, user, "file", None,
           f"{'платёжка' if kind == 'payment' else 'основание'}: {file.filename}")
    db.commit()
    db.refresh(req)
    return _serialize(db, req, user, detail=True)


@router.get("/{request_id}/files/{file_id}")
def download_file(
    request_id: int,
    file_id: int,
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    f = db.get(models.PaymentRequestFile, file_id)
    if not f or f.request_id != request_id:
        raise HTTPException(status_code=404, detail="Файл не найден")
    from urllib.parse import quote
    return Response(
        content=f.data, media_type=f.content_type,
        headers={"Content-Disposition":
                 f"inline; filename*=UTF-8''{quote(f.filename)}"},
    )


@router.delete("/{request_id}/files/{file_id}", status_code=204)
def delete_file(
    request_id: int,
    file_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    req = _get(db, request_id)
    f = db.get(models.PaymentRequestFile, file_id)
    if not f or f.request_id != request_id:
        raise HTTPException(status_code=404, detail="Файл не найден")
    _forbid(_can(req, user, db)["edit"], "Файл уже нельзя удалить")
    _event(db, req, user, "file_delete", None, f.filename)
    db.delete(f)
    db.commit()
