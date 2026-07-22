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


def call_all(method: str, key: str, params: dict | None = None, page_limit: int = 1000) -> list:
    """Собирает все страницы GET-метода в один список по ключу key в result."""
    params = dict(params or {})
    params.setdefault("limit", page_limit)
    out: list = []
    page = 1
    while True:
        params["page"] = page
        result, pagination = call(method, params)
        chunk = result.get(key, []) if isinstance(result, dict) else []
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


def fetch_orders_total(date_from: str, date_to: str) -> dict:
    """Сумма заказов (реализаций) за период и разбивка по клиентам (по имени)."""
    params = {"filter": {"include": "all", "period": {"date": {"from": date_from, "to": date_to}}}}
    rows = call_all("getOrder", "orders", params) or call_all("getOrder", "order", params)
    total = 0.0
    by_client: dict[str, float] = {}
    for o in rows:
        amt = float(o.get("totalSummaAfterDiscount") or o.get("totalSumma") or 0)
        total += amt
        cli = (o.get("client") or {})
        name = cli.get("clientName") or cli.get("clientLegalName") or "—"
        by_client[name] = by_client.get(name, 0.0) + amt
    return {"total": round(total, 2), "count": len(rows), "by_client": by_client}


def fetch_payments_total(date_from: str, date_to: str) -> dict:
    """Сумма оплат за период (по paymentDate)."""
    params = {"filter": {"period": {"date": {"from": date_from, "to": date_to}}}}
    rows = call_all("getPayment", "payments", params) or call_all("getPayment", "payment", params)
    total = 0.0
    for p in rows:
        total += float(p.get("amount") or 0)
    return {"total": round(total, 2), "count": len(rows)}
