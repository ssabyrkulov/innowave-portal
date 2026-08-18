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
import time
import urllib.error
import urllib.request

from ..config import settings

_login_lock = threading.Lock()

# --- Кэш выборок в памяти процесса ------------------------------------------
# SalesDoc API медленный (постраничные сетевые запросы), а данные меняются не
# ежесекундно. Держим результаты тяжёлых выборок в памяти: клик по сверке
# читает их мгновенно. Кэш «подогревается» фоновым потоком (см. warm_cache).
CACHE_TTL = 900  # сек — 15 минут (для тяжёлых выборок)
# Баланс и справочник клиентов меняются в SalesDoc постоянно и стоят один
# запрос, поэтому держим их совсем недолго — иначе правка в SalesDoc не видна
# на сайте по 15 минут.
LIVE_TTL = 60
_cache: dict[str, tuple[float, list]] = {}
_cache_lock = threading.Lock()
_last_sync: float | None = None


# Кэш ВЫЧИСЛЕННЫХ результатов (маленькие словари: клиент → сумма). В отличие
# от кэша сырых выгрузок он занимает килобайты, а не сотни мегабайт, поэтому
# именно им кэшируем тяжёлые агрегаты (компоненты сверки, фирмы клиентов).
_result_cache: dict[str, tuple[float, object]] = {}


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
        _result_cache.clear()


def _cached_result(key: str, ttl: float, build):
    """Вернуть результат из кэша результатов или посчитать и запомнить."""
    now = time.time()
    with _cache_lock:
        hit = _result_cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = build()
    global _last_sync
    with _cache_lock:
        _result_cache[key] = (time.time(), value)
        _last_sync = time.time()
    return value


def last_sync() -> float | None:
    """Момент последнего успешного обновления кэша (epoch) или None."""
    return _last_sync


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


def call(method: str, params: dict | None = None, _retry: bool = True,
         with_filial: bool = True) -> tuple[dict, dict | None]:
    """Вызов метода SalesDoc. Возвращает (result, pagination).

    with_filial=False — не подставлять filial_id из настроек. Нужно для
    диагностики: если филиал задан, документы других филиалов в выдачу не
    попадают, и это надо уметь отличить от дефекта API."""
    user_id, token = _ensure_session()
    auth: dict = {"token": token}
    if user_id:  # userId необязателен — по SalesDoc достаточно токена
        auth["userId"] = user_id
    payload: dict = {"method": method, "auth": auth}
    if settings.salesdoc_filial and with_filial:
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
    return (resp.get("result") or {}), resp.get("pagination")


def _pick(result, keys: tuple[str, ...]) -> list:
    """Список записей из result: по имени ключа либо сам result.

    Большинство методов кладут массив в result под своим именем, но не все:
    getStoreLog отдаёт массив напрямую, без обёртки. Раньше такой ответ
    молча превращался в ноль записей — и выглядело это как «журнал пуст»,
    а не как «мы не разобрали ответ»."""
    if isinstance(result, list):
        return result
    if not isinstance(result, dict):
        return []
    for k in keys:
        v = result.get(k)
        if isinstance(v, list):
            return v
    # Имя массива могли не угадать: если в ответе ровно один список, он и есть
    # выдача. Лучше взять его, чем отдать пустоту и соврать про пустой журнал.
    lists = [v for v in result.values() if isinstance(v, list)]
    return lists[0] if len(lists) == 1 else []


def call_all(method: str, key, params: dict | None = None, page_limit: int = 1000,
             use_cache: bool = False, ttl: float = CACHE_TTL) -> list:
    """Собирает все страницы GET-метода в один список. key — имя массива в
    result или кортеж возможных имён (у разных методов оно разное).

    use_cache=True — вернуть результат из памяти, если он свежий (моложе ttl),
    иначе выгрузить и положить в кэш. Ключ кэша — метод + параметры."""
    keys = (key,) if isinstance(key, str) else tuple(key)
    ck = None
    if use_cache:
        ck = json.dumps([method, list(keys), params or {}], sort_keys=True, ensure_ascii=False)
        now = time.time()
        with _cache_lock:
            hit = _cache.get(ck)
            if hit and now - hit[0] < ttl:
                return hit[1]

    params = dict(params or {})
    params.setdefault("limit", page_limit)
    out: list = []
    page = 1
    while True:
        params["page"] = page
        result, pagination = call(method, params)
        chunk = _pick(result, keys)
        out.extend(chunk)
        if not pagination or not chunk:
            break
        # Считаем ФАКТИЧЕСКИ полученные строки, а не «страница × лимит».
        # SalesDoc может отдать на странице меньше, чем мы запросили: тогда
        # расчётный счётчик убегал вперёд, цикл обрывался раньше времени и
        # хвост записей терялся — на практике пропадали самые свежие операции.
        total = int(pagination.get("total") or 0)
        if total and len(out) >= total:
            break
        page += 1
        if page > 1000:  # страховка от бесконечного цикла при странном ответе
            break

    if ck is not None:
        global _last_sync
        with _cache_lock:
            _cache[ck] = (time.time(), out)
            _last_sync = time.time()
    return out


