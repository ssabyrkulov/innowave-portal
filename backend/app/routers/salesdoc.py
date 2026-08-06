"""Сверка учёта с SalesDoc: тянем данные из SalesDoc и сопоставляем с нашей
1С-картиной (дебиторка, реализации, оплаты за период), показываем расхождения.

Сопоставление клиентов — по нормализованному имени (в нашей 1С-выгрузке нет
кода контрагента, а в SalesDoc имя приходит рядом с балансом).
"""

import difflib
import re
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import require_roles
from ..services import salesdoc, salesdoc_mirror
from .receipts import CUSTOMER_PAYMENT_PREFIX, receivables
from .sales import sales_summary

# Сопоставление имён 1С ↔ SalesDoc живёт в зеркале: дебиторке оно нужно так же,
# как сверке, а импортировать роутер из роутера нельзя (получится цикл).
_match_key = salesdoc_mirror.match_key
_extract_sd_id = salesdoc_mirror.extract_sd_id

router = APIRouter(prefix="/salesdoc", tags=["salesdoc"])

can_view = require_roles(models.Role.admin, models.Role.accountant)


can_edit = require_roles(models.Role.admin, models.Role.accountant)


@router.get("/status")
def status(_: models.User = Depends(can_view)):
    return {"configured": salesdoc.is_configured()}


@router.post("/reconnect")
def reconnect(_: models.User = Depends(require_roles(models.Role.admin, models.Role.accountant))):
    """Переподключить портал к SalesDoc (свежий вход / проверка токена)."""
    if not salesdoc.is_configured():
        raise HTTPException(status_code=503, detail="Интеграция SalesDoc не настроена")
    try:
        return salesdoc.reconnect()
    except salesdoc.SalesDocError as e:
        raise HTTPException(status_code=502, detail=str(e))


def _store_ids_for_org(db: Session, org: str) -> set | None:
    """SD_id складов выбранной фирмы плюс склады БЕЗ привязки к фирме.

    None — фильтр не применяем (выбраны «Обе» или привязка ещё не настроена).

    Склады без привязки включаем намеренно. Раньше они молча выпадали: при
    выборе фирмы реализации с такого склада исчезали, и клиент выглядел так,
    будто отгрузок в SalesDoc у него нет вовсе (оплаты при этом оставались —
    они по складам не делятся). Лучше показать лишнее и предупредить, чем
    незаметно спрятать.
    """
    o = (org or "").strip().lower()
    if o not in models.ORGS:
        return None
    rows = [s for s in db.query(models.SalesDocStore).all() if s.store_id]
    mine = {s.store_id.lower() for s in rows if s.organization == o}
    if not mine:
        return None
    unmapped = {s.store_id.lower() for s in rows if not s.organization}
    return mine | unmapped


@router.get("/store-clients")
def store_clients(
    db: Session = Depends(get_db),
    _: models.User = Depends(can_view),
    store_id: str = Query(default="", description="SD_id склада; пусто — все без фирмы"),
):
    """Точки, отгружавшиеся со склада без привязки к фирме.

    Пока склад не отнесён к фирме, его реализации показываются в обеих —
    и непонятно, чьи это точки. Здесь видно, по кому именно принимается
    решение: клиент, число отгрузок, сумма и период."""
    stores = {s.store_id.lower(): s for s in db.query(models.SalesDocStore).all()
              if s.store_id}
    targets = ([store_id.strip().lower()] if store_id.strip()
               else [sid for sid, s in stores.items() if not s.organization])
    names = {c.sd_id: c.name for c in db.query(models.SalesDocClient).all()}

    out = []
    for sid in targets:
        rows = (db.query(models.SalesDocOrder)
                .filter(models.SalesDocOrder.store_sd_id == sid,
                        models.SalesDocOrder.status != salesdoc.CANCELLED_STATUS)
                .all())
        agg: dict = {}
        for r in rows:
            key = r.client_sd_id or r.client_code_1c or "—"
            a = agg.setdefault(key, {"count": 0, "amount": 0.0,
                                     "first": None, "last": None})
            a["count"] += 1
            a["amount"] += float(r.amount or 0)
            if r.date:
                if a["first"] is None or r.date < a["first"]:
                    a["first"] = r.date
                if a["last"] is None or r.date > a["last"]:
                    a["last"] = r.date
        store = stores.get(sid)
        out.append({
            "store_id": sid,
            "name": (store.name if store else None) or sid,
            "clients": sorted(
                ({"name": names.get(k, k),
                  "count": v["count"],
                  "amount": round(v["amount"], 2),
                  "first": v["first"] and v["first"].isoformat(),
                  "last": v["last"] and v["last"].isoformat()}
                 for k, v in agg.items()),
                key=lambda x: -x["amount"],
            ),
        })
    return {"stores": out}


def _unmapped_stores(db: Session) -> list[str]:
    """Склады без фирмы, по которым реально были реализации.

    Пустой склад без привязки ни на что не влияет — его реализаций нет, дублить
    в обеих фирмах нечего. Раньше такие тоже попадали в предупреждение, и оно
    выглядело тревожнее, чем есть: из четырёх названий опасны были два."""
    stats = salesdoc_mirror.orders_by_store(db)
    return [
        s.name or s.store_id
        for s in db.query(models.SalesDocStore).all()
        if s.store_id and not s.organization
        # Только отгруженные: отменённые документы ни в одну сумму не идут,
        # дублировать в двух фирмах там нечего.
        and (stats.get(s.store_id.lower()) or {}).get("shipped_count")
    ]


class StoreItem(BaseModel):
    store_id: str
    name: str | None = None
    org: str | None = None


@router.get("/warehouses")
def list_warehouses(
    db: Session = Depends(get_db),
    _: models.User = Depends(can_view),
):
    """Склады SalesDoc + их привязка к фирмам (для настройки разделения)."""
    _require_configured()
    try:
        whs = salesdoc.fetch_warehouses()
    except salesdoc.SalesDocError as e:
        raise HTTPException(status_code=502, detail=str(e))
    saved = {s.store_id: s for s in db.query(models.SalesDocStore).all()}
    # Сколько реализаций реально прошло по каждому складу. Без этого привязка
    # делается вслепую: в справочнике SalesDoc склады есть, а какие из них
    # рабочие, а какие пустые — не видно.
    stats = salesdoc_mirror.orders_by_store(db)
    out = []
    seen: set = set()
    for w in whs:
        sid = w["sd_id"]
        seen.add(str(sid or "").lower())
        out.append({
            "store_id": sid,
            "code_1C": w["code_1C"],
            "name": w["name"],
            "org": saved[sid].organization if sid in saved else None,
            "stats": stats.get(str(sid or "").lower()),
        })
    # Склад, по которому есть отгрузки, но которого нет в справочнике, —
    # показываем отдельно, иначе его реализации остаются без объяснения.
    for sid, st in stats.items():
        if sid in seen:
            continue
        row = saved.get(sid)
        out.append({
            "store_id": sid or "(склад не указан)",
            "code_1C": None,
            "name": (row.name if row else None) or "(нет в справочнике SalesDoc)",
            "org": row.organization if row else None,
            "stats": st,
        })
    out.sort(key=lambda r: -((r["stats"] or {}).get("count") or 0))
    return {"warehouses": out, "orgs": list(models.ORGS)}


@router.post("/warehouses")
def save_warehouses(
    payload: list[StoreItem],
    db: Session = Depends(get_db),
    _: models.User = Depends(can_edit),
):
    for it in payload:
        if not it.store_id:
            continue
        row = db.query(models.SalesDocStore).filter_by(store_id=it.store_id).first()
        if row is None:
            row = models.SalesDocStore(store_id=it.store_id)
            db.add(row)
        row.name = it.name
        row.organization = it.org if it.org in models.ORGS else None
    db.commit()
    return {"status": "ok"}


def _require_configured():
    if not salesdoc.is_configured():
        raise HTTPException(
            status_code=503,
            detail="Интеграция SalesDoc не настроена: задайте SALESDOC_URL, "
                   "SALESDOC_LOGIN и SALESDOC_PASSWORD в окружении сервера.",
        )


def _fmt_som(v: float) -> str:
    return f"{round(v):,}".replace(",", " ") + " с"


def _sd_component(maps: dict, sd_id, code) -> float:
    """Сумма компонента SalesDoc для клиента: по SD_id + по коду 1С (запись
    попадает ровно в одну карту, поэтому двойного счёта нет)."""
    s = 0.0
    sid = (sd_id or "").lower()
    if sid and sid in maps["sd"]:
        s += maps["sd"][sid]
    if code is not None and str(code) in maps["code"]:
        s += maps["code"][str(code)]
    return round(s, 2)


def _diagnose_reason(row: dict, comp: dict) -> tuple[str, str]:
    """Короткий ярлык причины для колонки: одно-два слова — «реализации»,
    «возврат», «оплата» (или их сочетание). Если компоненты совпадают, но долг
    расходится — «баланс SD» либо «курс/период». Подробности — в карточке."""
    if not row["in_sd"]:
        return "bad", "нет в SD"
    if not row["in_1c"]:
        return "bad", "нет в 1С"
    empty = {"sd": {}, "code": {}}
    delta = round(row["sd_debt"] - row["our_debt"], 2)
    if abs(delta) < 1:
        return "ok", "сходится"
    # Баланс SalesDoc считает долгом только доставленное: пока заказ в статусе
    # «Отправлен», он в баланс не входит. Если разница ровно на эту сумму —
    # расхождения нет, товар просто в пути.
    transit = _sd_component(comp.get("in_transit", empty),
                            row["sd_id"], row["code_1C"])
    if transit and abs(round(delta + transit, 2)) < 1:
        return "ok", "в пути"
    sd_sales = _sd_component(comp["sales"], row["sd_id"], row["code_1C"])
    sd_ret = _sd_component(comp["returns"], row["sd_id"], row["code_1C"])
    sd_pay = _sd_component(comp["payments"], row["sd_id"], row["code_1C"])
    # Списание долга в SalesDoc пары в 1С не имеет: там долг просто обнулили.
    # Если разница ровно на эту сумму — расхождение объяснено полностью, и
    # выяснять больше нечего.
    wroff = _sd_component(comp.get("debt_writeoff", empty),
                          row["sd_id"], row["code_1C"])
    row["sd_debt_writeoff"] = round(wroff, 2)
    if wroff and abs(round(delta + wroff, 2)) < 1:
        return "warn", "списание долга в SD"

    # «В 1С реализаций больше» бывает по двум противоположным причинам, и
    # различить их можно, не открывая карточку: сравнить баланс SalesDoc с
    # тем, что складывается из его же журналов. Баланс считается по
    # доставленному, поэтому и сравниваем с доставленным. Если баланс знает
    # ровно недостающую сумму — документы в SalesDoc есть, просто выгрузка их
    # не отдаёт (так ведут себя документы деактивированных агентов), и в учёте
    # всё в порядке. Если не знает — отгрузку в SalesDoc действительно не
    # провели.
    sd_delivered = _sd_component(comp.get("delivered", empty),
                                 row["sd_id"], row["code_1C"])
    hidden = round(row["sd_debt"] - (sd_delivered - sd_ret - sd_pay), 2)
    sales_gap = round(row["our_sales"] - sd_sales, 2)
    row["sd_hidden"] = hidden
    sales_label = "реализации"
    if sales_gap >= 500 and abs(hidden - sales_gap) < 500:
        sales_label = "скрыто в SD"

    # Значимые расхождения по компонентам — коротким словом каждый.
    factors = [
        (sales_label, round(sd_sales - row["our_sales"], 2)),
        ("возврат", round(row["our_returns"] - sd_ret, 2)),
        ("оплата", round(row["our_pay"] - sd_pay, 2)),
    ]
    sig = sorted((f for f in factors if abs(f[1]) >= 500), key=lambda f: -abs(f[1]))
    if sig:
        return "warn", " · ".join(name for name, _ in sig)
    # Компоненты совпали, а долг расходится: баланс SD не отражает операции
    # (оплата не применена / входящий остаток) — либо курс/период.
    sd_txn_net = round(sd_sales - sd_ret - sd_pay, 2)
    if abs(round(row["sd_debt"] - sd_txn_net, 2)) >= 500:
        return "warn", "баланс SD"
    return "warn", "курс/период"


