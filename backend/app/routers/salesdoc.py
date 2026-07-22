"""Сверка учёта с SalesDoc: тянем данные из SalesDoc и сопоставляем с нашей
1С-картиной (дебиторка, реализации, оплаты за период), показываем расхождения.

Сопоставление клиентов — по нормализованному имени (в нашей 1С-выгрузке нет
кода контрагента, а в SalesDoc имя приходит рядом с балансом).
"""

import re
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import require_roles
from ..services import salesdoc
from .receipts import CUSTOMER_PAYMENT_PREFIX, receivables
from .sales import sales_summary

# Технические коды-хвосты вида z8_1249 / e4_1252 / (j1_129), которыми SalesDoc
# и 1С помечают контрагентов по-разному и из-за которых точное имя не
# совпадает. Для сопоставления их убираем.
_CODE_RE = re.compile(r"[a-zа-я]{0,3}\d*_\d+", re.IGNORECASE)


def _match_key(name: str) -> str:
    """Ключ сопоставления клиента по имени: без кодов, скобок, кавычек и лишних
    пробелов. Запасной вариант, если не удалось связать по ИД SalesDoc."""
    s = (name or "").lower().replace("ё", "е")
    s = re.sub(r"\([^)]*\)", " ", s)          # (…)
    s = _CODE_RE.sub(" ", s)                   # z8_1249 и т.п.
    s = re.sub(r"[\"“”«»']", " ", s)          # кавычки
    s = re.sub(r"[^\w\s-]", " ", s)            # прочая пунктуация
    return re.sub(r"\s+", " ", s).strip()


def _extract_sd_id(name: str) -> str | None:
    """Достаёт ИД клиента SalesDoc (напр. z8_1249) из имени контрагента 1С —
    самый надёжный ключ, т.к. он присутствует и там, и там."""
    m = _CODE_RE.search(name or "")
    return m.group(0).lower() if m else None

router = APIRouter(prefix="/salesdoc", tags=["salesdoc"])

can_view = require_roles(models.Role.admin, models.Role.accountant)


@router.get("/status")
def status(_: models.User = Depends(can_view)):
    return {"configured": salesdoc.is_configured()}


def _require_configured():
    if not salesdoc.is_configured():
        raise HTTPException(
            status_code=503,
            detail="Интеграция SalesDoc не настроена: задайте SALESDOC_URL, "
                   "SALESDOC_LOGIN и SALESDOC_PASSWORD в окружении сервера.",
        )


@router.get("/debt")
def reconcile_debt(
    db: Session = Depends(get_db),
    user: models.User = Depends(can_view),
    only_diff: bool = Query(default=False, description="Только строки с расхождением"),
    org: str = Query(default="all"),
):
    """Дебиторка: наш долг (из 1С) против баланса SalesDoc, по каждому клиенту."""
    _require_configured()
    try:
        sd_balance = salesdoc.fetch_balance()
        sd_clients = salesdoc.fetch_clients()
    except salesdoc.SalesDocError as e:
        raise HTTPException(status_code=502, detail=str(e))

    # Долг SalesDoc по ИД клиента (нет в списке долгов = 0).
    debt_by_id: dict[str, float] = {}
    for r in sd_balance:
        sid = (r["sd_id"] or "").lower()
        if sid:
            debt_by_id[sid] = debt_by_id.get(sid, 0.0) + r["debt"]

    # Справочник SalesDoc: индексы по ИД, коду 1С и по имени.
    sd_by_id: dict[str, dict] = {}
    sd_by_code: dict[str, dict] = {}
    sd_by_name: dict[str, dict] = {}
    for c in sd_clients:
        sid = (c["sd_id"] or "").lower()
        entry = {
            "sd_id": sid,
            "name": c["name"],
            "code_1C": c["code_1C"],
            "debt": round(debt_by_id.get(sid, 0.0), 2),
        }
        if sid:
            sd_by_id[sid] = entry
        if c["code_1C"]:
            sd_by_code[str(c["code_1C"])] = entry
        k = _match_key(c["name"])
        if k:
            sd_by_name.setdefault(k, entry)

    rec = receivables(db=db, _=user, org=org)
    rows = []
    matched_ids: set[str] = set()

    for c in rec["clients"]:
        name = c["client"]
        our_debt = round(c["debt"], 2)
        # 1) по ИД SalesDoc из имени, 2) по коду 1С, 3) по имени
        entry = None
        sid = _extract_sd_id(name)
        if sid and sid in sd_by_id:
            entry = sd_by_id[sid]
        if entry is None:
            entry = sd_by_name.get(_match_key(name))
        if entry:
            matched_ids.add(entry["sd_id"])
        sd_debt = entry["debt"] if entry else 0.0
        rows.append({
            "name": name,
            "our_debt": our_debt,
            "sd_debt": sd_debt,
            "diff": round(our_debt - sd_debt, 2),
            "in_1c": True,
            "in_sd": entry is not None,
            "code_1C": entry["code_1C"] if entry else None,
            "sd_id": entry["sd_id"] if entry else sid,
        })

    # Клиенты, которые есть только в SalesDoc и там висит долг.
    for entry in sd_by_id.values():
        if entry["sd_id"] in matched_ids or abs(entry["debt"]) < 0.5:
            continue
        rows.append({
            "name": entry["name"],
            "our_debt": 0.0,
            "sd_debt": entry["debt"],
            "diff": round(-entry["debt"], 2),
            "in_1c": False,
            "in_sd": True,
            "code_1C": entry["code_1C"],
            "sd_id": entry["sd_id"],
        })

    rows.sort(key=lambda x: -abs(x["diff"]))
    if only_diff:
        rows = [r for r in rows if abs(r["diff"]) >= 0.5]

    our_total = round(sum(r["our_debt"] for r in rows), 2)
    sd_total = round(sum(r["sd_debt"] for r in rows), 2)
    return {
        "our_total": our_total,
        "sd_total": sd_total,
        "diff": round(our_total - sd_total, 2),
        "count": len(rows),
        "matched": sum(1 for r in rows if r["in_1c"] and r["in_sd"]),
        "only_1c": sum(1 for r in rows if r["in_1c"] and not r["in_sd"]),
        "only_sd": sum(1 for r in rows if r["in_sd"] and not r["in_1c"]),
        "rows": rows,
    }


