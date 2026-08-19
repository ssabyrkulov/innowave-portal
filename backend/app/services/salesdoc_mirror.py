"""Зеркало SalesDoc в нашей базе: держим копию журналов заказов и оплат.

Зачем. SalesDoc не соблюдает серверный фильтр по клиенту (замер: 1173 записи
и с фильтром `params.client`, и без него), поэтому «показать одного клиента»
раньше означало выкачать журнал за 3 года — отсюда и тормоза при клике.

Как. Держим копию журналов у себя и обновляем её:
  • полная выгрузка — при первом запуске и раз в сутки (журналы небольшие,
    ~15 секунд на оба);
  • догрузка изменений — часто и дёшево, по filter.period.dateUpdate
    (за 7 дней меняется ~1600 записей ≈ 1–2 за десять минут).

Что это даёт. Карточка клиента и сверка читаются обычным SQL — мгновенно,
без обращения к SalesDoc; данные переживают перезапуск сервиса; токен
SalesDoc не дёргается на каждый клик.
"""

import re
import threading
import time
from datetime import date, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from ..database import SessionLocal
from . import salesdoc

# Раз в минуту — дежурная проверка: изменилось ли что-нибудь и сходится ли
# количество записей. Правки и новые записи видны по счётчику изменений,
# удаления — по расхождению количества (об удалении SalesDoc не сообщает,
# запись просто исчезает). В обоих случаях синхронизация запускается сразу,
# так что и удаление доезжает до сайта в течение минуты.
# Полная выгрузка раз в час — страховка на случай, если количество совпало
# случайно (например, одну запись добавили, а другую удалили).
# Частота почти ничего не стоит: сначала идёт дежурная проверка (одна запись),
# и данные выгружаются, только если счётчик изменений больше нуля. Токену это
# не вредит — его гасит лишь повторный вход, а мы работаем на постоянном.
FULL_EVERY = 3600  # 1 час
DELTA_EVERY = 60   # 1 минута — как часто просыпается фоновый цикл

# Как часто обновлять КАЖДЫЙ вид данных. Раньше цикл раз в минуту дёргал всё
# подряд, и это было дорого не по трафику, а по базе: у справочников нет дельты,
# поэтому клиенты (≈1300 точек), товары, агенты, склады, перемещения и остатки
# перекладывались целиком каждую минуту — тысячи SQL-запросов в минуту на
# небольшой Postgres, да ещё и в одном процессе с веб-сервером (GIL), из-за
# чего страницы заметно тормозили. Меняются эти справочники редко, поэтому у
# каждого вида теперь своя частота: движение документов — часто, справочники —
# редко. Отсчёт ведём по last_delta_at, он переживает перезапуск сервиса.
KIND_EVERY = {
    "orders": 60,        # документы меняются постоянно
    "payments": 60,
    "clients": 300,      # долги точек — раз в 5 минут достаточно
    "stock": 900,        # остатки складов
    "warehouses": 3600,  # справочники — раз в час
    "agents": 3600,
    "products": 3600,
    "movements": 3600,
}
# С каким запасом берём дельту: перекрываем прошлую синхронизацию, чтобы не
# потерять записи, попавшие на границу окна.
DELTA_OVERLAP = timedelta(hours=6)
# Глубина истории зеркала (долг накопительный — берём с запасом).
HISTORY_YEARS = 3

_sync_lock = threading.Lock()
# Отложенный запрос на полную выгрузку: ставится, когда «Обновить» нажали во
# время идущей синхронизации. Помнит и НАБОР видов: кнопка обновляет только
# документы (заказы и оплаты), «обновить всё» — весь список.
#   None            — запроса нет
#   "all"           — полная по всем видам
#   frozenset({...}) — полная только по этим видам
_pending_full = None
_pending_lock = threading.Lock()
# Что синхронизируется прямо сейчас. Без этого надпись «идёт обновление…» не
# отличает быстрое обновление документов по кнопке от часовой фоновой выгрузки
# визитов и справочников — а ждать их приходится совсем по-разному.
_current: dict = {"kind": None, "full": False, "started": None}

# Виды, которые обновляет кнопка «Обновить». Всё остальное (визиты ~100 тыс.
# строк, товары, остатки, справочники) человеку в этот момент не нужно и
# превращает нажатие в многоминутное ожидание — их обновляет фоновый цикл.
DOC_KINDS = ("orders", "payments")


def _request_full(kinds=None) -> None:
    """Запомнить просьбу о полной выгрузке (пришла, пока шла другая)."""
    global _pending_full
    with _pending_lock:
        want = "all" if not kinds else frozenset(kinds)
        if _pending_full is None:
            _pending_full = want
        elif _pending_full == "all" or want == "all":
            _pending_full = "all"
        else:
            _pending_full = _pending_full | want


def _take_pending():
    """Забрать отложенный запрос (и очистить его)."""
    global _pending_full
    with _pending_lock:
        p, _pending_full = _pending_full, None
        return p
_worker_started = False


def _window() -> tuple[str, str]:
    """Окно выгрузки: три года назад — и до конца СЛЕДУЮЩЕГО года.

    Конец окна берём с большим запасом не «на всякий случай», а по факту: в
    SalesDoc попадаются операции, датированные будущим. Обычно это опечатка в
    годе при ручном вводе (платёж от 02.09.2025 занесли как 02.09.2026), и
    такая запись всё равно двигает баланс клиента. Пока окно кончалось
    сегодняшним днём, мы её не видели — и получалось «операции сходятся, а
    баланс нет», хотя причина была именно в ней.
    """
    today = date.today()
    return f"{today.year - HISTORY_YEARS}-01-01", f"{today.year + 1}-12-31"


def _state(db: Session, kind: str) -> models.SalesDocSyncState:
    row = db.query(models.SalesDocSyncState).filter_by(kind=kind).first()
    if row is None:
        row = models.SalesDocSyncState(kind=kind)
        db.add(row)
        db.flush()
    return row


def _day(value) -> date | None:
    s = str(value or "").split(" ")[0]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


# Технические коды-хвосты вида z8_1249 / e4_1252 / (j1_129), которыми SalesDoc
# и 1С помечают контрагентов по-разному и из-за которых точное имя не
# совпадает. Для сопоставления их убираем.
_CODE_RE = re.compile(r"[a-zа-я]{0,3}\d*_\d+", re.IGNORECASE)


def match_key(name: str) -> str:
    """Ключ сопоставления клиента по имени: без кодов, скобок, кавычек и лишних
    пробелов. Запасной вариант, если не удалось связать по ИД SalesDoc."""
    s = (name or "").lower().replace("ё", "е")
    s = re.sub(r"\([^)]*\)", " ", s)          # (…)
    s = _CODE_RE.sub(" ", s)                   # z8_1249 и т.п.
    # Любую пунктуацию (кавычки, дефис и пр.) сводим к пробелу — «Ош-Нурзаман»
    # и «Ош Нурзаман» должны совпасть.
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def extract_sd_id(name: str) -> str | None:
    """Достаёт ИД клиента SalesDoc (напр. z8_1249) из имени контрагента 1С —
    самый надёжный ключ, т.к. он присутствует и там, и там."""
    m = _CODE_RE.search(name or "")
    return m.group(0).lower() if m else None


def _ref_id(v) -> str | None:
    """SD_id вложенной ссылки (агент, склад, направление) — если она словарь."""
    return str(v.get("SD_id") or "").lower() or None if isinstance(v, dict) else None


def _ref_name(v) -> str | None:
    return (v.get("name") or None) if isinstance(v, dict) else None


def _client_keys(cli: dict | None) -> tuple[str | None, str | None]:
    """SD_id клиента (в т.ч. вытащенный из CS_id вида F1-<SD_id>) и код 1С."""
    cli = cli or {}
    sd = str(cli.get("SD_id") or "").lower() or None
    if not sd:
        cs = str(cli.get("CS_id") or "").lower()
        if cs and "-" in cs:
            sd = cs.split("-", 1)[-1]
    code = str(cli.get("code_1C") or "") or None
    return sd, code


def _log_order_change(db: Session, sd_id: str, store_sd_id) -> None:
    """Записывает смену склада документа, если она случилась.

    SalesDoc такие правки нигде не хранит: в истории документа остаётся первый
    склад, а изменения не сохраняются — из-за этого и вышла путаница, когда
    портал и журнал SalesDoc показывали у одного документа разные склады. Раз
    мы раз в час перечитываем журнал целиком, правку видно нам — и только у нас
    эта история и может остаться.

    Смену статуса не пишем: Новый → Отправлен → Доставлен → Закрыт проходит
    каждый документ, и этот шум похоронил бы редкие правки склада."""
    row = db.query(models.SalesDocOrder).filter_by(sd_id=sd_id).first()
    if row is None:
        return  # новый документ — сравнивать не с чем
    old, new = row.store_sd_id, store_sd_id
    if old is None or new is None or str(old) == str(new):
        return
    db.add(models.SalesDocOrderChange(
        order_sd_id=sd_id, field="store",
        old_value=str(old), new_value=str(new),
        doc_date=row.date, client_sd_id=row.client_sd_id,
    ))


def _existing(db: Session, model) -> dict:
    """Все записи зеркала этого вида по sd_id — одним запросом.

    Нужен для пакетной перекладки: раньше _upsert на каждую запись делал
    отдельный SELECT, и выгрузка 1300 точек стоила 1300 запросов к базе."""
    return {row.sd_id: row for row in db.query(model).all()}