@router.get("/debt")
def reconcile_debt(
    db: Session = Depends(get_db),
    user: models.User = Depends(can_view),
    only_diff: bool = Query(default=False, description="Только строки с расхождением"),
    with_reason: bool = Query(default=True, description="Считать причину расхождения (из зеркала — бесплатно)"),
    refresh: bool = Query(default=False, description="Обновить данные SalesDoc (сбросить кэш)"),
    org: str = Query(default="all"),
):
    """Дебиторка: наш долг (из 1С) против баланса SalesDoc, по каждому клиенту.

    Данные SalesDoc берутся из кэша в памяти (быстро); refresh=true форсирует
    свежую выгрузку."""
    _require_configured()
    if refresh:
        # Пересборка идёт В ФОНЕ: пользователь не должен ждать выгрузку из
        # SalesDoc — список тут же отдаётся из зеркала, а свежие данные
        # доедут через несколько секунд сами.
        salesdoc.clear_cache()
        salesdoc_mirror.sync_async(full=True)

    mirror_ready = salesdoc_mirror.status(db)["ready"]
    if mirror_ready:
        # Читаем только из своей базы — мгновенно, без обращения к SalesDoc.
        sd_clients = salesdoc_mirror.clients_for_reconcile(db)
    else:
        # Зеркало ещё наполняется (первый запуск) — разово берём напрямую.
        try:
            sd_balance = salesdoc.fetch_balance(use_cache=True)
            sd_clients = salesdoc.fetch_clients(use_cache=True)
        except salesdoc.SalesDocError as e:
            raise HTTPException(status_code=502, detail=str(e))
        debt_by_id: dict[str, float] = {}
        for r in sd_balance:
            sid = (r["sd_id"] or "").lower()
            if sid:
                debt_by_id[sid] = debt_by_id.get(sid, 0.0) + r["debt"]
        sd_clients = [{**c, "debt": round(debt_by_id.get(
            (c["sd_id"] or "").lower(), 0.0), 2)} for c in sd_clients]

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
            "debt": c.get("debt", 0.0),
        }
        if sid:
            sd_by_id[sid] = entry
        if c["code_1C"]:
            sd_by_code[str(c["code_1C"])] = entry
        k = _match_key(c["name"])
        if k:
            sd_by_name.setdefault(k, entry)

    # Фирма клиента SalesDoc — по складам его заказов. Нужна, чтобы при выборе
    # одной фирмы показывать только её точки (в т.ч. «только SD»).
    o = (org or "").strip().lower()
    client_orgs = None  # sd_id → {"orgs": {фирмы}, "unmapped": [склады без фирмы]}
    if o in models.ORGS:
        if mirror_ready:
            client_orgs = salesdoc_mirror.client_store_orgs(db)
        else:
            store_org = {
                s.store_id.lower(): s.organization
                for s in db.query(models.SalesDocStore).all()
                if s.store_id and s.organization
            }
            if store_org:
                df, dt = salesdoc.reason_window()
                try:
                    live = salesdoc.fetch_client_store_orgs(
                        store_org, df, dt, use_cache=True
                    )
                    client_orgs = {
                        k: {"orgs": v, "unmapped": []} for k, v in live.items()
                    }
                except salesdoc.SalesDocError:
                    client_orgs = None

    rec = receivables(db=db, _=user, org=org)
    rows = []
    matched_ids: set[str] = set()

    links = {
        l.client_1c: l.sd_id.lower()
        for l in db.query(models.SalesDocClientLink).all()
    }
    for c in rec["clients"]:
        name = c["client"]
        our_debt = round(c["debt"], 2)
        client_org = c.get("organization")
        # 0) ручная связка, 1) ИД SalesDoc из имени, 2) код 1С, 3) имя
        entry = None
        if name in links and links[name] in sd_by_id:
            entry = sd_by_id[links[name]]
        if entry is None:
            sid = _extract_sd_id(name)
            if sid and sid in sd_by_id:
                entry = sd_by_id[sid]
        else:
            sid = None
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
            "organization": client_org,
            # Компоненты долга 1С — для «причины расхождения».
            "our_sales": round(c.get("shipped", 0.0), 2),
            "our_returns": round(c.get("returned", 0.0), 2),
            "our_pay": round(c.get("paid", 0.0), 2),
        })

    # Клиенты, которые есть только в SalesDoc и там висит долг.
    for entry in sd_by_id.values():
        if entry["sd_id"] in matched_ids or abs(entry["debt"]) < 0.5:
            continue
        info = client_orgs.get(entry["sd_id"]) if client_orgs is not None else None
        entry_orgs = (info or {}).get("orgs") or set()
        # При выбранной фирме показываем только её точки. Фирму точки SalesDoc
        # определяем по складам её реализаций — но склад известен не всегда,
        # и тогда мы не прячем строку молча, а показываем и объясняем почему
        # (иначе «почему этот клиент здесь?» невозможно понять из интерфейса).
        org_note, org_note_warn = None, False
        if o in models.ORGS and client_orgs is not None:
            if o in entry_orgs:
                # Пишем и когда всё в порядке: вопрос «почему эта точка здесь?»
                # возникает именно к строкам без пары в 1С, и ответ должен быть
                # виден сразу, а не выясняться расследованием.
                named = (info or {}).get("stores", {}).get(o) or []
                org_note = ("реализации на складах этой фирмы: " + ", ".join(
                    f"{s['name']} — {s['count']} шт., "
                    f"{s['first']} … {s['last']}" for s in named
                )) if named else None
            elif info is None:
                org_note = "фирма не определена: в SalesDoc нет реализаций этой точки"
                org_note_warn = True
            elif info.get("unmapped"):
                org_note = (
                    "фирма не определена: реализации на складах без фирмы — "
                    + ", ".join(s["name"] for s in info["unmapped"])
                )
                org_note_warn = True
            else:
                continue  # точка другой фирмы
        row_org = list(entry_orgs)[0] if len(entry_orgs) == 1 else None
        rows.append({
            "org_note": org_note,
            "org_note_warn": org_note_warn,
            "name": entry["name"],
            "our_debt": 0.0,
            "sd_debt": entry["debt"],
            "diff": round(-entry["debt"], 2),
            "in_1c": False,
            "in_sd": True,
            "code_1C": entry["code_1C"],
            "sd_id": entry["sd_id"],
            "organization": row_org,  # фирма по складам заказов SalesDoc
            "our_sales": 0.0,
            "our_returns": 0.0,
            "our_pay": 0.0,
        })

    rows.sort(key=lambda x: -abs(x["diff"]))

    # «Свежесть» расхождения: когда точка впервые попала в этот список. Момент
    # «разъехалось» не знает ни одна из систем — его видит только сама сверка,
    # поэтому отмечаем его здесь. Ключ включает фирму: списки по фирмам разные,
    # и событие у каждой своё. Ушедшее расхождение забываем — вернётся, значит
    # случилось заново.
    now = datetime.utcnow()
    prefix = (o if o in models.ORGS else "all") + ":"
    seen = {
        d.key: d for d in db.query(models.SalesDocDiffSeen)
        .filter(models.SalesDocDiffSeen.key.startswith(prefix)).all()
    }
    active: dict[str, models.SalesDocDiffSeen | None] = {}
    dirty = False
    for r in rows:
        if abs(r["diff"]) < 0.5:
            r["appeared_at"] = None
            continue
        k = prefix + (r.get("sd_id") or r["name"])
        row = active.get(k) or seen.get(k)
        if row is None:
            row = models.SalesDocDiffSeen(key=k, first_seen=now)
            db.add(row)
            dirty = True
        active[k] = row
        r["appeared_at"] = row.first_seen.isoformat()
    stale = [d.id for k, d in seen.items() if k not in active]
    if stale:
        db.query(models.SalesDocDiffSeen).filter(
            models.SalesDocDiffSeen.id.in_(stale)
        ).delete(synchronize_session=False)
        dirty = True
    if dirty:
        db.commit()

    # «Причина расхождения» по всем строкам сразу — одна массовая выгрузка из
    # SalesDoc (заказы/возвраты/оплаты), группируем по клиенту. Считаем только
    # по запросу: обычная загрузка дебиторки остаётся быстрой.
    # Причина расхождения считается по зеркалу — это обычная группировка в
    # базе, поэтому идёт всегда, без отдельной кнопки. Пока зеркало не
    # наполнено (первые секунды после старта), колонка просто пустая — список
    # из-за неё не тормозит.
    #
    # ВАЖНО: считаем ДО отбора «только расхождения». Документы, скрытые от
    # выгрузки, баланс SalesDoc учитывает — поэтому долги сходятся, и такие
    # точки в отфильтрованный список не попадают. Их масштаб виден только по
    # полному списку.
    hidden_rows: list[dict] = []
    if with_reason and salesdoc_mirror.status(db)["ready"]:
        df, dt = salesdoc.reason_window()
        comp = salesdoc_mirror.reconcile_components(
            db, date.fromisoformat(df), date.fromisoformat(dt),
            _store_ids_for_org(db, org),
        )
        for r in rows:
            lvl, txt = _diagnose_reason(r, comp)
            r["reason_level"] = lvl
            r["reason"] = txt
            if r["in_sd"] and r["in_1c"] and r.get("sd_hidden", 0) >= 500:
                hidden_rows.append({
                    "name": r["name"],
                    "sd_id": r["sd_id"],
                    "amount": r["sd_hidden"],
                    "reason": txt,
                })
        hidden_rows.sort(key=lambda x: -x["amount"])

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
        # Баланс SalesDoc — общий по клиенту (обе фирмы). При выборе одной
        # фирмы наш долг — только её, а долг SD — суммарный: это ожидаемо.
        "sd_account_wide": (org or "").strip().lower() in models.ORGS,
        "synced_at": salesdoc.last_sync(),  # epoch последнего обновления кэша
        # Склады без привязки к фирме: их реализации попадают в обе фирмы,
        # поэтому о них честно предупреждаем.
        "unmapped_stores": _unmapped_stores(db),
        # Документы, которые SalesDoc учитывает в балансе, но не отдаёт в
        # выгрузке. Считается по всем точкам, а не только по видимым в списке.
        "hidden": {
            "clients": len(hidden_rows),
            "amount": round(sum(h["amount"] for h in hidden_rows), 2),
            "top": hidden_rows[:20],
        },
        "rows": rows,
    }


@router.get("/warehouse-report")
def warehouse_report(
    db: Session = Depends(get_db),
    _: models.User = Depends(can_view),
    date_from: date = Query(...),
    date_to: date = Query(...),
):
    """Отчёт по складам SalesDoc: реализации, возвраты, остаток за период."""
    _require_configured()
    store_org = {
        s.store_id.lower(): s.organization
        for s in db.query(models.SalesDocStore).all()
        if s.store_id and s.organization
    }
    try:
        return salesdoc.warehouse_report(date_from.isoformat(), date_to.isoformat(), store_org)
    except salesdoc.SalesDocError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/payments-debug")
def payments_debug(
    _: models.User = Depends(can_view),
    date_from: date = Query(...),
    date_to: date = Query(...),
):
    """Диагностика оплат SalesDoc: с клиентом / без, по видам и типам."""
    _require_configured()
    try:
        return salesdoc.payments_diagnostic(date_from.isoformat(), date_to.isoformat())
    except salesdoc.SalesDocError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/returns-debug")
def returns_debug(
    db: Session = Depends(get_db),
    _: models.User = Depends(can_view),
    date_from: date = Query(...),
    date_to: date = Query(...),
    org: str = Query(default="all"),
):
    """Сравнение источников возвратов SalesDoc (getOrderDefect vs «Возврат с
    полки», тип 9) с возвратами 1С за период — чтобы понять, какой источник
    совпадает и нет ли задвоения."""
    _require_configured()
    try:
        data = salesdoc.returns_diagnostic(date_from.isoformat(), date_to.isoformat())
    except salesdoc.SalesDocError as e:
        raise HTTPException(status_code=502, detail=str(e))
    # Возвраты 1С за тот же период — эталон для сравнения.
    q = (
        models.org_scope(db.query(models.ReturnDoc), models.ReturnDoc, org)
        .filter(models.ReturnDoc.date >= date_from, models.ReturnDoc.date <= date_to)
    )
    rows = q.all()
    data["one_c"] = {
        "count": len(rows),
        "total": round(sum(float(r.amount) for r in rows), 2),
    }
    return data


@router.get("/speed-probe")
def speed_probe(
    db: Session = Depends(get_db),
    user: models.User = Depends(can_view),
    org: str = Query(default="all"),
):
    """Замер перед ускорением карточки клиента: соблюдает ли SalesDoc фильтр по
    клиенту и работает ли инкремент по dateUpdate. Клиента берём сам — первый
    из сверки, у которого есть ИД SalesDoc."""
    _require_configured()
    data = reconcile_debt(db=db, user=user, only_diff=False, org=org)
    target = next((r for r in data["rows"] if r.get("sd_id") and r["in_sd"]), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Не нашёл клиента с ИД SalesDoc")
    df, dt = salesdoc.reason_window()
    try:
        res = salesdoc.speed_probe(target["sd_id"], target.get("code_1C"), df, dt)
    except salesdoc.SalesDocError as e:
        raise HTTPException(status_code=502, detail=str(e))
    res["client_name"] = target["name"]
    return res


@router.get("/analyze")
def analyze_structure(
    db: Session = Depends(get_db),
    _: models.User = Depends(can_view),
    date_from: date = Query(...),
    date_to: date = Query(...),
):
    """Диагностика структуры SalesDoc по фактическим данным за период."""
    _require_configured()
    store_org = {
        s.store_id.lower(): s.organization
        for s in db.query(models.SalesDocStore).all()
        if s.store_id and s.organization
    }
    try:
        return salesdoc.analyze(date_from.isoformat(), date_to.isoformat(), store_org)
    except salesdoc.SalesDocError as e:
        raise HTTPException(status_code=502, detail=str(e))


class LinkItem(BaseModel):
    client_1c: str
    sd_id: str


@router.get("/matching")
def matching(
    db: Session = Depends(get_db),
    user: models.User = Depends(can_view),
    org: str = Query(default="all"),
):
    """Сводка сопоставления точек: сколько совпало, и списки несовпавших с
    обеих сторон + подсказки похожих (для ручной связки)."""
    data = reconcile_debt(db=db, user=user, only_diff=False, org=org)
    rows = data["rows"]
    only_1c = sorted(
        (r for r in rows if r["in_1c"] and not r["in_sd"]),
        key=lambda r: -abs(r["our_debt"]),
    )
    only_sd = sorted(
        (r for r in rows if r["in_sd"] and not r["in_1c"]),
        key=lambda r: -abs(r["sd_debt"]),
    )
    sd_keys = [(r, _match_key(r["name"])) for r in only_sd]

    def suggest(name: str):
        k = _match_key(name)
        scored = []
        for r, sk in sd_keys:
            score = difflib.SequenceMatcher(None, k, sk).ratio()
            if score >= 0.55:
                scored.append((score, r))
        scored.sort(key=lambda x: -x[0])
        return [
            {"sd_id": r["sd_id"], "name": r["name"], "sd_debt": r["sd_debt"],
             "score": round(s, 2)}
            for s, r in scored[:3]
        ]

    return {
        "matched": data["matched"],
        "only_1c_count": len(only_1c),
        "only_sd_count": len(only_sd),
        "only_1c": [
            {"name": r["name"], "our_debt": r["our_debt"],
             "organization": r["organization"], "suggestions": suggest(r["name"])}
            for r in only_1c
        ],
        "only_sd": [
            {"sd_id": r["sd_id"], "name": r["name"], "sd_debt": r["sd_debt"],
             "organization": r["organization"]}
            for r in only_sd
        ],
    }


@router.post("/link")
def create_link(
    payload: LinkItem,
    db: Session = Depends(get_db),
    _: models.User = Depends(can_edit),
):
    client = payload.client_1c.strip()
    sd_id = payload.sd_id.strip()
    if not client or not sd_id:
        raise HTTPException(status_code=400, detail="Нужны контрагент 1С и клиент SalesDoc")
    row = db.query(models.SalesDocClientLink).filter_by(client_1c=client).first()
    if row is None:
        row = models.SalesDocClientLink(client_1c=client)
        db.add(row)
    row.sd_id = sd_id
    db.commit()
    return {"status": "ok"}


@router.delete("/link/{client_1c:path}", status_code=204)
def delete_link(
    client_1c: str,
    db: Session = Depends(get_db),
    _: models.User = Depends(can_edit),
):
    db.query(models.SalesDocClientLink).filter_by(client_1c=client_1c).delete()
    db.commit()


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
    store_ids = _store_ids_for_org(db, org)  # реализации делим по складу
    try:
        sd_orders = salesdoc.fetch_orders_total(df, dt, store_ids, use_cache=True)
        sd_payments = salesdoc.fetch_payments_total(df, dt, use_cache=True)
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
    db: Session = Depends(get_db),
    _: models.User = Depends(can_view),
    sd_id: str | None = Query(default=None),
    code_1c: str | None = Query(default=None),
    date_from: date = Query(...),
    date_to: date = Query(...),
    org: str = Query(default="all"),
):
    """Детализация клиента в SalesDoc за период: реализации (со статусами),
    оплаты, возвраты. Каждый блок изолирован — сбой одного не рушит остальные."""
    _require_configured()
    if not sd_id and not code_1c:
        raise HTTPException(status_code=400, detail="Нужен sd_id или code_1c клиента")
    df, dt = date_from.isoformat(), date_to.isoformat()
    store_ids = _store_ids_for_org(db, org)  # реализации делим по складу

    # Читаем из зеркала — мгновенно, без обращения к SalesDoc. Свежесть
    # поддерживает фоновая синхронизация (дельта каждые 5 минут).
    state = salesdoc_mirror.status(db)
    if state["ready"]:
        data = salesdoc_mirror.client_detail(
            db, sd_id, code_1c, date_from, date_to, store_ids
        )
        data.update({"sd_id": sd_id, "code_1c": code_1c, "date_from": df,
                     "date_to": dt, "source": "mirror",
                     "synced_at": state["synced_at"]})
        return data

    # Зеркало ещё не наполнено (первый запуск) — отдаём напрямую из SalesDoc.
    def safe(fn, *extra):
        try:
            return fn(sd_id, code_1c, df, dt, *extra), None
        except salesdoc.SalesDocError as e:
            return None, str(e)

    orders, e1 = safe(salesdoc.fetch_client_orders, store_ids)
    payments, e2 = safe(salesdoc.fetch_client_payments)
    # getOrderDefect за 3 года пуст (проверено замером) — возвраты живут в
    # журнале оплат как «Возврат с полки», их и берём.
    shelf = (payments or {}).pop("shelf_returns", None) or {
        "total": 0.0, "count": 0, "items": []}
    return {
        "sd_id": sd_id,
        "code_1c": code_1c,
        "date_from": df,
        "date_to": dt,
        "orders": orders,
        "payments": payments,
        "returns": shelf,
        "source": "live",
        "synced_at": None,
        "errors": [e for e in (e1, e2) if e],
    }


