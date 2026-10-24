"""Сверка управленки с налоговой по журналам проводок.

Документная сверка (страница «Контуры 1С») ищет пару каждому документу. Но
остаток товара и деньги складываются из всех документов сразу, и пропуск
одного звена — забытое списание, оплата, внесённая в одну базу, ручная
операция — виден только здесь, в итоге: штуки по товару и поступления от
покупателей в двух журналах должны совпадать.

Эталон — управленка: она сверена с фактом (пересчёт склада, выписки банка).
Поэтому расхождение остатка — задача налоговой, а оплата, которой нет в
одной из баз, — задача той базы, где её нет.
"""

import re
from collections import defaultdict
from datetime import date

from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import models
from ..routers.ledger import base_scope

# Счета товаров: 1610 товары, 1620 материалы (бензин, упаковка), 1640 товары
# в налоговой Хайджина. Остаток считается по всем — забытый бензин в
# налоговой такая же дыра, как забытое списание подгузников.
GOODS_PREFIXES = ("161", "162", "163", "164")
# Покупатели и авансы покупателей — с кредита этих счетов приходят оплаты.
CUSTOMER_ACCOUNTS = ("1410", "3210")
# Касса и банк — куда приходят деньги.
MONEY_PREFIXES = ("10", "12")
# Тестовые QR-платежи по 1–2 сома в налоговой сажают одной суммой, в
# управленке — по точкам. Это не расхождение, и задачей быть не должно.
MIN_PAYMENT = 50.0
# QR-оплату на 1 166,40 в одной базе сажают как 1 166 — тыйыны округляют.
AMOUNT_TOLERANCE = 1.0
# Оплату в две базы сажают в разные дни: выписку разносят пакетом.
PAYMENT_DAYS = 3


def has_nal_ledger(db: Session, org: str) -> bool:
    return (db.query(models.LedgerEntry.id)
            .filter(models.LedgerEntry.organization == org,
                    models.LedgerEntry.base == "nal").first()) is not None


def _norm(name: str) -> str:
    s = (name or "").strip().lower().replace("ё", "е")
    s = re.sub(r"\s+", " ", s)
    # «Детские подгузники ONE» в налоговой и «Подгузники ONE» в управленке —
    # один товар: справочники баз заводили разные люди.
    s = re.sub(r"^детские\s+", "", s)
    return s


def parse_aliases(text: str | None) -> dict[str, str]:
    """«налоговое = управленческое» по строке → карта нормализованных имён."""
    out = {}
    for line in (text or "").splitlines():
        if "=" not in line:
            continue
        a, b = line.split("=", 1)
        if a.strip() and b.strip():
            out[_norm(a)] = _norm(b)
    return out


def stock_by_item(db: Session, org: str, base: str,
                  aliases: dict[str, str] | None = None) -> dict[str, dict]:
    """Остаток в штуках по каждому товару одного контура."""
    E = models.LedgerEntry
    goods = [E.debit_account.like(p + "%") for p in GOODS_PREFIXES] + \
            [E.credit_account.like(p + "%") for p in GOODS_PREFIXES]
    q = base_scope(
        db.query(E.debit_account, E.debit_sub, E.debit_qty,
                 E.credit_account, E.credit_sub, E.credit_qty)
        .filter(E.organization == org, or_(*goods)), base)
    aliases = aliases or {}
    out: dict[str, dict] = {}

    def add(sub, qty, sign):
        if not sub or qty is None:
            return
        key = _norm(sub)
        key = aliases.get(key, key)
        a = out.get(key)
        if a is None:
            a = out[key] = {"name": sub.strip(), "qty": 0.0}
        a["qty"] += sign * float(qty)

    for da, ds, dq, ca, cs, cq in q.all():
        if da and da.startswith(GOODS_PREFIXES):
            add(ds, dq, 1)
        if ca and ca.startswith(GOODS_PREFIXES):
            add(cs, cq, -1)
    return out


def stock_diffs(db: Session, org: str,
                aliases: dict[str, str] | None = None) -> list[dict]:
    """Товары, у которых остаток в налоговой не равен управленке."""
    upr = stock_by_item(db, org, "upr", aliases)
    nal = stock_by_item(db, org, "nal", aliases)
    out = []
    for key in sorted(set(upr) | set(nal)):
        u = round(upr.get(key, {}).get("qty", 0.0), 3)
        n = round(nal.get(key, {}).get("qty", 0.0), 3)
        if abs(u - n) < 0.001:
            continue
        # Название показываем управленческое: управленка — эталон.
        name = (upr.get(key) or nal.get(key))["name"]
        out.append({"key": key, "item": name, "upr": u, "nal": n,
                    "diff": round(n - u, 3)})
    return out