def call_all_ex(method: str, key, params: dict | None = None,
                page_limit: int = 1000, with_filial: bool = True) -> list:
    """То же, что call_all, но позволяет отключить подстановку филиала.

    Нужно диагностике: если в настройках задан филиал, документы других
    филиалов в выдачу не попадают — и это надо уметь отличить от «SalesDoc
    вообще не отдаёт документ». Без кэша: зонд должен видеть свежий ответ.
    """
    keys = (key,) if isinstance(key, str) else tuple(key)
    params = dict(params or {})
    params.setdefault("limit", page_limit)
    out: list = []
    page = 1
    while True:
        params["page"] = page
        result, pagination = call(method, params, with_filial=with_filial)
        chunk = _pick(result, keys)
        out.extend(chunk)
        if not pagination or not chunk:
            break
        total = int(pagination.get("total") or 0)
        if total and len(out) >= total:
            break
        page += 1
        if page > 1000:
            break
    return out


# ---------------------------------------------------------------------------
# Готовые выборки под сверку
# ---------------------------------------------------------------------------
def cashbox_of(payment: dict) -> tuple[str | None, str | None]:
    """Касса операции (SD_id, название) — публичная обёртка над _cashbox."""
    return _cashbox(payment)


def client_matches(cli: dict | None, sd_id, code_1c) -> bool:
    """Публичная обёртка над _client_matches — для диагностических зондов."""
    return _client_matches(cli, sd_id, code_1c)


def day(value) -> str:
    """Публичная обёртка над _day (дата без времени) — для зондов."""
    return _day(value)


def fetch_balance(use_cache: bool = False) -> list[dict]:
    """Текущая дебиторка по точкам: [{sd_id, code_1C, name, debt}]. В SalesDoc
    отрицательный баланс = клиент должен нам, поэтому долг = -balance."""
    rows = call_all("getBalance", "balance", use_cache=use_cache, ttl=LIVE_TTL)
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


def fetch_warehouses(use_cache: bool = False) -> list[dict]:
    """Справочник складов SalesDoc: [{sd_id, code_1C, name}]."""
    rows = call_all("getWarehouse", ("warehouse", "warehouses", "stores", "store"),
                    use_cache=use_cache)
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


def fetch_client_store_orgs(store_org: dict, date_from, date_to, use_cache: bool = False) -> dict:
    """По заказам SalesDoc определяет фирму каждого клиента: SD_id клиента
    (в нижнем регистре) → множество организаций (по складам его заказов).
    store_org: {store_sd_id(lower): org}.

    Кэшируем сам результат (маленькая карта), а не сырую выгрузку заказов —
    иначе в памяти оседают сотни мегабайт."""
    def build() -> dict:
        params = {"filter": {
            "include": "all",
            "status": [1, 2, 3, 4, 5],
            "period": {"date": {"from": date_from, "to": date_to}},
        }}
        out: dict[str, set] = {}
        for o in call_all("getOrder", ("orders", "order"), params):
            cli = (o.get("client") or {}).get("SD_id")
            st = (o.get("store") or {}).get("SD_id")
            if not cli:
                continue
            org = store_org.get(str(st or "").lower())
            if org:
                out.setdefault(str(cli).lower(), set()).add(org)
        return out

    if not use_cache:
        return build()
    key = json.dumps(["client_store_orgs", date_from, date_to,
                      sorted(store_org.items())], ensure_ascii=False)
    return _cached_result(key, CACHE_TTL, build)


def fetch_clients(use_cache: bool = False) -> list[dict]:
    """Справочник клиентов SalesDoc — вся вселенная точек, в т.ч. с нулевым
    балансом (их нет в getBalance).

    agents — закрепление точки за агентом: [{id, code, days}], где days —
    дни недели маршрута (1 = понедельник). Это и есть настоящее «за кем точка»,
    в отличие от агента в заказе, который лишь говорит, кто выписал документ."""
    rows = call_all("getClient", "client", use_cache=use_cache, ttl=LIVE_TTL)
    return [
        {
            "sd_id": r.get("SD_id"),
            "code_1C": r.get("code_1C"),
            "name": r.get("name") or "",
            "active": r.get("active"),
            "agents": [
                {"sd_id": str(a.get("id") or "").lower(),
                 "code_1C": a.get("code"),
                 "days": a.get("days") or []}
                for a in (r.get("agents") or [])
                if isinstance(a, dict) and a.get("id")
            ],
        }
        for r in rows
    ]