@router.get("/stock")
def stock(
    db: Session = Depends(get_db),
    _: models.User = Depends(can_view),
    q: str | None = Query(default=None, description="Поиск по названию товара"),
    all_items: bool = Query(default=False, description="Показывать и нулевые остатки"),
    org: str = Query(default="all"),
):
    """Остатки SalesDoc по складам и позициям (в штуках).

    Читается из зеркала — мгновенно. Сумм в остатках SalesDoc не отдаёт, только
    количество."""
    rows = salesdoc_mirror.stock_by_store(
        db, _store_ids_for_org(db, org), q, only_positive=not all_items
    )
    state = salesdoc_mirror.status(db)
    return {"warehouses": rows, "synced_at": state["synced_at"],
            "ready": state["ready"]}


@router.get("/client-debug")
def client_debug(
    db: Session = Depends(get_db),
    _: models.User = Depends(can_view),
    sd_id: str | None = Query(default=None),
    code_1c: str | None = Query(default=None),
):
    """Разбор «в SalesDoc операция есть, а в портале нет» по одному клиенту:
    что лежит в зеркале и что отдаёт SalesDoc напрямую."""
    _require_configured()
    if not sd_id and not code_1c:
        raise HTTPException(status_code=400, detail="Нужен sd_id или code_1c")
    df, dt = salesdoc.reason_window()

    sid = (sd_id or "").lower() or None
    def mirror_count(model):
        from sqlalchemy import or_
        conds = []
        if sid:
            conds.append(model.client_sd_id == sid)
        if code_1c:
            conds.append(model.client_code_1c == str(code_1c))
        if not conds:
            return 0
        return db.query(model).filter(or_(*conds)).count()

    try:
        live = salesdoc.client_debug(sd_id, code_1c, df, dt)
    except salesdoc.SalesDocError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return {
        "mirror": {
            "orders": mirror_count(models.SalesDocOrder),
            "payments": db.query(models.SalesDocPayment).filter(
                models.SalesDocPayment.client_sd_id == sid).count() if sid else 0,
            "orders_total_rows": db.query(models.SalesDocOrder).count(),
            "payments_total_rows": db.query(models.SalesDocPayment).count(),
            "synced_at": salesdoc_mirror.status(db)["synced_at"],
        },
        "live": live,
    }


@router.get("/mirror")
def mirror_status(
    db: Session = Depends(get_db),
    _: models.User = Depends(can_view),
):
    """Свежесть зеркала SalesDoc: когда обновлялось, сколько записей."""
    return salesdoc_mirror.status(db)


def _doc_key(value) -> str | None:
    """Ключ документа для сопоставления 1С ↔ SalesDoc.

    В SalesDoc номер накладной 1С лежит в code_1C — но иногда там оказывается
    служебный GUID (обмен отработал наполовину). GUID ключом быть не может, его
    отбрасываем."""
    s = re.sub(r"\s+", "", str(value or "")).lower()
    if not s or len(s) > 30 or re.fullmatch(r"[0-9a-f-]{32,}", s):
        return None
    return s


def _doc_tail(key: str | None) -> str | None:
    """Запасной ключ: последняя группа цифр без ведущих нулей («0000-000331» →
    «331»). Нужен, когда в системах разный префикс нумерации."""
    if not key:
        return None
    groups = re.findall(r"\d+", key)
    if not groups:
        return None
    tail = groups[-1].lstrip("0")
    return tail if len(tail) >= 2 else None


LIST_CAP = 300  # сколько строк отдаём в каждый список; остальное — счётчиком


@router.get("/shipments-compare")
def shipments_compare(
    db: Session = Depends(get_db),
    _: models.User = Depends(can_view),
    date_from: date = Query(...),
    date_to: date = Query(...),
    org: str = Query(default="all"),
):
    """Сверка реализаций 1С ↔ SalesDoc одной таблицей: у каждой реализации
    видно склад и сумму с обеих сторон, статус SalesDoc и вердикт.

    Сопоставляем по номеру накладной (в SalesDoc он приходит в code_1C после
    обмена с 1С), а если номера нет — по клиенту, дате и сумме. Второй способ
    оказался основным: в выгрузке продаж 1С номер документа заполнен не всегда.
    """
    o = (org or "").strip().lower()

    # --- 1С: строки продаж сводим в документы ---
    sales_q = models.org_scope(
        db.query(models.Sale).filter(models.Sale.date >= date_from,
                                     models.Sale.date <= date_to),
        models.Sale, org,
    )
    # Без номера документа строки одного дня по одному клиенту всё равно надо
    # свести в документ — иначе каждая позиция накладной станет отдельной
    # «реализацией» и сверять будет нечего.
    our_docs: dict = {}
    for s in sales_q.all():
        line = float(s.amount) * (1 - float(s.discount_pct or 0) / 100)
        key = (s.doc_number or f"~{s.client}|{s.date.isoformat()}")
        d = our_docs.get(key)
        if d is None:
            d = our_docs[key] = {
                "date": s.date.isoformat(),
                "doc_number": s.doc_number,
                "client": s.client,
                "warehouse": s.warehouse or None,
                "amount": 0.0,
                "doc_total": None,
                "positions": 0,
            }
        d["amount"] += line
        d["positions"] += 1
        if s.warehouse and not d["warehouse"]:
            d["warehouse"] = s.warehouse
        if s.doc_total is not None:
            d["doc_total"] = float(s.doc_total)
    our_list = []
    for d in our_docs.values():
        amt = d["doc_total"] if d["doc_total"] is not None else d["amount"]
        our_list.append({**d, "amount": round(float(amt), 2)})

    # --- SalesDoc: реализации из зеркала, только отгруженные ---
    store_ids = _store_ids_for_org(db, org)
    store_names = {
        s.store_id.lower(): (s.name or s.store_id)
        for s in db.query(models.SalesDocStore).all() if s.store_id
    }
    sd_q = db.query(models.SalesDocOrder).filter(
        models.SalesDocOrder.date >= date_from,
        models.SalesDocOrder.date <= date_to,
        models.SalesDocOrder.status.in_(list(salesdoc.SHIPPED_STATUSES)),
    )
    if store_ids:
        sd_q = sd_q.filter(models.SalesDocOrder.store_sd_id.in_(store_ids))
    sd_list = [
        {
            "date": r.date and r.date.isoformat(),
            "doc_number": r.code_1c,
            "sd_id": r.sd_id,
            "client_sd_id": r.client_sd_id or "",
            "client": r.client_sd_id or r.client_code_1c or "",
            "store": store_names.get(r.store_sd_id or "", r.store_sd_id or "(склад не указан)"),
            "status": r.status,
            "status_label": salesdoc.ORDER_STATUS.get(r.status, str(r.status)),
            "amount": round(float(r.amount or 0), 2),
        }
        for r in sd_q.all()
    ]
    # Имена точек — чтобы в таблице стояло название, а не «p4_1244».
    names = {c.sd_id: c.name for c in db.query(models.SalesDocClient).all()}
    for r in sd_list:
        r["client"] = names.get(r["client"], r["client"])
        r["_ckey"] = _match_key(r["client"])

    # Неотгруженные заявки («Новый»). В суммы отгрузок они не входят, но для
    # непарных документов 1С важно отличать «в SalesDoc нет вовсе» от «есть,
    # но застряло в Новых» — лечатся они по-разному: первое заводится, второе
    # проводится. Классический пример — Алым Дан с заявками, забытыми в Новых.
    new_q = db.query(models.SalesDocOrder).filter(
        models.SalesDocOrder.date >= date_from,
        models.SalesDocOrder.date <= date_to,
        models.SalesDocOrder.status == 1,
    )
    if store_ids:
        new_q = new_q.filter(models.SalesDocOrder.store_sd_id.in_(store_ids))
    new_list = [
        {
            "date": r.date and r.date.isoformat(),
            "doc_number": r.code_1c,
            "sd_id": r.sd_id,
            "client_sd_id": r.client_sd_id or "",
            "client": names.get(r.client_sd_id or "",
                                r.client_sd_id or r.client_code_1c or ""),
            "store": store_names.get(r.store_sd_id or "", r.store_sd_id or ""),
            "status": r.status,
            "status_label": salesdoc.ORDER_STATUS.get(r.status, str(r.status)),
            "amount": round(float(r.amount or 0), 2),
        }
        for r in new_q.all()
    ]
    for r in new_list:
        r["_ckey"] = _match_key(r["client"])

    # --- Индексы для сопоставления ---
    def build_indexes(rows_in):
        by_key: dict = {}      # по номеру накладной
        by_tail: dict = {}     # по числовому хвосту номера
        by_client: dict = {}   # по (клиент, дата) — когда номера нет
        by_client_any: dict = {}  # по клиенту без даты — последняя надежда
        for r in rows_in:
            k = _doc_key(r["doc_number"])
            if k:
                by_key.setdefault(k, []).append(r)
                t = _doc_tail(k)
                if t:
                    by_tail.setdefault(t, []).append(r)
            for ck in {r["client_sd_id"], r["_ckey"]}:
                if ck:
                    by_client.setdefault((ck, r["date"]), []).append(r)
                    by_client_any.setdefault(ck, []).append(r)
        return by_key, by_tail, by_client, by_client_any

    sd_by_key, sd_by_tail, sd_by_client, sd_by_client_any = build_indexes(sd_list)
    new_by_key, new_by_tail, new_by_client, new_by_client_any = build_indexes(new_list)

    used: set = set()
    rows: list[dict] = []
    by_number = 0

    def pick(pool: list, amount: float):
        """Из подходящих документов берём сначала совпадающий по сумме — иначе
        две отгрузки одного дня встали бы в пару крест-накрест."""
        free = [r for r in pool if id(r) not in used]
        exact = next((r for r in free if abs(r["amount"] - amount) < 0.5), None)
        return exact or (free[0] if free else None)

    for d in our_list:
        k = _doc_key(d["doc_number"])
        pair = pick(sd_by_key.get(k, []) or sd_by_tail.get(_doc_tail(k), []), d["amount"])
        how = "номер" if pair else None
        if pair is None:
            ckeys = {_extract_sd_id(d["client"]), _match_key(d["client"])}
            # Даты SalesDoc сдвинуты относительно 1С на день-два (dateDocument
            # проставляется при проведении) — ищем с допуском, от ближней даты
            # к дальней, и останавливаемся, как только нашли точную сумму.
            base = date.fromisoformat(d["date"])
            pool = []
            for delta in (0, 1, -1, 2, -2, 3, -3):
                dd = (base + timedelta(days=delta)).isoformat()
                for ck in ckeys:
                    if ck:
                        pool.extend(sd_by_client.get((ck, dd), []))
                if any(abs(r["amount"] - d["amount"]) < 0.5
                       and id(r) not in used for r in pool):
                    break
            pair = pick(pool, d["amount"])
            how = "клиент + дата" if pair else None
        if pair is None:
            # Дату документа в SalesDoc могли поменять при правке (у заказов
            # встречается «изменил Admin» через недели после отгрузки) — тогда
            # окно в несколько дней не помогает. Последняя попытка: тот же
            # клиент и ровно та же сумма, дата любая. Совпадение суммы до
            # копеек делает такую пару достаточно надёжной.
            for ck in {_extract_sd_id(d["client"]), _match_key(d["client"])}:
                if not ck:
                    continue
                cand = pick(sd_by_client_any.get(ck, []), d["amount"])
                if cand is not None and abs(cand["amount"] - d["amount"]) < 0.5:
                    pair, how = cand, "клиент + сумма"
                    break
        # Среди отгруженных пары нет — ищем в «Новых»: заявка может просто
        # висеть не проведённой.
        stuck = None
        if pair is None:
            k2 = _doc_key(d["doc_number"])
            stuck = pick(new_by_key.get(k2, [])
                         or new_by_tail.get(_doc_tail(k2), []), d["amount"])
            if stuck is None:
                base = date.fromisoformat(d["date"])
                pool2 = []
                for delta in (0, 1, -1, 2, -2, 3, -3):
                    dd = (base + timedelta(days=delta)).isoformat()
                    for ck in {_extract_sd_id(d["client"]), _match_key(d["client"])}:
                        if ck:
                            pool2.extend(new_by_client.get((ck, dd), []))
                    if any(abs(r["amount"] - d["amount"]) < 0.5
                           and id(r) not in used for r in pool2):
                        break
                stuck = pick(pool2, d["amount"])
                if stuck is None:  # та же поблажка по дате, что и выше
                    for ck in {_extract_sd_id(d["client"]), _match_key(d["client"])}:
                        if not ck:
                            continue
                        cand = pick(new_by_client_any.get(ck, []), d["amount"])
                        if cand is not None and abs(cand["amount"] - d["amount"]) < 0.5:
                            stuck = cand
                            break
        if pair is not None:
            used.add(id(pair))
            if how == "номер":
                by_number += 1
        elif stuck is not None:
            used.add(id(stuck))
            pair, how = stuck, "клиент + дата"
        same = pair is not None and stuck is None \
            and abs(d["amount"] - pair["amount"]) < 0.5
        rows.append({
            "date": d["date"],
            "client": d["client"],
            "doc_number": d["doc_number"],
            "our_warehouse": d["warehouse"],
            "our_amount": d["amount"],
            # Дата пары в SalesDoc: если она разъехалась с 1С, это видно сразу.
            "sd_date": pair and pair["date"],
            "sd_doc": pair and (pair["doc_number"] or pair["sd_id"]),
            "sd_store": pair and pair["store"],
            "sd_status": pair and pair["status_label"],
            "sd_amount": pair and pair["amount"],
            "diff": round(d["amount"] - pair["amount"], 2) if pair else None,
            "matched_by": how,
            "verdict": ("new_sd" if stuck is not None
                        else "ok" if same
                        else "diff" if pair else "only_1c"),
        })
    for r in sd_list:
        if id(r) in used:
            continue
        rows.append({
            "date": r["date"],
            "client": r["client"],
            "doc_number": None,
            "our_warehouse": None,
            "our_amount": None,
            "sd_doc": r["doc_number"] or r["sd_id"],
            "sd_store": r["store"],
            "sd_status": r["status_label"],
            "sd_amount": r["amount"],
            "diff": None,
            "matched_by": None,
            "verdict": "only_sd",
        })

    rows.sort(key=lambda x: (x["date"] or ""), reverse=True)
    counts = {v: sum(1 for r in rows if r["verdict"] == v)
              for v in ("ok", "diff", "only_1c", "only_sd", "new_sd")}

    def by_store(items, key) -> list[dict]:
        agg: dict = {}
        for r in items:
            name = r.get(key) or "(склад не указан)"
            a = agg.setdefault(name, {"name": name, "count": 0, "amount": 0.0})
            a["count"] += 1
            a["amount"] += r["amount"]
        for a in agg.values():
            a["amount"] = round(a["amount"], 2)
        return sorted(agg.values(), key=lambda x: -x["count"])

    return {
        "period": {"from": date_from.isoformat(), "to": date_to.isoformat()},
        "org": o,
        "our": {
            "count": len(our_list),
            "amount": round(sum(d["amount"] for d in our_list), 2),
            "by_store": by_store(our_list, "warehouse"),
            # Сколько документов 1С пришло без номера: пока их много, сверять
            # по номеру нечем и всё держится на «клиент + дата».
            "no_number": sum(1 for d in our_list if not d["doc_number"]),
            "no_warehouse": sum(1 for d in our_list if not d["warehouse"]),
        },
        "sd": {
            "count": len(sd_list),
            "amount": round(sum(r["amount"] for r in sd_list), 2),
            "by_store": by_store(sd_list, "store"),
        },
        "counts": counts,
        "matched_by_number": by_number,
        "rows": rows[:LIST_CAP],
        "total_rows": len(rows),
        "cap": LIST_CAP,
        "unmapped_stores": _unmapped_stores(db),
    }


