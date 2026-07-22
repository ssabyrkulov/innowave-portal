"""Единый браузер операций 1С: реализации, поступления (банк/касса),
возвраты и исходящие платежи (банк/касса) в одном месте — с фильтрами по
периоду и тексту, постраничной выдачей и итоговой суммой по выборке.

Одна ручка GET /operations?type=… отдаёт нормализованную форму
{columns, rows, total, total_amount}, поэтому фронт рисует любую вкладку
одним и тем же компонентом таблицы.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import get_current_user

router = APIRouter(prefix="/operations", tags=["operations"])

# Тип колонки управляет отрисовкой на фронте:
#   date — дд.мм.гггг, money — сумма (выравнивание вправо), num — число, text.
Col = dict


def _c(key: str, label: str, kind: str = "text") -> Col:
    return {"key": key, "label": label, "type": kind}


# ---------------------------------------------------------------------------
# Описание каждого типа операций. Все объявлены единообразно, чтобы ручка и
# фронт работали по одной схеме.
#   model       — таблица SQLAlchemy
#   date_col    — колонка даты (фильтр периода + сортировка)
#   text_cols   — где искать подстроку q
#   amount_col  — сумма для итога по выборке (в сомах, где есть пересчёт)
#   base        — доп. фильтр (напр. вид «банк/касса»)
#   columns     — колонки для таблицы
#   row         — сериализация записи в словарь по ключам колонок
# ---------------------------------------------------------------------------
def _sales_row(s: models.Sale) -> dict:
    return {
        "date": s.date.isoformat(),
        "doc_number": s.doc_number,
        "client": s.client,
        "product": s.product,
        "qty": float(s.qty),
        "price": float(s.price),
        "amount": float(s.amount),
        "warehouse": s.warehouse,
        "agent": s.agent or s.responsible,
    }


def _receipt_row(r: models.Receipt) -> dict:
    return {
        "date": r.date.isoformat(),
        "payer": r.payer,
        "operation": r.operation,
        "amount": float(r.amount),
        "currency": r.currency,
        "amount_kgs": float(r.amount_kgs),
    }


def _return_row(r: models.ReturnDoc) -> dict:
    return {
        "date": r.date.isoformat(),
        "client": r.client,
        "amount": float(r.amount),
        "currency": r.currency,
    }


def _expense_row(e: models.Expense) -> dict:
    return {
        "date": e.date.isoformat(),
        "counterparty": e.counterparty,
        "basis": e.basis,
        "doc_number": e.doc_number,
        "amount": float(e.amount),
        "currency": e.currency,
        "amount_kgs": float(e.amount_kgs),
    }


TYPES: dict[str, dict] = {
    "sales": {
        "label": "Реализации",
        "model": models.Sale,
        "date_col": lambda M: M.date,
        "text_cols": lambda M: [M.client, M.product, M.doc_number],
        "amount_col": lambda M: M.amount,
        "base": None,
        "columns": [
            _c("date", "Дата", "date"),
            _c("doc_number", "Документ"),
            _c("client", "Клиент"),
            _c("product", "Номенклатура"),
            _c("qty", "Кол-во", "num"),
            _c("price", "Цена", "money"),
            _c("amount", "Сумма", "money"),
            _c("warehouse", "Склад"),
            _c("agent", "Агент"),
        ],
        "row": _sales_row,
    },
    "receipt_bank": {
        "label": "Оплаты · банк",
        "model": models.Receipt,
        "date_col": lambda M: M.date,
        "text_cols": lambda M: [M.payer, M.operation],
        "amount_col": lambda M: M.amount_kgs,
        # Старые записи без вида (NULL) считаем банковскими.
        "base": lambda M: or_(M.kind == "bank", M.kind.is_(None)),
        "columns": [
            _c("date", "Дата", "date"),
            _c("payer", "Плательщик"),
            _c("operation", "Операция"),
            _c("amount", "Сумма", "money"),
            _c("currency", "Валюта"),
            _c("amount_kgs", "В сомах", "money"),
        ],
        "row": _receipt_row,
    },
    "receipt_cash": {
        "label": "Оплаты · касса",
        "model": models.Receipt,
        "date_col": lambda M: M.date,
        "text_cols": lambda M: [M.payer, M.operation],
        "amount_col": lambda M: M.amount_kgs,
        "base": lambda M: M.kind == "cash",
        "columns": [
            _c("date", "Дата", "date"),
            _c("payer", "Плательщик"),
            _c("operation", "Операция"),
            _c("amount", "Сумма", "money"),
            _c("currency", "Валюта"),
            _c("amount_kgs", "В сомах", "money"),
        ],
        "row": _receipt_row,
    },
    "returns": {
        "label": "Возвраты",
        "model": models.ReturnDoc,
        "date_col": lambda M: M.date,
        "text_cols": lambda M: [M.client],
        "amount_col": lambda M: M.amount,
        "base": None,
        "columns": [
            _c("date", "Дата", "date"),
            _c("client", "Клиент"),
            _c("amount", "Сумма", "money"),
            _c("currency", "Валюта"),
        ],
        "row": _return_row,
    },
    "expense_bank": {
        "label": "Исходящие · банк",
        "model": models.Expense,
        "date_col": lambda M: M.date,
        "text_cols": lambda M: [M.counterparty, M.basis],
        "amount_col": lambda M: M.amount_kgs,
        "base": lambda M: M.kind == "bank",
        "columns": [
            _c("date", "Дата", "date"),
            _c("counterparty", "Контрагент"),
            _c("basis", "Основание"),
            _c("doc_number", "№"),
            _c("amount", "Сумма", "money"),
            _c("currency", "Валюта"),
            _c("amount_kgs", "В сомах", "money"),
        ],
        "row": _expense_row,
    },
    "expense_cash": {
        "label": "Исходящие · касса",
        "model": models.Expense,
        "date_col": lambda M: M.date,
        "text_cols": lambda M: [M.counterparty, M.basis],
        "amount_col": lambda M: M.amount_kgs,
        "base": lambda M: M.kind == "cash",
        "columns": [
            _c("date", "Дата", "date"),
            _c("counterparty", "Контрагент"),
            _c("basis", "Основание"),
            _c("doc_number", "№"),
            _c("amount", "Сумма", "money"),
            _c("currency", "Валюта"),
            _c("amount_kgs", "В сомах", "money"),
        ],
        "row": _expense_row,
    },
}

# Порядок вкладок в интерфейсе.
TYPE_ORDER = [
    "sales",
    "receipt_bank",
    "receipt_cash",
    "returns",
    "expense_bank",
    "expense_cash",
]


@router.get("/types")
def operation_types(_: models.User = Depends(get_current_user)):
    """Список доступных вкладок с их колонками — фронт строит переключатель."""
    return [
        {"type": t, "label": TYPES[t]["label"], "columns": TYPES[t]["columns"]}
        for t in TYPE_ORDER
    ]


@router.get("")
def list_operations(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    type: str = Query(default="sales"),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    q: str | None = Query(default=None, description="Поиск по тексту (клиент/товар и т.п.)"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=10, le=500),
    org: str = Query(default="all"),
):
    cfg = TYPES.get(type)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"Неизвестный тип операций: {type}")

    M = cfg["model"]
    date_col = cfg["date_col"](M)

    query = models.org_scope(db.query(M), M, org)
    if cfg["base"] is not None:
        query = query.filter(cfg["base"](M))
    if date_from:
        query = query.filter(date_col >= date_from)
    if date_to:
        query = query.filter(date_col <= date_to)
    if q and q.strip():
        needle = f"%{q.strip()}%"
        cols = cfg["text_cols"](M)
        query = query.filter(or_(*[c.ilike(needle) for c in cols]))

    # Итог по всей выборке (не только по странице), до пагинации.
    amount_col = cfg["amount_col"](M)
    total = query.count()
    total_amount = query.with_entities(
        func.coalesce(func.sum(amount_col), 0)
    ).scalar()

    rows = (
        query.order_by(date_col.desc(), M.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    serialize = cfg["row"]
    pages = (int(total) + page_size - 1) // page_size if total else 0
    return {
        "type": type,
        "label": cfg["label"],
        "columns": cfg["columns"],
        "rows": [serialize(r) for r in rows],
        "page": page,
        "page_size": page_size,
        "total": int(total),
        "pages": pages,
        "total_amount": round(float(total_amount), 2),
    }
