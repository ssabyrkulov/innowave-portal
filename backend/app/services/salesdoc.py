"""Клиент SalesDoc API V2 для сверки учёта.

Один эндпоинт POST {url}/api/v2, тип операции в поле method, токен-авторизация.
Токен кэшируем в памяти процесса и перелогиниваемся при 401. Используем
стандартный urllib, чтобы не тянуть лишних зависимостей в прод.

Все методы здесь — только чтение (get*): дёргаем справочники/остатки/финансы,
ничего в SalesDoc не пишем.
"""

import json
import re
import threading
import urllib.error
import urllib.request

from ..config import settings

_login_lock = threading.Lock()


def _clean(text: str) -> str:
    """Сжимает HTML-страницы ошибок (nginx 403 и т.п.) в короткую строку."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()[:160]


class SalesDocError(Exception):
    """Ошибка обращения к SalesDoc (сеть, авторизация, бизнес-ошибка API)."""


# Токен уникален на устройство: держим одну сессию на процесс.
_session: dict = {"userId": None, "token": None}


def is_configured() -> bool:
    return bool(settings.salesdoc_url and settings.salesdoc_login and settings.salesdoc_password)


def _endpoint() -> str:
    return settings.salesdoc_url.rstrip("/") + "/api/v2"


def _raw_post(payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _endpoint(),
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            # nginx/WAF SalesDoc отбивает дефолтный Python-urllib UA → 403.
            # Представляемся обычным клиентом.
            "User-Agent": "Mozilla/5.0 (compatible; InnoWavePortal/1.0)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"message": f"HTTP {e.code}: {_clean(body)}"}
    except Exception as e:  # noqa: BLE001 — сеть/DNS/таймаут
        raise SalesDocError(f"SalesDoc недоступен: {e}") from e


def _login() -> None:
    status, resp = _raw_post({
        "method": "login",
        "auth": {"login": settings.salesdoc_login, "password": settings.salesdoc_password},
    })
    if not (isinstance(resp, dict) and resp.get("status")):
        msg = (resp or {}).get("message") or (resp or {}).get("error") or resp
        raise SalesDocError(f"Не удалось войти в SalesDoc: {msg}")
    res = resp["result"]
    _session["userId"] = res.get("userId")
    _session["token"] = res.get("token")


def _ensure_session() -> tuple[str, str]:
    """Гарантирует наличие токена. Логинимся под замком, чтобы параллельные
    запросы не устроили двойной логин (новый логин гасит прежний токен)."""
    if not _session["token"]:
        with _login_lock:
            if not _session["token"]:
                _login()
    return _session["userId"], _session["token"]


def _refresh_if_stale(used_token: str) -> None:
    """Перелогин на 401 — но только если токеном ещё никто не занялся, иначе
    параллельные 401 будут бесконечно гасить токены друг друга."""
    with _login_lock:
        if _session["token"] == used_token:
            _login()


def call(method: str, params: dict | None = None, _retry: bool = True) -> tuple[dict, dict | None]:
    """Вызов метода SalesDoc. Возвращает (result, pagination)."""
    user_id, token = _ensure_session()
    payload: dict = {
        "method": method,
        "auth": {"userId": user_id, "token": token},
    }
    if settings.salesdoc_filial:
        payload["filial"] = {"filial_id": settings.salesdoc_filial}
    if params is not None:
        payload["params"] = params

    status, resp = _raw_post(payload)
    if not (isinstance(resp, dict) and resp.get("status")):
        code = str(resp.get("code")) if isinstance(resp, dict) else ""
        # 401 — токен протух/погашен: перелогиниваемся один раз.
        if _retry and (code == "401" or status == 401):
            _refresh_if_stale(token)
            return call(method, params, _retry=False)
        msg = (resp or {}).get("message") or (resp or {}).get("error") or resp
        raise SalesDocError(f"SalesDoc: ошибка метода {method}: {msg}")
    return resp["result"], resp.get("pagination")


def _pick(result: dict, keys: tuple[str, ...]) -> list:
    """Первый непустой список из result по одному из возможных имён ключа."""
    if not isinstance(result, dict):
        return []
    for k in keys:
        v = result.get(k)
        if isinstance(v, list):
            return v
    return []


def call_all(method: str, key, params: dict | None = None, page_limit: int = 1000) -> list:
    """Собирает все страницы GET-метода в один список. key — имя массива в
    result или кортеж возможных имён (у разных методов оно разное)."""
    keys = (key,) if isinstance(key, str) else tuple(key)
    params = dict(params or {})
    params.setdefault("limit", page_limit)
    out: list = []
    page = 1
    while True:
        params["page"] = page
        result, pagination = call(method, params)
        chunk = _pick(result, keys)
        out.extend(chunk)
        if not pagination:
            break
        total = pagination.get("total") or 0
        limit = pagination.get("limit") or page_limit
        if page * limit >= total or not chunk:
            break
        page += 1
    return out


# ---------------------------------------------------------------------------
# Готовые выборки под сверку
# ---------------------------------------------------------------------------
def fetch_balance() -> list[dict]:
    """Текущая дебиторка по точкам: [{sd_id, code_1C, name, debt}]. В SalesDoc
    отрицательный баланс = клиент должен нам, поэтому долг = -balance."""
    rows = call_all("getBalance", "balance")
    out = []
    for r in rows:
        bal = float(r.get("balance") or 0)
        out.append({
            "sd_id": r.get("SD_id"),
            "code_1C": r.get("code_1C"),
            "name": r.get("name") or "",
            "balance": bal,
            "debt": round(-bal, 2),  # долг клиента (положительный = должен нам)
            "active": bool(r.get("active", True)),
        })
    return out


def fetch_warehouses() -> list[dict]:
    """Справочник складов SalesDoc: [{sd_id, code_1C, name}]."""
    rows = call_all("getWarehouse", ("warehouse", "warehouses", "stores", "store"))
    return [
        {"sd_id": r.get("SD_id"), "code_1C": r.get("code_1C"), "name": r.get("name") or ""}
        for r in rows
    ]


def _store_ok(store: dict | None, store_ids: set | None) -> bool:
    """Заказ относится к выбранной фирме, если его склад в наборе store_ids.
    store_ids=None → фильтр не применяется (все склады)."""
    if not store_ids:
        return True
    store = store or {}
    sid = str(store.get("SD_id") or "").lower()
    return sid in store_ids


def fetch_client_store_orgs(store_org: dict, date_from, date_to) -> dict:
    """По заказам SalesDoc определяет фирму каждого клиента: SD_id клиента
    (в нижнем регистре) → множество организаций (по складам его заказов).
    store_org: {store_sd_id(lower): org}."""
    params = {"filter": {
        "include": "all",
        "status": [1, 2, 3, 4, 5],
        "period": {"date": {"from": date_from, "to": date_to}},
    }}
    rows = call_all("getOrder", ("orders", "order"), params)
    out: dict[str, set] = {}
    for o in rows:
        cli = (o.get("client") or {}).get("SD_id")
        st = (o.get("store") or {}).get("SD_id")
        if not cli:
            continue
        org = store_org.get(str(st or "").lower())
        if org:
            out.setdefault(str(cli).lower(), set()).add(org)
    return out


def fetch_clients() -> list[dict]:
    """Справочник клиентов SalesDoc: [{sd_id, code_1C, name}] — вся вселенная
    точек, в т.ч. с нулевым балансом (их нет в getBalance)."""
    rows = call_all("getClient", "client")
    return [
        {
            "sd_id": r.get("SD_id"),
            "code_1C": r.get("code_1C"),
            "name": r.get("name") or "",
        }
        for r in rows
    ]


ORDER_STATUS = {1: "Новый", 2: "Отправлен", 3: "Доставлен", 4: "Закрыт", 5: "Отменён"}
# В сумму реализаций идут только отгруженные: Отправлен/Доставлен/Закрыт.
# «Новый» ещё не отгружен, «Отменён» — не продажа.
SHIPPED_STATUSES = {2, 3, 4}


def _client_ref(sd_id: str | None, code_1c: str | None) -> dict:
    """Ссылка на клиента для фильтра: предпочитаем SD_id (он есть всегда)."""
    if sd_id:
        return {"SD_id": sd_id}
    if code_1c:
        return {"code_1C": code_1c}
    return {}


def _day(value) -> str:
    return str(value or "").split(" ")[0]


def _client_matches(cli: dict | None, sd_id, code_1c) -> bool:
    """Серверный фильтр getOrder/getPayment по клиенту SalesDoc не соблюдает —
    сверяем принадлежность записи клиенту сами по SD_id (или коду 1С)."""
    cli = cli or {}
    if sd_id:
        return str(cli.get("SD_id") or "").lower() == str(sd_id).lower()
    if code_1c:
        return str(cli.get("code_1C") or "") == str(code_1c)
    return False


def fetch_client_orders(sd_id, code_1c, date_from, date_to, store_ids=None) -> dict:
    """Заказы (реализации) клиента за период со статусами. Явно запрашиваем все
    статусы — иначе getOrder отдаёт только «Новые» и теряет отгруженные.
    store_ids — набор складов выбранной фирмы (None = все)."""
    params = {
        "client": _client_ref(sd_id, code_1c),
        "filter": {
            "include": "all",
            "status": [1, 2, 3, 4, 5],
            "period": {"date": {"from": date_from, "to": date_to}},
        },
    }
    rows = call_all("getOrder", ("orders", "order"), params)
    items, total, counted = [], 0.0, 0
    for o in rows:
        if not _client_matches(o.get("client"), sd_id, code_1c):
            continue
        if not _store_ok(o.get("store"), store_ids):
            continue
        amt = float(o.get("totalSummaAfterDiscount") or o.get("totalSumma") or 0)
        st = o.get("status")
        is_counted = st in SHIPPED_STATUSES
        if is_counted:
            total += amt
            counted += 1
        items.append({
            "date": _day(o.get("dateDocument") or o.get("dateCreate")),
            "code_1C": o.get("code_1C"),
            "status": st,
            "status_label": ORDER_STATUS.get(st, str(st)),
            "amount": round(amt, 2),
            "counted": is_counted,
            "returns": round(float(o.get("totalReturnsSumma") or 0), 2),
        })
    items.sort(key=lambda x: x["date"], reverse=True)
    return {"total": round(total, 2), "count": counted, "items": items}


def fetch_client_returns(sd_id, code_1c, date_from, date_to) -> dict:
    """Возвраты клиента за период."""
    params = {
        "client": _client_ref(sd_id, code_1c),
        "filter": {"period": {"date": {"from": date_from, "to": date_to}}},
    }
    rows = call_all("getOrderDefect", ("defects", "orderDefects", "defect", "orderDefect", "orders"), params)
    items, total = [], 0.0
    for r in rows:
        if not _client_matches(r.get("client"), sd_id, code_1c):
            continue
        amt = float(r.get("summa") or r.get("totalSumma") or 0)
        total += amt
        items.append({"date": _day(r.get("date") or r.get("dateLoad")), "amount": round(amt, 2)})
    items.sort(key=lambda x: x["date"], reverse=True)
    return {"total": round(total, 2), "count": len(items), "items": items}


PAY_TXN = {
    1: "Заказ", 2: "Долг", 3: "Оплата", 4: "Конверсия",
    6: "Нач. остаток", 7: "Выплата клиенту", 8: "Списание долга", 9: "Возврат с полки",
}
# В «оплаты» (деньги от клиента) идёт только транзакция типа «Оплата».
PAYMENT_TXN = 3


def fetch_payment_types() -> dict:
    """Справочник типов оплат: ключ (code/sd) → название (Наличные/Банк…)."""
    try:
        rows = call_all("getPaymentType", ("currency", "paymentType", "paymentTypes", "types"))
    except SalesDocError:
        return {}
    m: dict = {}
    for r in rows:
        name = r.get("name") or r.get("short") or ""
        if r.get("code_1C"):
            m[("code", str(r["code_1C"]))] = name
        if r.get("SD_id"):
            m[("sd", str(r["SD_id"]).lower())] = name
    return m


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def fetch_client_payments(sd_id, code_1c, date_from, date_to) -> dict:
    """Оплаты клиента за период. getPayment — общий журнал операций (заказы,
    долги, конверсии…); в сумму берём только «Оплата» (transactionType=3).
    У getPayment фильтра по клиенту в доке нет — фильтруем локально."""
    params = {"filter": {"period": {"date": {"from": date_from, "to": date_to}}}}
    rows = call_all("getPayment", ("payments", "payment"), params)
    ptypes = fetch_payment_types()
    items, total, counted = [], 0.0, 0
    for p in rows:
        if not _client_matches(p.get("client"), sd_id, code_1c):
            continue
        amt = float(p.get("amount") or 0)
        txn = _to_int(p.get("transactionType"))
        is_counted = txn == PAYMENT_TXN
        if is_counted:
            total += amt
            counted += 1
        pt = p.get("paymentType") or {}
        type_name = (
            ptypes.get(("code", str(pt.get("code_1C"))))
            or ptypes.get(("sd", str(pt.get("SD_id") or "").lower()))
            or ""
        )
        items.append({
            "date": _day(p.get("paymentDate")),
            "amount": round(amt, 2),
            "txn": txn,
            "txn_label": PAY_TXN.get(txn, str(txn) if txn is not None else "—"),
            "type_name": type_name,
            "counted": is_counted,
        })
    items.sort(key=lambda x: x["date"], reverse=True)
    return {"total": round(total, 2), "count": counted, "items": items}


def fetch_orders_total(date_from: str, date_to: str, store_ids=None) -> dict:
    """Сумма заказов (реализаций) за период и разбивка по клиентам (по имени).
    store_ids — набор складов выбранной фирмы (None = все)."""
    params = {"filter": {
        "include": "all",
        "status": sorted(SHIPPED_STATUSES),  # только отгруженные
        "period": {"date": {"from": date_from, "to": date_to}},
    }}
    rows = call_all("getOrder", ("orders", "order"), params)
    total = 0.0
    by_client: dict[str, float] = {}
    for o in rows:
        if o.get("status") not in SHIPPED_STATUSES:
            continue
        if not _store_ok(o.get("store"), store_ids):
            continue
        amt = float(o.get("totalSummaAfterDiscount") or o.get("totalSumma") or 0)
        total += amt
        cli = (o.get("client") or {})
        name = cli.get("clientName") or cli.get("clientLegalName") or "—"
        by_client[name] = by_client.get(name, 0.0) + amt
    return {"total": round(total, 2), "count": len(rows), "by_client": by_client}


def fetch_payments_total(date_from: str, date_to: str) -> dict:
    """Сумма оплат за период (по paymentDate)."""
    params = {"filter": {"period": {"date": {"from": date_from, "to": date_to}}}}
    rows = call_all("getPayment", ("payments", "payment"), params)
    total, count = 0.0, 0
    for p in rows:
        if _to_int(p.get("transactionType")) != PAYMENT_TXN:  # только «Оплата»
            continue
        total += float(p.get("amount") or 0)
        count += 1
    return {"total": round(total, 2), "count": count}
