import enum
from datetime import datetime, date

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Role(str, enum.Enum):
    admin = "admin"
    accountant = "accountant"
    viewer = "viewer"


class Direction(str, enum.Enum):
    incoming = "incoming"
    outgoing = "outgoing"


class Status(str, enum.Enum):
    planned = "planned"
    paid = "paid"
    overdue = "overdue"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    email: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.viewer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    payments: Mapped[list["Payment"]] = relationship(
        back_populates="creator", cascade="all, delete-orphan"
    )


class Sale(Base):
    """Строка реализации товаров из 1С (импорт из Excel)."""

    __tablename__ = "sales"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    client: Mapped[str] = mapped_column(String, nullable=False, index=True)
    warehouse: Mapped[str | None] = mapped_column(String, nullable=True)
    product: Mapped[str] = mapped_column(String, nullable=False)
    qty: Mapped[float] = mapped_column(Numeric(14, 3), nullable=False)
    price: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="KGS", nullable=False)
    doc_number: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    doc_total: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    unit: Mapped[str | None] = mapped_column(String, nullable=True)
    agent: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    discount_pct: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    account: Mapped[str | None] = mapped_column(String, nullable=True)
    responsible: Mapped[str | None] = mapped_column(String, nullable=True)
    # Хэш строки для идемпотентного импорта: повторная загрузка того же
    # файла не создаёт дублей.
    row_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Receipt(Base):
    """Поступление денежных средств из 1С (оплаты клиентов и прочее)."""

    __tablename__ = "receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="KGS", nullable=False)
    # Курс к сому и сумма в сомах; для KGS курс = 1.
    rate: Mapped[float] = mapped_column(Numeric(12, 4), default=1, nullable=False)
    amount_kgs: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    payer: Mapped[str] = mapped_column(String, nullable=False, index=True)
    operation: Mapped[str] = mapped_column(String, nullable=False, index=True)
    row_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ClientAlias(Base):
    """Ручное сопоставление: имя плательщика → клиент из продаж."""

    __tablename__ = "client_aliases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    payer: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    client: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ImportLog(Base):
    """Журнал загрузок Excel — кто, когда и что импортировал."""

    __tablename__ = "import_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    added: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    errors_count: Mapped[int] = mapped_column(Integer, default=0)
    replace_period: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship()


class ViolationAck(Base):
    """Принятые (закрытые) нарушения контроля."""

    __tablename__ = "violation_acks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    vhash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship()


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="KGS", nullable=False)
    direction: Mapped[Direction] = mapped_column(
        Enum(Direction), default=Direction.outgoing, nullable=False
    )
    status: Mapped[Status] = mapped_column(
        Enum(Status), default=Status.planned, nullable=False
    )
    due_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    counterparty: Mapped[str | None] = mapped_column(String, nullable=True)
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    note: Mapped[str | None] = mapped_column(String, nullable=True)

    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    creator: Mapped["User"] = relationship(back_populates="payments")
