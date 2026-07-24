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

import threading
import time
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from .. import models
from ..database import SessionLocal
from . import salesdoc

# Полная выгрузка — раз в сутки; проверка изменений — раз в минуту.
# Частота почти ничего не стоит: сначала идёт дежурная проверка (одна запись),
# и данные выгружаются, только если счётчик изменений больше нуля. Токену это
# не вредит — его гасит лишь повторный вход, а мы работаем на постоянном.
FULL_EVERY = 24 * 3600
DELTA_EVERY = 60  # 1 минута
# С каким запасом берём дельту: перекрываем прошлую синхронизацию, чтобы не
# потерять записи, попавшие на границу окна.
DELTA_OVERLAP = timedelta(hours=6)
# Глубина истории зеркала (долг накопительный — берём с запасом).
HISTORY_YEARS = 3

_sync_lock = threading.Lock()
_worker_started = False


def _window() -> tuple[str, str]:
    today = date.today()
    return f"{today.year - HISTORY_YEARS}-01-01", today.isoformat()


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


def _upsert(db: Session, model, sd_id: str, values: dict) -> None:
    """Обновляем запись зеркала по SD_id либо создаём новую."""
    row = db.query(model).filter_by(sd_id=sd_id).first()
    if row is None:
        row = model(sd_id=sd_id)
        db.add(row)
    for k, v in values.items():
        setattr(row, k, v)
    row.synced_at = datetime.utcnow()


def _order_filter(date_from: str, date_to: str, updated_since: str | None) -> dict:
    """Заказы: все статусы (нужны и отменённые — их показываем зачёркнутыми)."""
    period = ({"dateUpdate": {"from": updated_since, "to": date_to}}
              if updated_since else {"date": {"from": date_from, "to": date_to}})
    return {"filter": {"include": "all", "status": [1, 2, 3, 4, 5], "period": period}}


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


def sync_orders(db: Session, updated_since: str | None = None) -> int:
    df, dt = _window()
    rows = salesdoc.call_all("getOrder", ("orders", "order"),
                             _order_filter(df, dt, updated_since))
    for o in rows:
        sd_id = str(o.get("SD_id") or o.get("CS_id") or "").strip()
        if not sd_id:
            continue
        cli_sd, cli_code = _client_keys(o.get("client"))
        _upsert(db, models.SalesDocOrder, sd_id, {
            "client_sd_id": cli_sd,
            "client_code_1c": cli_code,
            "store_sd_id": str((o.get("store") or {}).get("SD_id") or "").lower() or None,
            "date": _day(o.get("dateDocument") or o.get("dateCreate")),
            "status": o.get("status"),
            "amount": float(o.get("totalSummaAfterDiscount") or o.get("totalSumma") or 0),
            "returns_amount": float(o.get("totalReturnsSumma") or 0),
            "code_1c": o.get("code_1C"),
        })
    return len(rows)


def sync_payments(db: Session, updated_since: str | None = None) -> int:
    df, dt = _window()
    rows = salesdoc.call_all("getPayment", ("payments", "payment"),
                             _payment_filter(df, dt, updated_since))
    ptypes = salesdoc.fetch_payment_types()
    for p in rows:
        sd_id = str(p.get("SD_id") or p.get("CS_id") or "").strip()
        if not sd_id:
            continue
        cli_sd, cli_code = _client_keys(p.get("client"))
        pt = p.get("paymentType") or {}
        _upsert(db, models.SalesDocPayment, sd_id, {
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
        })
    return len(rows)


def sync(full: bool = False) -> dict:
    """Обновить зеркало. full=True — полная выгрузка, иначе дельта изменений.

    Идёт под замком: параллельные синхронизации только мешали бы друг другу и
    зря дёргали токен SalesDoc."""
    if not salesdoc.is_configured():
        return {"skipped": "SalesDoc не настроен"}
    if not _sync_lock.acquire(blocking=False):
        return {"skipped": "синхронизация уже идёт"}
    started = time.time()
    result: dict = {"full": full}
    try:
        db = SessionLocal()
        try:
            for kind, fn, method in (("orders", sync_orders, "getOrder"),
                                     ("payments", sync_payments, "getPayment")):
                st = _state(db, kind)
                since = None
                if not full and st.last_full_at:
                    base = st.last_delta_at or st.last_full_at
                    since = (base - DELTA_OVERLAP).date().isoformat()
                    # Дежурная проверка: если с прошлого раза ничего не
                    # изменилось — не выгружаем ничего. Ночью и в выходные
                    # это почти весь трафик.
                    changed = changed_count(method, since)
                    if changed == 0:
                        st.last_delta_at = datetime.utcnow()
                        result[kind] = {"unchanged": True, "total": st.rows}
                        db.commit()
                        continue
                    if changed is not None:
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
                    st.rows = db.query(
                        models.SalesDocOrder if kind == "orders" else models.SalesDocPayment
                    ).count()
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


def status(db: Session) -> dict:
    """Свежесть зеркала — для показа «данные на …» и кнопки обновления."""
    out: dict = {"configured": salesdoc.is_configured(), "kinds": {}}
    newest: datetime | None = None
    for kind in ("orders", "payments"):
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
    return out


def client_detail(db: Session, sd_id: str | None, code_1c: str | None,
                  date_from: date, date_to: date, store_ids=None) -> dict:
    """Детализация клиента из зеркала — обычный SQL, без обращения к SalesDoc.

    Реализации делятся по складу выбранной фирмы (store_ids), оплаты и
    возвраты — общие по клиенту: склад в журнале оплат SalesDoc не хранит."""
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
    if store_ids:
        orders_q = orders_q.filter(models.SalesDocOrder.store_sd_id.in_(store_ids))
    orders, o_total, o_count = [], 0.0, 0
    for o in orders_q.order_by(models.SalesDocOrder.date.desc()).all():
        counted = o.status in salesdoc.SHIPPED_STATUSES
        amt = float(o.amount or 0)
        if counted:
            o_total += amt
            o_count += 1
        orders.append({
            "date": o.date and o.date.isoformat(),
            "code_1C": o.code_1c,
            "status": o.status,
            "status_label": salesdoc.ORDER_STATUS.get(o.status, str(o.status)),
            "amount": round(amt, 2),
            "counted": counted,
            "returns": round(float(o.returns_amount or 0), 2),
        })

    # --- Оплаты и возвраты: один журнал, разделяем по виду операции ---
    pays, p_total, p_count = [], 0.0, 0
    rets, r_total = [], 0.0
    for p in match(models.SalesDocPayment).order_by(
            models.SalesDocPayment.date.desc()).all():
        amt = float(p.amount or 0)
        if p.txn == salesdoc.SHELF_RETURN_TXN:   # «Возврат с полки» — это возврат
            r_total += amt
            rets.append({"date": p.date and p.date.isoformat(), "amount": round(amt, 2)})
            continue
        counted = p.txn == salesdoc.PAYMENT_TXN
        if counted:
            p_total += amt
            p_count += 1
        pays.append({
            "date": p.date and p.date.isoformat(),
            "amount": round(amt, 2),
            "txn": p.txn,
            "txn_label": salesdoc.PAY_TXN.get(p.txn, str(p.txn) if p.txn is not None else "—"),
            "type_name": p.type_name or "",
            "counted": counted,
        })

    return {
        "orders": {"total": round(o_total, 2), "count": o_count, "items": orders},
        "payments": {"total": round(p_total, 2), "count": p_count, "items": pays,
                     "scanned": len(pays) + len(rets), "matched": len(pays)},
        "returns": {"total": round(r_total, 2), "count": len(rets), "items": rets},
        "errors": [],
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
