"""«Дубли точек»: одна и та же торговая точка, заведённая в SalesDoc дважды.

Одинаковое имя само по себе ещё не дубль — «Береке» или «Нур» в городе десятки.
Дубль — это одно имя в одном месте: агент не нашёл точку в планшете и завёл
новую карточку, и теперь продажи, долг и визиты одной лавки разъехались по
двум кодам. Поэтому группа строится по двум признакам сразу — имени
(без кода в скобках и слов вроде «магазин», «маркет») и GPS-метке
из карточки getClient.

Виды групп:
  * «дубль»          — одно имя, метки в пределах DUP_RADIUS_M: почти наверняка
                       одна точка, карточки надо объединить;
  * «одно место»     — разные имена, метки в пределах SAME_SPOT_M: возможно,
                       точку переименовали (или в одном здании две лавки);
  * «нет координат»  — одно имя, но у кого-то нет метки: проверить вручную.

У каждой карточки — заказы, отгрузки, последний заказ, агенты и долг, а для
группы — какую карточку оставить (у которой больше заказов, при равенстве —
заведённую раньше). Всё из зеркала, к SalesDoc отчёт не обращается.
"""

import math
import re
from collections import defaultdict

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from . import salesdoc
from .salesdoc_mirror import match_key

DUP_RADIUS_M = 60
SAME_SPOT_M = 15
MAX_BARE_GROUP = 6

# Слова, которые агенты то пишут, то нет: «Магазин Нур» и «Нур маркет» — одно.
_STOP = {
    "магазин", "маг", "маркет", "market", "мини", "mini", "супермаркет",
    "минимаркет", "супер", "super", "ип", "осоо", "оао", "ооо", "тц", "тд",
    "киоск", "дукон", "дүкөн", "точка", "м", "и",
}


