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


def _static_mode() -> bool:
    """Статический режим: задан готовый токен (как в 1С). userId — необязателен
    (по SalesDoc достаточно токена)."""
    return bool(settings.salesdoc_token)


def is_configured() -> bool:
    if not settings.salesdoc_url:
        return False
    return _static_mode() or bool(settings.salesdoc_login and settings.salesdoc_password)


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
    """Гарантирует наличие токена. В статическом режиме берём токен/userId из
    настроек и НЕ логинимся (иначе погасили бы токен 1С). Иначе логинимся под
    замком, чтобы параллельные запросы не устроили двойной логин."""
    if _static_mode():
        return settings.salesdoc_user_id or "", settings.salesdoc_token
    if not _session["token"]:
        with _login_lock:
            if not _session["token"]:
                _login()
    return _session["userId"], _session["token"]


def reconnect() -> dict:
    """Портал заново берёт токен. В обычном режиме — свежий login (после этого
    портал работает, но токен 1С гаснет — общий аккаунт). В статическом —
    проверяет заданный токен лёгким запросом."""
    _session["token"] = None
    _session["userId"] = None
    if _static_mode():
        fetch_warehouses()  # бросит SalesDocError, если токен недействителен
        return {"mode": "static", "connected": True}
    with _login_lock:
        _login()
    return {"mode": "login", "connected": True}


def _refresh_if_stale(used_token: str) -> None:
    """Перелогин на 401 — но только если токеном ещё никто не занялся, иначе
    параллельные 401 будут бесконечно гасить токены друг друга."""
    with _login_lock:
        if _session["token"] == used_token:
            _login()


def call(method: str, params: dict | None = None, _retry: bool = True) -> tuple[dict, dict | None]:
    """Вызов метода SalesDoc. Возвращает (result, pagination)."""
    user_id, token = _ensure_session()
    auth: dict = {"token": token}
    if user_id:  # userId необязателен — по SalesDoc достаточно токена
        auth["userId"] = user_id
    payload: dict = {"method": method, "auth": auth}
    if settings.salesdoc_filial:
        payload["filial"] = {"filial_id": settings.salesdoc_filial}
    if params is not None:
        payload["params"] = params

    status, resp = _raw_post(payload)
    if not (isinstance(resp, dict) and resp.get("status")):
        code = str(resp.get("code")) if isinstance(resp, dict) else ""
        is_401 = code == "401" or status == 401
        if is_401 and _static_mode():
            # Статический токен устарел (вероятно, 1С перелогинилась и сменила
            # токен). Логиниться нельзя — погасим токен 1С. Просим обновить.
            raise SalesDocError(
                "SalesDoc-токен недействителен. Скорее всего 1С перелогинилась "
                "и сменила токен. Обновите SALESDOC_TOKEN и SALESDOC_USER_ID из "
                "настроек интеграции 1С (или заведите отдельного пользователя "
                "SalesDoc для портала)."
            )
        # Обычный режим: 401 — перелогиниваемся один раз.
        if _retry and is_401:
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


def warehouse_report(date_from, date_to, store_org: dict) -> dict:
    """По каждому складу за период: реализации, возвраты (из заказов) и текущий
    остаток (количество). Приход/списания в API SalesDoc — методы записи, через
    GET не читаются, поэтому их здесь нет."""
    warehouses = fetch_warehouses()
    base: dict = {}
    for w in warehouses:
        k = str(w["sd_id"]).lower()
        base[k] = {
            "sd_id": w["sd_id"], "name": w["name"],
            "org": store_org.get(k),
            "sales": 0.0, "returns": 0.0, "orders": 0,
            "stock_qty": 0.0, "stock_pos": 0,
        }

    params = {"filter": {
        "include": "all",
        "status": [1, 2, 3, 4, 5],
        "period": {"date": {"from": date_from, "to": date_to}},
    }}
    for o in call_all("getOrder", ("orders", "order"), params):
        k = str((o.get("store") or {}).get("SD_id") or "").lower()
        if not k:
            continue
        d = base.get(k)
        if d is None:
            d = base[k] = {"sd_id": k, "name": k, "org": store_org.get(k),
                           "sales": 0.0, "returns": 0.0, "orders": 0,
                           "stock_qty": 0.0, "stock_pos": 0}
        st = o.get("status")
        if st in SHIPPED_STATUSES:
            d["sales"] += float(o.get("totalSummaAfterDiscount") or o.get("totalSumma") or 0)
            d["orders"] += 1
        if st != 5:
            d["returns"] += float(o.get("totalReturnsSumma") or 0)

    try:
        for w in call_all("getStock", ("warehouse", "warehouses")):
            k = str(w.get("SD_id") or "").lower()
            d = base.get(k)
            if d is None:
                continue
            prods = w.get("products") or []
            d["stock_pos"] = len(prods)
            d["stock_qty"] = round(sum(float(p.get("quantity") or 0) for p in prods), 3)
    except SalesDocError:
        pass

    rows = sorted(base.values(), key=lambda x: -x["sales"])
    for d in rows:
        d["sales"] = round(d["sales"], 2)
        d["returns"] = round(d["returns"], 2)
    return {"date_from": date_from, "date_to": date_to, "warehouses": rows}