@router.get("/store-orders")
def store_orders(
    db: Session = Depends(get_db),
    _: models.User = Depends(can_view),
    store_id: str = Query(default="", description="SD_id склада; пусто — склад не указан"),
):
    """Реализации, лежащие на конкретном складе SalesDoc."""
    rows = salesdoc_mirror.store_orders(db, store_id)
    return {
        "store_id": store_id,
        "count": len(rows),
        "amount": round(sum(r["amount"] for r in rows if r["counted"]), 2),
        "items": rows,
    }


@router.get("/why")
def why_here(
    db: Session = Depends(get_db),
    user: models.User = Depends(can_view),
    query: str = Query(..., min_length=2, description="Часть имени точки"),
    org: str = Query(default="all"),
):
    """Почему точка есть (или её нет) в списке сверки выбранной фирмы.

    Вопрос «почему эта точка здесь, а соседняя нет» возникает постоянно, и
    ответ каждый раз собирался расследованием. Здесь он выдаётся по шагам: есть
    ли точка в 1С по каждой фирме, какой у неё долг в SalesDoc и на складах
    какой фирмы лежат её реализации."""
    o = (org or "").strip().lower()
    q = query.strip().lower()

    sd_clients = salesdoc_mirror.clients_for_reconcile(db)
    hits = [c for c in sd_clients if q in (c["name"] or "").lower()][:20]
    client_orgs = salesdoc_mirror.client_store_orgs(db)
    links = {
        _match_key(l.client_1c): l.sd_id.lower()
        for l in db.query(models.SalesDocClientLink).all()
    }

    # Дебиторка 1С по каждой фирме отдельно — точка может быть заведена в одной
    # и отсутствовать в другой, и именно это чаще всего всё и объясняет.
    # ВАЖНО: это дебиторка, то есть точки, по которым в 1С ЕСТЬ ОПЕРАЦИИ.
    # Карточка контрагента может быть заведена в обеих фирмах, но если продаж и
    # оплат по ней у фирмы нет, сюда она не попадёт — и «нет в 1С» означает
    # именно «нет операций», а не «нет контрагента».
    per_org: dict[str, dict] = {}
    for firm in models.ORGS:
        rec = receivables(db=db, _=user, org=firm)
        per_org[firm] = {_match_key(c["client"]): c for c in rec["clients"]}

    out = []
    for c in hits:
        sid = (c["sd_id"] or "").lower()
        key = _match_key(c["name"])
        info = client_orgs.get(sid) or {}
        stores_orgs = sorted(info.get("orgs") or [])
        debt = round(float(c.get("debt") or 0), 2)
        in_1c = {
            firm: (per_org[firm].get(key) or {}).get("debt")
            for firm in models.ORGS
        }
        # Если точного совпадения имени нет — показываем ближайшие. Так видно
        # разницу между «операций по точке у фирмы нет» и «операции есть, но
        # имя написано иначе и склейка не сработала».
        similar: dict[str, list] = {}
        for firm in models.ORGS:
            if in_1c[firm] is not None:
                continue
            close = difflib.get_close_matches(key, list(per_org[firm]), n=3, cutoff=0.55)
            similar[firm] = [
                {"name": per_org[firm][k]["client"],
                 "debt": round(float(per_org[firm][k]["debt"]), 2)}
                for k in close
            ]
        if o in models.ORGS:
            if in_1c.get(o) is not None:
                verdict = ("в списке как «обе»: по точке есть операции в 1С "
                           "этой фирмы — если суммы сходятся, при включённом "
                           "«только расхождения» строки не видно")
            elif similar.get(o):
                verdict = ("в списке как «только SD», хотя похожая точка в 1С "
                           "этой фирмы есть: имена не совпали и склейка не "
                           "сработала — свяжите точки вручную")
            elif abs(debt) < 0.5:
                verdict = "не в списке: долг в SalesDoc нулевой"
            elif stores_orgs and o not in stores_orgs:
                verdict = ("не в списке: операций по точке в 1С этой фирмы "
                           "нет, а её реализации лежат на складах другой "
                           "фирмы — " + ", ".join(stores_orgs))
            elif not stores_orgs:
                verdict = ("в списке как «только SD»: операций в 1С этой фирмы "
                           "нет, а фирму определить не по чему — реализаций "
                           "на складах с привязкой у точки нет")
            else:
                verdict = ("в списке как «только SD»: операций в 1С этой фирмы "
                           "нет, а реализации лежат на её складах")
        else:
            verdict = "выбраны обе фирмы — фильтр по фирме не применяется"
        out.append({
            "sd_id": sid,
            "name": c["name"],
            "code_1C": c["code_1C"],
            "sd_debt": debt,
            "store_orgs": stores_orgs,
            "stores": {
                firm: [f"{s['name']} — {s['count']} шт., {s['first']} … {s['last']}"
                       for s in items]
                for firm, items in (info.get("stores") or {}).items()
            },
            "unmapped_stores": [s["name"] for s in (info.get("unmapped") or [])],
            "in_1c": in_1c,
            "similar": similar,
            "linked_manually": links.get(key) == sid,
            "verdict": verdict,
        })
    return {"org": o, "query": query, "clients": out}


@router.get("/api-probe")
def api_probe(
    db: Session = Depends(get_db),
    _: models.User = Depends(can_view),
    client: str = Query(default="", description="Часть имени или ИД точки"),
    doc_number: str = Query(default="", description="Номер заявки из интерфейса SalesDoc"),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
):
    """Глубокая проверка API SalesDoc — батарея замеров на живых данных.

    Повод: интерфейс SalesDoc показывает заявку, а getOrder её не отдаёт.
    Проверяем всё, из-за чего документ может теряться в самой выгрузке:
    целостность пагинации (заявленный total против фактически отданного),
    дубликаты ИД, скрытые статусы вне 1–5, поведение каждого ключа фильтра
    периода, и полный сырой список заказов точки со всеми полями дат и сумм."""
    _require_configured()
    from collections import Counter

    dfw, dtw = salesdoc.reason_window()
    base_filter = {"include": "all", "status": [1, 2, 3, 4, 5],
                   "period": {"date": {"from": dfw, "to": dtw}}}
    out: dict = {"window": {"from": dfw, "to": dtw}, "verdicts": []}
    try:
        # --- 1. Целостность пагинации: заявлено против получено ---
        _, pg = salesdoc.call("getOrder", {"limit": 1, "page": 1,
                                           "filter": base_filter})
        declared = int((pg or {}).get("total") or 0)
        rows = salesdoc.call_all("getOrder", ("orders", "order"),
                                 {"filter": base_filter})
        ids = [str(r.get("SD_id") or r.get("CS_id") or "") for r in rows]
        cnt = Counter(ids)
        dups = sorted(i for i, c in cnt.items() if c > 1)
        out["journal"] = {"declared_total": declared, "received": len(rows),
                          "unique": len(cnt), "duplicates": dups[:5]}
        if declared and declared != len(rows):
            out["verdicts"].append(
                f"Пагинация теряет строки: сервер заявляет {declared} записей, "
                f"а отдаёт {len(rows)} — часть журнала недоступна через API")
        if dups:
            out["verdicts"].append(
                "В журнале дубликаты ИД: " + ", ".join(dups[:5]))

        hist = Counter(str(r.get("status")) for r in rows)
        out["status_histogram"] = dict(hist)

        # --- 1б. Журнал БЕЗ фильтра периода ---
        # Ключевая проверка: все наши запросы идут с period.date. Если у
        # документа поле даты пустое или сервер фильтрует по другому полю,
        # такой документ выпадает из ЛЮБОГО запроса с периодом — и выглядит
        # как несуществующий, хотя в интерфейсе он есть.
        no_period_filter = {"include": "all", "status": [1, 2, 3, 4, 5]}
        _, pg_np = salesdoc.call("getOrder", {"limit": 1, "page": 1,
                                              "filter": no_period_filter})
        np_total = int((pg_np or {}).get("total") or 0)
        out["no_period"] = {"total": np_total, "extra": []}
        if np_total > declared:
            np_rows = salesdoc.call_all("getOrder", ("orders", "order"),
                                        {"filter": no_period_filter})
            have = set(ids)
            extra = [r for r in np_rows
                     if str(r.get("SD_id") or r.get("CS_id") or "") not in have]
            out["no_period"]["extra"] = [
                {
                    "sd_id": r.get("SD_id") or r.get("CS_id"),
                    "number": r.get("invoiceNumber") or r.get("code_1C"),
                    "client": (r.get("client") or {}).get("SD_id"),
                    "status": r.get("status"),
                    "amount": round(float(r.get("totalSummaAfterDiscount")
                                          or r.get("totalSumma") or 0), 2),
                    "dates": {k: v for k, v in r.items()
                              if "date" in k.lower() and v},
                }
                for r in extra[:20]
            ]
            out["verdicts"].append(
                f"БЕЗ фильтра периода приходит {np_total} документов, а с "
                f"фильтром — {declared}. Разница {np_total - declared}: эти "
                "документы выпадают из любого запроса с периодом. Причина "
                "найдена — фильтровать журнал по датам нельзя")

        # --- 1в. Филиал: не он ли отсекает документы ---
        # Если в настройках задан filial_id, мы подставляем его в каждый
        # запрос — и документы других филиалов просто не приходят. Это надо
        # исключить прежде, чем винить API.
        from ..config import settings as _st
        out["filial"] = {"configured": _st.salesdoc_filial or None}
        if _st.salesdoc_filial:
            _, pg_nf = salesdoc.call("getOrder", {"limit": 1, "page": 1,
                                                  "filter": no_period_filter},
                                     with_filial=False)
            nf_total = int((pg_nf or {}).get("total") or 0)
            out["filial"]["without_filial_total"] = nf_total
            if nf_total > np_total:
                out["verdicts"].append(
                    f"БЕЗ филиала приходит {nf_total} документов, с филиалом "
                    f"{np_total}. Причина найдена: настройка SALESDOC_FILIAL "
                    "отсекает документы других филиалов — её нужно убрать")

        # --- 2. Одна гигантская страница против пагинации ---
        result, _pg2 = salesdoc.call("getOrder", {"limit": 5000, "page": 1,
                                                  "filter": base_filter})
        big = salesdoc._pick(result, ("orders", "order"))
        out["big_page"] = {"received": len(big)}
        if len(big) != len(rows):
            if declared and len(rows) == declared:
                # Постраничная выгрузка получила всё заявленное — значит сервер
                # просто ограничивает размер одной страницы. Это не потеря
                # данных, а свойство API, которое пагинация компенсирует.
                out["big_page"]["note"] = (
                    f"сервер ограничивает страницу {len(big)} строками — "
                    "пагинация это компенсирует, потерь нет")
            else:
                out["verdicts"].append(
                    f"Одной страницей приходит {len(big)} строк, пагинацией "
                    f"{len(rows)} при заявленных {declared} — сервер отдаёт "
                    "разные наборы")

        # --- 3. Скрытые статусы: вдруг у документа статус вне 1–5 ---
        pool = rows
        ext_filter = {**base_filter, "status": list(range(0, 11))}
        try:
            ext_rows = salesdoc.call_all("getOrder", ("orders", "order"),
                                         {"filter": ext_filter})
            out["extended_statuses"] = {
                "received": len(ext_rows),
                "histogram": dict(Counter(str(r.get("status")) for r in ext_rows)),
            }
            if len(ext_rows) > len(rows):
                out["verdicts"].append(
                    f"Со статусами 0–10 журнал больше на "
                    f"{len(ext_rows) - len(rows)} строк — существуют статусы "
                    "вне 1–5, такие документы наша выгрузка не видела")
                pool = ext_rows
        except salesdoc.SalesDocError as e:
            out["extended_statuses"] = {"error": str(e)}

        # --- 4. Все заказы точки: сырые даты, суммы, полный набор полей ---
        cq = (client or "").strip().lower()
        if cq:
            names = {c.sd_id: c.name
                     for c in db.query(models.SalesDocClient).all()}
            target = {sid for sid, nm in names.items()
                      if cq in sid or cq in (nm or "").lower()}
            corders, all_keys = [], set()
            for r in pool:
                cli = r.get("client") or {}
                csd = str(cli.get("SD_id") or "").lower()
                ccs = str(cli.get("CS_id") or "").lower().split("-", 1)[-1]
                if csd not in target and ccs not in target:
                    continue
                all_keys |= set(r.keys())
                corders.append({
                    "sd_id": r.get("SD_id") or r.get("CS_id"),
                    "code_1C": r.get("code_1C"),
                    "status": r.get("status"),
                    "store": (r.get("store") or {}).get("SD_id"),
                    "totalSumma": r.get("totalSumma"),
                    "totalSummaAfterDiscount": r.get("totalSummaAfterDiscount"),
                    "totalReturnsSumma": r.get("totalReturnsSumma"),
                    # Все поля с датами как есть: если у «невидимой» заявки
                    # дата лежит в другом поле или с опечаткой — тут видно.
                    "dates": {k: v for k, v in r.items()
                              if "date" in k.lower() and v},
                })
            corders.sort(key=lambda x: str(x["dates"]), reverse=True)
            out["client_orders"] = {
                "query": client,
                "matched_clients": [names[i] for i in sorted(target)][:5],
                "count": len(corders),
                "all_keys": sorted(all_keys),
                "orders": corders[:60],
            }

        # --- 4б. Поиск заявки по номеру из интерфейса ---
        # В полях документа есть invoiceNumber — судя по всему, это и есть
        # номер, который виден в журнале SalesDoc («1961»). Ищем его по всему
        # журналу, без фильтра по клиенту: если заявка привязана не к тому
        # клиенту или с другой суммой — она всё равно найдётся.
        dn = (doc_number or "").strip().lower()
        if dn:
            # Ищем вхождением, а не точным равенством: в шапке SalesDoc номер
            # «1131», а идентификатор — «d0_1131». Требуя точного совпадения,
            # поиск не находил документ ни по одному из двух написаний.
            hits = [r for r in pool if any(
                dn in str(r.get(k) or "").strip().lower()
                for k in ("invoiceNumber", "code_1C", "SD_id", "CS_id")
            )]
            out["by_number"] = {"query": doc_number, "count": len(hits),
                                "orders": hits[:3]}
            if hits:
                out["verdicts"].append(
                    f"Заявка №{doc_number} в выгрузке ЕСТЬ — сырые поля ниже: "
                    "сверьте клиента, сумму и даты, что-то из них отличается "
                    "от ожидаемого")
            else:
                out["verdicts"].append(
                    f"Заявки №{doc_number} в выгрузке НЕТ — ни под одним "
                    "клиентом, ни в одном статусе, ни с какой суммой. API её "
                    "не отдаёт; это аргумент для поддержки SalesDoc")

        # --- 5. Какие ключи периода реально фильтруют ---
        if date_from and date_to:
            probes = []
            for key in ("date", "dateUpdate", "dateCreate", "dateDocument",
                        "dateShipping"):
                f = {"include": "all", "status": [1, 2, 3, 4, 5],
                     "period": {key: {"from": date_from.isoformat(),
                                      "to": date_to.isoformat()}}}
                try:
                    _, pgk = salesdoc.call("getOrder", {"limit": 1, "page": 1,
                                                        "filter": f})
                    tot = int((pgk or {}).get("total") or 0)
                    note = ""
                    if declared and tot == declared:
                        note = "фильтр игнорируется — отдан весь журнал"
                    probes.append({"key": key, "total": tot, "note": note})
                except salesdoc.SalesDocError as e:
                    probes.append({"key": key, "error": str(e)})
            out["period_keys"] = probes

        if not out["verdicts"]:
            out["verdicts"].append(
                "Механических дефектов не найдено: пагинация целая, дубликатов "
                "нет, скрытых статусов нет. Если документ виден в интерфейсе, "
                "но его нет в списке заказов точки ниже — API его действительно "
                "не отдаёт, это вопрос к поддержке SalesDoc")
    except salesdoc.SalesDocError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return out