def _upsert(db: Session, model, sd_id: str, values: dict,
            cache: dict | None = None) -> None:
    """Обновляем запись зеркала по SD_id либо создаём новую.

    cache — заранее прочитанные записи (см. _existing). Без него делается
    точечный SELECT: так остаются работать редкие одиночные вызовы."""
    row = cache.get(sd_id) if cache is not None else \
        db.query(model).filter_by(sd_id=sd_id).first()
    if row is None:
        row = model(sd_id=sd_id)
        db.add(row)
        if cache is not None:
            cache[sd_id] = row
    changed = False
    for k, v in values.items():
        if getattr(row, k) != v:
            setattr(row, k, v)
            changed = True
    # synced_at трогаем только у изменившихся строк: иначе каждая выгрузка
    # переписывала бы всю таблицу целиком, даже когда в SalesDoc ничего не
    # поменялось.
    if changed or row.synced_at is None:
        row.synced_at = datetime.utcnow()


def _order_filter(date_from: str, date_to: str, updated_since: str | None) -> dict:
    """Заказы: все статусы (нужны и отменённые — их показываем зачёркнутыми)."""
    period = ({"dateUpdate": {"from": updated_since, "to": date_to}}
              if updated_since else {"date": {"from": date_from, "to": date_to}})
    return {"filter": {"include": "all", "status": [1, 2, 3, 4, 5], "period": period}}


def _cashbox(payment: dict) -> tuple[str | None, str | None]:
    """Касса операции: (SD_id, название). Имя ключа в ответе SalesDoc не
    зафиксировано в документации — перебираем известные написания, чтобы
    поле не потерялось молча."""
    for key in ("cashbox", "cashBox", "cash_box", "cashdesk", "cashDesk", "kassa"):
        box = payment.get(key)
        if isinstance(box, dict) and (box.get("SD_id") or box.get("name")):
            sid = str(box.get("SD_id") or "").lower() or None
            return sid, (box.get("name") or None)
        if isinstance(box, str) and box.strip():
            return None, box.strip()
    return None, None


def _payment_orders(payment: dict) -> str | None:
    """ИД заказов, которые закрывает оплата (поле orders), через запятую.

    Склада у оплаты нет, а у заказа есть — значит фирму оплаты можно узнать
    через заказы, которые она гасит. Другого признака в операции не оказалось:
    касса в ответе одна на весь аккаунт («1»)."""
    ids: list[str] = []
    for it in payment.get("orders") or []:
        v = it.get("SD_id") or it.get("CS_id") if isinstance(it, dict) else it
        if v:
            v = str(v).lower()
            if v not in ids:
                ids.append(v)
    return ",".join(ids) or None


def _payment_filter(date_from: str, date_to: str, updated_since: str | None) -> dict:
    """Оплаты: весь журнал — в нём же лежат «Возврат с полки» (тип 9)."""
    period = ({"dateUpdate": {"from": updated_since, "to": date_to}}
              if updated_since else {"date": {"from": date_from, "to": date_to}})
    return {"filter": {"period": period}}


def changed_count(method: str, updated_since: str) -> int | None:
    """Сколько записей изменилось с указанной даты — БЕЗ выгрузки самих данных.

    Просим одну запись (limit=1): SalesDoc возвращает в pagination общее число
    подходящих. Это «дежурная проверка»: если ноль — синхронизировать нечего и
    мы не тратим ни трафик, ни токен. None — счётчик получить не удалось."""
    _, dt = _window()
    params = {
        "limit": 1, "page": 1,
        "filter": {"period": {"dateUpdate": {"from": updated_since, "to": dt}}},
    }
    if method == "getOrder":
        params["filter"]["include"] = "all"
        params["filter"]["status"] = [1, 2, 3, 4, 5]
    try:
        _, pagination = salesdoc.call(method, params)
    except salesdoc.SalesDocError:
        return None
    if not pagination:
        return None
    total = pagination.get("total")
    return int(total) if isinstance(total, (int, float, str)) and str(total).isdigit() else None


def _drop_missing(db: Session, model, seen: set, date_from: date, date_to: date) -> int:
    """Убирает из зеркала записи, которых больше нет в SalesDoc.

    Вызывается только после ПОЛНОЙ выгрузки: тогда `seen` — это весь набор
    существующих записей за период, и всё, чего в нём нет, в SalesDoc удалено
    (например, ошибочную оплату убрали вручную). Без этого исправления в
    SalesDoc не доезжали бы до сайта — расхождение висело бы вечно."""
    stale = [
        row.id for row in db.query(model.id, model.sd_id)
        .filter(model.date >= date_from, model.date <= date_to).all()
        if row.sd_id not in seen
    ]
    if stale:
        db.query(model).filter(model.id.in_(stale)).delete(synchronize_session=False)
    return len(stale)


def total_count(method: str) -> int | None:
    """Сколько всего записей в SalesDoc за наш период — БЕЗ выгрузки данных.

    Нужно, чтобы ловить удаления. Удалённая запись просто исчезает из ответов
    SalesDoc: пометки «удалена» нет, в счётчик изменений она не попадает.
    Единственный способ заметить пропажу — сравнить количество: если у нас
    записей больше, чем в SalesDoc, значит что-то убрали."""
    df, dt = _window()
    params = {"limit": 1, "page": 1,
              "filter": {"period": {"date": {"from": df, "to": dt}}}}
    if method == "getOrder":
        params["filter"]["include"] = "all"
        params["filter"]["status"] = [1, 2, 3, 4, 5]
    try:
        _, pagination = salesdoc.call(method, params)
    except salesdoc.SalesDocError:
        return None
    total = (pagination or {}).get("total")
    return int(total) if str(total).isdigit() else None


def mirror_count(db: Session, model) -> int:
    """Сколько записей этого вида в зеркале за тот же период."""
    df, dt = _window()
    return (
        db.query(model)
        .filter(model.date >= date.fromisoformat(df),
                model.date <= date.fromisoformat(dt))
        .count()
    )


def sync_orders(db: Session, updated_since: str | None = None) -> int:
    df, dt = _window()
    rows = salesdoc.call_all("getOrder", ("orders", "order"),
                             _order_filter(df, dt, updated_since))
    cache = _existing(db, models.SalesDocOrder)
    seen: set = set()
    for o in rows:
        sd_id = str(o.get("SD_id") or o.get("CS_id") or "").strip()
        if not sd_id:
            continue
        seen.add(sd_id)
        cli_sd, cli_code = _client_keys(o.get("client"))
        store_sd_id = str((o.get("store") or {}).get("SD_id") or "").lower() or None
        _log_order_change(db, sd_id, store_sd_id)
        _upsert(db, models.SalesDocOrder, sd_id, cache=cache, values={
            "client_sd_id": cli_sd,
            "client_code_1c": cli_code,
            "store_sd_id": store_sd_id,
            "date": _day(o.get("dateDocument") or o.get("dateCreate")),
            "status": o.get("status"),
            "amount": float(o.get("totalSummaAfterDiscount") or o.get("totalSumma") or 0),
            "returns_amount": float(o.get("totalReturnsSumma") or 0),
            "code_1c": o.get("code_1C"),
            "agent_sd_id": _ref_id(o.get("agent")),
            "agent_name": _ref_name(o.get("agent")),
        })
    if updated_since is None:  # полная выгрузка — вычищаем удалённое в SalesDoc
        db.flush()
        _drop_missing(db, models.SalesDocOrder, seen,
                      date.fromisoformat(df), date.fromisoformat(dt))
    return len(rows)


def sync_payments(db: Session, updated_since: str | None = None) -> int:
    df, dt = _window()
    rows = salesdoc.call_all("getPayment", ("payments", "payment"),
                             _payment_filter(df, dt, updated_since))
    ptypes = salesdoc.fetch_payment_types()
    cache = _existing(db, models.SalesDocPayment)
    seen: set = set()
    for p in rows:
        sd_id = str(p.get("SD_id") or p.get("CS_id") or "").strip()
        if not sd_id:
            continue
        seen.add(sd_id)
        cli_sd, cli_code = _client_keys(p.get("client"))
        pt = p.get("paymentType") or {}
        box = _cashbox(p)
        _upsert(db, models.SalesDocPayment, sd_id, cache=cache, values={
            "client_sd_id": cli_sd,
            "client_code_1c": cli_code,
            "date": _day(p.get("paymentDate")),
            "amount": float(p.get("amount") or 0),
            "txn": salesdoc._to_int(p.get("transactionType")),
            "type_name": (
                ptypes.get(("code", str(pt.get("code_1C"))))
                or ptypes.get(("sd", str(pt.get("SD_id") or "").lower()))
                or None
            ),
            "type_sd_id": str(pt.get("SD_id") or pt.get("CS_id") or "").lower() or None,
            "type_code_1c": str(pt.get("code_1C") or "") or None,
            "cashbox_sd_id": box[0],
            "cashbox_name": box[1],
            "order_ids": _payment_orders(p),
            "trade_sd_id": str((p.get("trade") or {}).get("SD_id") or "").lower() or None
            if isinstance(p.get("trade"), dict) else None,
            "code_1c": str(p.get("code_1C") or "") or None,
        })
    if updated_since is None:  # полная выгрузка — вычищаем удалённое в SalesDoc
        db.flush()
        _drop_missing(db, models.SalesDocPayment, seen,
                      date.fromisoformat(df), date.fromisoformat(dt))
    return len(rows)