@router.get("/period")
def reconcile_period(
    db: Session = Depends(get_db),
    user: models.User = Depends(can_view),
    date_from: date = Query(...),
    date_to: date = Query(...),
    org: str = Query(default="all"),
):
    """Итоги за период: реализации и оплаты — 1С против SalesDoc."""
    _require_configured()
    df, dt = date_from.isoformat(), date_to.isoformat()
    try:
        sd_orders = salesdoc.fetch_orders_total(df, dt)
        sd_payments = salesdoc.fetch_payments_total(df, dt)
    except salesdoc.SalesDocError as e:
        raise HTTPException(status_code=502, detail=str(e))

    our_sales = sales_summary(db=db, _=user, date_from=date_from, date_to=date_to, top=1, org=org)
    our_sales_total = round(float(our_sales["revenue"]), 2)

    # Наши оплаты клиентов за период (в сомах).
    q = (
        models.org_scope(db.query(models.Receipt), models.Receipt, org)
        .filter(models.Receipt.date >= date_from, models.Receipt.date <= date_to)
        .filter(models.Receipt.operation.like(f"{CUSTOMER_PAYMENT_PREFIX}%"))
    )
    our_pay_total = round(sum(float(r.amount_kgs) for r in q.all()), 2)

    return {
        "date_from": df,
        "date_to": dt,
        "sales": {
            "our": our_sales_total,
            "sd": sd_orders["total"],
            "diff": round(our_sales_total - sd_orders["total"], 2),
            "sd_count": sd_orders["count"],
        },
        "payments": {
            "our": our_pay_total,
            "sd": sd_payments["total"],
            "diff": round(our_pay_total - sd_payments["total"], 2),
            "sd_count": sd_payments["count"],
        },
    }


@router.get("/client-detail")
def client_detail(
    _: models.User = Depends(can_view),
    sd_id: str | None = Query(default=None),
    code_1c: str | None = Query(default=None),
    date_from: date = Query(...),
    date_to: date = Query(...),
):
    """Детализация клиента в SalesDoc за период: реализации (со статусами),
    оплаты, возвраты. Каждый блок изолирован — сбой одного не рушит остальные."""
    _require_configured()
    if not sd_id and not code_1c:
        raise HTTPException(status_code=400, detail="Нужен sd_id или code_1c клиента")
    df, dt = date_from.isoformat(), date_to.isoformat()

    def safe(fn):
        try:
            return fn(sd_id, code_1c, df, dt), None
        except salesdoc.SalesDocError as e:
            return None, str(e)

    orders, e1 = safe(salesdoc.fetch_client_orders)
    payments, e2 = safe(salesdoc.fetch_client_payments)
    returns, e3 = safe(salesdoc.fetch_client_returns)
    return {
        "sd_id": sd_id,
        "code_1c": code_1c,
        "date_from": df,
        "date_to": dt,
        "orders": orders,
        "payments": payments,
        "returns": returns,
        "errors": [e for e in (e1, e2, e3) if e],
    }