@router.get("/journal-anatomy")
def journal_anatomy(
    _: models.User = Depends(can_view),
):
    """Анатомия журнала getOrder: по каким срезам он полон, а где дыры.

    Когда документ виден в интерфейсе, но не приходит в выгрузке, причина
    почти всегда в каком-то одном измерении: агент, склад, направление
    торговли, период. Считаем распределение выдачи по каждому и сверяем со
    справочниками — измерение, у которого в журнале ноль записей при живом
    справочнике, и есть виновник."""
    _require_configured()
    from collections import Counter

    rows = salesdoc.call_all("getOrder", ("orders", "order"), {"filter": {
        "include": "all", "status": [1, 2, 3, 4, 5]}})

    def ref(v):
        return str((v or {}).get("SD_id") or "").lower() or None

    months = Counter()
    by_agent = Counter()
    by_store = Counter()
    by_trade = Counter()
    by_price = Counter()
    no_invoice = 0
    for r in rows:
        d = str(r.get("dateDocument") or r.get("dateCreate") or "")[:7]
        if d:
            months[d] += 1
        by_agent[ref(r.get("agent")) or "(без агента)"] += 1
        by_store[ref(r.get("store")) or "(без склада)"] += 1
        by_trade[ref(r.get("trade")) or "(без направления)"] += 1
        by_price[ref(r.get("priceType")) or "(без типа цены)"] += 1
        if not r.get("invoiceNumber"):
            no_invoice += 1

    # Справочники: агент/склад, который есть в системе, но по которому в
    # журнале ни одного заказа, — главный подозреваемый.
    def catalog(method: str, key):
        try:
            return salesdoc.call_all(method, key)
        except salesdoc.SalesDocError:
            return []

    agents = [
        {"sd_id": str(a.get("SD_id") or "").lower(),
         "name": a.get("name"), "active": a.get("active"),
         "orders": by_agent.get(str(a.get("SD_id") or "").lower(), 0)}
        for a in catalog("getAgent", "agent")
    ]
    agents.sort(key=lambda x: (x["orders"], x["name"] or ""))
    stores = [
        {"sd_id": str(w.get("SD_id") or "").lower(),
         "name": w.get("name"), "active": w.get("active"),
         "orders": by_store.get(str(w.get("SD_id") or "").lower(), 0)}
        for w in catalog("getWarehouse", ("store", "warehouse", "stores"))
    ]
    stores.sort(key=lambda x: (x["orders"], x["name"] or ""))

    # Вердикт: если ВСЕ заказы выдачи принадлежат активным агентам, а у всех
    # неактивных ровно ноль — выгрузка отсекает документы по признаку
    # «агент деактивирован». Агенты увольняются, их отключают, и вся история
    # их продаж пропадает из API, оставаясь в интерфейсе.
    inactive = [a for a in agents if a["active"] == "N"]
    active_orders = sum(a["orders"] for a in agents if a["active"] != "N")
    verdict = None
    if inactive and all(a["orders"] == 0 for a in inactive) and active_orders:
        verdict = (
            f"Все {active_orders} заказов выдачи принадлежат активным агентам, "
            f"а у всех {len(inactive)} неактивных — ровно ноль. getOrder не "
            "отдаёт заказы деактивированных агентов: их документы остаются в "
            "интерфейсе, но исчезают из выгрузки."
        )

    return {
        "total": len(rows),
        "verdict": verdict,
        "months": [{"month": m, "count": c} for m, c in sorted(months.items())],
        "no_invoice_number": no_invoice,
        "agents": agents,
        "agents_without_orders": [a for a in agents if a["orders"] == 0],
        "stores": stores,
        "by_trade": [{"value": k, "count": v} for k, v in by_trade.most_common()],
        "by_price_type": [{"value": k, "count": v} for k, v in by_price.most_common()],
    }


@router.get("/method-probe")
def method_probe(
    _: models.User = Depends(can_view),
):
    """Какие методы существуют в API SalesDoc — прямой опрос по списку имён.

    Документация покрывает не всё (invoiceNumber в ней тоже не было). Зонд
    дёргает каждого кандидата с limit=1 и по ответу решает: метод есть (пришли
    данные), есть но пустой, или сервер его не знает. Интересуют прежде всего
    визиты, маршруты и задачи агентов — для работы с дебиторкой."""
    _require_configured()
    candidates = [
        # Визиты и маршруты агентов
        "getVisit", "getVisits", "getAgentVisit", "getCheckin", "getCheckIn",
        "getRoute", "getRoutes", "getRouteSheet", "getPlan", "getPlanVisit",
        # Задачи и планы
        "getTask", "getTasks", "getTodo", "getEvent", "getNote",
        # Агенты и команда
        "getAgent", "getAgents", "getUser", "getUsers", "getEmployee",
        # Долги и прочее полезное
        "getDebt", "getClientDebt", "getSupervisor", "getTerritory",
        "getCategory", "getProduct", "getPriceType",
        # Складские документы: списание, перемещение, оприходование,
        # инвентаризация. Где SalesDoc их держит — не выяснено, а списания в
        # нём есть, поэтому перебираем правдоподобные имена.
        "getWriteOff", "getWriteOffs", "getWriteoff", "getStockWriteOff",
        "getStockMovement", "getMovement", "getMovements", "getTransfer",
        "getInventory", "getInventarization", "getRevision", "getRecount",
        "getStockDocument", "getStockDocuments", "getStockOperation",
        "getDocument", "getDocuments", "getAct", "getWaybill",
        "getIncome", "getExpense", "getConsumption", "getSupply",
        "getOrderDefect", "getDefect", "getDefects", "getReturn", "getReturns",
    ]
    out = []
    for m in candidates:
        try:
            result, pagination = salesdoc.call(m, {"limit": 1, "page": 1})
            keys = sorted(result.keys()) if isinstance(result, dict) else []
            total = (pagination or {}).get("total")
            # Пример первой записи: по нему видно, какие поля есть у визита
            # (точка, агент, время, результат) — без этого метод бесполезен.
            sample = None
            if isinstance(result, dict):
                for v in result.values():
                    if isinstance(v, list) and v:
                        sample = v[0]
                        break
            out.append({"method": m, "exists": True, "keys": keys,
                        "total": total, "sample": sample})
        except salesdoc.SalesDocError as e:
            msg = str(e)
            out.append({"method": m, "exists": False,
                        "error": msg[:160]})
    found = [r["method"] for r in out if r["exists"]]
    return {"found": found, "results": out}


@router.get("/by-guid")
def reconcile_by_guid(
    db: Session = Depends(get_db),
    _: models.User = Depends(can_view),
    org: str = Query(default="all"),
    limit: int = Query(default=200, ge=1, le=1000),
):
    """Сверка операций 1С ↔ SalesDoc по GUID документа.

    Идентификатор — единственная связка, которая не врёт: поиск по сумме и
    дате с допуском рвётся от любой правки документа, а GUID переживает и
    правку, и переоформление. 1С отдаёт его в колонке ДокументGUID, SalesDoc —
    в поле code_1C.

    Обе стороны несут его не везде, поэтому ручка сначала честно отвечает, по
    каким видам операций сверка вообще возможна: если в выгрузке 1С колонки
    нет, так и написано — вместе с тем, что для этого нужно. Как только колонка
    появится, вид сам перейдёт в рабочее состояние."""
    O, P = models.SalesDocOrder, models.SalesDocPayment

    def ours(model, *filters):
        """Документы 1С: GUID и как их назвать в списке расхождений."""
        q = models.org_scope(db.query(model), model, org)
        for f in filters:
            q = q.filter(f)
        return q.all()

    # GUID 1С выглядит как 8-4-4-4-12 шестнадцатеричных знаков. Проверка нужна
    # не для красоты: если стороны заполняют code_1C по-разному (одна — GUID,
    # другая — внутренний код вида d0_1131), совпадений не будет никогда, а
    # выглядеть это будет как «документов нет» вместо «ключи разного вида».
    _GUID_RE = re.compile(r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.I)

    def shape(values: list[str]) -> str:
        if not values:
            return "—"
        guids = sum(1 for v in values if _GUID_RE.match(v))
        if guids == len(values):
            return "GUID 1С"
        if guids:
            return f"смешанный ({guids} из {len(values)} — GUID)"
        return "не GUID"

    def index(rows, key, label, amount):
        """GUID → карточка документа. Построчные выгрузки дают на документ
        несколько строк — сворачиваем в один документ и складываем суммы."""
        out: dict[str, dict] = {}
        for r in rows:
            g = (key(r) or "").strip().lower()
            if not g:
                continue
            e = out.setdefault(g, {"guid": g, "label": label(r), "amount": 0.0,
                                   "date": None, "lines": 0})
            e["amount"] += float(amount(r) or 0)
            e["lines"] += 1
            d = getattr(r, "date", None)
            if d and (e["date"] is None or d.isoformat() < e["date"]):
                e["date"] = d.isoformat()
        return out

    sd_names = {c.sd_id: c.name for c in db.query(models.SalesDocClient).all()}

    def sd_client(row):
        return (sd_names.get((row.client_sd_id or "").lower())
                or row.client_sd_id or row.client_code_1c or "—")

    kinds = []

    def add(kind, label, our_rows, our_key, our_label, our_amount,
            their_rows, their_key, their_label, their_amount,
            our_source, their_source, our_doc_key, note=None):
        ours_idx = index(our_rows, our_key, our_label, our_amount)
        theirs_idx = index(their_rows, their_key, their_label, their_amount)
        # Выгрузки 1С построчные: 5233 строки — это далеко не 5233 документа.
        # Сравнивать охват идентификатором со строками нельзя, иначе «103 из
        # 311» читается как треть, хотя на деле это все документы файла.
        our_total = len(our_rows)
        our_docs = len({our_doc_key(r) for r in our_rows}) if our_rows else 0
        their_total = len(their_rows)
        matched, only_1c, only_sd = [], [], []
        for g, e in ours_idx.items():
            t = theirs_idx.get(g)
            if t is None:
                only_1c.append(e)
            else:
                matched.append({**e, "sd_amount": round(t["amount"], 2),
                                "delta": round(e["amount"] - t["amount"], 2),
                                "sd_label": t["label"], "sd_date": t["date"]})
        for g, e in theirs_idx.items():
            if g not in ours_idx:
                only_sd.append(e)

        # Состояние вида: сверять можно только там, где GUID есть с обеих
        # сторон. Иначе честно называем, какой половины не хватает.
        our_ids = list(ours_idx)[:5]
        their_ids = list(theirs_idx)[:5]
        our_shape, their_shape = shape(our_ids), shape(their_ids)

        if not ours_idx and not theirs_idx:
            status, hint = "no_guid_both", "идентификатора нет ни в 1С, ни в SalesDoc"
        elif not ours_idx:
            status, hint = "no_guid_1c", f"нет колонки ДокументGUID в выгрузке «{our_source}»"
        elif not theirs_idx:
            status, hint = "no_guid_sd", f"SalesDoc не отдаёт code_1C в {their_source}"
        elif our_shape != their_shape:
            # Ключи есть с обеих сторон, но разного вида — сверять их бесполезно.
            status = "shape_mismatch"
            hint = (f"1С даёт «{our_shape}», SalesDoc — «{their_shape}»: "
                    "это разные ключи, совпадений не будет")
        else:
            status, hint = "ready", None

        kinds.append({
            "kind": kind, "label": label, "status": status, "hint": hint,
            "note": note,
            "ours": {"source": our_source, "rows": our_total, "docs": our_docs,
                     "docs_with_guid": len(ours_idx),
                     "shape": our_shape, "sample": our_ids},
            # У SalesDoc строка = документ. Документы без code_1C — те, что в
            # 1С не проведены (или проведены, но связь не записалась); это
            # отдельная находка, а не шум.
            "theirs": {"source": their_source, "rows": their_total,
                       "docs": their_total,
                       "docs_with_guid": len(theirs_idx),
                       "without_guid": their_total - len(theirs_idx),
                       "shape": their_shape, "sample": their_ids},
            "matched": len(matched),
            "diff_count": sum(1 for m in matched if abs(m["delta"]) >= 1),
            "only_1c_count": len(only_1c),
            "only_sd_count": len(only_sd),
            "diffs": sorted((m for m in matched if abs(m["delta"]) >= 1),
                            key=lambda m: -abs(m["delta"]))[:limit],
            "only_1c": sorted(only_1c, key=lambda e: -abs(e["amount"]))[:limit],
            "only_sd": sorted(only_sd, key=lambda e: -abs(e["amount"]))[:limit],
        })

    # --- Реализации: строки 1С против заказов SalesDoc ---
    add("sales", "Реализации",
        ours(models.Sale), lambda s: s.doc_guid,
        lambda s: s.client, lambda s: s.amount,
        db.query(O).filter(O.status != salesdoc.CANCELLED_STATUS).all(),
        lambda o: o.code_1c, sd_client, lambda o: o.amount,
        "Реализация товаров и услуг", "getOrder",
        our_doc_key=lambda s: (s.doc_number, s.date, s.client))

    # --- Возвраты: документы 1С против операции «Возврат с полки» ---
    add("returns", "Возвраты",
        ours(models.ReturnDoc), lambda r: r.doc_guid,
        lambda r: r.client, lambda r: r.amount,
        db.query(P).filter(P.txn == salesdoc.SHELF_RETURN_TXN).all(),
        lambda p: p.code_1c, sd_client, lambda p: p.amount,
        "Возврат товаров от покупателя", "getPayment · возврат с полки",
        our_doc_key=lambda r: r.row_hash)

    # --- Оплаты покупателей ---
    add("payments", "Оплаты покупателей",
        ours(models.Receipt,
             models.Receipt.operation.like(f"{CUSTOMER_PAYMENT_PREFIX}%")),
        lambda r: r.doc_guid, lambda r: r.payer, lambda r: r.amount_kgs,
        db.query(P).filter(P.txn == salesdoc.PAYMENT_TXN).all(),
        lambda p: p.code_1c, sd_client, lambda p: p.amount,
        "Платёжное поручение входящее / ПКО", "getPayment · оплата",
        our_doc_key=lambda r: r.row_hash)

    # --- Закупки и списания: пары в SalesDoc нет вовсе ---
    for kind, label, model, source in (
            ("purchases", "Поступления товаров", models.Purchase,
             "Поступление товаров и услуг"),
            ("writeoffs", "Списания товаров", models.WriteOff,
             "Списание товаров")):
        rows = ours(model)
        idx = index(rows, lambda r: r.doc_guid, lambda r: r.doc_number or "—",
                    lambda r: getattr(r, "amount_kgs", None) or r.qty)
        docs = len({(r.doc_number, r.date) for r in rows}) if rows else 0
        kinds.append({
            "kind": kind, "label": label, "status": "no_counterpart",
            "hint": "в SalesDoc таких документов нет — сверять не с чем",
            "note": "Идентификаторы в 1С есть и хранятся: они делают импорт "
                    "идемпотентным и пригодятся, если SalesDoc заведёт "
                    "складские документы.",
            "ours": {"source": source, "rows": len(rows), "docs": docs,
                     "docs_with_guid": len(idx),
                     "shape": shape(list(idx)[:5]), "sample": list(idx)[:5]},
            "theirs": {"source": "—", "rows": 0, "docs": 0,
                       "docs_with_guid": 0, "without_guid": 0,
                       "shape": "—", "sample": []},
            "matched": 0, "diff_count": 0, "only_1c_count": 0, "only_sd_count": 0,
            "diffs": [], "only_1c": [], "only_sd": [],
        })

    return {"org": (org or "all"), "kinds": kinds}