def _receipts(db: Session, org: str, base: str, since: date) -> list[dict]:
    """Поступления от покупателей — по документу, с суммой и плательщиком."""
    E = models.LedgerEntry
    q = base_scope(
        db.query(E.doc_guid, E.doc, E.date, E.credit_sub, E.amount,
                 E.debit_account)
        .filter(E.organization == org, E.date >= since,
                E.credit_account.in_(CUSTOMER_ACCOUNTS)), base)
    docs: dict[str, dict] = {}
    for guid, doc, d, party, amount, debit in q.all():
        if not debit or not debit.startswith(MONEY_PREFIXES):
            continue
        key = guid or f"{doc}|{d}"
        x = docs.get(key)
        if x is None:
            x = docs[key] = {"doc": doc or "", "date": d,
                             "party": (party or "").strip(), "amount": 0.0}
        x["amount"] += float(amount or 0)
    return [x for x in docs.values() if x["amount"] >= MIN_PAYMENT]


def payment_diffs(db: Session, org: str, since: date) -> dict:
    """Оплаты покупателей, которые есть в одной базе и нет в другой.

    Пара — та же сумма (до сома) и дата в пределах трёх дней. Контрагенты
    в базах разные (в налоговой розница идёт на «ККМ», в управленке — на
    точку), поэтому плательщика для пары не сравниваем."""
    upr = sorted(_receipts(db, org, "upr", since), key=lambda x: x["date"])
    nal = _receipts(db, org, "nal", since)
    free = list(range(len(nal)))
    only_upr = []
    for u in upr:
        best = None
        for i in free:
            n = nal[i]
            if abs(n["amount"] - u["amount"]) >= AMOUNT_TOLERANCE:
                continue
            days = abs((n["date"] - u["date"]).days)
            if days <= PAYMENT_DAYS and (best is None or days < best[0]):
                best = (days, i)
        if best is None:
            only_upr.append(u)
        else:
            free.remove(best[1])
    return {"only_upr": only_upr, "only_nal": [nal[i] for i in free]}


def _doc_number(doc: str) -> str | None:
    m = re.search(r"(\d+)\s+от", doc or "")
    return (m.group(1).lstrip("0") or "0") if m else None


def extra_events(db: Session, cfg: "models.TaskBotSettings | None",
                 today: date) -> list[dict]:
    """Расхождения остатков и оплат — в виде событий журнала контуров.

    Считаются только для фирм, где загружен налоговый журнал: без него
    налоговой стороны нет вовсе, и «всё расходится» было бы враньём."""
    if cfg is None:
        return []
    aliases = parse_aliases(cfg.item_aliases)
    since = cfg.control_from or today.replace(day=1)
    stock_orgs = split_orgs(cfg.stock_orgs)
    pay_orgs = split_orgs(cfg.payment_orgs)
    out: list[dict] = []
    for org in models.ORGS:
        if org not in stock_orgs and org not in pay_orgs:
            continue
        if not has_nal_ledger(db, org):
            continue
        if org in stock_orgs:
            for d in stock_diffs(db, org, aliases):
                out.append({
                    "key": f"{org}|stock|{d['key']}",
                    "organization": org, "kind": "stock", "side": "tax",
                    "doc_date": today,
                    "doc_number": f"упр {_fmt(d['upr'])} · нал {_fmt(d['nal'])}",
                    "party": d["item"], "qty": d["diff"], "amount": 0,
                    "is_task": True,
                })
        if org in pay_orgs:
            res = payment_diffs(db, org, since)
            for side, rows in (("upr", res["only_upr"]), ("tax", res["only_nal"])):
                repeats: dict[str, int] = defaultdict(int)
                for r in sorted(rows, key=lambda x: (x["date"], x["doc"])):
                    base = "|".join([org, "payment", side, r["date"].isoformat(),
                                     r["party"], f'{r["amount"]:.2f}'])
                    repeats[base] += 1
                    key = base if repeats[base] == 1 else f"{base}#{repeats[base]}"
                    age = (today - r["date"]).days
                    out.append({
                        "key": key, "organization": org, "kind": "payment",
                        "side": side, "doc_date": r["date"],
                        "doc_number": _doc_number(r["doc"]),
                        "party": r["party"], "qty": 0,
                        "amount": round(r["amount"], 2),
                        "is_task": age >= (cfg.grace_days or 0),
                    })
    return out


def split_orgs(value: str | None) -> set[str]:
    return {o.strip() for o in (value or "").split(",") if o.strip() in models.ORGS}


def _fmt(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ") if abs(x - round(x)) < 0.001 else f"{x:,.2f}".replace(",", " ")