def norm_name(name: str) -> str:
    words = [w for w in match_key(name).split() if w not in _STOP]
    return " ".join(sorted(words))


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Расстояние по поверхности Земли между (lat, lon) в метрах."""
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


class _DSU:
    def __init__(self):
        self.p: dict = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def _point(c) -> tuple[float, float] | None:
    return (c.lat, c.lon) if c.lat is not None and c.lon is not None else None


def _groups(clients) -> list[tuple[str, list]]:
    by_name: dict[str, list] = defaultdict(list)
    for c in clients:
        key = norm_name(c.name)
        if key:
            by_name[key].append(c)

    out: list[tuple[str, list]] = []
    used: set[str] = set()

    # 1. Одно имя, рядом.
    for same in by_name.values():
        if len(same) < 2:
            continue
        geo = [c for c in same if _point(c)]
        dsu = _DSU()
        for i, a in enumerate(geo):
            for b in geo[i + 1:]:
                if distance_m(_point(a), _point(b)) <= DUP_RADIUS_M:
                    dsu.union(a.sd_id, b.sd_id)
        clusters: dict = defaultdict(list)
        for c in geo:
            clusters[dsu.find(c.sd_id)].append(c)
        for members in clusters.values():
            if len(members) > 1:
                out.append(("дубль", members))
                used.update(c.sd_id for c in members)
        # 2. Одно имя, но без метки — расстояние не проверить.
        # Если таких имён по городу много («Береке»), рядом с карточкой без
        # метки показываем только другие карточки без метки — иначе группа
        # разрастается до десятков заведомо разных лавок.
        bare = [c for c in same if not _point(c)]
        if bare:
            rest = [c for c in same if c.sd_id not in used]
            if len(rest) > MAX_BARE_GROUP:
                rest = bare
            if len(rest) > 1:
                out.append(("нет координат", rest))
                used.update(c.sd_id for c in rest)

    # 3. Разные имена в одной точке. Сетка ~SAME_SPOT_M, соседей смотрим в
    # соседних ячейках, чтобы не сравнивать каждую точку с каждой.
    geo = [c for c in clients if _point(c) and c.sd_id not in used]
    # Градус долготы короче градуса широты (на 43° — в 0,73 раза), поэтому
    # ячейку по долготе берём с запасом до 60° широты.
    step = SAME_SPOT_M / 111_000
    cells: dict = defaultdict(list)
    for c in geo:
        cells[(int(c.lat // step), int(c.lon // (step * 2)))].append(c)
    dsu = _DSU()
    for (i, j), here in cells.items():
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for a in here:
                    for b in cells.get((i + di, j + dj), ()):
                        if (a.sd_id < b.sd_id
                                and distance_m(_point(a), _point(b)) <= SAME_SPOT_M):
                            dsu.union(a.sd_id, b.sd_id)
    clusters = defaultdict(list)
    for c in geo:
        if c.sd_id in dsu.p:
            clusters[dsu.find(c.sd_id)].append(c)
    for members in clusters.values():
        if len(members) > 1:
            out.append(("одно место", members))
    return out


def report(db: Session) -> dict:
    C = models.SalesDocClient
    O = models.SalesDocOrder
    clients = db.query(C).all()
    with_geo = sum(1 for c in clients if _point(c))
    groups = _groups(clients)

    ids = {c.sd_id for _, members in groups for c in members}
    orders: dict = {}
    if ids:
        shipped = list(salesdoc.SHIPPED_STATUSES)
        for sid, n, total, last in (
                db.query(O.client_sd_id, func.count(O.id),
                         func.coalesce(func.sum(O.amount), 0), func.max(O.date))
                .filter(O.client_sd_id.in_(ids), O.status.in_(shipped))
                .group_by(O.client_sd_id).all()):
            orders[sid] = (int(n), float(total or 0), last)
    agent_names = {a.sd_id: a.name for a in db.query(models.SalesDocAgent).all()}
    agents: dict = defaultdict(list)
    if ids:
        for link in (db.query(models.SalesDocClientAgent)
                     .filter(models.SalesDocClientAgent.client_sd_id.in_(ids)).all()):
            agents[link.client_sd_id].append(
                agent_names.get(link.agent_sd_id) or link.agent_sd_id)

    kind_order = {"дубль": 0, "нет координат": 1, "одно место": 2}
    rows = []
    for kind, members in groups:
        cards = []
        for c in members:
            n, total, last = orders.get(c.sd_id, (0, 0.0, None))
            cards.append({
                "sd_id": c.sd_id,
                "code_1c": c.code_1c,
                "name": c.name,
                "address": c.address,
                "waymark": c.waymark,
                "lat": c.lat,
                "lon": c.lon,
                "inn": c.inn,
                "created": c.sd_created,
                "active": c.active,
                "debt": float(c.debt or 0),
                "orders": n,
                "shipped": round(total, 2),
                "last_order": last.isoformat() if last else None,
                "agents": sorted(set(agents.get(c.sd_id, []))),
            })
        # Оставить ту, что живее: активную; с бóльшим числом заказов; с долгом
        # (иначе долг уйдёт вместе с удалённой карточкой); с более свежим
        # заказом; при прочем равенстве — заведённую раньше.
        # Сортировки устойчивые: последняя задаёт главный признак.
        cards.sort(key=lambda r: (r["created"] or "9999", r["sd_id"]))
        cards.sort(key=lambda r: r["last_order"] or "", reverse=True)
        cards.sort(key=lambda r: (not r["active"], -r["orders"], abs(r["debt"]) < 1))
        cards[0]["keep"] = True
        pts = [(r["lat"], r["lon"]) for r in cards if r["lat"] is not None]
        spread = max((distance_m(a, b) for i, a in enumerate(pts)
                      for b in pts[i + 1:]), default=None)
        rows.append({
            "kind": kind,
            "name": cards[0]["name"],
            "spread_m": round(spread) if spread is not None else None,
            "shipped": round(sum(r["shipped"] for r in cards), 2),
            "debt": round(sum(r["debt"] for r in cards), 2),
            "cards": cards,
        })
    rows.sort(key=lambda g: (kind_order[g["kind"]], -g["shipped"], g["name"]))
    return {
        "clients": len(clients),
        "with_geo": with_geo,
        "dup_radius_m": DUP_RADIUS_M,
        "same_spot_m": SAME_SPOT_M,
        "counts": {k: sum(1 for g in rows if g["kind"] == k) for k in kind_order},
        "groups": rows,
    }


def _clean(name: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\([^)]*\)", " ", name or "")).strip()


def task_events(db: Session, today) -> list[dict]:
    """Подтверждённые дубли точек (одно имя, одна GPS-метка) — задачи для
    журнала контуров: какую карточку оставить, какие закрыть.

    Считаются только активные карточки: деактивировали лишнюю в SalesDoc —
    в группе осталась одна, и задача закрылась сама. Ключ — состав группы,
    поэтому из трёх карточек закрыли одну — старая задача закрывается, а
    на оставшиеся две приходит новая. Пока в зеркале нет ни одной GPS-метки,
    задач нет вовсе: «дублей не найдено» тогда значило бы «не искали»."""
    rep = report(db)
    if not rep["with_geo"]:
        return []
    out = []
    for g in rep["groups"]:
        if g["kind"] != "дубль":
            continue
        active = [c for c in g["cards"] if c["active"]]
        if len(active) < 2:
            continue
        keep = next((c for c in g["cards"] if c.get("keep") and c["active"]), active[0])
        close = [c for c in active if c is not keep]
        debt = round(sum(c["debt"] for c in close if abs(c["debt"]) >= 1), 2)
        out.append({
            "key": "hygiene|clientdup|" + "+".join(sorted(c["sd_id"] for c in active)),
            "organization": "hygiene", "kind": "clientdup", "side": "upr",
            "doc_date": today,
            "doc_number": (f"оставить {keep['sd_id']} · закрыть "
                           + ", ".join(c["sd_id"] for c in close)),
            "party": _clean(keep["name"]) or keep["sd_id"],
            "qty": len(close), "amount": debt, "is_task": True,
        })
    return out