ID_MATCH_KINDS = {
    "sales": "Реализации",
    "returns": "Возвраты",
    "payments": "Оплаты покупателей",
    "purchases": "Поступления товаров",
    "writeoffs": "Списания товаров",
    "movements": "Перемещения между складами",
}

# Виды, у которых одна из сторон в портал пока не заведена. Помечать их
# «только в 1С» или «только в SalesDoc» нельзя: это утверждение об отсутствии
# документа, а на деле мы во второй системе просто не искали.
NO_SD_SIDE = {"purchases", "writeoffs"}
NO_1C_SIDE = {"movements"}

# Насколько далеко разрешаем расходиться датам при сопоставлении без
# идентификатора: документ правят задним числом, и день-два разницы — норма.
ID_MATCH_DAYS = 3
# Разница сумм, ниже которой считаем документы одним и тем же (копейки
# округления), и порог, ниже которого расхождение не стоит показа.
ID_MATCH_TOL = 1.0


@router.get("/id-match")
def id_match(
    db: Session = Depends(get_db),
    _: models.User = Depends(can_view),
    kind: str = Query(default="sales"),
    verdict: str = Query(default="", description="Фильтр по метке"),
    q: str = Query(default="", description="Поиск по контрагенту, номеру, ИД"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=10, le=500),
    org: str = Query(default="all"),
):
    """Операции 1С и SalesDoc одним списком, с меткой на каждой строке.

    Сначала документы связываются по идентификатору — это надёжно. Оставшиеся
    пробуем сопоставить по контрагенту, дате (±3 дня) и сумме: пока колонки
    ДокументGUID нет в выгрузках 1С, иначе список выродился бы в две
    несвязанные простыни. Такая пара помечается отдельно — «по сумме», чтобы
    не выдавать догадку за факт.
    """
    if kind not in ID_MATCH_KINDS:
        raise HTTPException(status_code=404, detail=f"Неизвестный вид: {kind}")

    def doc(guid, date_, number, client, amount, extra=None):
        return {"guid": (guid or "").strip().lower() or None,
                "date": date_.isoformat() if date_ else None,
                "number": number, "client": client,
                "amount": round(float(amount or 0), 2), **(extra or {})}

    ours: list[dict] = []
    theirs: list[dict] = []
    O, P = models.SalesDocOrder, models.SalesDocPayment
    sd_names = {c.sd_id: c.name for c in db.query(models.SalesDocClient).all()}

    def sd_doc(r, extra=None):
        return doc(r.code_1c, r.date, getattr(r, "sd_id", None),
                   sd_names.get((r.client_sd_id or "").lower())
                   or r.client_sd_id or r.client_code_1c or "—",
                   r.amount, extra)

    def scope(model):
        return models.org_scope(db.query(model), model, org)

    if kind == "sales":
        # Построчная выгрузка: сворачиваем строки в документы по номеру.
        by_doc: dict = {}
        for s in scope(models.Sale).all():
            key = (s.doc_number, s.date, s.client)
            d = by_doc.get(key)
            if d is None:
                d = by_doc[key] = {"guid": s.doc_guid, "date": s.date,
                                   "number": s.doc_number, "client": s.client,
                                   "amount": 0.0, "lines": 0,
                                   "total": s.doc_total}
            d["lines"] += 1
            d["amount"] += float(s.amount or 0) * (
                1 - float(s.discount_pct or 0) / 100)
            if s.doc_total is not None:
                d["total"] = s.doc_total
        for d in by_doc.values():
            amount = d["total"] if d["total"] is not None else d["amount"]
            ours.append(doc(d["guid"], d["date"], d["number"], d["client"],
                            amount, {"lines": d["lines"]}))
        theirs = [sd_doc(o, {"status": salesdoc.ORDER_STATUS.get(o.status, o.status)})
                  for o in db.query(O).filter(
                      O.status != salesdoc.CANCELLED_STATUS).all()]
    elif kind == "returns":
        ours = [doc(r.doc_guid, r.date, None, r.client, r.amount)
                for r in scope(models.ReturnDoc).all()]
        theirs = [sd_doc(p) for p in
                  db.query(P).filter(P.txn == salesdoc.SHELF_RETURN_TXN).all()]
    elif kind == "payments":
        ours = [doc(r.doc_guid, r.date, None, r.payer, r.amount_kgs,
                    {"note": "касса" if r.kind == "cash" else "банк"})
                for r in scope(models.Receipt).filter(
                    models.Receipt.operation.like(f"{CUSTOMER_PAYMENT_PREFIX}%")).all()]
        theirs = [sd_doc(p) for p in
                  db.query(P).filter(P.txn == salesdoc.PAYMENT_TXN).all()]
    elif kind == "movements":
        # Перемещения есть только в SalesDoc: 1С выгружает их отдельным файлом
        # «Перемещение товаров», а его портал пока не грузит.
        theirs = [doc(m.code_1c, m.date, m.sd_id,
                      f"{m.from_store_name or m.from_store_sd_id or '—'} → "
                      f"{m.to_store_name or m.to_store_sd_id or '—'}",
                      0, {"qty": float(m.qty or 0), "lines": m.positions})
                  for m in db.query(models.SalesDocMovement).all()]
    else:
        # Закупки и списания: где SalesDoc держит такие документы, пока не
        # выяснено — метода в API мы не нашли. Поэтому сторона SD здесь не
        # запрашивается вовсе, и помечать строки «только в 1С» нельзя: это
        # означало бы «в SalesDoc документа нет», а на деле мы там не искали.
        model = models.Purchase if kind == "purchases" else models.WriteOff
        by_doc = {}
        for r in scope(model).all():
            key = (r.doc_number, r.date)
            d = by_doc.setdefault(key, {
                "guid": r.doc_guid, "date": r.date, "number": r.doc_number,
                "client": getattr(r, "supplier", None) or r.warehouse or "—",
                "amount": 0.0, "lines": 0})
            d["lines"] += 1
            d["amount"] += float(getattr(r, "amount_kgs", None) or 0)
            d["qty"] = d.get("qty", 0.0) + float(r.qty or 0)
        for d in by_doc.values():
            ours.append(doc(d["guid"], d["date"], d["number"], d["client"],
                            d["amount"], {"lines": d["lines"],
                                          "qty": round(d.get("qty", 0.0), 1)}))

    # --- Связывание: сначала по идентификатору, потом по сумме и дате ---
    has_sd = bool(theirs)
    their_by_guid = {t["guid"]: t for t in theirs if t["guid"]}
    used: set[int] = set()
    rows: list[dict] = []

    def norm(name):
        return salesdoc_mirror.match_key(name or "")

    # Индекс оставшихся документов SalesDoc для сопоставления по сумме.
    pool: dict[tuple, list] = {}
    for i, t in enumerate(theirs):
        pool.setdefault((norm(t["client"]), round(t["amount"], 2)), []).append(i)
    idx_of = {id(t): i for i, t in enumerate(theirs)}

    def near(a, b) -> bool:
        if not a or not b:
            return False
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days) <= ID_MATCH_DAYS

    for o in ours:
        t = their_by_guid.get(o["guid"]) if o["guid"] else None
        if t is not None:
            used.add(idx_of[id(t)])
            delta = round(o["amount"] - t["amount"], 2)
            rows.append({"verdict": "diff" if abs(delta) >= ID_MATCH_TOL else "id",
                         "ours": o, "theirs": t, "delta": delta})
            continue
        # Идентификатора нет — пробуем по контрагенту, сумме и дате.
        found = None
        for i in pool.get((norm(o["client"]), o["amount"]), []):
            if i in used or not near(o["date"], theirs[i]["date"]):
                continue
            found = i
            break
        if found is not None:
            used.add(found)
            rows.append({"verdict": "guess", "ours": o, "theirs": theirs[found],
                         "delta": 0.0})
        else:
            rows.append({"verdict": "only_1c" if has_sd else "no_sd_side",
                         "ours": o, "theirs": None,
                         "delta": o["amount"] if has_sd else 0.0})
    no_1c = kind in NO_1C_SIDE
    for i, t in enumerate(theirs):
        if i not in used:
            rows.append({"verdict": "no_1c_side" if no_1c else "only_sd",
                         "ours": None, "theirs": t,
                         "delta": 0.0 if no_1c else -t["amount"]})

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    if verdict:
        rows = [r for r in rows if r["verdict"] == verdict]
    needle = q.strip().lower()
    if needle:
        def hit(r):
            for side in ("ours", "theirs"):
                d = r[side]
                if d and any(needle in str(d.get(f) or "").lower()
                             for f in ("client", "number", "guid")):
                    return True
            return False
        rows = [r for r in rows if hit(r)]

    rows.sort(key=lambda r: ((r["ours"] or r["theirs"])["date"] or ""), reverse=True)
    total = len(rows)
    start = (page - 1) * page_size
    return {
        "kind": kind, "label": ID_MATCH_KINDS[kind],
        # У списаний в выгрузке 1С суммы нет — только количество. Показывать
        # там «0 KGS» значит утверждать, что документ на ноль сомов.
        "measure": "qty" if kind in ("writeoffs", "movements") else "money",
        "has_1c": kind not in NO_1C_SIDE,
        # Сколько документов SalesDoc вообще несут идентификатор. У перемещений
        # code_1C пуст у всех до одного — значит связать их с 1С по ИД будет
        # нечем даже после того, как появится выгрузка из 1С.
        "sd_docs": len(theirs),
        "sd_with_guid": sum(1 for t in theirs if t["guid"]),
        "counts": counts, "total": total,
        "page": page, "page_size": page_size,
        "has_sd": bool(theirs),
        "rows": rows[start:start + page_size],
    }


@router.get("/movements-probe")
def movements_probe(
    _: models.User = Depends(can_view),
    method: str = Query(default="", description="Опросить произвольный метод"),
):
    """Что такое getMovement: перемещения между складами или списания?

    Метод нашёлся зондом (91 запись), но по имени не понять, что в нём лежит.
    Ответ дают поля: если у записи два склада («откуда» и «куда») — это
    перемещение; если один склад и статья затрат — списание. Смотрим сырые
    записи целиком, а не гадаем по названию.

    Заодно опрашиваем getConsumption и getInventory: они существуют, но на
    limit=1 вернули ноль — а ноль на одной странице ещё не значит, что метод
    пуст (у getOrderDefect фильтр статусов по умолчанию скрывал всё)."""
    _require_configured()
    out: dict = {}

    # Раздел списаний в интерфейсе SalesDoc живёт по адресу /stock/excretion —
    # значит и метод, скорее всего, getExcretion. Имена методов в этом API
    # повторяют разделы (movement, inventory, consumption), так что догадка
    # дешёвая, а проверка — один запрос.
    probes = [("getExcretion", ("excretion", "excretions")),
              ("getStockExcretion", ("excretion", "excretions")),
              ("getMovement", ("movement", "movements")),
              ("getConsumption", ("consumption", "consumptions")),
              ("getInventory", ("inventory", "inventories"))]
    # Произвольный метод — чтобы следующая догадка не требовала выкладки.
    if method.strip():
        name = method.strip()
        probes = [(name, (name.replace("get", "", 1).lower(), "data", "items"))]

    for method, keys in probes:
        entry: dict = {"method": method}
        try:
            # «Ноль записей» у SalesDoc дважды означало не «пусто», а «спросили
            # не так»: getOrder скрывал заказы без списка статусов,
            # getOrderDefect — без него же. Поэтому не верим первому нулю, а
            # перебираем формы запроса и показываем, какая что вернула.
            today = date.today()
            wide = {"from": f"{today.year - 3}-01-01",
                    "to": f"{today.year + 1}-12-31"}
            shapes = [
                ("без параметров", None),
                ("include=all + все статусы",
                 {"filter": {"include": "all", "status": [1, 2, 3, 4, 5]}}),
                ("период по date", {"filter": {"period": {"date": wide}}}),
                ("период + все статусы",
                 {"filter": {"include": "all", "status": [1, 2, 3, 4, 5],
                             "period": {"date": wide}}}),
                ("период по dateCreate",
                 {"filter": {"period": {"dateCreate": wide}}}),
            ]
            rows, tried = [], []
            for name, params in shapes:
                try:
                    got = salesdoc.call_all(method, keys, params) if params \
                        else salesdoc.call_all(method, keys)
                except salesdoc.SalesDocError as e:
                    tried.append({"shape": name, "error": str(e)[:120]})
                    continue
                tried.append({"shape": name, "count": len(got)})
                if len(got) > len(rows):
                    rows = got
                    entry["worked"] = name
            entry["attempts"] = tried
            entry["count"] = len(rows)
            entry["sample"] = rows[:3]
            fields: dict = {}
            for r in rows[:200]:
                if not isinstance(r, dict):
                    continue
                for k, v in r.items():
                    f = fields.setdefault(k, {"field": k, "filled": 0,
                                              "example": None})
                    if v not in (None, "", [], {}):
                        f["filled"] += 1
                        if f["example"] is None:
                            f["example"] = v
            entry["fields"] = sorted(fields.values(), key=lambda f: -f["filled"])

            # Товарные строки: у складского документа они и есть суть.
            for key in ("detail", "details", "items", "products", "lines"):
                sample_lines = (rows[0].get(key) if rows and isinstance(rows[0], dict)
                                else None)
                if isinstance(sample_lines, list) and sample_lines:
                    entry["line_field"] = key
                    entry["line_fields"] = sorted(
                        {k for r in sample_lines if isinstance(r, dict) for k in r})
                    break

            # Признак перемещения — две ссылки на склад в одной записи.
            store_fields = [f["field"] for f in entry["fields"]
                            if any(t in f["field"].lower()
                                   for t in ("store", "warehouse", "sklad"))]
            entry["store_fields"] = store_fields
            if len(store_fields) >= 2:
                entry["verdict"] = (
                    f"Две ссылки на склад ({', '.join(store_fields)}) — это "
                    "перемещение между складами, а не списание.")
            elif store_fields and rows:
                entry["verdict"] = (
                    f"Один склад ({store_fields[0]}) и нет второго — похоже на "
                    "списание или расход со склада.")
            elif rows:
                entry["verdict"] = "Складских ссылок в записи нет — см. поля."
            else:
                entry["verdict"] = (
                    "Метод существует, но не отдал ни одной записи ни при одной "
                    "форме запроса — включая период за три года и все статусы. "
                    "Похоже, он действительно пуст.")
        except salesdoc.SalesDocError as e:
            entry["error"] = str(e)[:200]
        out[method] = entry

    return out