def sync_clients(db: Session, updated_since: str | None = None) -> int:
    """Справочник точек SalesDoc и их текущий долг — в зеркало.

    Оба метода отдают всё разом и стоят по одному запросу, поэтому делаем
    всегда полностью (дельты у них нет). Благодаря этому список сверки
    читается только из нашей базы и открывается мгновенно."""
    clients = salesdoc.fetch_clients()
    balance = salesdoc.fetch_balance()
    debt_by_id = {}
    for b in balance:
        sid = (b["sd_id"] or "").lower()
        if sid:
            debt_by_id[sid] = debt_by_id.get(sid, 0.0) + b["debt"]

    seen: set = set()
    cache = _existing(db, models.SalesDocClient)
    # Закрепление точек за агентами: снятие агента с точки в SalesDoc — это
    # исчезновение записи, а не флаг, поэтому набор сравниваем целиком. Но
    # переписывать таблицу вслепую каждый раз нельзя — раньше она полностью
    # удалялась и создавалась заново при каждой синхронизации. Теперь считаем
    # желаемый набор и трогаем базу, только если он изменился.
    want: set = set()
    for c in clients:
        sid = (c["sd_id"] or "").lower()
        if not sid:
            continue
        seen.add(sid)
        _upsert(db, models.SalesDocClient, sid, cache=cache, values={
            "code_1c": str(c["code_1C"]) if c["code_1C"] else None,
            "name": c["name"] or "",
            "debt": round(debt_by_id.get(sid, 0.0), 2),
            "in_balance": sid in debt_by_id,
            # Признак активности приходил из getClient, но в зеркало не
            # писался — и неактивные точки участвовали в сверке наравне с
            # рабочими, ничем от них не отличаясь. Их не видно в списке
            # клиентов SalesDoc, поэтому строка про такую точку выглядит как
            # выдумка портала.
            "active": bool(c.get("active", True)),
        })
        for a in c.get("agents") or []:
            want.add((sid, a["sd_id"],
                      ",".join(str(d) for d in a.get("days") or []) or None))

    have = {(r.client_sd_id, r.agent_sd_id, r.days)
            for r in db.query(models.SalesDocClientAgent).all()}
    if have != want:
        db.query(models.SalesDocClientAgent).delete(synchronize_session=False)
        db.flush()
        for cli, agent, days in want:
            db.add(models.SalesDocClientAgent(
                client_sd_id=cli, agent_sd_id=agent, days=days))
    db.flush()
    # Точки, которых больше нет в SalesDoc, убираем из зеркала.
    stale = [row.id for row in db.query(models.SalesDocClient.id,
                                        models.SalesDocClient.sd_id).all()
             if row.sd_id not in seen]
    if stale:
        db.query(models.SalesDocClient).filter(
            models.SalesDocClient.id.in_(stale)).delete(synchronize_session=False)
    return len(clients)


def clients_for_reconcile(db: Session) -> list[dict]:
    """Точки SalesDoc из зеркала — в том же виде, что раньше приходил из API."""
    return [
        {"sd_id": c.sd_id, "code_1C": c.code_1c, "name": c.name,
         "debt": float(c.debt or 0), "active": bool(c.active)}
        for c in db.query(models.SalesDocClient).all()
    ]


def client_store_orgs(db: Session) -> dict[str, dict]:
    """Фирма каждой точки SalesDoc — по складам её реализаций, из зеркала.

    Раньше это считалось живым запросом в SalesDoc: выкачивался весь журнал
    заказов за 4 года только ради карты «клиент → фирма». Запрос тяжёлый, и
    когда он падал, привязка молча становилась пустой — тогда точки «только SD»
    показывались сразу в обеих фирмах. Теперь берём то же самое из зеркала:
    мгновенно и без сбоев.

    Отменённые документы не в счёт: точка не становится клиентом фирмы от того,
    что на её склад однажды выписали и тут же отменили заказ. Именно так и
    вышло, что точка со всеми отгрузками Innowave считалась ещё и клиентом
    гигиены.

    Возвращает {sd_id клиента: {"orgs": множество фирм по складам с привязкой,
    "stores": {фирма: [склады с количеством и периодом]}, "unmapped": то же по
    складам без привязки}}. Отсутствие клиента в словаре означает, что
    реализаций у точки нет вовсе — фирму определить не по чему.
    """
    stores = {
        s.store_id.lower(): s
        for s in db.query(models.SalesDocStore).all() if s.store_id
    }
    out: dict[str, dict] = {}
    from sqlalchemy import func

    rows = (
        db.query(models.SalesDocOrder.client_sd_id,
                 models.SalesDocOrder.store_sd_id,
                 func.count(models.SalesDocOrder.id),
                 func.min(models.SalesDocOrder.date),
                 func.max(models.SalesDocOrder.date))
        .filter(models.SalesDocOrder.status != salesdoc.CANCELLED_STATUS)
        .group_by(models.SalesDocOrder.client_sd_id,
                  models.SalesDocOrder.store_sd_id)
        .all()
    )
    for cli, store_id, cnt, first, last in rows:
        if not cli:
            continue
        entry = out.setdefault(
            str(cli).lower(), {"orgs": set(), "stores": {}, "unmapped": []})
        store = stores.get(str(store_id or "").lower())
        if store is None:
            # Склада нет в справочнике зеркала (новый, переименованный или
            # удалённый в SalesDoc). Раньше такой заказ молча выбрасывался —
            # и если ВСЕ заказы точки шли через такой склад, у неё оставался
            # пустой набор фирм, а строка «только SD» пропадала из сверки при
            # любой выбранной фирме: долг переставал быть виден вообще.
            # Считаем это «складом без фирмы» и показываем с пояснением.
            entry["unmapped"].append({
                "name": f"склад {store_id or '—'} (нет в справочнике складов)",
                "count": int(cnt or 0),
                "first": first and first.isoformat(),
                "last": last and last.isoformat(),
            })
            continue
        name = store.name or store.store_id
        # Количество и период по каждому складу: «склад этой фирмы» без цифр
        # выглядит как приговор, а на деле там может быть одна старая отгрузка.
        item = {"name": name, "count": int(cnt or 0),
                "first": first and first.isoformat(),
                "last": last and last.isoformat()}
        if store.organization:
            entry["orgs"].add(store.organization)
            entry["stores"].setdefault(store.organization, []).append(item)
        else:
            entry["unmapped"].append(item)
    return out


def orders_by_store(db: Session) -> dict[str, dict]:
    """Реализации по складам за всю глубину зеркала: сколько документов, на
    какую сумму и с какой по какую дату. Отвечает на вопрос «по каким складам
    вообще шли отгрузки» и заодно показывает, какие склады мёртвые.

    Отгруженные считаем отдельно от общего числа: отменённый документ на складе
    лежит, но ни в какую сумму не идёт. Без этого разделения склад с двумя
    отменёнными заказами выглядел как рабочий на 25 200 — и решение о привязке
    принималось по несуществующим деньгам."""
    from sqlalchemy import case, func

    shipped = case(
        (models.SalesDocOrder.status.in_(list(salesdoc.SHIPPED_STATUSES)), 1),
        else_=0,
    )
    rows = (
        db.query(
            models.SalesDocOrder.store_sd_id,
            func.count(models.SalesDocOrder.id),
            func.sum(shipped),
            func.sum(shipped * models.SalesDocOrder.amount),
            func.min(models.SalesDocOrder.date),
            func.max(models.SalesDocOrder.date),
        )
        .group_by(models.SalesDocOrder.store_sd_id)
        .all()
    )
    return {
        (sid or ""): {
            "count": int(cnt or 0),
            "shipped_count": int(ship_cnt or 0),
            "amount": round(float(ship_sum or 0), 2),
            "first": first and first.isoformat(),
            "last": last and last.isoformat(),
        }
        for sid, cnt, ship_cnt, ship_sum, first, last in rows
    }


def store_orders(db: Session, store_id: str, limit: int = 500) -> list[dict]:
    """Реализации конкретного склада: дата, номер, точка, сумма, статус.

    Нужно, чтобы на вопрос «что вообще лежит на этом складе» отвечал портал, а
    не ручной поиск в SalesDoc. Пустой store_id — заказы, у которых склад не
    проставлен вовсе."""
    sid = (store_id or "").strip().lower()
    q = db.query(models.SalesDocOrder)
    q = q.filter(models.SalesDocOrder.store_sd_id == sid) if sid else \
        q.filter(models.SalesDocOrder.store_sd_id.is_(None))
    names = {c.sd_id: c.name for c in db.query(models.SalesDocClient).all()}
    return [
        {
            "date": r.date and r.date.isoformat(),
            "sd_id": r.sd_id,
            "doc_number": r.code_1c,
            "client": names.get(r.client_sd_id or "", r.client_sd_id or r.client_code_1c or ""),
            "amount": round(float(r.amount or 0), 2),
            "status": r.status,
            "status_label": salesdoc.ORDER_STATUS.get(r.status, str(r.status)),
            "counted": r.status in salesdoc.SHIPPED_STATUSES,
        }
        for r in q.order_by(models.SalesDocOrder.date.desc()).limit(limit).all()
    ]