def analyze(date_from, date_to, store_org: dict) -> dict:
    """Разбор структуры SalesDoc по фактическим данным: склады, филиалы
    (по префиксу CS_id), покрытие заказов складом, и — главное — торгуют ли
    точки с одним складом/фирмой или с несколькими (пересечение фирм)."""
    warehouses = fetch_warehouses()
    wh_name = {str(w["sd_id"]).lower(): w["name"] for w in warehouses}

    params = {"filter": {
        "include": "all",
        "status": [1, 2, 3, 4, 5],
        "period": {"date": {"from": date_from, "to": date_to}},
    }}
    orders = call_all("getOrder", ("orders", "order"), params)

    store_stat: dict = {}
    client_stores: dict = {}
    filials: dict = {}
    with_store = without_store = 0
    for o in orders:
        cs = str(o.get("CS_id") or "")
        pref = cs.split("-", 1)[0] if "-" in cs else "(без префикса)"
        filials[pref] = filials.get(pref, 0) + 1
        st = str((o.get("store") or {}).get("SD_id") or "").lower()
        amt = float(o.get("totalSummaAfterDiscount") or o.get("totalSumma") or 0)
        if st:
            with_store += 1
            d = store_stat.setdefault(st, {"name": wh_name.get(st, st), "count": 0, "sum": 0.0})
            d["count"] += 1
            d["sum"] += amt
        else:
            without_store += 1
        cli = str((o.get("client") or {}).get("SD_id") or "").lower()
        if cli and st:
            client_stores.setdefault(cli, set()).add(st)

    multi_store = sum(1 for v in client_stores.values() if len(v) > 1)
    cross_firm = 0
    firm_sum: dict = {}
    unmapped_stores = set()
    for v in client_stores.values():
        orgs = set()
        for s in v:
            if s in store_org:
                orgs.add(store_org[s])
            else:
                unmapped_stores.add(s)
        if len(orgs) > 1:
            cross_firm += 1
    for st, d in store_stat.items():
        org = store_org.get(st)
        if org:
            firm_sum[org] = round(firm_sum.get(org, 0.0) + d["sum"], 2)
        else:
            unmapped_stores.add(st)

    try:
        clients_total = len(fetch_clients())
    except SalesDocError:
        clients_total = None

    return {
        "date_from": date_from,
        "date_to": date_to,
        "warehouses": [
            {"sd_id": w["sd_id"], "name": w["name"], "code_1C": w["code_1C"],
             "org": store_org.get(str(w["sd_id"]).lower())}
            for w in warehouses
        ],
        "orders_total": len(orders),
        "orders_with_store": with_store,
        "orders_without_store": without_store,
        "stores": sorted(
            ({"sd_id": k, "name": v["name"], "org": store_org.get(k),
              "orders": v["count"], "sum": round(v["sum"], 2)}
             for k, v in store_stat.items()),
            key=lambda x: -x["sum"],
        ),
        "filials": sorted(
            ({"prefix": k, "orders": v} for k, v in filials.items()),
            key=lambda x: -x["orders"],
        ),
        "clients_ordered": len(client_stores),
        "clients_multi_store": multi_store,
        "clients_cross_firm": cross_firm,
        "clients_total": clients_total,
        "firm_sum": firm_sum,
        "unmapped_stores": sorted(unmapped_stores),
    }


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
    сверяем принадлежность записи клиенту сами. Пробуем по всем ключам: SD_id,
    CS_id (= F1-<SD_id>) и коду 1С — у оплат клиент бывает записан иначе, чем
    у заказов."""
    cli = cli or {}
    csd = str(cli.get("SD_id") or "").lower()
    ccs = str(cli.get("CS_id") or "").lower()
    ccode = str(cli.get("code_1C") or "")
    sid = str(sd_id or "").lower()
    if sid and (csd == sid or (ccs and ccs.split("-", 1)[-1] == sid)):
        return True
    if code_1c and ccode and ccode == str(code_1c):
        return True
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
        # (клиент совпал)
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
    return {
        "total": round(total, 2), "count": counted, "items": items,
        "scanned": len(rows), "matched": len(items),
    }


def payments_diagnostic(date_from, date_to, sample=10) -> dict:
    """Разбор оплат SalesDoc: сколько с привязкой к клиенту, сколько без, по
    видам операции и типам оплаты — чтобы понять, почему оплаты не находятся."""
    rows = call_all(
        "getPayment", ("payments", "payment"),
        {"filter": {"period": {"date": {"from": date_from, "to": date_to}}}},
    )
    ptypes = fetch_payment_types()
    with_sd = with_code = without = 0
    txn: dict = {}
    by_type: dict = {}
    samples: list = []
    for p in rows:
        cli = p.get("client") or {}
        if cli.get("SD_id"):
            with_sd += 1
        elif cli.get("code_1C"):
            with_code += 1
        else:
            without += 1
        t = _to_int(p.get("transactionType"))
        txn[PAY_TXN.get(t, str(t))] = txn.get(PAY_TXN.get(t, str(t)), 0) + 1
        pt = p.get("paymentType") or {}
        tname = (
            ptypes.get(("code", str(pt.get("code_1C"))))
            or ptypes.get(("sd", str(pt.get("SD_id") or "").lower()))
            or "(без типа)"
        )
        d = by_type.setdefault(tname, {"with_client": 0, "without_client": 0})
        d["with_client" if (cli.get("SD_id") or cli.get("code_1C")) else "without_client"] += 1
        if len(samples) < sample:
            samples.append({
                "date": _day(p.get("paymentDate")),
                "amount": float(p.get("amount") or 0),
                "txn": PAY_TXN.get(t, str(t)),
                "type": tname,
                "client_sd": cli.get("SD_id"),
                "client_code": cli.get("code_1C"),
                "cashbox": (p.get("cashbox") or {}).get("name") or (p.get("cashbox") or {}).get("SD_id"),
            })
    return {
        "scanned": len(rows),
        "with_client_sdid": with_sd,
        "with_client_code": with_code,
        "without_client": without,
        "txn": txn,
        "by_type": [{"type": k, **v} for k, v in sorted(by_type.items(), key=lambda x: -(x[1]["with_client"] + x[1]["without_client"]))],
        "samples": samples,
    }


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
