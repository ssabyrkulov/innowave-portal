from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import get_current_user, require_roles

router = APIRouter(prefix="/payments", tags=["payments"])

# Admin and accountant may modify payments; viewer is read-only.
can_edit = require_roles(models.Role.admin, models.Role.accountant)


def _serialize(payment: models.Payment) -> schemas.PaymentOut:
    out = schemas.PaymentOut.model_validate(payment)
    out.creator_name = payment.creator.full_name if payment.creator else None
    return out


@router.get("", response_model=list[schemas.PaymentOut])
def list_payments(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    status_filter: models.Status | None = Query(default=None, alias="status"),
    direction: models.Direction | None = Query(default=None),
):
    query = db.query(models.Payment)
    if date_from:
        query = query.filter(models.Payment.due_date >= date_from)
    if date_to:
        query = query.filter(models.Payment.due_date <= date_to)
    if status_filter:
        query = query.filter(models.Payment.status == status_filter)
    if direction:
        query = query.filter(models.Payment.direction == direction)
    payments = query.order_by(models.Payment.due_date.asc()).all()
    return [_serialize(p) for p in payments]


@router.get("/summary", response_model=schemas.CalendarSummary)
def summary(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
):
    query = db.query(models.Payment)
    if date_from:
        query = query.filter(models.Payment.due_date >= date_from)
    if date_to:
        query = query.filter(models.Payment.due_date <= date_to)
    payments = query.all()
    incoming = sum(
        (p.amount for p in payments if p.direction == models.Direction.incoming), 0
    )
    outgoing = sum(
        (p.amount for p in payments if p.direction == models.Direction.outgoing), 0
    )
    return schemas.CalendarSummary(
        incoming_total=incoming,
        outgoing_total=outgoing,
        balance=incoming - outgoing,
        count=len(payments),
    )


@router.post("", response_model=schemas.PaymentOut, status_code=status.HTTP_201_CREATED)
def create_payment(
    payload: schemas.PaymentCreate,
    db: Session = Depends(get_db),
    current: models.User = Depends(can_edit),
):
    payment = models.Payment(**payload.model_dump(), created_by=current.id)
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return _serialize(payment)


@router.patch("/{payment_id}", response_model=schemas.PaymentOut)
def update_payment(
    payment_id: int,
    payload: schemas.PaymentUpdate,
    db: Session = Depends(get_db),
    current: models.User = Depends(can_edit),
):
    payment = db.get(models.Payment, payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(payment, key, value)
    db.commit()
    db.refresh(payment)
    return _serialize(payment)


@router.delete("/{payment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_payment(
    payment_id: int,
    db: Session = Depends(get_db),
    current: models.User = Depends(can_edit),
):
    payment = db.get(models.Payment, payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    db.delete(payment)
    db.commit()