def order_changes(db: Session, limit: int = 200) -> list[dict]:
    """История смен склада и статуса документов — та, которой нет в SalesDoc."""
    stores = {
        s.store_id.lower(): (s.name or s.store_id)
        for s in db.query(models.SalesDocStore).all() if s.store_id
    }
    names = {c.sd_id: c.name for c in db.query(models.SalesDocClient).all()}

    def label(field: str, value: str | None) -> str:
        if value is None:
            return "—"
        if field == "store":
            return stores.get(value.lower(), value)
        return salesdoc.ORDER_STATUS.get(_int(value), value)

    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    rows = (
        db.query(models.SalesDocOrderChange)
        .order_by(models.SalesDocOrderChange.noticed_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "order_sd_id": c.order_sd_id,
            "field": c.field,
            "field_label": "склад" if c.field == "store" else "статус",
            "old": label(c.field, c.old_value),
            "new": label(c.field, c.new_value),
            "doc_date": c.doc_date and c.doc_date.isoformat(),
            "client": names.get(c.client_sd_id or "", c.client_sd_id or ""),
            "noticed_at": c.noticed_at and c.noticed_at.isoformat(),
        }
        for c in rows
    ]


def payment_split_stats(db: Session) -> dict:
    """Чем вообще можно делить оплаты по фирмам — на живых цифрах.

    У оплаты нет склада. Кандидатов в признак фирмы ровно три: касса, поле
    trade и связь с заказами (orders). Гадать бесполезно — считаем, сколько
    оплат каждый признак реально покрывает. Если признак пустой у большинства
    записей, делить по нему нельзя, и это надо знать до того, как строить на
    нём логику."""
    from sqlalchemy import func

    P = models.SalesDocPayment
    q = db.query(P).filter(P.txn == salesdoc.PAYMENT_TXN)
    total = q.count()
    linked = q.filter(P.order_ids.isnot(None), P.order_ids != "").count()

    def group(col):
        rows = (
            db.query(col, func.count(P.id))
            .filter(P.txn == salesdoc.PAYMENT_TXN)
            .group_by(col).all()
        )
        return sorted(
            [{"value": v or "(пусто)", "count": int(c or 0)} for v, c in rows],
            key=lambda x: -x["count"],
        )

    # По годам: связь могла появиться недавно (её проставляет обмен с 1С).
    by_year: dict[str, dict] = {}
    for d, oids in db.query(P.date, P.order_ids).filter(
            P.txn == salesdoc.PAYMENT_TXN).all():
        y = str(d.year) if d else "—"
        e = by_year.setdefault(y, {"year": y, "total": 0, "linked": 0})
        e["total"] += 1
        if oids:
            e["linked"] += 1

    return {
        "total": total,
        "linked": linked,
        "unlinked": total - linked,
        "by_year": sorted(by_year.values(), key=lambda x: x["year"], reverse=True),
        "cashboxes": group(P.cashbox_sd_id),
        "trades": group(P.trade_sd_id),
        "types": group(P.type_name),
    }


def cashboxes(db: Session) -> list[dict]:
    """Кассы из журнала оплат: сколько операций и на какую сумму на каждой.

    Нужно, чтобы понять, делятся ли кассы SalesDoc по фирмам. Если делятся —
    касса станет вторым источником привязки: у оплаты склада нет, и для точки,
    у которой в SalesDoc одни оплаты, фирму больше определять не по чему.
    """
    from sqlalchemy import func

    rows = (
        db.query(
            models.SalesDocPayment.cashbox_sd_id,
            models.SalesDocPayment.cashbox_name,
            func.count(models.SalesDocPayment.id),
            func.sum(models.SalesDocPayment.amount),
        )
        .group_by(models.SalesDocPayment.cashbox_sd_id,
                  models.SalesDocPayment.cashbox_name)
        .all()
    )
    out = [
        {
            "cashbox_id": sid,
            "name": name or (sid or "(касса не указана)"),
            "count": int(cnt or 0),
            "amount": round(float(total or 0), 2),
        }
        for sid, name, cnt, total in rows
    ]
    out.sort(key=lambda x: -x["count"])
    return out


VISITS_BACK_DAYS = 400   # сколько истории визитов держим
VISITS_FWD_DAYS = 60     # план: будущие визиты (planned=1) тоже нужны


def sync_visits(db: Session, updated_since: str | None = None) -> int:
    """Зеркало визитов агентов (getVisit).

    Записей ~100 тысяч, поэтому визиты обновляются только при полной
    синхронизации (раз в час), а не минутной дельтой. Работает ли у getVisit
    фильтр периода — неизвестно заранее: проверяем сравнением счётчиков и,
    если сервер фильтр игнорирует, честно выкачиваем всё."""
    import hashlib as _h

    today = date.today()
    df = (today - timedelta(days=VISITS_BACK_DAYS)).isoformat()
    dt = (today + timedelta(days=VISITS_FWD_DAYS)).isoformat()
    params = {"filter": {"period": {"date": {"from": df, "to": dt}}}}
    try:
        _, pg_all = salesdoc.call("getVisit", {"limit": 1, "page": 1})
        _, pg_win = salesdoc.call("getVisit", {"limit": 1, "page": 1, **params})
        t_all = int((pg_all or {}).get("total") or 0)
        t_win = int((pg_win or {}).get("total") or 0)
        filter_works = bool(t_all and t_win and t_win < t_all)
    except salesdoc.SalesDocError:
        filter_works = False

    rows = salesdoc.call_all("getVisit", ("visit", "visits"),
                             params if filter_works else None)
    parsed, seen = [], set()
    for v in rows:
        at = None
        for k in ("date", "start_date"):
            s = str(v.get(k) or "").strip()
            if not s:
                continue
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    at = datetime.strptime(s[:19 if " " in s else 10], fmt)
                    break
                except ValueError:
                    continue
            if at:
                break
        if at is None:
            continue
        # Время визита. Поле date приходит без времени (полночь у всех
        # записей), но если время лежит отдельным полем — подбираем его.
        # Не найдётся — панель честно покажет «—», а не фиктивные 00:00.
        if at.time() == datetime.min.time():
            for k in ("time", "start_time", "time_start", "timeStart",
                      "visit_time", "check_in", "checkin"):
                m = re.match(r"^(\d{1,2}):(\d{2})",
                             str(v.get(k) or "").strip())
                if m:
                    at = at.replace(hour=int(m.group(1)) % 24,
                                    minute=int(m.group(2)) % 60)
                    break
        key = _h.sha1(
            f"{v.get('agent_id')}|{v.get('client_id')}|{v.get('date')}".encode()
        ).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        parsed.append(models.SalesDocVisit(
            key=key,
            agent_sd_id=str(v.get("agent_id") or "").lower() or None,
            agent_name=v.get("agent_name") or None,
            client_sd_id=str(v.get("client_id") or "").lower() or None,
            client_name=v.get("client_name") or None,
            at=at,
            planned=bool(v.get("planned")),
            visited=bool(v.get("visited")),
            reject=str(v.get("reject")) if v.get("reject") not in (None, "", 0) else None,
            has_order=bool(v.get("has_order")),
            order_summa=float(v.get("order_summa") or 0),
        ))

    # Снапшот-замена: сначала выгрузили (выше), потом чистим и вставляем.
    q = db.query(models.SalesDocVisit)
    if filter_works:
        q = q.filter(models.SalesDocVisit.at
                     >= datetime.fromisoformat(df + "T00:00:00"))
    q.delete(synchronize_session=False)
    db.bulk_save_objects(parsed)
    return len(parsed)


def sync_warehouses(db: Session, updated_since: str | None = None) -> int:
    """Названия складов из SalesDoc — в справочник привязки.

    Раньше склад, не заведённый вручную, показывался голым идентификатором
    («d0_5»). Теперь имена подтягиваются автоматически; ручной остаётся только
    привязка к фирме, которую SalesDoc не знает."""
    rows = salesdoc.fetch_warehouses()
    for w in rows:
        sid = str(w.get("sd_id") or "").strip()
        if not sid:
            continue
        row = db.query(models.SalesDocStore).filter_by(store_id=sid).first()
        if row is None:
            row = models.SalesDocStore(store_id=sid)
            db.add(row)
        if w.get("name"):
            row.name = w["name"]  # организацию не трогаем — она задаётся вручную
    return len(rows)


def sync_products(db: Session, updated_since: str | None = None) -> int:
    """Справочник номенклатуры SalesDoc (getProduct) — в зеркало.

    Справочник маленький, дельты нет — читаем целиком. Неактивные позиции не
    удаляем: они встречаются в старых документах, и без них строка перемещения
    осталась бы безымянной."""
    rows = salesdoc.call_all("getProduct", ("product", "products"))
    cache = _existing(db, models.SalesDocProduct)
    for p in rows:
        sid = str(p.get("SD_id") or "").lower()
        if not sid:
            continue
        act = p.get("active")
        _upsert(db, models.SalesDocProduct, sid, cache=cache, values={
            "code_1c": str(p.get("code_1C") or "") or None,
            "name": p.get("name") or "",
            "active": not (act in ("N", "n", False, 0, "0")),
            # unit.code_1C у этого API — сама единица («шт»), а не код.
            "unit": (p.get("unit") or {}).get("code_1C")
            if isinstance(p.get("unit"), dict) else None,
            "barcode": p.get("barCode") or None,
            "pack_quantity": p.get("packQuantity") or None,
            "weight": p.get("weight") or None,
            "volume": p.get("volume") or None,
            "category_sd_id": _ref_id(p.get("category")),
            "group_sd_id": _ref_id(p.get("group")),
            "brand_sd_id": _ref_id(p.get("brand")),
        })
    return len(rows)


STORE_LOG_DAYS = 1200  # глубина журнала склада — как у остального зеркала