@router.get("/txn-types")
def txn_types(
    db: Session = Depends(get_db),
    _: models.User = Depends(can_view),
):
    """Какие виды операций реально встречаются в журнале SalesDoc и что портал
    с каждым делает.

    В журнале лежат не только оплаты: там же возврат с полки, списание долга,
    выплата клиенту, конверсия, начальный остаток. В долг портал засчитывает
    оплату и возврат, остальное баланс SalesDoc меняет, а в 1С пары не имеет.
    Пока эти виды не показаны, точка со списанным долгом выглядит расхождением
    неизвестного происхождения — отсюда и таблица: что есть, сколько, и как
    учитывается."""
    from sqlalchemy import func

    P = models.SalesDocPayment
    names = {c.sd_id: c.name for c in db.query(models.SalesDocClient).all()}
    rows = (db.query(P.txn, func.count(P.id), func.sum(P.amount),
                     func.min(P.date), func.max(P.date))
            .group_by(P.txn).all())

    def role(t):
        if t == salesdoc.PAYMENT_TXN:
            return "считается оплатой"
        if t == salesdoc.SHELF_RETURN_TXN:
            return "считается возвратом"
        if t in salesdoc.BALANCE_ONLY_TXN:
            return "меняет баланс SD, пары в 1С нет"
        return "в расчёт долга не идёт"

    types = sorted(
        ({"txn": t,
          "label": salesdoc.PAY_TXN.get(t, str(t) if t is not None else "—"),
          "role": role(t),
          "count": int(n or 0),
          "amount": round(float(s or 0), 2),
          "first": f.isoformat() if f else None,
          "last": l.isoformat() if l else None}
         for t, n, s, f, l in rows),
        key=lambda x: -abs(x["amount"]))

    # Сами операции «только баланс» — их немного, и именно их обычно ищут.
    ops = [
        {"sd_id": p.sd_id,
         "date": p.date and p.date.isoformat(),
         "client": names.get((p.client_sd_id or "").lower()) or p.client_sd_id
                   or p.client_code_1c,
         "amount": float(p.amount or 0),
         "txn": p.txn,
         "label": salesdoc.PAY_TXN.get(p.txn, str(p.txn)),
         "type_name": p.type_name}
        for p in (db.query(P).filter(P.txn.in_(sorted(salesdoc.BALANCE_ONLY_TXN)))
                  .order_by(P.date.desc()).limit(200).all())
    ]
    return {"types": types, "balance_only": ops,
            "balance_only_total": round(sum(o["amount"] for o in ops), 2)}


@router.get("/agent-model")
def agent_model(
    db: Session = Depends(get_db),
    _: models.User = Depends(can_view),
):
    """К чему привязан агент в SalesDoc: к точке или к документу?

    Вопрос не праздный: от ответа зависит, что делать при увольнении. Если
    агент — реквизит ТОЧКИ, увольнение оставляет её без хозяина, и точку надо
    переназначить. Если реквизит ДОКУМЕНТА — «закрепления» нет вообще, есть
    только след «кто последний продал», и переназначать нечего: следующий заказ
    выпишет другой агент, а вся прошлая история уедет из выгрузки вместе с
    уволенным.

    Зонд отвечает фактами, а не догадками:
      • сырые поля getClient и getAgent — есть ли у точки вообще поле агента;
      • сколько точек за свою историю сменили агента (если много — привязка
        документная, а не карточная);
      • что стало с точками деактивированных агентов.
    """
    from sqlalchemy import func

    _require_configured()
    out: dict = {}

    # --- 1. Сырая карточка точки: есть ли в ней агент/территория вообще ---
    try:
        result, _pag = salesdoc.call("getClient", {"limit": 1, "page": 1})
        sample = None
        if isinstance(result, dict):
            for v in result.values():
                if isinstance(v, list) and v:
                    sample = v[0]
                    break
        out["client_sample"] = sample
        keys = sorted(sample.keys()) if isinstance(sample, dict) else []
        out["client_keys"] = keys
        # Поле агента у точки — главный признак «карточной» привязки.
        hits = [k for k in keys
                if any(t in k.lower() for t in ("agent", "manager", "territor",
                                                "supervisor", "route"))]
        out["client_agent_fields"] = hits
    except salesdoc.SalesDocError as e:
        out["client_error"] = str(e)[:200]

    for method in ("getAgent", "getTerritory", "getSupervisor"):
        try:
            result, pag = salesdoc.call(method, {"limit": 1, "page": 1})
            sample = None
            if isinstance(result, dict):
                for v in result.values():
                    if isinstance(v, list) and v:
                        sample = v[0]
                        break
            out[f"{method}_sample"] = sample
            out[f"{method}_total"] = (pag or {}).get("total")
        except salesdoc.SalesDocError as e:
            out[f"{method}_error"] = str(e)[:200]

    # --- 2. Меняется ли агент у точки от документа к документу ---
    # Считаем по зеркалу заказов: агент в каждом заказе свой реквизит, и если
    # у точки за историю встречается больше одного агента, «закрепления» в
    # документах нет — есть только последовательность.
    O = models.SalesDocOrder
    rows = (db.query(O.client_sd_id, O.agent_sd_id, O.agent_name,
                     func.count(O.id), func.max(O.date))
            .filter(O.client_sd_id.isnot(None), O.agent_name.isnot(None))
            .group_by(O.client_sd_id, O.agent_sd_id, O.agent_name)
            .all())
    per_client: dict[str, list] = {}
    for cli, ag_id, ag_name, cnt, last in rows:
        per_client.setdefault(cli, []).append(
            {"agent": ag_name, "agent_sd_id": ag_id, "orders": cnt,
             "last": last.isoformat() if last else None})
    multi = {c: v for c, v in per_client.items() if len(v) > 1}
    out["clients_with_orders"] = len(per_client)
    out["clients_multi_agent"] = len(multi)
    out["orders_with_agent"] = sum(
        a["orders"] for v in per_client.values() for a in v)
    out["orders_total"] = db.query(func.count(O.id)).scalar() or 0

    # --- 3. Закрепление из карточек точек и что с точками уволенных ---
    agents = {a.sd_id: a for a in db.query(models.SalesDocAgent).all()}
    clients = {c.sd_id: c for c in db.query(models.SalesDocClient).all()}
    names = {sid: c.name for sid, c in clients.items()}
    inactive = {sid for sid, a in agents.items() if not a.active}

    links: dict[str, list] = {}
    for l in db.query(models.SalesDocClientAgent).all():
        links.setdefault((l.client_sd_id or "").lower(), []).append(l)
    out["clients_total"] = len(clients)
    out["clients_assigned"] = len(links)
    out["clients_unassigned"] = len(clients) - len(links)

    # Точка «осиротела», если все закреплённые за ней агенты деактивированы.
    # Это и есть авторитетный ответ, а не догадка по истории заказов: так
    # считает сам SalesDoc.
    orphans = []
    for cli, ls in links.items():
        if any(l.agent_sd_id not in inactive for l in ls):
            continue
        orphans.append({
            "client_sd_id": cli,
            "client": names.get(cli) or cli,
            "debt": float(clients[cli].debt or 0) if cli in clients else 0.0,
            "agents": [
                {"agent": (agents[l.agent_sd_id].name
                           if l.agent_sd_id in agents else l.agent_sd_id),
                 "agent_sd_id": l.agent_sd_id, "days": l.days,
                 "orders": sum(a["orders"] for a in per_client.get(cli, [])),
                 "last": (per_client.get(cli) or [{}])[0].get("last")}
                for l in ls
            ],
        })
    out["agents_total"] = len(agents)
    out["agents_inactive"] = len(inactive)
    out["orphan_clients"] = sorted(orphans, key=lambda o: -o["debt"])[:100]
    out["orphan_count"] = len(orphans)
    out["orphan_debt"] = round(sum(o["debt"] for o in orphans), 2)

    # --- 4. Вердикт ---
    verdicts = []
    if out.get("client_agent_fields"):
        verdicts.append(
            "У карточки точки есть поля " + ", ".join(out["client_agent_fields"])
            + " — значит агент (или территория) закрепляется за ТОЧКОЙ, и при "
            "увольнении точку надо переназначить в справочнике SalesDoc.")
    elif "client_keys" in out:
        verdicts.append(
            "В карточке точки (getClient) поля агента нет — только "
            + ", ".join(out["client_keys"][:12])
            + ". Значит через API «закрепления» не видно: агент известен только "
            "как реквизит документа.")
    if out["clients_total"]:
        verdicts.append(
            f"Закрепление есть у {out['clients_assigned']} точек из "
            f"{out['clients_total']}; без агента — {out['clients_unassigned']}.")
    if out["orders_total"] and not out["orders_with_agent"]:
        verdicts.append(
            "Агент в зеркале заказов ещё не заполнен: поле добавлено недавно, а "
            "дельта-синхронизация обновляет только изменённые документы. "
            "Заполнится при ближайшей полной выгрузке (раз в час) или по кнопке "
            "«Обновить зеркало полностью».")
    elif out["clients_with_orders"]:
        share = round(100 * out["clients_multi_agent"] / out["clients_with_orders"])
        verdicts.append(
            f"У {out['clients_multi_agent']} из {out['clients_with_orders']} точек "
            f"({share}%) в истории заказов больше одного агента. "
            + ("Агент в заказе меняется от документа к документу — он говорит, "
               "кто выписал документ, а не за кем точка." if share >= 20 else
               "Агент в заказах у точки почти всегда один — на практике он "
               "совпадает с закреплением."))
    if out["orphan_count"]:
        verdicts.append(
            f"{out['orphan_count']} точек закреплены только за деактивированными "
            f"агентами (долг {out['orphan_debt']}). Их заказы API больше не "
            "отдаёт, и точки некому вести — их надо переназначить в SalesDoc.")
    out["verdicts"] = verdicts
    return out


@router.get("/visit-debt")
def visit_debt(
    db: Session = Depends(get_db),
    _: models.User = Depends(can_view),
):
    """Дебиторка × визиты: у каждого должника — когда были, когда придут,
    когда платил; у каждого агента — долг его точек и отдача визитов.

    Отвечает на главный вопрос работы с долгами: «куда ехать за деньгами».
    Всё из зеркала — мгновенно."""
    from sqlalchemy import func

    V = models.SalesDocVisit
    P = models.SalesDocPayment
    now = datetime.utcnow()

    debtors = (db.query(models.SalesDocClient)
               .filter(models.SalesDocClient.debt >= 0.5).all())

    last_visit = dict(
        db.query(V.client_sd_id, func.max(V.at))
        .filter(V.visited.is_(True)).group_by(V.client_sd_id).all())
    next_plan = dict(
        db.query(V.client_sd_id, func.min(V.at))
        .filter(V.planned.is_(True), V.visited.is_(False), V.at >= now)
        .group_by(V.client_sd_id).all())
    last_pay = dict(
        db.query(P.client_sd_id, func.max(P.date))
        .filter(P.txn == salesdoc.PAYMENT_TXN).group_by(P.client_sd_id).all())

    # Агент точки — по её последнему визиту за 90 дней.
    agent_of: dict[str, str] = {}
    recent = (db.query(V).filter(V.at >= now - timedelta(days=90))
              .order_by(V.at.asc()).all())
    for v in recent:
        if v.client_sd_id and v.agent_name:
            agent_of[v.client_sd_id] = v.agent_name

    rows = []
    for c in debtors:
        sid = c.sd_id
        lv = last_visit.get(sid)
        rows.append({
            "sd_id": sid,
            "name": c.name,
            "debt": round(float(c.debt), 2),
            "agent": agent_of.get(sid),
            "last_visit": lv and lv.date().isoformat(),
            "days_since_visit": (now - lv).days if lv else None,
            "next_planned": (np := next_plan.get(sid)) and np.date().isoformat(),
            "last_payment": (lp := last_pay.get(sid)) and lp.isoformat(),
            "days_since_payment": (now.date() - lp).days if (lp := last_pay.get(sid)) else None,
        })
    rows.sort(key=lambda x: -x["debt"])

    # Отдача агентов за 30 дней + долг их портфеля.
    ag: dict[str, dict] = {}
    for v in recent:
        if v.at < now - timedelta(days=30) or not v.agent_name:
            continue
        a = ag.setdefault(v.agent_name, {"visits": 0, "with_order": 0, "summa": 0.0})
        if v.visited:
            a["visits"] += 1
            if v.has_order:
                a["with_order"] += 1
            a["summa"] += float(v.order_summa or 0)
    debt_by_agent: dict[str, float] = {}
    for r in rows:
        if r["agent"]:
            debt_by_agent[r["agent"]] = debt_by_agent.get(r["agent"], 0.0) + r["debt"]
    agents = sorted(
        ({"agent": name,
          "visits_30d": v["visits"],
          "with_order": v["with_order"],
          "order_summa": round(v["summa"], 2),
          "portfolio_debt": round(debt_by_agent.get(name, 0.0), 2)}
         for name, v in ag.items()),
        key=lambda x: -x["portfolio_debt"])

    return {
        "debtors": rows[:300],
        "debtors_total": len(rows),
        "debt_sum": round(sum(r["debt"] for r in rows), 2),
        "agents": agents,
        "visits_ready": bool(last_visit or recent),
    }


@router.get("/visits-sample")
def visits_sample(
    _: models.User = Depends(can_view),
):
    """Первые визиты getVisit как есть + сводка полей с примерами значений.

    По ним проектируется зеркало визитов и «Дебиторка × визиты»: важно узнать,
    как визит ссылается на точку и агента, где время и есть ли результат."""
    _require_configured()
    try:
        result, pagination = salesdoc.call("getVisit", {"limit": 5, "page": 1})
    except salesdoc.SalesDocError as e:
        raise HTTPException(status_code=502, detail=str(e))
    rows = salesdoc._pick(result, ("visit", "visits"))
    fields: dict = {}
    for r in rows:
        if isinstance(r, dict):
            for k, v in r.items():
                if k not in fields and v not in (None, "", []):
                    fields[k] = v
    # Проверяем заодно, работает ли фильтр периода — от этого зависит,
    # сможем ли грузить визиты инкрементально.
    period_probe = {}
    for key in ("date", "dateUpdate"):
        try:
            _, pg = salesdoc.call("getVisit", {"limit": 1, "page": 1, "filter": {
                "period": {key: {"from": "2026-07-01", "to": "2026-07-31"}}}})
            period_probe[key] = (pg or {}).get("total")
        except salesdoc.SalesDocError as e:
            period_probe[key] = f"ошибка: {e}"
    return {
        "total": (pagination or {}).get("total"),
        "fields": {k: v for k, v in sorted(fields.items())},
        "period_filter": period_probe,
        "rows": rows,
    }


