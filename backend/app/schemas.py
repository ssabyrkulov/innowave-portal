from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from .models import Direction, Role, Status


# ---------- Auth ----------
class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ---------- Users ----------
class UserBase(BaseModel):
    email: EmailStr
    full_name: str
    role: Role = Role.viewer
    is_active: bool = True
    agent_name: str | None = None


class UserCreate(UserBase):
    password: str = Field(min_length=6)


class UserUpdate(BaseModel):
    full_name: str | None = None
    role: Role | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=6)
    agent_name: str | None = None


class UserOut(UserBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


# ---------- Payments ----------
class PaymentBase(BaseModel):
    title: str
    amount: Decimal = Field(gt=0)
    currency: str = "KGS"
    direction: Direction = Direction.outgoing
    status: Status = Status.planned
    due_date: date
    counterparty: str | None = None
    category: str | None = None
    note: str | None = None


class PaymentCreate(PaymentBase):
    pass


class PaymentUpdate(BaseModel):
    title: str | None = None
    amount: Decimal | None = Field(default=None, gt=0)
    currency: str | None = None
    direction: Direction | None = None
    status: Status | None = None
    due_date: date | None = None
    counterparty: str | None = None
    category: str | None = None
    note: str | None = None


class PaymentOut(PaymentBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_by: int
    creator_name: str | None = None
    created_at: datetime
    updated_at: datetime


class CalendarSummary(BaseModel):
    incoming_total: Decimal
    outgoing_total: Decimal
    balance: Decimal
    count: int