def sync_store_log(db: Session, updated_since: str | None = None) -> int:
    """Журнал движений склада — в зеркало, по всем складам.

    Метод устроен иначе прочих: параметры идут напрямую (storeId, from, to), а
    не внутри filter, склад обязателен, ответ приходит массивом без обёртки, и
    сервер отбивает частые запросы кодом 429. Отсюда обход складов по одному с
    паузой и повторами.

    Дельты у метода нет — перекладываем целиком. Строк тысячи, не десятки
    тысяч, так что это дёшево."""
    import hashlib

    today = date.today()
    frm = (today - timedelta(days=STORE_LOG_DAYS)).isoformat()
    to = (today + timedelta(days=1)).isoformat()
    stores = [(s.store_id, s.name or s.store_id)
              for s in db.query(models.SalesDocStore).all() if s.store_id]
    names = {p.sd_id: p.name for p in db.query(models.SalesDocProduct).all()}

    fetched: list[tuple] = []
    for i, (sid, sname) in enumerate(stores):
        if i:
            time.sleep(1.2)
        rows = None
        for attempt in range(4):
            try:
                rows = salesdoc.call_all("getStoreLog", ("storeLog", "log"),
                                         {"storeId": sid, "from": frm, "to": to})
                break
            except salesdoc.SalesDocError as e:
                if "429" in str(e) and attempt < 3:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise
        for r in rows or []:
            if isinstance(r, dict):
                fetched.append((sid, sname, r))

    # Пустой ответ по всем складам — не повод стирать зеркало: скорее всего
    # это сбой связи, а не опустевший журнал.
    if not fetched:
        return 0

    db.query(models.SalesDocStoreLog).delete(synchronize_session=False)
    for sid, sname, r in fetched:
        pid = str(r.get("product_id") or "").lower() or None
        raw = f"{sid}|{r.get('document')}|{r.get('document_id')}|{pid}|{r.get('date')}"
        db.add(models.SalesDocStoreLog(
            key=hashlib.sha256(raw.encode()).hexdigest()[:48],
            store_sd_id=sid, store_name=sname,
            document=str(r.get("document") or "") or None,
            document_sd_id=str(r.get("document_id") or "") or None,
            at=_stamp(r.get("date")),
            product_sd_id=pid,
            product_name=names.get(pid),
            quantity=float(r.get("quantity") or 0),
        ))
    return len(fetched)


