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
    # Любую пунктуацию (кавычки, дефис и пр.) сводим к пробелу — «Ош-Нурзаман»
    # и «Ош Нурзаман» должны совпасть.
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _extract_sd_id(name: str) -> str | None:
    """Достаёт ИД клиента SalesDoc (напр. z8_1249) из имени контрагента 1С —
    самый надёжный ключ, т.к. он присутствует и там, и там."""
    m = _CODE_RE.search(name or "")
    return m.group(0).lower() if m else None

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
    delta = round(row["sd_debt"] - row["our_debt"], 2)
    if abs(delta) < 1:
        return "ok", "сходится"
    sd_sales = _sd_component(comp["sales"], row["sd_id"], row["code_1C"])
    sd_ret = _sd_component(comp["returns"], row["sd_id"], row["code_1C"])
    sd_pay = _sd_component(comp["payments"], row["sd_id"], row["code_1C"])
    # Значимые расхождения по компонентам — коротким словом каждый.
    factors = [
        ("реализации", round(sd_sales - row["our_sales"], 2)),
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

    if only_diff:
        rows = [r for r in rows if abs(r["diff"]) >= 0.5]

    # «Причина расхождения» по всем строкам сразу — одна массовая выгрузка из
    # SalesDoc (заказы/возвраты/оплаты), группируем по клиенту. Считаем только
    # по запросу: обычная загрузка дебиторки остаётся быстрой.
    # Причина расхождения считается по зеркалу — это обычная группировка в
    # базе, поэтому идёт всегда, без отдельной кнопки. Пока зеркало не
    # наполнено (первые секунды после старта), колонка просто пустая — список
    # из-за неё не тормозит.
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

    # --- Индексы для сопоставления ---
    sd_by_key: dict = {}      # по номеру накладной
    sd_by_tail: dict = {}     # по числовому хвосту номера
    sd_by_client: dict = {}   # по (клиент, дата) — когда номера нет
    for r in sd_list:
        k = _doc_key(r["doc_number"])
        if k:
            sd_by_key.setdefault(k, []).append(r)
            t = _doc_tail(k)
            if t:
                sd_by_tail.setdefault(t, []).append(r)
        for ck in {r["client_sd_id"], r["_ckey"]}:
            if ck:
                sd_by_client.setdefault((ck, r["date"]), []).append(r)

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
            pool = [r for ck in ckeys if ck
                    for r in sd_by_client.get((ck, d["date"]), [])]
            pair = pick(pool, d["amount"])
            how = "клиент + дата" if pair else None
        if pair is not None:
            used.add(id(pair))
            if how == "номер":
                by_number += 1
        same = pair is not None and abs(d["amount"] - pair["amount"]) < 0.5
        rows.append({
            "date": d["date"],
            "client": d["client"],
            "doc_number": d["doc_number"],
            "our_warehouse": d["warehouse"],
            "our_amount": d["amount"],
            "sd_doc": pair and (pair["doc_number"] or pair["sd_id"]),
            "sd_store": pair and pair["store"],
            "sd_status": pair and pair["status_label"],
            "sd_amount": pair and pair["amount"],
            "diff": round(d["amount"] - pair["amount"], 2) if pair else None,
            "matched_by": how,
            "verdict": "ok" if same else ("diff" if pair else "only_1c"),
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
              for v in ("ok", "diff", "only_1c", "only_sd")}

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

        # --- 2. Одна гигантская страница против пагинации ---
        result, _pg2 = salesdoc.call("getOrder", {"limit": 5000, "page": 1,
                                                  "filter": base_filter})
        big = salesdoc._pick(result, ("orders", "order"))
        out["big_page"] = {"received": len(big)}
        if len(big) != len(rows):
            out["verdicts"].append(
                f"Одной страницей приходит {len(big)} строк, пагинацией "
                f"{len(rows)} — сервер отдаёт разные наборы")

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


@router.get("/find-doc")
def find_doc(
    db: Session = Depends(get_db),
    _: models.User = Depends(can_view),
    amount: float = Query(..., description="Сумма документа"),
    query: str = Query(default="", description="Часть имени точки (необязательно)"),
):
    """Где документ с такой суммой: в 1С, в зеркале, в живом SalesDoc.

    Вопрос «почему на портале не видно реализацию на N» повторяется, а ответ
    каждый раз разный: документ не доехал до зеркала, скрыт фильтром по складу,
    не в том статусе или его просто нет. Ищем сумму во всех трёх местах и
    говорим, на каком шаге она потерялась."""
    tol = 0.01
    qq = (query or "").strip().lower()
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
    for r in db.query(models.SalesDocOrder).filter(
            models.SalesDocOrder.amount >= amount - tol,
            models.SalesDocOrder.amount <= amount + tol).all():
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
            "notes": notes,
        })

    # --- 1С: итог документа продаж ---
    sales_hits, seen_docs = [], set()
    for s in db.query(models.Sale).filter(
            models.Sale.doc_total >= amount - tol,
            models.Sale.doc_total <= amount + tol).all():
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
        for o in salesdoc.call_all("getOrder", ("orders", "order"), {"filter": {
                "include": "all", "status": [1, 2, 3, 4, 5],
                "period": {"date": {"from": df, "to": dt}}}}):
            amt = float(o.get("totalSummaAfterDiscount") or o.get("totalSumma") or 0)
            if abs(amt - amount) > tol:
                continue
            cli = o.get("client") or {}
            cli_sd = str(cli.get("SD_id") or "").lower() or None
            if not match_client(cli_sd, cli.get("code_1C")):
                continue
            live_hits.append({
                "sd_id": str(o.get("SD_id") or o.get("CS_id") or "").strip(),
                "date": (o.get("dateDocument") or o.get("dateCreate") or "")[:10],
                "client": names.get(cli_sd or "", cli_sd or ""),
                "store": (o.get("store") or {}).get("name")
                         or (o.get("store") or {}).get("SD_id"),
                "status_label": salesdoc.ORDER_STATUS.get(o.get("status"),
                                                          str(o.get("status"))),
                "amount": round(amt, 2),
            })
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
        verdicts.append("В SalesDoc документа с такой суммой нет — если он есть "
                        "в 1С, отгрузка в SalesDoc не проведена")

    return {
        "amount": amount,
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
