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


# Организации группы. Данные 1С грузятся по фирмам (разные папки Drive) и
# нигде не смешиваются: разрез по organization есть во всех выгружаемых
# таблицах. hygiene — Innowave Hygiene (единый налог), innowave — Innowave
# (общий налог).
DEFAULT_ORG = "hygiene"
ORGS = ("hygiene", "innowave")

# Плейсхолдер номенклатуры для документных реализаций (выгрузка без разбивки
# по товарам — только Дата/Сумма/Контрагент, как у Innowave). По нему такие
# продажи отличаются от построчных и исключаются из товарных проверок.
DOC_SALE_PRODUCT = "Реализация (документ)"


def normalize_org(value: str | None) -> str:
    v = (value or "").strip().lower()
    return v if v in ORGS else DEFAULT_ORG


def org_scope(query, model, org: str | None):
    """Фильтрует запрос по организации. 'all'/пусто → обе фирмы вместе."""
    o = (org or "").strip().lower()
    if o in ORGS:
        return query.filter(model.organization == o)
    return query


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
    # Если задано — пользователь является торговым агентом (значение совпадает
    # с именем агента в продажах); он видит только своих клиентов в «Мой день».
    agent_name: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    payments: Mapped[list["Payment"]] = relationship(
        back_populates="creator", cascade="all, delete-orphan"
    )