ORDER_STATUS = {1: "Новый", 2: "Отправлен", 3: "Доставлен", 4: "Закрыт", 5: "Отменён"}
# Статусы для запроса возвратных документов. Указываем явно: у getOrderDefect
# фильтр статусов по умолчанию — [1], то есть без списка сервер отдаёт только
# новые документы, а проведённые возвраты остаются невидимыми (та же ловушка,
# что была у getOrder).
ALL_DEFECT_STATUSES = [1, 2, 3, 4, 5]
# В сумму реализаций идут только отгруженные: Отправлен/Доставлен/Закрыт.
# «Новый» ещё не отгружен, «Отменён» — не продажа.
SHIPPED_STATUSES = {2, 3, 4}
CANCELLED_STATUS = 5


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
            "sd_id": o.get("SD_id") or o.get("CS_id"),
            "code_1C": o.get("code_1C"),
            "store": (o.get("store") or {}).get("name")
                     or (o.get("store") or {}).get("SD_id") or "",
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
        "filter": {"status": ALL_DEFECT_STATUSES,
                   "period": {"date": {"from": date_from, "to": date_to}}},
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
# Возврат товара в SalesDoc записывается не отдельным документом, а операцией
# «Возврат с полки» (тип 9) в журнале оплат getPayment. Учитываем её как
# возврат (в 1С это выгрузка возвратов).
SHELF_RETURN_TXN = 9
# Операции, которые меняют баланс SalesDoc, но денег и товара не двигают и в
# 1С пары не имеют: списание долга (8) и выплата клиенту (7). Их нельзя
# считать оплатой — иначе «оплачено» в портале разойдётся с кассой, — но и
# игнорировать нельзя: точка со списанным долгом иначе выглядит расхождением
# без объяснения.
BALANCE_ONLY_TXN = {7, 8}


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
    shelf_items, shelf_total = [], 0.0  # «Возврат с полки» — это возвраты
    for p in rows:
        if not _client_matches(p.get("client"), sd_id, code_1c):
            continue
        # (клиент совпал)
        amt = float(p.get("amount") or 0)
        txn = _to_int(p.get("transactionType"))
        # «Возврат с полки» (тип 9) — это возврат товара, а не оплата: уводим
        # его в отдельный список, чтобы не мозолил глаза среди оплат.
        if txn == SHELF_RETURN_TXN:
            shelf_total += amt
            shelf_items.append({"date": _day(p.get("paymentDate")), "amount": round(amt, 2)})
            continue
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
            "sd_id": p.get("SD_id") or p.get("CS_id"),
            "amount": round(amt, 2),
            "txn": txn,
            "txn_label": PAY_TXN.get(txn, str(txn) if txn is not None else "—"),
            "type_name": type_name,
            "cashbox": (p.get("cashbox") or {}).get("name") or "",
            "counted": is_counted,
        })
    items.sort(key=lambda x: x["date"], reverse=True)
    shelf_items.sort(key=lambda x: x["date"], reverse=True)
    return {
        "total": round(total, 2), "count": counted, "items": items,
        "scanned": len(rows), "matched": len(items),
        # Возвраты, спрятанные в журнале оплат как «Возврат с полки».
        "shelf_returns": {
            "total": round(shelf_total, 2), "count": len(shelf_items), "items": shelf_items,
        },
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


def _client_key_any(cli: dict | None) -> str:
    """Ключ клиента для сопоставления источников возвратов: SD_id, иначе код 1С."""
    k = _client_key_sd(cli)
    if k:
        return k
    return "code:" + str((cli or {}).get("code_1C") or "?")


def returns_diagnostic(date_from: str, date_to: str) -> dict:
    """Сравнивает два источника возвратов в SalesDoc за период:
    getOrderDefect (документы брака) и «Возврат с полки» (тип 9 в журнале
    оплат). Показывает суммы, число клиентов в каждом и пересечение — чтобы
    понять, суммировать их безопасно или это одни и те же возвраты (дубли)."""
    period = {"period": {"date": {"from": date_from, "to": date_to}}}

    # --- getOrderDefect (брак/возвратные заказы) ---
    def_by_client: dict[str, float] = {}
    def_keys: dict[str, set] = {}
    def_count = 0
    def_total = 0.0
    for r in call_all("getOrderDefect",
                      ("defects", "orderDefects", "defect", "orderDefect", "orders"),
                      {"filter": {"status": ALL_DEFECT_STATUSES, **period}}):
        amt = float(r.get("summa") or r.get("totalSumma") or 0)
        k = _client_key_any(r.get("client"))
        def_count += 1
        def_total += amt
        def_by_client[k] = def_by_client.get(k, 0.0) + amt
        def_keys.setdefault(k, set()).add((_day(r.get("date") or r.get("dateLoad")), round(amt, 2)))

    # --- «Возврат с полки» (тип 9 в getPayment) ---
    shelf_by_client: dict[str, float] = {}
    shelf_keys: dict[str, set] = {}
    shelf_count = 0
    shelf_total = 0.0
    for p in call_all("getPayment", ("payments", "payment"), {"filter": period}):
        if _to_int(p.get("transactionType")) != SHELF_RETURN_TXN:
            continue
        amt = float(p.get("amount") or 0)
        k = _client_key_any(p.get("client"))
        shelf_count += 1
        shelf_total += amt
        shelf_by_client[k] = shelf_by_client.get(k, 0.0) + amt
        shelf_keys.setdefault(k, set()).add((_day(p.get("paymentDate")), round(amt, 2)))

    # Пересечение: клиенты в обоих источниках и совпадающие (дата, сумма) —
    # это «подозрение на дубль» (один возврат учтён и там, и там).
    both = set(def_by_client) & set(shelf_by_client)
    dup_count = 0
    dup_amount = 0.0
    for k in both:
        for (d, a) in (def_keys.get(k, set()) & shelf_keys.get(k, set())):
            dup_count += 1
            dup_amount += a

    return {
        "date_from": date_from,
        "date_to": date_to,
        "defects": {"count": def_count, "total": round(def_total, 2), "clients": len(def_by_client)},
        "shelf": {"count": shelf_count, "total": round(shelf_total, 2), "clients": len(shelf_by_client)},
        "clients_with_both": len(both),
        "overlap": {"count": dup_count, "amount": round(dup_amount, 2)},
    }


def speed_probe(sd_id: str, code_1c: str | None, date_from: str, date_to: str) -> dict:
    """Проверяет две вещи на живых данных SalesDoc, от которых зависит, как
    строить быструю карточку клиента:

    1) Соблюдает ли сервер фильтр по клиенту (params.client). Если да —
       заказы/возвраты можно тянуть точечно и мгновенно, без кэша.
    2) Работает ли инкремент по filter.period.dateUpdate — «отдай только то,
       что изменилось». На нём строится дежурное зеркало оплат.

    Только чтение, ничего не меняет. Замеряет время каждого варианта."""
    out: dict = {"sd_id": sd_id, "code_1C": code_1c,
                 "date_from": date_from, "date_to": date_to}
    period = {"period": {"date": {"from": date_from, "to": date_to}}}
    all_status = [1, 2, 3, 4, 5]

    def timed(fn):
        t0 = time.time()
        try:
            rows = fn()
            return {"rows": rows, "ms": int((time.time() - t0) * 1000), "error": None}
        except SalesDocError as e:
            return {"rows": [], "ms": int((time.time() - t0) * 1000), "error": str(e)}

    def mine(rows, getter):
        """Сколько записей реально относятся к нашему клиенту."""
        return sum(1 for r in rows if _client_matches(getter(r), sd_id, code_1c))

    # --- 1. Заказы: с фильтром по клиенту vs без него ---
    with_f = timed(lambda: call_all(
        "getOrder", ("orders", "order"),
        {"client": _client_ref(sd_id, code_1c),
         "filter": {"include": "all", "status": all_status, **period}}))
    without_f = timed(lambda: call_all(
        "getOrder", ("orders", "order"),
        {"filter": {"include": "all", "status": all_status, **period}}))

    got = len(with_f["rows"])
    mine_with = mine(with_f["rows"], lambda o: o.get("client"))
    total_all = len(without_f["rows"])
    # Фильтр считаем рабочим, если ответ заметно меньше общего журнала и
    # состоит (почти) только из записей нашего клиента.
    server_filter_works = bool(
        not with_f["error"] and got and total_all
        and got < total_all * 0.5 and mine_with >= got * 0.9
    )
    out["orders"] = {
        "with_client_filter": {"returned": got, "of_this_client": mine_with,
                               "ms": with_f["ms"], "error": with_f["error"]},
        "without_filter": {"returned": total_all, "ms": without_f["ms"],
                           "error": without_f["error"]},
        "server_filter_works": server_filter_works,
        "speedup": round(without_f["ms"] / with_f["ms"], 1)
        if with_f["ms"] and not with_f["error"] else None,
    }

    # --- 2. Возвраты-документы: тот же тест ---
    d_with = timed(lambda: call_all(
        "getOrderDefect", ("defects", "orderDefects", "defect", "orderDefect", "orders"),
        {"client": _client_ref(sd_id, code_1c),
         "filter": {"status": ALL_DEFECT_STATUSES, **period}}))
    d_all = timed(lambda: call_all(
        "getOrderDefect", ("defects", "orderDefects", "defect", "orderDefect", "orders"),
        {"filter": {"status": ALL_DEFECT_STATUSES, **period}}))
    out["defects"] = {
        "with_client_filter": {"returned": len(d_with["rows"]), "ms": d_with["ms"],
                               "error": d_with["error"]},
        "without_filter": {"returned": len(d_all["rows"]), "ms": d_all["ms"],
                           "error": d_all["error"]},
        "server_filter_works": bool(
            not d_with["error"] and d_all["rows"]
            and len(d_with["rows"]) < len(d_all["rows"])
        ),
    }

    # --- 3. Оплаты: фильтр по типу операции (клиентского фильтра у них нет) ---
    p_typed = timed(lambda: call_all(
        "getPayment", ("payments", "payment"),
        {"filter": {"transactionType": PAYMENT_TXN, **period}}))
    p_all = timed(lambda: call_all(
        "getPayment", ("payments", "payment"), {"filter": period}))
    typed_rows = p_typed["rows"]
    only_pay = sum(1 for p in typed_rows if _to_int(p.get("transactionType")) == PAYMENT_TXN)
    out["payments"] = {
        "with_type_filter": {"returned": len(typed_rows), "really_type3": only_pay,
                             "ms": p_typed["ms"], "error": p_typed["error"]},
        "without_filter": {"returned": len(p_all["rows"]), "ms": p_all["ms"],
                           "error": p_all["error"]},
        "type_filter_works": bool(
            not p_typed["error"] and typed_rows
            and only_pay >= len(typed_rows) * 0.9
            and len(typed_rows) < len(p_all["rows"])
        ),
    }

    # --- 4. Инкремент: ловит ли dateUpdate свежие записи ---
    # Берём «изменённые за последние 7 дней» и сравниваем с тем, сколько
    # записей за этот же срок есть по обычной дате документа.
    from datetime import date as _date, timedelta
    today = _date.today()
    week_ago = (today - timedelta(days=7)).isoformat()
    upd = timed(lambda: call_all(
        "getPayment", ("payments", "payment"),
        {"filter": {"period": {"dateUpdate": {"from": week_ago, "to": today.isoformat()}}}}))
    recent = timed(lambda: call_all(
        "getPayment", ("payments", "payment"),
        {"filter": {"period": {"date": {"from": week_ago, "to": today.isoformat()}}}}))
    out["incremental"] = {
        "since": week_ago,
        "by_dateUpdate": {"returned": len(upd["rows"]), "ms": upd["ms"],
                          "error": upd["error"]},
        "by_date": {"returned": len(recent["rows"]), "ms": recent["ms"],
                    "error": recent["error"]},
        # Инкремент пригоден, если dateUpdate отдаёт не меньше, чем появилось
        # новых за тот же период (иначе свежие записи будут теряться).
        "usable": bool(
            not upd["error"] and upd["rows"]
            and len(upd["rows"]) >= len(recent["rows"])
            and len(upd["rows"]) < len(p_all["rows"])
        ),
    }
    return out


def _client_order_stores(orders: list, sid: str, code: str) -> list[dict]:
    """Заказы клиента в разрезе складов и статусов.

    Отвечает на вопрос «почему реализаций ноль»: если заказы есть, но лежат на
    складе, не привязанном к выбранной фирме, отбор их отсекал. Заодно видно,
    не отменены ли они — отменённые в сумму не идут."""
    wh = {}
    try:
        wh = {str(w["sd_id"]).lower(): w["name"] for w in fetch_warehouses()}
    except SalesDocError:
        pass
    stat: dict = {}
    for o in orders:
        if not _client_matches(o.get("client"), sid, code):
            continue
        st = str((o.get("store") or {}).get("SD_id") or "").lower()
        key = wh.get(st) or st or "(склад не указан)"
        d = stat.setdefault(key, {"count": 0, "sum": 0.0, "statuses": {}})
        d["count"] += 1
        d["sum"] += float(o.get("totalSummaAfterDiscount") or o.get("totalSumma") or 0)
        label = ORDER_STATUS.get(o.get("status"), str(o.get("status")))
        d["statuses"][label] = d["statuses"].get(label, 0) + 1
    return [
        {"store": k, "count": v["count"], "sum": round(v["sum"], 2),
         "statuses": ", ".join(f"{n}: {c}" for n, c in v["statuses"].items())}
        for k, v in sorted(stat.items(), key=lambda x: -x[1]["sum"])
    ]


def raw_order(sd_id: str, date_from: str, date_to: str) -> dict | None:
    """Сырой ответ SalesDoc по одному документу — как он приходит из API.

    Нужен, когда портал и интерфейс SalesDoc показывают у документа разные
    склады: спорить бесполезно, надо смотреть, что именно отдаёт метод. Точечно
    документ запросить нельзя (серверные фильтры getOrder не работают), поэтому
    тянем узкий период и находим документ локально."""
    want = str(sd_id or "").strip().lower()
    if not want:
        return None
    rows = call_all("getOrder", ("orders", "order"), {"filter": {
        "include": "all",
        "status": [1, 2, 3, 4, 5],
        "period": {"date": {"from": date_from, "to": date_to}},
    }})
    for o in rows:
        ids = {str(o.get("SD_id") or "").lower(), str(o.get("CS_id") or "").lower()}
        if want in ids:
            return o
    return None


def raw_payment(sd_id: str, date_from: str, date_to: str) -> dict | None:
    """Сырой ответ SalesDoc по одной операции журнала оплат.

    Нужен, чтобы увидеть все поля операции целиком: у оплаты нет склада, и
    вопрос «к какой фирме её отнести» упирается в то, есть ли там вообще хоть
    какой-то признак — касса, филиал, пользователь."""
    want = str(sd_id or "").strip().lower()
    if not want:
        return None
    rows = call_all("getPayment", ("payments", "payment"), {"filter": {
        "period": {"date": {"from": date_from, "to": date_to}},
    }})
    for p in rows:
        ids = {str(p.get("SD_id") or "").lower(), str(p.get("CS_id") or "").lower()}
        if want in ids:
            return p
    return None


def raw_orders_of_day(date_from: str, date_to: str, client_sd_id: str | None,
                      limit: int = 20) -> list[dict]:
    """Сырые документы за период (при желании — только по одной точке).

    Соседние документы того же дня показывают, чем они отличаются: у одного
    склад один, у другого другой, и видно, какое именно поле разъезжается."""
    want = str(client_sd_id or "").strip().lower()
    rows = call_all("getOrder", ("orders", "order"), {"filter": {
        "include": "all",
        "status": [1, 2, 3, 4, 5],
        "period": {"date": {"from": date_from, "to": date_to}},
    }})
    out = []
    for o in rows:
        if want:
            cli = (o.get("client") or {})
            ids = {str(cli.get("SD_id") or "").lower(), str(cli.get("CS_id") or "").lower()}
            if want not in ids:
                continue
        out.append(o)
        if len(out) >= limit:
            break
    return out


def client_debug(sd_id: str | None, code_1c: str | None,
                 date_from: str, date_to: str) -> dict:
    """Почему по клиенту не видно операций: что реально отдаёт SalesDoc.

    Тянем журналы напрямую (мимо зеркала) и считаем, сколько записей ссылаются
    на этого клиента по каждому ключу — SD_id, CS_id (F1-<SD_id>) и коду 1С.
    Показываем и «сырые» ссылки на клиента, чтобы увидеть, в каком виде
    SalesDoc его записал: чаще всего расхождение именно в этом."""
    sid = str(sd_id or "").lower()
    code = str(code_1c or "")
    period = {"period": {"date": {"from": date_from, "to": date_to}}}

    def hits(rows, getter, txn_of=None):
        by_sd = by_cs = by_code = 0
        matched = 0  # записей, совпавших хоть по одному ключу — БЕЗ двойного
        # счёта: SalesDoc пишет один и тот же идентификатор и в SD_id, и в
        # CS_id, поэтому сумма по ключам вдвое больше реального числа записей.
        samples: list = []
        # Разбивка по видам операций: баланс SalesDoc двигают не только оплаты,
        # но и списание долга, начальный остаток, конверсия. Когда операции
        # сходятся, а баланс — нет, ответ обычно именно здесь.
        by_txn: dict = {}
        for r in rows:
            cli = getter(r) or {}
            c_sd = str(cli.get("SD_id") or "").lower()
            c_cs = str(cli.get("CS_id") or "").lower()
            c_code = str(cli.get("code_1C") or "")
            match = False
            if sid and c_sd == sid:
                by_sd += 1
                match = True
            if sid and c_cs and c_cs.split("-", 1)[-1] == sid:
                by_cs += 1
                match = True
            if code and c_code and c_code == code:
                by_code += 1
                match = True
            if match:
                matched += 1
                if len(samples) < 5:
                    samples.append(cli)
                if txn_of is not None:
                    t = _to_int(txn_of(r))
                    name = PAY_TXN.get(t, str(t))
                    d = by_txn.setdefault(name, {"count": 0, "sum": 0.0})
                    d["count"] += 1
                    d["sum"] += float(r.get("amount") or 0)
        out = {"matched": matched,
               "by_sd_id": by_sd, "by_cs_id": by_cs, "by_code_1c": by_code,
               "scanned": len(rows), "client_refs": samples}
        if txn_of is not None:
            out["by_txn"] = [{"txn": k, **v} for k, v in
                             sorted(by_txn.items(), key=lambda x: -abs(x[1]["sum"]))]
        return out

    orders = call_all("getOrder", ("orders", "order"),
                      {"filter": {"include": "all", "status": [1, 2, 3, 4, 5], **period}})
    payments = call_all("getPayment", ("payments", "payment"), {"filter": period})
    try:
        defects = call_all(
            "getOrderDefect",
            ("defects", "orderDefects", "defect", "orderDefect", "orders"),
            {"filter": {"status": ALL_DEFECT_STATUSES, **period}})
    except SalesDocError:
        defects = []

    return {
        "sd_id": sd_id, "code_1C": code_1c,
        "date_from": date_from, "date_to": date_to,
        "orders": {
            **hits(orders, lambda o: o.get("client")),
            # По каким складам и статусам лежат заказы клиента: если склад не
            # привязан к фирме, его отгрузки отсекались отбором — это первое,
            # что нужно проверить, когда реализаций «ноль».
            "by_store": _client_order_stores(orders, sid, code),
        },
        "payments": hits(payments, lambda p: p.get("client"),
                         txn_of=lambda p: p.get("transactionType")),
        "defects": hits(defects, lambda d: d.get("client")),
    }


def fetch_orders_total(date_from: str, date_to: str, store_ids=None,
                       use_cache: bool = False) -> dict:
    """Сумма заказов (реализаций) за период и разбивка по клиентам (по имени).
    store_ids — набор складов выбранной фирмы (None = все)."""
    if use_cache:
        key = json.dumps(["orders_total", date_from, date_to,
                          sorted(store_ids) if store_ids else None], ensure_ascii=False)
        return _cached_result(
            key, CACHE_TTL,
            lambda: fetch_orders_total(date_from, date_to, store_ids, use_cache=False),
        )
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


def reason_window() -> tuple[str, str]:
    """Окно «всей истории» для причины расхождения. Единое для эндпоинта и
    фонового прогрева, чтобы ключи кэша совпадали (клик читал из кэша)."""
    from datetime import date
    today = date.today()
    # Конец окна — конец следующего года: в SalesDoc встречаются операции,
    # датированные будущим (обычно опечатка в годе при ручном вводе), и они
    # тоже двигают баланс клиента.
    return f"{today.year - 3}-01-01", f"{today.year + 1}-12-31"


def warm_cache(force: bool = False) -> bool:
    """Прогреть кэш тяжёлых выборок SalesDoc (баланс, клиенты, склады, 3-летние
    заказы/оплаты/возвраты), чтобы клик по сверке читал их мгновенно. Только
    чтение; ошибки не пробрасываются (фон не должен падать)."""
    if not is_configured():
        return False
    if force:
        clear_cache()
    try:
        fetch_balance(use_cache=True)
        fetch_clients(use_cache=True)
        fetch_warehouses(use_cache=True)
        df, dt = reason_window()
        fetch_reconcile_components(df, dt, store_ids=None, use_cache=True)
        fetch_client_store_orgs({}, df, dt, use_cache=True)
        return True
    except Exception:  # noqa: BLE001 — фон терпит любые сбои
        return False


_warmer_started = False


def start_background_warmer(interval: float = CACHE_TTL - 120) -> None:
    """Фоновый поток держит кэш «тёплым»: обновляет его чуть раньше, чем он
    протухнет, поэтому пользователь почти всегда попадает в готовые данные.
    Запускать один раз при старте; при выключенной интеграции — no-op."""
    global _warmer_started
    if _warmer_started or not is_configured():
        return
    _warmer_started = True

    def loop():
        while True:
            warm_cache(force=True)
            time.sleep(max(interval, 60))

    threading.Thread(target=loop, name="salesdoc-warmer", daemon=True).start()


def fetch_payments_total(date_from: str, date_to: str, use_cache: bool = False) -> dict:
    """Сумма оплат за период (по paymentDate)."""
    if use_cache:
        key = json.dumps(["payments_total", date_from, date_to], ensure_ascii=False)
        return _cached_result(
            key, CACHE_TTL,
            lambda: fetch_payments_total(date_from, date_to, use_cache=False),
        )
    params = {"filter": {"period": {"date": {"from": date_from, "to": date_to}}}}
    rows = call_all("getPayment", ("payments", "payment"), params)
    total, count = 0.0, 0
    for p in rows:
        if _to_int(p.get("transactionType")) != PAYMENT_TXN:  # только «Оплата»
            continue
        total += float(p.get("amount") or 0)
        count += 1
    return {"total": round(total, 2), "count": count}


def _client_key_sd(cli: dict | None) -> str | None:
    """Каноничный SD_id клиента (в нижнем регистре) из записи заказа/оплаты/
    возврата: сначала SD_id, иначе из CS_id вида F1-<SD_id>."""
    cli = cli or {}
    sd = str(cli.get("SD_id") or "").lower()
    if sd:
        return sd
    cs = str(cli.get("CS_id") or "").lower()
    if cs and "-" in cs:
        return cs.split("-", 1)[-1]
    return None


def fetch_reconcile_components(date_from: str, date_to: str, store_ids=None,
                              use_cache: bool = False) -> dict:
    """Массовая выгрузка для «причины расхождения» по ВСЕМ клиентам сразу: один
    проход по заказам, возвратам и оплатам с агрегацией по клиенту. Возвращает
    для каждого компонента две карты — по SD_id и по коду 1С (у части записей
    клиент записан только кодом). Реализации делятся по складу (store_ids),
    возвраты и оплаты — общие по клиенту (склад в этих методах недоступен).

    Всё последовательно (в call_all нет параллелизма), чтобы не гасить токен.
    Кэшируем ИТОГ (карты клиент → сумма, это килобайты), а не сырые выгрузки
    за 3 года: раньше они оседали в памяти сотнями мегабайт и роняли инстанс."""
    if use_cache:
        key = json.dumps(["reconcile_components", date_from, date_to,
                          sorted(store_ids) if store_ids else None], ensure_ascii=False)
        return _cached_result(
            key, CACHE_TTL,
            lambda: fetch_reconcile_components(date_from, date_to, store_ids, use_cache=False),
        )

    def accumulate(cli, amt, by_sd, by_code):
        key = _client_key_sd(cli)
        if key:
            by_sd[key] = by_sd.get(key, 0.0) + amt
            return
        code = str((cli or {}).get("code_1C") or "")
        if code:
            by_code[code] = by_code.get(code, 0.0) + amt

    period = {"period": {"date": {"from": date_from, "to": date_to}}}

    # --- Реализации (только отгруженные), с делением по складу ---
    sales_sd: dict[str, float] = {}
    sales_code: dict[str, float] = {}
    oparams = {"filter": {"include": "all", "status": sorted(SHIPPED_STATUSES), **period}}
    for o in call_all("getOrder", ("orders", "order"), oparams):
        if o.get("status") not in SHIPPED_STATUSES:
            continue
        if not _store_ok(o.get("store"), store_ids):
            continue
        amt = float(o.get("totalSummaAfterDiscount") or o.get("totalSumma") or 0)
        accumulate(o.get("client"), amt, sales_sd, sales_code)

    # --- Возвраты (getOrderDefect; склад недоступен → общие по клиенту) ---
    returns_sd: dict[str, float] = {}
    returns_code: dict[str, float] = {}
    try:
        for r in call_all(
            "getOrderDefect",
            ("defects", "orderDefects", "defect", "orderDefect", "orders"),
            {"filter": {"status": ALL_DEFECT_STATUSES, **period}},
        ):
            amt = float(r.get("summa") or r.get("totalSumma") or 0)
            accumulate(r.get("client"), amt, returns_sd, returns_code)
    except SalesDocError:
        pass

    # --- Оплаты (тип 3) и «Возврат с полки» (тип 9) из журнала оплат ---
    # В SalesDoc возврат товара — это операция «Возврат с полки» в getPayment,
    # а не документ getOrderDefect. Считаем её возвратом, чтобы сходилось с 1С.
    pay_sd: dict[str, float] = {}
    pay_code: dict[str, float] = {}
    for p in call_all("getPayment", ("payments", "payment"), {"filter": period}):
        txn = _to_int(p.get("transactionType"))
        amt = float(p.get("amount") or 0)
        if txn == PAYMENT_TXN:
            accumulate(p.get("client"), amt, pay_sd, pay_code)
        elif txn == SHELF_RETURN_TXN:
            accumulate(p.get("client"), amt, returns_sd, returns_code)

    return {
        "sales": {"sd": sales_sd, "code": sales_code},
        "returns": {"sd": returns_sd, "code": returns_code},
        "payments": {"sd": pay_sd, "code": pay_code},
    }
