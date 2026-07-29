"""Сверка учёта с SalesDoc: тянем данные из SalesDoc и сопоставляем с нашей
1С-картиной (дебиторка, реализации, оплаты за период), показываем расхождения.

Сопоставление клиентов — по нормализованному имени (в нашей 1С-выгрузке нет
кода контрагента, а в SalesDoc имя приходит рядом с балансом).
"""

import difflib
import re
from datetime import date

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
    """Склады, которым не задана фирма — их операции попадают в обе фирмы."""
    return [
        s.name or s.store_id
        for s in db.query(models.SalesDocStore).all()
        if s.store_id and not s.organization
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
                org_note = ("склады этой фирмы: " + ", ".join(named)) if named else None
            elif info is None:
                org_note = "фирма не определена: в SalesDoc нет реализаций этой точки"
                org_note_warn = True
            elif info.get("unmapped"):
                org_note = (
                    "фирма не определена: реализации на складах без фирмы — "
                    + ", ".join(info["unmapped"])
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


@router.get("/cashboxes")
def cashboxes(
    db: Session = Depends(get_db),
    _: models.User = Depends(can_view),
):
    """Кассы из журнала оплат SalesDoc: сколько операций и на какую сумму.

    Если кассы разведены по фирмам, их можно будет привязать так же, как
    склады, и определять фирму точки, у которой в SalesDoc одни оплаты."""
    return {"cashboxes": salesdoc_mirror.cashboxes(db)}


@router.post("/mirror/sync")
def mirror_sync(
    _: models.User = Depends(can_edit),
    full: bool = Query(default=False, description="Полная выгрузка вместо дельты"),
):
    """Запустить обновление зеркала в фоне. Ответ приходит сразу — данные
    доезжают сами, страницу это не задерживает."""
    _require_configured()
    return salesdoc_mirror.sync_async(full=full)