def _stamp(value):
    """Дата-время из ответа SalesDoc ('2026-07-27 05:47:24')."""
    s = str(value or "").strip().replace("T", " ")[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def sync_movements(db: Session, updated_since: str | None = None) -> int:
    """Перемещения между складами (getMovement) — в зеркало вместе со строками.

    Дельты у метода нет, документов немного (десятки), поэтому перекладываем
    целиком: так же уходят и удалённые в SalesDoc."""
    rows = salesdoc.call_all("getMovement", ("movement", "movements"))
    names = {p.sd_id: p.name for p in db.query(models.SalesDocProduct).all()}
    # Запасной источник имён: позиции, которых нет в справочнике, но которые
    # лежат на складах.
    for r in db.query(models.SalesDocStock).all():
        if r.product_sd_id:
            names.setdefault(r.product_sd_id, r.product_name)
    db.query(models.SalesDocMovementLine).delete(synchronize_session=False)
    cache = _existing(db, models.SalesDocMovement)
    seen: set = set()
    for m in rows:
        sd_id = str(m.get("SD_id") or m.get("CS_id") or "").strip()
        if not sd_id:
            continue
        seen.add(sd_id)
        detail = [d for d in (m.get("detail") or []) if isinstance(d, dict)]
        qty = sum(float(d.get("quantity") or 0) for d in detail)
        frm, to = m.get("from_store") or {}, m.get("to_store") or {}
        _upsert(db, models.SalesDocMovement, sd_id, cache=cache, values={
            "date": _day(m.get("date")),
            "from_store_sd_id": _ref_id(frm),
            "from_store_name": _ref_name(frm),
            "to_store_sd_id": _ref_id(to),
            "to_store_name": _ref_name(to),
            "qty": qty,
            "positions": len(detail),
            "code_1c": str(m.get("code_1C") or "") or None,
        })
        for d in detail:
            pid = str(d.get("SD_id") or "").lower() or None
            db.add(models.SalesDocMovementLine(
                movement_sd_id=sd_id,
                product_sd_id=pid,
                product_code_1c=str(d.get("code_1C") or "") or None,
                product_name=names.get(pid),
                qty=float(d.get("quantity") or 0),
            ))
    db.flush()
    stale = [row.id for row in db.query(models.SalesDocMovement.id,
                                        models.SalesDocMovement.sd_id).all()
             if row.sd_id not in seen]
    if stale:
        db.query(models.SalesDocMovement).filter(
            models.SalesDocMovement.id.in_(stale)).delete(synchronize_session=False)
    return len(rows)


def sync_agents(db: Session, updated_since: str | None = None) -> int:
    """Справочник агентов с признаком «работает сейчас» (getAgent.active).

    Справочник маленький (два десятка записей), дельты у него нет — читаем
    целиком. Уволенных из справочника не удаляем: их имена ещё встречаются в
    истории 1С, и по ним надо уметь сказать «этот уже не работает»."""
    rows = salesdoc.call_all("getAgent", ("agent", "agents"))
    for a in rows:
        sid = str(a.get("SD_id") or "").lower()
        if not sid:
            continue
        row = db.query(models.SalesDocAgent).filter_by(sd_id=sid).first()
        if row is None:
            row = models.SalesDocAgent(sd_id=sid)
            db.add(row)
        row.name = a.get("name") or row.name or ""
        # SalesDoc отдаёт признак строкой «Y»/«N», а не булевым.
        act = a.get("active")
        row.active = not (act in ("N", "n", False, 0, "0"))
        row.synced_at = datetime.utcnow()
    return len(rows)


# Агент «ведёт точку», если работал по ней за это окно. Дольше — уже история:
# маршруты за год пересобираются не раз, и старый агент к точке отношения не
# имеет, даже если формально числится.
AGENT_WINDOW_DAYS = 180


def agent_by_client_1c(db: Session) -> dict:
    """Кто из действующих агентов ведёт каждую точку — ключом по имени 1С.

    Главный источник — закрепление в карточке точки (getClient.agents): это
    настоящее «за кем точка», да ещё и с днями маршрута. Если закрепления нет,
    смотрим по следам работы: агент последнего заказа, затем последнего визита.
    Заказы деактивированных агентов SalesDoc в выгрузку не отдаёт вовсе,
    поэтому по следам виден только действующий; признак active всё равно
    проверяем по справочнику — иначе «агента нет» и «агент уволен» неразличимы,
    а это ровно тот случай, когда точка осталась без хозяина.

    Возвращает {имя контрагента 1С: {agent, active, source, at, days}} — плюс
    справочник агентов для фильтра."""
    agents = {a.sd_id: a for a in db.query(models.SalesDocAgent).all()}
    active_ids = {sid for sid, a in agents.items() if a.active}
    # Имена тоже нужны: в 1С агент записан именем, без ИД SalesDoc.
    active_names = {match_key(a.name) for a in agents.values() if a.active and a.name}
    known_names = {match_key(a.name): a.active for a in agents.values() if a.name}

    since = datetime.utcnow() - timedelta(days=AGENT_WINDOW_DAYS)
    # События «агент работал по точке»: заказы и визиты. Ключ — клиент SalesDoc.
    events: dict[str, list[tuple]] = {}

    def add(client_sd_id, at, agent_sd_id, agent_name, source):
        if not client_sd_id or not agent_name or at is None:
            return
        events.setdefault(str(client_sd_id).lower(), []).append(
            (at, agent_sd_id, agent_name, source))

    for o in (db.query(models.SalesDocOrder)
              .filter(models.SalesDocOrder.agent_name.isnot(None),
                      models.SalesDocOrder.date.isnot(None),
                      models.SalesDocOrder.date >= since.date(),
                      models.SalesDocOrder.status != salesdoc.CANCELLED_STATUS)
              .all()):
        add(o.client_sd_id, datetime.combine(o.date, datetime.min.time()),
            o.agent_sd_id, o.agent_name, "заказ")

    # Визитов ~100 тыс. — сворачиваем в СУБД: на пару «точка + агент» нужен
    # только последний визит, тянуть каждый в питон незачем.
    visit_rows = (
        db.query(models.SalesDocVisit.client_sd_id,
                 models.SalesDocVisit.agent_sd_id,
                 models.SalesDocVisit.agent_name,
                 func.max(models.SalesDocVisit.at))
        .filter(models.SalesDocVisit.visited.is_(True),
                models.SalesDocVisit.at >= since)
        .group_by(models.SalesDocVisit.client_sd_id,
                  models.SalesDocVisit.agent_sd_id,
                  models.SalesDocVisit.agent_name)
        .all())
    for client_sd_id, agent_sd_id, agent_name, at in visit_rows:
        add(client_sd_id, at, agent_sd_id, agent_name, "визит")

    def is_active(agent_sd_id, agent_name) -> bool:
        if agent_sd_id and agent_sd_id in agents:
            return agent_sd_id in active_ids
        return known_names.get(match_key(agent_name), True)

    by_sd_id: dict[str, dict] = {}
    for cli, evs in events.items():
        # Действующий агент важнее свежести: если точку последним трогал
        # уволенный, показывать надо того, кто работает по ней сейчас.
        evs.sort(key=lambda e: (is_active(e[1], e[2]), e[0]), reverse=True)
        at, agent_sd_id, agent_name, source = evs[0]
        by_sd_id[cli] = {
            "agent": agent_name,
            "agent_active": is_active(agent_sd_id, agent_name),
            "agent_source": source,
            "agent_at": at.date().isoformat(),
            "agent_days": None,
        }

    # Закрепление из карточки точки перекрывает следы работы: это то, что
    # SalesDoc считает правдой, включая случай «точка закреплена за уволенным».
    for link in db.query(models.SalesDocClientAgent).all():
        a = agents.get(link.agent_sd_id)
        if a is None:
            continue
        cli = (link.client_sd_id or "").lower()
        cur = by_sd_id.get(cli)
        # Несколько агентов на точке — берём действующего, иначе первого.
        if cur and cur.get("agent_source") == "карточка" and not (
                a.active and not cur["agent_active"]):
            continue
        by_sd_id[cli] = {
            "agent": a.name,
            "agent_active": bool(a.active),
            "agent_source": "карточка",
            "agent_at": (cur or {}).get("agent_at"),
            "agent_days": link.days,
        }

    # Индексы для сопоставления с 1С: по коду, по ИД в имени, по имени.
    by_code: dict[str, dict] = {}
    by_key: dict[str, dict] = {}
    for c in db.query(models.SalesDocClient).all():
        e = by_sd_id.get((c.sd_id or "").lower())
        if not e:
            continue
        if c.code_1c:
            by_code[str(c.code_1c)] = e
        if c.name:
            by_key.setdefault(match_key(c.name), e)

    links = {l.client_1c: (l.sd_id or "").lower()
             for l in db.query(models.SalesDocClientLink).all()}

    out: dict[str, dict] = {}

    def resolve(name: str) -> dict | None:
        sid = links.get(name) or extract_sd_id(name)
        return (by_sd_id.get(sid) if sid else None) or by_key.get(match_key(name))

    for name in {s.client for s in db.query(models.Sale.client).distinct()}:
        e = resolve(name)
        if e:
            out[name] = e

    return {
        "by_client": out,
        "by_sd_id": by_sd_id,
        "by_code_1c": by_code,
        "agents": sorted(
            ({"name": a.name, "active": bool(a.active)} for a in agents.values() if a.name),
            key=lambda a: (not a["active"], a["name"]),
        ),
        "active_names": sorted(active_names),
    }


def sync_stock(db: Session, updated_since: str | None = None) -> int:
    """Остатки по складам и позициям — в зеркало.

    getStock отдаёт срез целиком (склады → товары → количество), дельты у него
    нет, поэтому выгружаем полностью и вычищаем пропавшие позиции."""
    rows = salesdoc.call_all("getStock", ("warehouse", "warehouses"))
    cache = _existing(db, models.SalesDocStock)
    seen: set = set()
    n = 0
    for w in rows:
        store = str(w.get("SD_id") or "").lower()
        if not store:
            continue
        for p in (w.get("products") or []):
            pid = str(p.get("SD_id") or p.get("code_1C") or p.get("name") or "").strip()
            if not pid:
                continue
            key = f"{store}:{pid}"
            seen.add(key)
            n += 1
            _upsert(db, models.SalesDocStock, key, cache=cache, values={
                "store_sd_id": store,
                "store_name": w.get("name") or None,
                "product_sd_id": str(p.get("SD_id") or "") or None,
                "product_name": p.get("name") or "",
                "code_1c": str(p.get("code_1C") or "") or None,
                "quantity": float(p.get("quantity") or 0),
            })
    db.flush()
    stale = [r.id for r in db.query(models.SalesDocStock.id,
                                    models.SalesDocStock.sd_id).all()
             if r.sd_id not in seen]
    if stale:
        db.query(models.SalesDocStock).filter(
            models.SalesDocStock.id.in_(stale)).delete(synchronize_session=False)
    return n


def stock_by_store(db: Session, store_ids=None, q: str | None = None,
                   only_positive: bool = True) -> list[dict]:
    """Остатки из зеркала: по каждому складу список позиций с количеством."""
    query = db.query(models.SalesDocStock)
    if store_ids:
        query = query.filter(models.SalesDocStock.store_sd_id.in_(store_ids))
    if q:
        query = query.filter(models.SalesDocStock.product_name.ilike(f"%{q.strip()}%"))
    if only_positive:
        query = query.filter(models.SalesDocStock.quantity > 0)

    names = {s.store_id.lower(): (s.name, s.organization)
             for s in db.query(models.SalesDocStore).all() if s.store_id}
    grouped: dict[str, dict] = {}

    # Показываем ВСЕ склады из справочника, даже пустые: иначе склад без
    # остатков (возвратный, перевалочный) просто исчезал из списка и выглядел
    # как потерянный. При поиске по товару пустые не показываем — они бы
    # только мешали результатам.
    if not q:
        for sid, (name, org) in names.items():
            if store_ids and sid not in store_ids:
                continue
            grouped[sid] = {
                "store_id": sid, "store": name or sid, "org": org,
                "positions": 0, "total_qty": 0.0, "items": [],
            }

    for r in query.order_by(models.SalesDocStock.product_name).all():
        # Название склада: из справочника SalesDoc, иначе из самих остатков,
        # и лишь в крайнем случае — идентификатор.
        name, org = names.get(r.store_sd_id, (None, None))
        name = name or r.store_name or r.store_sd_id
        g = grouped.setdefault(r.store_sd_id, {
            "store_id": r.store_sd_id, "store": name,
            "org": org, "positions": 0, "total_qty": 0.0, "items": [],
        })
        qty = float(r.quantity or 0)
        g["positions"] += 1
        g["total_qty"] += qty
        g["items"].append({"name": r.product_name, "code_1C": r.code_1c,
                           "qty": round(qty, 3)})
    out = sorted(grouped.values(), key=lambda x: -x["total_qty"])
    for g in out:
        g["total_qty"] = round(g["total_qty"], 3)
    return out


def sync(full: bool = False, kinds=None) -> dict:
    """Обновить зеркало. full=True — полная выгрузка, иначе дельта изменений.

    Идёт под замком: параллельные синхронизации только мешали бы друг другу и
    зря дёргали токен SalesDoc. Но запрос на ПОЛНУЮ выгрузку под замком не
    выбрасывается, а запоминается и выполняется следом.

    Раньше он терялся: дельты идут каждую минуту и занимают заметную её часть,
    поэтому нажатие «Обновить» почти всегда приходилось на занятый замок и
    молча пропадало. А документ, который в SalesDoc не менялся (у него старый
    `dateUpdate`), дельта не забирает в принципе — он мог приехать только
    часовой полной выгрузкой. Со стороны это выглядело как «обновляю, а ничего
    не меняется».
    """
    if not salesdoc.is_configured():
        return {"skipped": "SalesDoc не настроен"}
    if not _sync_lock.acquire(blocking=False):
        if full:
            _request_full(kinds)
            return {"skipped": "синхронизация уже идёт", "full_queued": True}
        return {"skipped": "синхронизация уже идёт"}
    pend = _take_pending()       # просили, пока никто не работал
    if pend is not None:
        full = True
        kinds = None if pend == "all" else sorted(pend)
    result = _sync_once(full, kinds)   # замок отпускает сама
    # Запрос пришёл, пока мы работали, — выполняем сразу, а не через час.
    pend = _take_pending()
    if pend is not None and _sync_lock.acquire(blocking=False):
        result = {"first": result,
                  "then_full": _sync_once(True, None if pend == "all" else sorted(pend))}
    return result


def full_pending() -> bool:
    """Есть ли невыполненный запрос на полную выгрузку — для показа в UI."""
    with _pending_lock:
        return _pending_full is not None


def _sync_once(full: bool = False, kinds=None) -> dict:
    """Один проход синхронизации. Вызывается только с уже взятым замком и
    отпускает его сам. kinds — ограничить набор видов (None — все)."""
    started = time.time()
    result: dict = {"full": full}
    try:
        db = SessionLocal()
        try:
            for kind, fn, method, model in (
                    ("orders", sync_orders, "getOrder", models.SalesDocOrder),
                    ("payments", sync_payments, "getPayment", models.SalesDocPayment),
                    ("clients", sync_clients, None, models.SalesDocClient),
                    ("warehouses", sync_warehouses, None, models.SalesDocStore),
                    ("agents", sync_agents, None, models.SalesDocAgent),
                    ("products", sync_products, None, models.SalesDocProduct),
                    ("movements", sync_movements, None, models.SalesDocMovement),
                    ("store_log", sync_store_log, None, models.SalesDocStoreLog),
                    ("stock", sync_stock, None, models.SalesDocStock),
                    ("visits", sync_visits, None, models.SalesDocVisit)):
                # Точечное обновление: кнопка просит только документы, всё
                # остальное в этот момент человеку не нужно.
                if kinds and kind not in kinds:
                    continue
                _current.update({"kind": kind, "full": full, "started": time.time()})
                st = _state(db, kind)
                # Визиты (~100 тыс. строк) — только при полной синхронизации:
                # для минутной дельты набор слишком тяжёлый.
                if kind in ("visits", "store_log") and not full \
                        and st.last_full_at is not None:
                    result[kind] = {"skipped": "обновляется при полной синхронизации"}
                    continue
                # Своя частота у каждого вида (см. KIND_EVERY): справочники не
                # трогаем каждую минуту — это и была главная причина тормозов.
                every = KIND_EVERY.get(kind)
                if every and not full and st.last_delta_at is not None:
                    waited = (datetime.utcnow() - st.last_delta_at).total_seconds()
                    if waited < every:
                        result[kind] = {"skipped": f"обновляется раз в {every} с"}
                        continue
                since = None
                if method is not None and not full and st.last_full_at:
                    base = st.last_delta_at or st.last_full_at
                    since = (base - DELTA_OVERLAP).date().isoformat()
                    changed = changed_count(method, since)
                    # Удаления не попадают в «изменённые» — их видно только по
                    # расхождению количества. Если числа не сходятся, сразу
                    # перевыгружаем целиком: это вычистит пропавшие записи.
                    sd_total = total_count(method)
                    ours = mirror_count(db, model)
                    if sd_total is not None and sd_total != ours:
                        since = None
                        result.setdefault("resync", {})[kind] = {
                            "ours": ours, "salesdoc": sd_total}
                    elif changed == 0:
                        # Ничего не изменилось и количество сходится — тратить
                        # трафик незачем.
                        st.last_delta_at = datetime.utcnow()
                        result[kind] = {"unchanged": True, "total": st.rows}
                        db.commit()
                        continue
                    elif changed is not None:
                        result.setdefault("changed", {})[kind] = changed
                try:
                    n = fn(db, since)
                    # Сессия без autoflush — сбрасываем сами, иначе count()
                    # ниже не увидит только что добавленные строки.
                    db.flush()
                    now = datetime.utcnow()
                    if since is None:
                        st.last_full_at = now
                    st.last_delta_at = now
                    st.rows = db.query(model).count()
                    st.last_error = None
                    result[kind] = {"fetched": n, "total": st.rows}
                except salesdoc.SalesDocError as e:
                    st.last_error = str(e)
                    result[kind] = {"error": str(e)}
                db.commit()
        finally:
            db.close()
    finally:
        _sync_lock.release()
    result["seconds"] = round(time.time() - started, 1)
    return result


def sync_async(full: bool = False, kinds=None) -> dict:
    """Запустить синхронизацию в фоне и сразу вернуть управление.

    Пользователь никогда не должен ждать выгрузку из SalesDoc: страницы
    читаются из зеркала, а обновление идёт незаметно и доезжает само."""
    threading.Thread(target=sync, kwargs={"full": full, "kinds": kinds},
                     name="salesdoc-sync-once", daemon=True).start()
    return {"started": True, "full": full, "kinds": list(kinds) if kinds else None}


def status(db: Session) -> dict:
    """Свежесть зеркала — для показа «данные на …» и кнопки обновления."""
    out: dict = {"configured": salesdoc.is_configured(), "kinds": {}}
    newest: datetime | None = None
    for kind in ("orders", "payments", "clients", "stock", "visits"):
        st = db.query(models.SalesDocSyncState).filter_by(kind=kind).first()
        if st is None:
            out["kinds"][kind] = None
            continue
        out["kinds"][kind] = {
            "rows": st.rows,
            "last_full_at": st.last_full_at and st.last_full_at.isoformat(),
            "last_delta_at": st.last_delta_at and st.last_delta_at.isoformat(),
            "error": st.last_error,
        }
        if st.last_delta_at and (newest is None or st.last_delta_at > newest):
            newest = st.last_delta_at
    out["synced_at"] = newest and newest.isoformat()
    out["ready"] = bool(newest)
    # Состояние прямо сейчас: идёт ли выгрузка и не ждёт ли своей очереди
    # запрошенная полная. Без этого кнопка «Обновить» ничего не сообщает
    # пользователю — он жмёт её повторно и думает, что она не работает.
    out["running"] = _sync_lock.locked()
    # Какой именно вид данных обновляется и сколько уже идёт.
    cur = dict(_current)
    out["current"] = ({"kind": cur["kind"], "full": cur["full"],
                       "seconds": round(time.time() - cur["started"])}
                      if out["running"] and cur.get("started") else None)
    out["full_pending"] = full_pending()
    # Берём САМУЮ СТАРУЮ полную выгрузку из ключевых видов: показать надо
    # «полностью выгружено не позже чем», а не «хоть что-то обновилось».
    full_at = None
    for kind in ("orders", "payments", "clients"):
        st = db.query(models.SalesDocSyncState).filter_by(kind=kind).first()
        if st is not None and st.last_full_at and (full_at is None or st.last_full_at < full_at):
            full_at = st.last_full_at
    out["full_at"] = full_at and full_at.isoformat()
    out["errors"] = {k: v["error"] for k, v in out["kinds"].items()
                     if v and v.get("error")}
    return out


def client_detail(db: Session, sd_id: str | None, code_1c: str | None,
                  date_from: date, date_to: date, store_ids=None) -> dict:
    """Детализация клиента из зеркала — обычный SQL, без обращения к SalesDoc.

    Реализации делятся по складу выбранной фирмы (store_ids). У оплаты склада
    нет, но в ответе SalesDoc есть поле orders — заказы, которые она гасит; по
    их складам оплату к фирме отнести можно. Оплату без такой связи (аванс,
    начальный остаток) поделить нечем — показываем при любой фирме."""
    sid = (sd_id or "").lower() or None

    def match(model):
        q = db.query(model).filter(model.date >= date_from, model.date <= date_to)
        conds = []
        if sid:
            conds.append(model.client_sd_id == sid)
        if code_1c:
            conds.append(model.client_code_1c == str(code_1c))
        if not conds:
            return q.filter(False)
        from sqlalchemy import or_
        return q.filter(or_(*conds))

    # --- Реализации ---
    orders_q = match(models.SalesDocOrder)
    # Сколько реализаций клиента вообще есть и сколько остаётся после отбора по
    # складам выбранной фирмы. Разницу показываем в карточке: иначе «Реализации
    # 0» выглядит как «в SalesDoc отгрузок нет», хотя они просто с чужого склада.
    all_orders_count = orders_q.count()
    # Названия складов: в зеркале заказа лежит только идентификатор («d0_5»),
    # а в сверке нужен человеческий склад — по нему видно, чьей фирме документ.
    store_names = {
        s.store_id.lower(): (s.name or s.store_id)
        for s in db.query(models.SalesDocStore).all() if s.store_id
    }
    # Склады скрытых реализаций. Без них сообщение «12 реализаций скрыто»
    # бесполезно: не видно ни строк, ни складов — а именно склад и объясняет,
    # почему точка оказалась в чужой фирме.
    hidden_stores: list[str] = []
    if store_ids:
        for (sid,) in (orders_q.with_entities(models.SalesDocOrder.store_sd_id)
                       .distinct().all()):
            if (sid or "") in store_ids:
                continue
            name = store_names.get(sid or "", sid or "склад не указан")
            if name not in hidden_stores:
                hidden_stores.append(name)
        orders_q = orders_q.filter(models.SalesDocOrder.store_sd_id.in_(store_ids))
    hidden_by_store = all_orders_count - orders_q.count()
    orders, o_total, o_count = [], 0.0, 0
    for o in orders_q.order_by(models.SalesDocOrder.date.desc()).all():
        counted = o.status in salesdoc.SHIPPED_STATUSES
        amt = float(o.amount or 0)
        if counted:
            o_total += amt
            o_count += 1
        orders.append({
            "date": o.date and o.date.isoformat(),
            # Идентификаторы нужны, чтобы различать одинаковые строки: в один
            # день по клиенту бывает несколько отгрузок на равные суммы, и без
            # номера непонятно, какая из них какой накладной 1С соответствует.
            "sd_id": o.sd_id,
            "code_1C": o.code_1c,
            "store": store_names.get(o.store_sd_id or "", o.store_sd_id or ""),
            "status": o.status,
            "status_label": salesdoc.ORDER_STATUS.get(o.status, str(o.status)),
            "amount": round(amt, 2),
            "counted": counted,
            "returns": round(float(o.returns_amount or 0), 2),
        })

    # Склад каждого заказа этого клиента — чтобы отнести оплату к фирме через
    # заказы, которые она гасит. Берём заказы ДО отбора по складам: оплата
    # может гасить как раз чужой заказ, и об этом надо знать.
    order_store = {
        o.sd_id: o.store_sd_id
        for o in match(models.SalesDocOrder).with_entities(
            models.SalesDocOrder.sd_id, models.SalesDocOrder.store_sd_id).all()
    }

    def payment_stores(p) -> list[str]:
        """Склады заказов, которые гасит оплата (пустой список — связи нет)."""
        return [order_store[i] for i in (p.order_ids or "").split(",")
                if i and i in order_store and order_store[i]]

    # --- Оплаты и возвраты: один журнал, разделяем по виду операции ---
    pays, p_total, p_count = [], 0.0, 0
    rets, r_total = [], 0.0
    hidden_pay, hidden_pay_stores = 0, []
    for p in match(models.SalesDocPayment).order_by(
            models.SalesDocPayment.date.desc()).all():
        amt = float(p.amount or 0)
        if p.txn == salesdoc.SHELF_RETURN_TXN:   # «Возврат с полки» — это возврат
            r_total += amt
            rets.append({"date": p.date and p.date.isoformat(), "sd_id": p.sd_id,
                         "amount": round(amt, 2)})
            continue
        # Оплата по заказам чужой фирмы — не наша. Оплату без связи с заказами
        # (аванс, начальный остаток) поделить нечем, её оставляем.
        pstores = payment_stores(p)
        if store_ids and pstores and not (set(pstores) & set(store_ids)):
            hidden_pay += 1
            for s in pstores:
                name = store_names.get(s, s)
                if name not in hidden_pay_stores:
                    hidden_pay_stores.append(name)
            continue
        counted = p.txn == salesdoc.PAYMENT_TXN
        if counted:
            p_total += amt
            p_count += 1
        pays.append({
            "date": p.date and p.date.isoformat(),
            "sd_id": p.sd_id,
            "amount": round(amt, 2),
            "txn": p.txn,
            "txn_label": salesdoc.PAY_TXN.get(p.txn, str(p.txn) if p.txn is not None else "—"),
            "type_name": p.type_name or "",
            # Касса операции: у оплаты нет склада, и это единственный признак,
            # по которому видно, к какой фирме её посадили.
            "cashbox": p.cashbox_name or "",
            # Склады заказов, которые гасит оплата: показываем в строке — по ним
            # и видно, чьей фирме эта оплата.
            "stores": sorted({store_names.get(s, s) for s in pstores}),
            "counted": counted,
        })

    # Баланс, который считает сам SalesDoc (getBalance), против баланса по его
    # же операциям. Если они расходятся — значит SalesDoc учитывает документы,
    # которых нет в его выгрузках: долг есть, а документа не видно.
    cli_row = None
    if sid:
        cli_row = db.query(models.SalesDocClient).filter_by(sd_id=sid).first()
    if cli_row is None and code_1c:
        cli_row = db.query(models.SalesDocClient).filter_by(
            code_1c=str(code_1c)).first()
    by_ops = round(o_total - p_total - r_total, 2)
    sd_balance = round(float(cli_row.debt), 2) if cli_row else None
    # Сумма отгруженного, но ещё не доставленного. Похоже, SalesDoc считает
    # долгом только доставленные заказы: если разница баланса ровно на эту
    # сумму, расхождение объясняется статусом, а не потерянными документами.
    in_transit = round(sum(
        o["amount"] for o in orders if o["status"] == 2), 2)

    return {
        "balance": {
            "sd": sd_balance,
            "in_balance": bool(cli_row.in_balance) if cli_row else False,
            "by_ops": by_ops,
            "in_transit": in_transit,
            # Разница объясняется статусом «Отправлен», а не пропажей
            # документов: баланс SalesDoc просто ещё не считает их долгом.
            "explained_by_transit": (
                sd_balance is not None and in_transit > 0
                and abs((sd_balance - by_ops) + in_transit) < 0.5
            ),
            # Разница = сумма документов, известных балансу SalesDoc, но
            # отсутствующих в его журналах.
            "diff": None if sd_balance is None else round(sd_balance - by_ops, 2),
        },
        "orders": {"total": round(o_total, 2), "count": o_count, "items": orders,
                   "hidden_by_store": hidden_by_store,
                   "hidden_stores": hidden_stores},
        "payments": {"total": round(p_total, 2), "count": p_count, "items": pays,
                     "scanned": len(pays) + len(rets), "matched": len(pays),
                     "hidden_by_store": hidden_pay,
                     "hidden_stores": hidden_pay_stores},
        "returns": {"total": round(r_total, 2), "count": len(rets), "items": rets},
        "errors": [],
    }


def reconcile_components(db: Session, date_from: date, date_to: date,
                         store_ids=None) -> dict:
    """Суммы реализаций / возвратов / оплат по ВСЕМ клиентам сразу — из зеркала.

    Питает колонку «Причина» в сверке. Раньше для этого выкачивались журналы
    SalesDoc за 3 года; теперь это обычная группировка в базе — мгновенно.

    Формат совпадает с прежним: по каждому компоненту две карты — по SD_id
    клиента и по коду 1С (запись попадает ровно в одну, двойного счёта нет)."""
    from sqlalchemy import func

    def collect(model, extra_filter=None, store_filter=False):
        by_sd: dict[str, float] = {}
        by_code: dict[str, float] = {}
        q = (
            db.query(model.client_sd_id, model.client_code_1c,
                     func.sum(model.amount))
            .filter(model.date >= date_from, model.date <= date_to)
        )
        if extra_filter is not None:
            q = q.filter(extra_filter)
        if store_filter and store_ids:
            q = q.filter(model.store_sd_id.in_(store_ids))
        for cli_sd, cli_code, total in q.group_by(
                model.client_sd_id, model.client_code_1c).all():
            amount = float(total or 0)
            if cli_sd:
                by_sd[cli_sd] = by_sd.get(cli_sd, 0.0) + amount
            elif cli_code:
                by_code[cli_code] = by_code.get(cli_code, 0.0) + amount
        return {"sd": by_sd, "code": by_code}

    O, P = models.SalesDocOrder, models.SalesDocPayment

    def collect_payments(txn: int) -> dict:
        """Оплаты с делением по фирме через заказы, которые они гасят.

        Склада у оплаты нет, поэтому SQL-фильтром не обойтись: смотрим поле
        orders. Оплату без связи с заказами (аванс, начальный остаток) поделить
        нечем — она считается в обеих фирмах, иначе просто пропала бы."""
        by_sd: dict[str, float] = {}
        by_code: dict[str, float] = {}
        need = set(store_ids) if store_ids else None
        order_store = dict(db.query(O.sd_id, O.store_sd_id).all()) if need else {}
        rows = (
            db.query(P.client_sd_id, P.client_code_1c, P.amount, P.order_ids)
            .filter(P.date >= date_from, P.date <= date_to, P.txn == txn)
            .all()
        )
        for cli_sd, cli_code, amount, oids in rows:
            if need and oids:
                stores = {order_store.get(i) for i in oids.split(",") if i}
                stores.discard(None)
                if stores and not (stores & need):
                    continue
            amount = float(amount or 0)
            if cli_sd:
                by_sd[cli_sd] = by_sd.get(cli_sd, 0.0) + amount
            elif cli_code:
                by_code[cli_code] = by_code.get(cli_code, 0.0) + amount
        return {"sd": by_sd, "code": by_code}

    def _collect_payments_all() -> dict:
        """Оплаты клиента без деления по фирме — пара к delivered_all."""
        by_sd: dict[str, float] = {}
        by_code: dict[str, float] = {}
        for cli_sd, cli_code, amount, _oids in db.query(
                P.client_sd_id, P.client_code_1c, P.amount, P.order_ids).filter(
                P.date >= date_from, P.date <= date_to,
                P.txn == salesdoc.PAYMENT_TXN).all():
            amount = float(amount or 0)
            if cli_sd:
                by_sd[cli_sd] = by_sd.get(cli_sd, 0.0) + amount
            elif cli_code:
                by_code[cli_code] = by_code.get(cli_code, 0.0) + amount
        return {"sd": by_sd, "code": by_code}

    return {
        # Отобраны ли компоненты по складам фирмы. От этого зависит, можно ли
        # сравнивать их с балансом SalesDoc — он по фирмам не делится.
        "_store_filtered": bool(store_ids),
        # Реализации — только отгруженные, делятся по складу выбранной фирмы.
        "sales": collect(O, O.status.in_(sorted(salesdoc.SHIPPED_STATUSES)),
                         store_filter=True),
        # Отгружено, но ещё не доставлено: баланс SalesDoc такие заказы
        # долгом не считает, и разница на эту сумму — норма, а не ошибка.
        "in_transit": collect(O, O.status == 2, store_filter=True),
        # Доставленное — то, из чего SalesDoc и складывает свой баланс.
        # Сравнив баланс с этой суммой, видно, знает ли SalesDoc документы,
        # которых нет в его выгрузке.
        "delivered": collect(O, O.status.in_([3, 4]), store_filter=True),
        # Возвраты — из журнала операций, по клиенту (склада там нет).
        "returns": collect(P, P.txn == salesdoc.SHELF_RETURN_TXN),
        "payments": collect_payments(salesdoc.PAYMENT_TXN),
        # Списание долга и выплата клиенту: баланс SalesDoc они меняют, а в
        # 1С такой операции нет. Без этого компонента точка со списанным
        # долгом выглядит расхождением непонятного происхождения.
        "debt_writeoff": collect(P, P.txn.in_(sorted(salesdoc.BALANCE_ONLY_TXN))),
        # Те же доставленное и оплаты, но БЕЗ отбора по складам фирмы. Нужны
        # ровно для одного расчёта — «сколько SalesDoc держит в балансе сверх
        # того, что отдаёт по API». Баланс из getBalance по фирмам не делится
        # вовсе, поэтому сравнивать его можно только с неотфильтрованными
        # суммами: иначе весь оборот чужой фирмы засчитывается как «скрытый».
        "delivered_all": (collect(O, O.status.in_([3, 4])) if store_ids
                          else collect(O, O.status.in_([3, 4]), store_filter=True)),
        "payments_all": (_collect_payments_all() if store_ids
                         else collect_payments(salesdoc.PAYMENT_TXN)),
    }


def start_background_sync() -> None:
    """Фоновое обновление: дельта каждые 5 минут, полная выгрузка раз в сутки.

    Первый запуск наполняет зеркало целиком, поэтому раздел сразу открывается
    быстро, даже если им ещё не пользовались."""
    global _worker_started
    if _worker_started or not salesdoc.is_configured():
        return
    _worker_started = True

    def loop():
        last_full = 0.0
        while True:
            try:
                need_full = (time.time() - last_full) >= FULL_EVERY
                r = sync(full=need_full)
                if need_full and "skipped" not in r:
                    last_full = time.time()
                print(f"[salesdoc] зеркало обновлено: {r}", flush=True)
            except Exception as e:  # noqa: BLE001 — фон не должен падать
                print(f"[salesdoc] сбой синхронизации: {e}", flush=True)
            time.sleep(DELTA_EVERY)

    threading.Thread(target=loop, name="salesdoc-mirror", daemon=True).start()