class Sale(Base):
    """Строка реализации товаров из 1С (импорт из Excel)."""

    __tablename__ = "sales"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organization: Mapped[str] = mapped_column(String, default=DEFAULT_ORG, nullable=False, index=True)
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
    # GUID документа 1С. SalesDoc отдаёт его же в поле code_1C, поэтому
    # связка документов по нему точная — в отличие от поиска по сумме и
    # дате с допуском, который рвётся от любой правки документа.
    doc_guid: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # Хэш строки для идемпотентного импорта: повторная загрузка того же
    # файла не создаёт дублей.
    row_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Purchase(Base):
    """Строка поступления товаров из 1С (закупка у поставщика, ВыгрузкаПост).

    Цена и итог документа — в валюте закупки (у импортных поставок USD),
    сумма строки 1С отдаёт уже пересчитанной в сомы."""

    __tablename__ = "purchases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organization: Mapped[str] = mapped_column(String, default=DEFAULT_ORG, nullable=False, index=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    supplier: Mapped[str] = mapped_column(String, nullable=False, index=True)
    warehouse: Mapped[str | None] = mapped_column(String, nullable=True)
    product: Mapped[str | None] = mapped_column(String, nullable=True)
    qty: Mapped[float | None] = mapped_column(Numeric(14, 3), nullable=True)
    price: Mapped[float | None] = mapped_column(Numeric(14, 4), nullable=True)
    amount_kgs: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="KGS", nullable=False)
    doc_number: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    doc_total: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    unit: Mapped[str | None] = mapped_column(String, nullable=True)
    account: Mapped[str | None] = mapped_column(String, nullable=True)
    # GUID документа 1С — тот же идентификатор, который SalesDoc отдаёт в
    # code_1C. Он делает связку документов точной: раньше приходилось искать
    # по сумме и дате с допуском, а правка суммы или даты рвала совпадение.
    doc_guid: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WriteOff(Base):
    """Строка списания товаров из 1С (ВыгрузкаСпис).

    Сумму 1С в этой выгрузке не отдаёт — только количество, поэтому списания
    участвуют в товарном балансе (остатки), но не в деньгах. Зато отдаёт счёт
    затрат, субконто и комментарий: по ним видно, куда ушёл товар — торговому
    агенту, на маркетинг, в брак."""

    __tablename__ = "writeoffs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organization: Mapped[str] = mapped_column(String, default=DEFAULT_ORG, nullable=False, index=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    doc_number: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    doc_guid: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    warehouse: Mapped[str | None] = mapped_column(String, nullable=True)
    product: Mapped[str | None] = mapped_column(String, nullable=True)
    qty: Mapped[float | None] = mapped_column(Numeric(14, 3), nullable=True)
    unit: Mapped[str | None] = mapped_column(String, nullable=True)
    account: Mapped[str | None] = mapped_column(String, nullable=True)
    cost_account: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    subconto: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    comment: Mapped[str | None] = mapped_column(String, nullable=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Receipt(Base):
    """Поступление денежных средств из 1С (оплаты клиентов и прочее)."""

    __tablename__ = "receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organization: Mapped[str] = mapped_column(String, default=DEFAULT_ORG, nullable=False, index=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="KGS", nullable=False)
    # Курс к сому и сумма в сомах; для KGS курс = 1.
    rate: Mapped[float] = mapped_column(Numeric(12, 4), default=1, nullable=False)
    amount_kgs: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    payer: Mapped[str] = mapped_column(String, nullable=False, index=True)
    operation: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # Источник денег: банк (ВыгрузкаБанкВх) или касса (ВыгрузкаПКО). Старые
    # записи без вида считаем банком (историю до разделения не пере-размечаем).
    kind: Mapped[str | None] = mapped_column(String, default="bank", nullable=True)  # bank|cash
    # GUID документа 1С. SalesDoc отдаёт его же в поле code_1C, поэтому
    # связка документов по нему точная — в отличие от поиска по сумме и
    # дате с допуском, который рвётся от любой правки документа.
    doc_guid: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    row_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ReturnLine(Base):
    """Товарная строка возврата от покупателя (из построчного ТовВозв).

    ReturnDoc хранит возвраты суммами по клиентам — для дебиторки этого
    достаточно, но для расчётного остатка нужен товар и количество: возврат
    возвращает товар на склад."""

    __tablename__ = "return_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organization: Mapped[str] = mapped_column(String, default=DEFAULT_ORG, nullable=False, index=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    client: Mapped[str | None] = mapped_column(String, nullable=True)
    product: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    qty: Mapped[float | None] = mapped_column(Numeric(14, 3), nullable=True)
    amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    # GUID документа 1С. SalesDoc отдаёт его же в поле code_1C, поэтому
    # связка документов по нему точная — в отличие от поиска по сумме и
    # дате с допуском, который рвётся от любой правки документа.
    doc_guid: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Expense(Base):
    """Расход денежных средств: исходящие платёжки (банк) и РКО (касса)."""

    __tablename__ = "expenses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organization: Mapped[str] = mapped_column(String, default=DEFAULT_ORG, nullable=False, index=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="KGS", nullable=False)
    rate: Mapped[float] = mapped_column(Numeric(12, 4), default=1, nullable=False)
    amount_kgs: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    counterparty: Mapped[str] = mapped_column(String, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String, default="bank", nullable=False)  # bank|cash
    basis: Mapped[str | None] = mapped_column(String, nullable=True)  # Основание / ВидОперации
    doc_number: Mapped[str | None] = mapped_column(String, nullable=True)
    # GUID документа 1С — есть в новом формате выгрузок. Точный ключ сверки
    # вместо совпадения по дате и сумме.
    doc_guid: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    row_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ReturnDoc(Base):
    """Возврат товаров от покупателя (уровень документа) из 1С."""

    __tablename__ = "return_docs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organization: Mapped[str] = mapped_column(String, default=DEFAULT_ORG, nullable=False, index=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="KGS", nullable=False)
    client: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # GUID документа 1С. SalesDoc отдаёт его же в поле code_1C, поэтому
    # связка документов по нему точная — в отличие от поиска по сумме и
    # дате с допуском, который рвётся от любой правки документа.
    doc_guid: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    row_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CashBalance(Base):
    """Снапшот остатков денег по кассам/счетам (ВыгрузкаБанкКасса)."""

    __tablename__ = "cash_balances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organization: Mapped[str] = mapped_column(String, default=DEFAULT_ORG, nullable=False, index=True)
    account: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class StockBalance(Base):
    """Снапшот остатков товаров по складам (ВыгрузкаОст)."""

    __tablename__ = "stock_balances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organization: Mapped[str] = mapped_column(String, default=DEFAULT_ORG, nullable=False, index=True)
    product: Mapped[str] = mapped_column(String, nullable=False)
    # GUID номенклатуры 1С (из плоской выгрузки нашей обработки). SalesDoc
    # отдаёт его же в getProduct.code_1C — точный ключ вместо сопоставления по
    # названию, которое рвётся от переименования позиции.
    product_guid: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    warehouse: Mapped[str | None] = mapped_column(String, nullable=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    qty: Mapped[float] = mapped_column(Numeric(14, 3), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ClientAlias(Base):
    """Ручное сопоставление: имя плательщика → клиент из продаж."""

    __tablename__ = "client_aliases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    payer: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    client: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AgentTarget(Base):
    """План продаж агента на месяц (YYYY-MM)."""

    __tablename__ = "agent_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    agent: Mapped[str] = mapped_column(String, nullable=False, index=True)
    month: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)

    __table_args__ = ()


class BudgetItem(Base):
    """План БДДС: сумма по статье движения денег на месяц (план-факт).

    direction: 'in' — поступление, 'out' — выплата.
    article — статья ДДС (совпадает со статьёй в факте: Receipt.operation /
    Expense.basis), по ней и сводится план с фактом.
    """

    __tablename__ = "budget_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organization: Mapped[str] = mapped_column(String, default="hygiene", nullable=False)
    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)  # YYYY-MM
    direction: Mapped[str] = mapped_column(String(3), nullable=False)  # in|out
    article: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    note: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ClientActivity(Base):
    """Действие агента по клиенту: визит, звонок, обещание оплаты, коммент.

    Превращает «Агентов» из отчёта в рабочий инструмент — агент фиксирует, что
    сделал в полях, а обещания оплаты всплывают в срок.
    """

    __tablename__ = "client_activities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    agent: Mapped[str] = mapped_column(String, nullable=False, index=True)
    client: Mapped[str] = mapped_column(String, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)  # visit|call|promise|order|note
    note: Mapped[str | None] = mapped_column(String, nullable=True)
    promise_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    creator: Mapped["User"] = relationship()


class BadDebtClient(Base):
    """Помеченный контрагент в дебиторке. kind задаёт категорию:
    'bad_debt' — безнадёжный (к взысканию не рассчитываем),
    'disputed' — под вопросом (была оплата или нет — неясно, агенты сменились).

    Помеченные убираются из активной дебиторки в свою вкладку — чтобы не
    искажать «живой» долг и не тревожить агентов пустой работой. Клиент может
    быть максимум в одной категории (client уникален).
    """

    __tablename__ = "bad_debt_clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    client: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String, default="bad_debt", nullable=False, index=True)
    note: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    creator: Mapped["User"] = relationship()


class SalesDocStore(Base):
    """Привязка склада SalesDoc к организации портала.

    В SalesDoc одна база на обе фирмы; склады (2+2) относятся к разным
    организациям. По этой карте реализации из SalesDoc делятся по фирмам.
    """

    __tablename__ = "salesdoc_stores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    store_id: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)  # SD_id
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    organization: Mapped[str | None] = mapped_column(String, nullable=True)  # hygiene|innowave|None
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SalesDocOrder(Base):
    """Зеркало заказа (реализации) SalesDoc.

    Серверный фильтр по клиенту SalesDoc не соблюдает — на один клиент он всё
    равно отдаёт весь журнал (проверено: 1173 записи и с фильтром, и без).
    Поэтому держим копию журнала у себя: карточка клиента и сверка читаются
    обычным SQL — мгновенно и без обращения к SalesDoc.
    """

    __tablename__ = "salesdoc_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    sd_id: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    client_sd_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    client_code_1c: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    store_sd_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    returns_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    code_1c: Mapped[str | None] = mapped_column(String, nullable=True)
    # Агент заказа. Именно он показывает, кто сейчас ведёт точку: getOrder не
    # отдаёт заказы деактивированных агентов, поэтому всё, что здесь лежит, —
    # работа действующих сотрудников.
    agent_sd_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    agent_name: Mapped[str | None] = mapped_column(String, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SalesDocPayment(Base):
    """Зеркало операции журнала оплат SalesDoc.

    Тут же живут и возвраты: в SalesDoc возврат товара — это операция
    «Возврат с полки» (transactionType=9), а не отдельный документ
    (getOrderDefect за 3 года пуст). Поэтому храним журнал целиком и
    разделяем по виду операции при чтении.
    """

    __tablename__ = "salesdoc_payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    sd_id: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    client_sd_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    client_code_1c: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    txn: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    type_name: Mapped[str | None] = mapped_column(String, nullable=True)
    # Касса, на которую посажена операция. У оплат склада нет, поэтому фирму
    # такой точки по складам не определить — касса единственная зацепка.
    # Идентификаторы способа оплаты. Название хранить мало: фирму планируется
    # зашивать в способ («Bank Innowave (KGS)»), а названия переименовывают —
    # отбор должен идти по идентификатору, который не меняется.
    type_sd_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    type_code_1c: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    cashbox_sd_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    cashbox_name: Mapped[str | None] = mapped_column(String, nullable=True)
    # Заказы, которые закрывает эта оплата (поле orders в ответе SalesDoc),
    # через запятую. Единственный признак фирмы у оплаты: сама она склада не
    # знает, но заказ знает — а у заказа склад есть.
    order_ids: Mapped[str | None] = mapped_column(String, nullable=True)
    trade_sd_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # GUID документа 1С на стороне SalesDoc — вторая половина связки по
    # идентификатору (у заказов это code_1c).
    code_1c: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SalesDocOrderChange(Base):
    """Замеченная смена склада (или статуса) у документа SalesDoc.

    В самом SalesDoc история изменений склад не пишет: в журнале документа
    остаётся первый склад, а последующие правки нигде не сохраняются — понять,
    когда и на что склад поменяли, невозможно. Раз зеркало раз в час
    перечитывает журнал целиком, мы такие правки видим и записываем: это
    единственное место, где эта история вообще существует.
    """

    __tablename__ = "salesdoc_order_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    order_sd_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    field: Mapped[str] = mapped_column(String, nullable=False)  # store|status
    old_value: Mapped[str | None] = mapped_column(String, nullable=True)
    new_value: Mapped[str | None] = mapped_column(String, nullable=True)
    doc_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    client_sd_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    noticed_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True)


class SalesDocClient(Base):
    """Зеркало точки SalesDoc: справочник + текущий долг.

    Чтобы список сверки читался только из нашей базы и открывался мгновенно —
    без похода в SalesDoc на каждое открытие раздела.
    """

    __tablename__ = "salesdoc_clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    sd_id: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    code_1c: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String, default="")
    debt: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    # Был ли клиент в ответе getBalance. Нолём долг может быть по двум разным
    # причинам: SalesDoc сообщил ноль или не упомянул точку вовсе — и это
    # надо различать, иначе «баланс 0» читается как факт, а не как заглушка.
    in_balance: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SalesDocStock(Base):
    """Зеркало остатков SalesDoc: сколько штук каждой позиции на каждом складе.

    SalesDoc отдаёт только количество — цен и сумм в остатках у него нет.
    Ключ строки — «склад:товар» (sd_id), чтобы одна позиция на разных складах
    хранилась отдельно.
    """

    __tablename__ = "salesdoc_stock"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    sd_id: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    store_sd_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    store_name: Mapped[str | None] = mapped_column(String, nullable=True)
    product_sd_id: Mapped[str | None] = mapped_column(String, nullable=True)
    product_name: Mapped[str] = mapped_column(String, default="", index=True)
    code_1c: Mapped[str | None] = mapped_column(String, nullable=True)
    quantity: Mapped[float] = mapped_column(Numeric(14, 3), default=0)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SalesDocVisit(Base):
    """Зеркало визита агента SalesDoc (getVisit).

    Визит — ядро работы с дебиторкой: по нему видно, когда точку последний раз
    посещали, когда запланирован следующий визит (planned=1, visited=0 с
    будущей датой) и чем визит закончился (has_order). Собственного ИД у
    визита в API нет — ключ синтетический: агент + точка + время."""

    __tablename__ = "salesdoc_visits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    key: Mapped[str] = mapped_column(String(40), unique=True, nullable=False, index=True)
    agent_sd_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    agent_name: Mapped[str | None] = mapped_column(String, nullable=True)
    client_sd_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    client_name: Mapped[str | None] = mapped_column(String, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    planned: Mapped[bool] = mapped_column(Boolean, default=False)
    visited: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    reject: Mapped[str | None] = mapped_column(String, nullable=True)
    has_order: Mapped[bool] = mapped_column(Boolean, default=False)
    order_summa: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SalesDocClientAgent(Base):
    """Закрепление точки за агентом (getClient.agents) с днями маршрута.

    Это настоящее «за кем точка», в отличие от агента в заказе: тот говорит
    лишь, кто выписал конкретный документ. days — дни недели визита по
    маршруту (1 = понедельник); их SalesDoc хранит прямо в карточке точки."""

    __tablename__ = "salesdoc_client_agents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    client_sd_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    agent_sd_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    days: Mapped[str | None] = mapped_column(String, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SalesDocStoreLog(Base):
    """Строка журнала движений склада SalesDoc (getStoreLog).

    Единственное место, где видны списания: отдельного метода для их чтения в
    API нет (setExcretion умеет записать, getExcretion не существует). Здесь же
    лежат поступления, обмены, корректировки и возвраты — то есть весь товарный
    оборот построчно, с количеством и знаком: плюс приход, минус расход.

    Своего идентификатора у строки нет, ключ синтетический: склад + документ +
    товар + время."""

    __tablename__ = "salesdoc_store_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    key: Mapped[str] = mapped_column(String(48), unique=True, nullable=False, index=True)
    store_sd_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    store_name: Mapped[str | None] = mapped_column(String, nullable=True)
    document: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    document_sd_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    product_sd_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    product_name: Mapped[str | None] = mapped_column(String, nullable=True)
    quantity: Mapped[float] = mapped_column(Numeric(14, 3), default=0)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SalesDocProduct(Base):
    """Справочник номенклатуры SalesDoc (getProduct).

    Нужен по двум причинам. Первая: строки перемещений и остатков приходят
    только с идентификатором товара — без справочника это набор кодов вида
    d0_15 вместо названий. Вторая важнее: code_1C здесь — GUID номенклатуры
    1С, то есть точный ключ вместо нынешнего сопоставления по нормализованному
    названию, где «Подгузники StarKid размер L*4» и «Детские подгузники
    StarKid размер L» склеиваются лишь потому, что мы срезаем фасовку."""

    __tablename__ = "salesdoc_products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    sd_id: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    code_1c: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String, default="", index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    unit: Mapped[str | None] = mapped_column(String, nullable=True)
    barcode: Mapped[str | None] = mapped_column(String, nullable=True)
    pack_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    weight: Mapped[float | None] = mapped_column(Numeric(12, 3), nullable=True)
    volume: Mapped[float | None] = mapped_column(Numeric(12, 3), nullable=True)
    category_sd_id: Mapped[str | None] = mapped_column(String, nullable=True)
    group_sd_id: Mapped[str | None] = mapped_column(String, nullable=True)
    brand_sd_id: Mapped[str | None] = mapped_column(String, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SalesDocMovement(Base):
    """Перемещение товара между складами SalesDoc (getMovement).

    Списанием оно не является — у документа два склада, «откуда» и «куда», а
    статьи затрат нет. Для товарного баланса это важно: перемещение не меняет
    общий остаток, но меняет остаток каждого склада, и без него расклад по
    складам не сходится. Сумм в документе нет, только количество."""

    __tablename__ = "salesdoc_movements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    sd_id: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    from_store_sd_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    from_store_name: Mapped[str | None] = mapped_column(String, nullable=True)
    to_store_sd_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    to_store_name: Mapped[str | None] = mapped_column(String, nullable=True)
    qty: Mapped[float] = mapped_column(Numeric(14, 3), default=0)
    positions: Mapped[int] = mapped_column(Integer, default=0)
    code_1c: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SalesDocMovementLine(Base):
    """Товарная строка перемещения: что именно и сколько переехало."""

    __tablename__ = "salesdoc_movement_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    movement_sd_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    product_sd_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    product_code_1c: Mapped[str | None] = mapped_column(String, nullable=True)
    product_name: Mapped[str | None] = mapped_column(String, nullable=True)
    qty: Mapped[float] = mapped_column(Numeric(14, 3), default=0)


class SalesDocAgent(Base):
    """Справочник агентов SalesDoc с признаком «работает сейчас».

    Признак active — не украшение: деактивированного агента SalesDoc вычёркивает
    из выгрузки вместе со всей его историей продаж, а его точки остаются без
    хозяина. В дебиторке колонка «агент» без этого признака бесполезна:
    непонятно, кому ставить задачу по долгу."""

    __tablename__ = "salesdoc_agents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    sd_id: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SalesDocSyncState(Base):
    """Состояние синхронизации зеркала: когда последний раз обновляли и как.

    kind — 'orders' | 'payments'. Хранит момент последней полной выгрузки и
    последней догрузки изменений, чтобы дельту брать с нужной точки.
    """

    __tablename__ = "salesdoc_sync_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    kind: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    last_full_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_delta_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rows: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)


class SalesDocDiffSeen(Base):
    """Когда расхождение по точке впервые появилось в списке сверки.

    Ни 1С, ни SalesDoc момента «разъехалось» не знают — его видит только сам
    список. Запоминаем первое появление, чтобы сортировать сверку по свежести:
    новые проблемы сверху, застарелые внизу. Когда расхождение уходит, запись
    удаляется — повторное появление считается новым событием.
    """

    __tablename__ = "salesdoc_diff_seen"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # «org:ключ точки» — по фирмам списки разные, событие тоже своё у каждой.
    key: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SalesDocClientFirm(Base):
    """Фирма точки SalesDoc, заданная вручную.

    Обычно фирму точки портал выводит по складам её реализаций. Но SalesDoc
    отдаёт через API не все документы (см. docs/SD_НЕВИДИМЫЕ_ДОКУМЕНТЫ.md): у
    части точек заказов не видно вовсе, и тогда фирму вычислить нечем — такие
    строки показывались в обеих фирмах сразу (вся клиентура Инновейв лезла в
    список Хайджин). Ручная привязка перекрывает догадку и работает независимо
    от того, починит ли SalesDoc выдачу.
    """

    __tablename__ = "salesdoc_client_firms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    sd_id: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    organization: Mapped[str] = mapped_column(String, nullable=False)
    set_by: Mapped[str | None] = mapped_column(String, nullable=True)
    set_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SalesDocClientLink(Base):
    """Ручная связка контрагента 1С с клиентом SalesDoc (по SD_id).

    Когда имена в системах не совпадают (латиница/кириллица, сокращения),
    пользователь связывает точки вручную — сверка честно склеивает их.
    """

    __tablename__ = "salesdoc_client_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    client_1c: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    sd_id: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TaxOperation(Base):
    """Операция налогового контура (черновик, отдельно от управленки).

    Налоговая база (1С ред. 1.7) выгружается Эрмеком отдельными файлами.
    Смешивать их с управленческими таблицами нельзя — задвоятся деньги и
    продажи, поэтому весь налоговый контур живёт в одной своей таблице.
    kind: sale (строка реализации) | return (возврат) | cash_in (ПКО) |
    cash_out (РКО)."""

    __tablename__ = "tax_operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organization: Mapped[str] = mapped_column(String, default=DEFAULT_ORG, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String, nullable=False, index=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    counterparty: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="KGS", nullable=False)
    doc_number: Mapped[str | None] = mapped_column(String, nullable=True)
    doc_total: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    warehouse: Mapped[str | None] = mapped_column(String, nullable=True)
    product: Mapped[str | None] = mapped_column(String, nullable=True)
    qty: Mapped[float | None] = mapped_column(Numeric(14, 3), nullable=True)
    # Вид операции кассы («Выдача подотчётнику», «Оплата от покупателя»…) —
    # по нему деньги раскладываются на подотчёт/зарплату/инкассацию/клиентов.
    operation: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # Из какого файла приехала строка («Приходный кассовый ордер», «ПП
    # входящее», …). В старом пакете на каждый вид приходился ровно один файл,
    # и замена шла по виду. В новом на один вид их несколько — ПКО, ПП
    # входящее и платёжный ордер поступления все дают cash_in, — и замена по
    # виду стирала бы предыдущий файл. Поэтому снапшот заменяется по паре
    # «вид + источник».
    source: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # GUID документа 1С: в новом формате есть у всех документов, и это точный
    # ключ сверки налоговой с управленкой вместо совпадения по дате и сумме.
    doc_guid: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # Счёт учёта строки. Без него в остатках товаров оказывались бензин,
    # дизтопливо и офисная мебель: в налоговой базе они приходуются тем же
    # документом «Поступление товары», что и подгузники, и по одному
    # количеству их не отличить.
    account: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TaxClientLink(Base):
    """Связка контрагентов налоговой базы и управленки — парами, без
    ограничения направления.

    Дробление бывает в обе стороны: Байго Трейд в налоговой проведён шестью
    юрлицами (много НАЛ → один УПР), а Императив — одно налоговое юрлицо, за
    которым в управленке несколько точек Алдей (один НАЛ → много УПР). Поэтому
    храним пары: у налогового имени может быть несколько управленческих и
    наоборот. Сводки склеивают обороты, сверка ищет пару среди связанных.
    """

    __tablename__ = "tax_client_pairs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    tax_name: Mapped[str] = mapped_column(String, nullable=False, index=True)
    upr_name: Mapped[str] = mapped_column(String, nullable=False, index=True)
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
    # SHA-256 содержимого файла — автоприём не обрабатывает один файл дважды
    file_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
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