@router.get("/find-doc")
def find_doc(
    db: Session = Depends(get_db),
    _: models.User = Depends(can_view),
    amount: float | None = Query(default=None, description="Сумма документа"),
    query: str = Query(default="", description="Часть имени точки или её ИД"),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
):
    """Где документ: в 1С, в зеркале, в живом SalesDoc.

    Вопрос «почему на портале не видно реализацию на N» повторяется, а ответ
    каждый раз разный: документ не доехал до зеркала, скрыт фильтром по складу,
    не в том статусе или его просто нет. Ищем во всех трёх местах и говорим, на
    каком шаге документ потерялся.

    Искать можно и без суммы — по точке и дате: «какие реализации сегодня по
    t5_388» отвечает идентификаторами, а их-то и надо назвать, когда сумму
    вспоминать неоткуда. Но без единого условия поиск бессмыслен: пустой запрос
    вернул бы весь журнал."""
    tol = 0.01
    qq = (query or "").strip().lower()
    if amount is None and not qq and date_from is None and date_to is None:
        raise HTTPException(
            status_code=400,
            detail="Укажите хотя бы одно условие: сумму, точку или дату")

    def amount_ok(value: float) -> bool:
        return amount is None or abs(value - amount) <= tol

    def date_ok(d: date) -> bool:
        if date_from and d < date_from:
            return False
        return not (date_to and d > date_to)
    names = {c.sd_id: c.name for c in db.query(models.SalesDocClient).all()}
    stores = {s.store_id.lower(): s
              for s in db.query(models.SalesDocStore).all() if s.store_id}

    def match_client(cli_sd, cli_code) -> bool:
        if not qq:
            return True
        nm = (names.get(cli_sd or "") or "").lower()
        return qq in nm or qq in (cli_sd or "") or qq in str(cli_code or "").lower()

    # --- Зеркало ---
    mirror_hits = []
    mirror_q = db.query(models.SalesDocOrder)
    if amount is not None:
        mirror_q = mirror_q.filter(models.SalesDocOrder.amount >= amount - tol,
                                   models.SalesDocOrder.amount <= amount + tol)
    if date_from:
        mirror_q = mirror_q.filter(models.SalesDocOrder.date >= date_from)
    if date_to:
        mirror_q = mirror_q.filter(models.SalesDocOrder.date <= date_to)
    for r in mirror_q.limit(500).all():
        if not match_client(r.client_sd_id, r.client_code_1c):
            continue
        st = stores.get(r.store_sd_id or "")
        notes = []
        if r.status == salesdoc.CANCELLED_STATUS:
            notes.append("статус «Отменён» — в суммы не идёт")
        elif r.status not in salesdoc.SHIPPED_STATUSES:
            notes.append("статус «Новый» — в сумму отгрузок не идёт")
        if st and st.organization:
            notes.append(f"склад фирмы {st.organization}: при другой выбранной "
                         "фирме строка скрыта")
        mirror_hits.append({
            "sd_id": r.sd_id,
            "date": r.date and r.date.isoformat(),
            "client": names.get(r.client_sd_id or "", r.client_sd_id or ""),
            "store": (st.name if st else None) or r.store_sd_id,
            "status_label": salesdoc.ORDER_STATUS.get(r.status, str(r.status)),
            "amount": float(r.amount or 0),
            "code_1C": r.code_1c,
            "agent": r.agent_name,
            "notes": notes,
        })
    mirror_hits.sort(key=lambda h: h["date"] or "", reverse=True)

    # --- 1С: итог документа продаж ---
    sales_hits, seen_docs = [], set()
    sales_q = db.query(models.Sale).filter(models.Sale.doc_total.isnot(None))
    if amount is not None:
        sales_q = sales_q.filter(models.Sale.doc_total >= amount - tol,
                                 models.Sale.doc_total <= amount + tol)
    if date_from:
        sales_q = sales_q.filter(models.Sale.date >= date_from)
    if date_to:
        sales_q = sales_q.filter(models.Sale.date <= date_to)
    for s in sales_q.limit(5000).all():
        key = (s.doc_number, s.date, s.client)
        if key in seen_docs:
            continue
        seen_docs.add(key)
        if qq and qq not in s.client.lower():
            continue
        sales_hits.append({
            "date": s.date.isoformat(),
            "client": s.client,
            "doc_number": s.doc_number,
            "warehouse": s.warehouse,
            "amount": float(s.doc_total),
        })

    # --- Живой SalesDoc: тем же методом, что и зеркало ---
    live_hits, live_error = [], None
    try:
        df, dt = salesdoc.reason_window()
        # Когда ищут по дате, окно берём заданное: реализация «сегодня» может
        # оказаться за пределами стандартного окна причин расхождений.
        if date_from:
            df = date_from.isoformat()
        if date_to:
            dt = date_to.isoformat()
        for o in salesdoc.call_all("getOrder", ("orders", "order"), {"filter": {
                "include": "all", "status": [1, 2, 3, 4, 5],
                "period": {"date": {"from": df, "to": dt}}}}):
            amt = float(o.get("totalSummaAfterDiscount") or o.get("totalSumma") or 0)
            if not amount_ok(amt):
                continue
            cli = o.get("client") or {}
            cli_sd = str(cli.get("SD_id") or "").lower() or None
            if not match_client(cli_sd, cli.get("code_1C")):
                continue
            odate = (o.get("dateDocument") or o.get("dateCreate") or "")[:10]
            if (date_from or date_to) and odate:
                try:
                    if not date_ok(date.fromisoformat(odate)):
                        continue
                except ValueError:
                    pass
            live_hits.append({
                "sd_id": str(o.get("SD_id") or o.get("CS_id") or "").strip(),
                "date": odate,
                "client": names.get(cli_sd or "", cli_sd or ""),
                "store": (o.get("store") or {}).get("name")
                         or (o.get("store") or {}).get("SD_id"),
                "status_label": salesdoc.ORDER_STATUS.get(o.get("status"),
                                                          str(o.get("status"))),
                "amount": round(amt, 2),
                "number": o.get("number") or o.get("code_1C"),
                "agent": (o.get("agent") or {}).get("name")
                         if isinstance(o.get("agent"), dict) else None,
            })
        live_hits.sort(key=lambda h: h["date"] or "", reverse=True)
    except salesdoc.SalesDocError as e:
        live_error = str(e)

    # --- Рядом с датой 1С: все заказы точки из живого SalesDoc ---
    # «По сумме не нашлось» — это ещё не «документа нет»: SalesDoc может
    # отдавать его с другой суммой (скидка, правка, частичный возврат) или под
    # другим клиентом. Показываем всё, что API отдаёт по точке возле даты
    # документа 1С, с обеими суммами — до и после скидки.
    nearby, nearby_client = [], None
    if sales_hits and not live_hits and live_error is None:
        h0 = sales_hits[0]
        sid = _extract_sd_id(h0["client"])
        if sid:
            nearby_client = h0["client"]
            d0 = date.fromisoformat(h0["date"])
            try:
                for o in salesdoc.raw_orders_of_day(
                        (d0 - timedelta(days=45)).isoformat(),
                        (d0 + timedelta(days=45)).isoformat(), sid, limit=50):
                    nearby.append({
                        "sd_id": str(o.get("SD_id") or o.get("CS_id") or ""),
                        "number": o.get("number") or o.get("code_1C"),
                        "date": (o.get("dateDocument") or o.get("dateCreate") or "")[:10],
                        "store": (o.get("store") or {}).get("name")
                                 or (o.get("store") or {}).get("SD_id"),
                        "status_label": salesdoc.ORDER_STATUS.get(
                            o.get("status"), str(o.get("status"))),
                        "total": round(float(o.get("totalSumma") or 0), 2),
                        "total_after": round(float(
                            o.get("totalSummaAfterDiscount") or 0), 2),
                        "returns": round(float(o.get("totalReturnsSumma") or 0), 2),
                    })
            except salesdoc.SalesDocError as e:
                live_error = str(e)

    # --- Вердикт: на каком шаге документ теряется ---
    mirror_ids = {h["sd_id"] for h in mirror_hits}
    live_ids = {h["sd_id"] for h in live_hits}
    verdicts = []
    if nearby_client is not None:
        if not nearby:
            verdicts.append(
                f"API SalesDoc не отдаёт ни одного заказа «{nearby_client}» "
                "рядом с датой 1С — хотя в интерфейсе SalesDoc они могут быть "
                "видны. Похоже на другой филиал или заявку, не попадающую в "
                "выгрузку getOrder")
        else:
            verdicts.append(
                f"По сумме не нашлось, но рядом с датой 1С у «{nearby_client}» "
                "есть заказы (список ниже) — сравните суммы: возможно, в "
                "SalesDoc документ проведён с другой суммой")
    for h in live_hits:
        if h["sd_id"] not in mirror_ids:
            verdicts.append(f"«{h['client']}» {h['date']}: в SalesDoc есть, в "
                            "зеркале нет — зеркало пропустило документ, нажмите "
                            "«↻ Обновить»")
    for h in mirror_hits:
        if live_error is None and h["sd_id"] not in live_ids:
            verdicts.append(f"«{h['client']}» {h['date']}: в зеркале есть, в "
                            "SalesDoc уже нет — документ удалён, при полной "
                            "сверке зеркало его вычистит")
        for n in h["notes"]:
            verdicts.append(f"«{h['client']}» {h['date']}: {n}")
    if not live_hits and not mirror_hits and live_error is None:
        what = "с такой суммой" if amount is not None else "по этим условиям"
        verdicts.append(f"В SalesDoc документа {what} нет — если он есть "
                        "в 1С, отгрузка в SalesDoc не проведена")

    return {
        "amount": amount,
        "query": query,
        "date_from": date_from and date_from.isoformat(),
        "date_to": date_to and date_to.isoformat(),
        "in_1c": sales_hits,
        "in_mirror": mirror_hits,
        "in_salesdoc": live_hits,
        "nearby": nearby,
        "nearby_client": nearby_client,
        "live_error": live_error,
        "verdicts": verdicts,
    }


@router.get("/payment-raw")
def payment_raw(
    db: Session = Depends(get_db),
    _: models.User = Depends(can_view),
    sd_id: str = Query(..., description="ИД операции журнала оплат SalesDoc"),
):
    """Сырой ответ SalesDoc по одной оплате — со всеми полями.

    У оплаты нет склада, поэтому к фирме её отнести не по чему. Прежде чем
    что-то придумывать, надо увидеть, какие признаки в операции вообще есть."""
    _require_configured()
    row = db.query(models.SalesDocPayment).filter(
        models.SalesDocPayment.sd_id == sd_id).first()
    if row is None or row.date is None:
        raise HTTPException(status_code=404, detail="Оплаты нет в зеркале")
    df = (row.date - timedelta(days=1)).isoformat()
    dt = (row.date + timedelta(days=1)).isoformat()
    try:
        raw = salesdoc.raw_payment(sd_id, df, dt)
    except salesdoc.SalesDocError as e:
        raise HTTPException(status_code=502, detail=str(e))
    # Заказы, которые гасит оплата, — разворачиваем в понятные строки со
    # складом. Это и есть признак фирмы; если список пуст, оплата ни к чему не
    # привязана и поделить её нечем.
    store_names = {
        s.store_id.lower(): (s.name or s.store_id)
        for s in db.query(models.SalesDocStore).all() if s.store_id
    }
    linked = []
    for it in (raw or {}).get("orders") or []:
        oid = str((it.get("SD_id") or it.get("CS_id")) if isinstance(it, dict) else it or "").lower()
        if not oid:
            continue
        o = db.query(models.SalesDocOrder).filter(
            models.SalesDocOrder.sd_id == oid).first()
        linked.append({
            "sd_id": oid,
            "found": o is not None,
            "date": o.date.isoformat() if o and o.date else None,
            "store": store_names.get(o.store_sd_id or "", o.store_sd_id or "")
                     if o else None,
            "amount": float(o.amount or 0) if o else None,
            "status": salesdoc.ORDER_STATUS.get(o.status, str(o.status)) if o else None,
        })

    return {
        "mirror": {
            "sd_id": row.sd_id,
            "date": row.date.isoformat(),
            "amount": float(row.amount or 0),
            "txn": row.txn,
            "type_name": row.type_name,
            "cashbox_sd_id": row.cashbox_sd_id,
            "cashbox_name": row.cashbox_name,
            "order_ids": row.order_ids,
        },
        "linked": linked,
        # Список полей отдельно: сразу видно, есть ли в операции хоть один
        # признак фирмы, или его там нет вовсе.
        "fields": sorted(raw.keys()) if isinstance(raw, dict) else [],
        "raw": raw,
    }


@router.get("/order-changes")
def order_changes(
    db: Session = Depends(get_db),
    _: models.User = Depends(can_view),
):
    """Смены склада и статуса документов, замеченные зеркалом.

    В SalesDoc такой истории нет — правки склада там не сохраняются."""
    return {"changes": salesdoc_mirror.order_changes(db)}


@router.get("/order-raw")
def order_raw(
    db: Session = Depends(get_db),
    _: models.User = Depends(can_view),
    sd_id: str = Query(..., description="ИД документа SalesDoc"),
):
    """Сырой ответ SalesDoc по документу + соседние документы того же дня.

    Когда портал и интерфейс SalesDoc показывают у документа разные склады,
    спорить бесполезно — надо посмотреть, что отдаёт сам метод getOrder."""
    _require_configured()
    row = db.query(models.SalesDocOrder).filter(
        models.SalesDocOrder.sd_id == sd_id).first()
    if row is None or row.date is None:
        raise HTTPException(status_code=404, detail="Документа нет в зеркале")
    df = (row.date - timedelta(days=1)).isoformat()
    dt = (row.date + timedelta(days=1)).isoformat()
    try:
        return {
            "mirror": {
                "sd_id": row.sd_id,
                "date": row.date.isoformat(),
                "store_sd_id": row.store_sd_id,
                "status": row.status,
                "amount": float(row.amount or 0),
                "code_1C": row.code_1c,
                "client_sd_id": row.client_sd_id,
                "synced_at": row.synced_at and row.synced_at.isoformat(),
            },
            "raw": salesdoc.raw_order(sd_id, df, dt),
            # Соседние документы той же точки за те же дни: видно, чем они
            # отличаются и какое поле «разъезжается».
            "siblings": salesdoc.raw_orders_of_day(df, dt, row.client_sd_id),
        }
    except salesdoc.SalesDocError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/cashboxes")
def cashboxes(
    db: Session = Depends(get_db),
    _: models.User = Depends(can_view),
):
    """Чем можно делить оплаты SalesDoc по фирмам — на живых цифрах.

    Склада у оплаты нет; кандидаты — касса, поле trade и связь с заказами.
    Отдаём покрытие каждого признака, чтобы решение принималось по фактам."""
    return {
        "cashboxes": salesdoc_mirror.cashboxes(db),
        "split": salesdoc_mirror.payment_split_stats(db),
    }


@router.post("/mirror/sync")
def mirror_sync(
    _: models.User = Depends(can_edit),
    full: bool = Query(default=False, description="Полная выгрузка вместо дельты"),
):
    """Запустить обновление зеркала в фоне. Ответ приходит сразу — данные
    доезжают сами, страницу это не задерживает."""
    _require_configured()
    return salesdoc_mirror.sync_async(full=full)
