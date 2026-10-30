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
from datetime import date, timedelta

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
# QR вечера пятницы управленка вносит во вторник — это четыре дня.
QR_DAYS = 5


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
    upr = _receipts(db, org, "upr", since)
    nal = _receipts(db, org, "nal", since)
    return pair_payments(upr, nal)


# Расчёты с персоналом по оплате труда.
SALARY_ACCOUNT = "3520"
# Аванс 5-го числа закрывает начисление в конце месяца, а зарплату за месяц
# платят в начале следующего. Выплата без начисления — задача, только если
# начисления нет и через 35 дней.
SALARY_GRACE_DAYS = 35


# Расчёты с подотчётными лицами.
ACCOUNTABLE_ACCOUNT = "1520"
# Срок авансового отчёта — 30 дней с выдачи.
ACCOUNTABLE_DAYS = 30


def accountable_overdue(db: Session, org: str, base: str, today: date) -> list[dict]:
    """Подотчёт, по которому нет отчёта дольше 30 дней.

    Выданное под отчёт (Дт 1520) закрывается авансовым отчётом или
    возвратом (Кт 1520). Закрытие идёт в счёт самых ранних выдач; что из
    выданного раньше чем 30 дней назад так и не закрыто — просрочено."""
    E = models.LedgerEntry
    q = base_scope(db.query(
        E.date, E.debit_account, E.debit_sub, E.credit_account, E.credit_sub,
        E.amount).filter(E.organization == org, or_(
            E.debit_account == ACCOUNTABLE_ACCOUNT,
            E.credit_account == ACCOUNTABLE_ACCOUNT)), base)
    issued: dict = defaultdict(list)
    closed: dict = defaultdict(float)
    for d, da, ds, ka, ks, amount in q.all():
        a = float(amount or 0)
        if da == ka:
            continue
        if da == ACCOUNTABLE_ACCOUNT:
            issued[(ds or "").strip()].append((d, a))
        if ka == ACCOUNTABLE_ACCOUNT:
            closed[(ks or "").strip()] += a
    cutoff = today - timedelta(days=ACCOUNTABLE_DAYS)
    out = []
    for who, rows in issued.items():
        left = closed.get(who, 0.0)
        overdue, since = 0.0, None
        for d, a in sorted(rows):
            covered = min(a, left)
            left -= covered
            rest = a - covered
            if rest > 0.005 and d <= cutoff:
                overdue += rest
                since = since or d
        if overdue >= 1 and who:
            out.append({"person": who, "amount": round(overdue, 2), "since": since})
    return out


def salary_gaps(db: Session, org: str, base: str, today: date) -> list[dict]:
    """Сотрудники, которым выплачено больше, чем начислено.

    Начисленное выплачивается, а обратное не проверял никто: месяцы,
    начисленные символической суммой, неначисленный месяц, оплата услуг
    «прочим расходом» на счёте зарплаты, доля зарплаты, которую фирма
    платит, но не начисляет. Считаем по сотруднику: всё начисленное против
    выплаченного (и удержанного) раньше чем 35 дней назад."""
    E = models.LedgerEntry
    q = base_scope(db.query(
        E.date, E.debit_account, E.debit_sub, E.credit_account, E.credit_sub,
        E.amount).filter(E.organization == org, or_(
            E.debit_account == SALARY_ACCOUNT,
            E.credit_account == SALARY_ACCOUNT)), base)
    cutoff = today - timedelta(days=SALARY_GRACE_DAYS)
    accrued: dict = defaultdict(float)
    paid: dict = defaultdict(list)
    for d, da, ds, ka, ks, amount in q.all():
        a = float(amount or 0)
        if da == ka:
            continue
        if ka == SALARY_ACCOUNT:
            accrued[(ks or "").strip()] += a
        if da == SALARY_ACCOUNT and d <= cutoff:
            paid[(ds or "").strip()].append((d, a))
    out = []
    for who, rows in paid.items():
        excess = sum(a for _, a in rows) - accrued.get(who, 0.0)
        if excess < 1 or not who:
            continue
        # С какой выплаты начался перебор: идём от последних к первым, пока
        # не наберём лишнее.
        left, since = excess, None
        for d, a in sorted(rows, reverse=True):
            since = d
            left -= a
            if left <= 0:
                break
        out.append({"employee": who, "amount": round(excess, 2), "since": since})
    return out


def qr_missing(db: Session, org: str) -> list[dict]:
    """QR-оплаты из отчёта банка, которых нет в управленке.

    Сверка баз видит только то, что внесено хотя бы в одну: три QR вечера
    26.10 на 7 182 сом не внесли ни управленка, ни налоговая — и задачи не
    было. Пара — оплата покупателя в управленке на ту же сумму (до сома) в
    пределах пяти дней: QR вечера пятницы вносят во вторник. Две-три QR
    одной точки, внесённые одной оплатой, — тоже пара.

    Дата «Контроль с» здесь не действует: отчёт загружают за тот период,
    который хотят сверить, — загрузили июль, значит, сверяем июль. Сдвигать
    «Контроль с» ради этого нельзя: вместе с QR всплыла бы вся история
    документов."""
    Q = models.QrPayment
    qr = [{"txn_id": t, "date": d, "paid_at": at, "amount": float(a or 0),
           "party": c or ""}
          for t, d, at, a, s, c in db.query(
              Q.txn_id, Q.date, Q.paid_at, Q.amount, Q.status, Q.comment)
          .filter(Q.organization == org).all()
          if (s or "").lower() == "оплата прошла" and float(a or 0) >= MIN_PAYMENT]
    if not qr:
        return []
    first = min(q["date"] for q in qr)
    upr = _receipts(db, org, "upr", first - timedelta(days=QR_DAYS))
    missing, free = _pair_qr(qr, upr)
    missing = _batches(missing, upr, free, QR_DAYS)
    return _split_qr(missing, [upr[i] for i in sorted(free)])


def _pair_qr(qr: list[dict], upr: list[dict]) -> tuple[list[dict], set]:
    """QR по порядку времени — к самой ранней свободной оплате той же суммы,
    внесённой в день QR или позже, но не позже чем через QR_DAYS.

    Сверка баз ищет пару сначала в тот же день — там это верно. А QR
    банк зачисляет с задержкой: QR вечера пятницы 31.07 пришли в выписку
    04.08, и каждая следующая оплата той же суммы съезжает на день-два. Пара
    «в тот же день» цепляла QR 04.08 к оплате 04.08, и QR 31.07 оставался
    без пары, хотя оплат в управленке ровно столько же, сколько QR."""
    by_amount: dict = defaultdict(list)
    for i, u in enumerate(upr):
        by_amount[round(u["amount"])].append(i)
    free = set(range(len(upr)))
    missing = []
    for q in sorted(qr, key=lambda x: (x["date"], x["paid_at"])):
        a = round(q["amount"])
        cand = [i for k in (a - 1, a, a + 1) for i in by_amount.get(k, ())
                if i in free
                and abs(upr[i]["amount"] - q["amount"]) < AMOUNT_TOLERANCE
                and 0 <= (upr[i]["date"] - q["date"]).days <= QR_DAYS]
        if cand:
            free.discard(min(cand, key=lambda i: (upr[i]["date"], i)))
        else:
            missing.append(q)
    return missing, free


def _split_qr(missing: list[dict], free: list[dict]) -> list[dict]:
    """Один QR, разнесённый в управленке на несколько точек: QR 25.08 на
    16 300 — это «Маркет Таза шоп Араванская» 9 300 и «Тазалык дуйносу»
    7 000. Если сумма двух-четырёх непарных оплат управленки в пределах
    окна равна непарному QR (до сома на оплату), QR внесён."""
    from itertools import combinations

    left = list(free)
    out = []
    for q in sorted(missing, key=lambda x: x["date"]):
        near = [u for u in left if abs((u["date"] - q["date"]).days) <= QR_DAYS
                and u["amount"] < q["amount"] + AMOUNT_TOLERANCE]
        hit = None
        if 2 <= len(near) <= 12:
            for k in range(2, min(BATCH_MAX, len(near)) + 1):
                hit = next((c for c in combinations(near, k)
                            if abs(sum(u["amount"] for u in c) - q["amount"])
                            < AMOUNT_TOLERANCE * k), None)
                if hit:
                    break
        if hit:
            for u in hit:
                left.remove(u)
        else:
            out.append(q)
    return out


def _other_inflows(db: Session, org: str, since: date) -> list[dict]:
    """Поступления на кассу и банк налоговой, проведённые НЕ как оплата
    покупателя: возврат аванса поставщиком, прочий приход, займ. Переводы
    между своими счетами (касса ↔ банк) не в счёт."""
    E = models.LedgerEntry
    q = base_scope(
        db.query(E.doc_guid, E.doc, E.date, E.credit_account, E.credit_sub,
                 E.content, E.amount, E.debit_account)
        .filter(E.organization == org, E.date >= since), "nal")
    docs: dict[str, dict] = {}
    for guid, doc, d, acc, party, content, amount, debit in q.all():
        if not debit or not debit.startswith(MONEY_PREFIXES):
            continue
        if not acc or acc in CUSTOMER_ACCOUNTS or acc.startswith(MONEY_PREFIXES):
            continue
        key = guid or f"{doc}|{d}"
        x = docs.get(key)
        if x is None:
            x = docs[key] = {"doc": doc or "", "date": d, "account": acc,
                             "party": (party or "").strip(),
                             "content": (content or "").strip(), "amount": 0.0}
        x["amount"] += float(amount or 0)
    return [x for x in docs.values() if x["amount"] >= MIN_PAYMENT]


def _wrong_operation(only_upr: list[dict], other: list[dict]) -> tuple[list, list]:
    """Оплата покупателя из управленки, которой нет среди оплат покупателей
    налоговой, но в налоговой в те же дни есть поступление на ту же сумму
    другим видом операции — значит, её провели не тем видом: Ош рынок
    25 200 от 05.10 налоговая записала «возвратом аванса» от поставщика."""
    free = list(range(len(other)))
    left, wrong = [], []
    for u in sorted(only_upr, key=lambda x: x["date"]):
        best = None
        for i in free:
            n = other[i]
            days = abs((n["date"] - u["date"]).days)
            if abs(n["amount"] - u["amount"]) < AMOUNT_TOLERANCE and days <= PAYMENT_DAYS \
                    and (best is None or days < best[0]):
                best = (days, i)
        if best is None:
            left.append(u)
        else:
            free.remove(best[1])
            wrong.append((u, other[best[1]]))
    return left, wrong


def _party_mismatch(db: Session, org: str, pairs: list) -> list:
    """Пары, где сумма и дата сошлись, а покупатель — нет: в налоговой оплата
    записана на юрлицо (Булак Маркет, ИП Маманов), а в управленке это оплата
    точки или другого покупателя. Розница в налоговой — «ККМ», не юрлицо.
    Связки, которые налоговая сама держит по реализациям (Байго → ИП
    Атабекова), ошибкой не считаются."""
    from ..routers.tax import _customer_tokens, _is_legal, _party_aliases

    odd = [(u, n) for u, n in pairs
           if _is_legal(n["party"])
           and not _customer_tokens(u["party"]) & _customer_tokens(n["party"])]
    if not odd:
        return []
    aliases = _party_aliases(db, org)
    return [(u, n) for u, n in odd
            if (_customer_tokens(u["party"]), _customer_tokens(n["party"])) not in aliases]


def pair_payments(upr: list[dict], nal: list[dict],
                  days_max: int = PAYMENT_DAYS) -> dict:
    """Пары оплат: сначала — та же дата и сумма, потом оставшимся — ±3 дня.

    Розничные точки платят одними и теми же суммами (874, 1 749, 3 499 —
    цены упаковок), по десятку в день. Если сразу искать ближайшую в окне
    ±3 дня, один непарный платёж «съедает» чужую пару соседнего дня, тот —
    следующего, и «без пары» в итоге оказываются Комбини и Уют 22.10, хотя
    на самом деле в налоговой не хватает двух оплат 12.10, слитых в одну
    «ККМ 2 624» от 09.10."""
    free = set(range(len(nal)))
    rest = []
    pairs = []
    by_day: dict = defaultdict(list)
    for i, n in enumerate(nal):
        by_day[(n["date"], round(n["amount"]))].append(i)
    for u in sorted(upr, key=lambda x: x["date"]):
        same = [i for i in by_day.get((u["date"], round(u["amount"])), [])
                if i in free and abs(nal[i]["amount"] - u["amount"]) < AMOUNT_TOLERANCE]
        if same:
            free.remove(same[0])
            pairs.append((u, nal[same[0]]))
        else:
            rest.append(u)
    only_upr = []
    for u in rest:
        best = None
        for i in free:
            n = nal[i]
            if abs(n["amount"] - u["amount"]) >= AMOUNT_TOLERANCE:
                continue
            days = abs((n["date"] - u["date"]).days)
            if days <= days_max and (best is None or days < best[0]):
                best = (days, i)
        if best is None:
            only_upr.append(u)
        else:
            free.remove(best[1])
            pairs.append((u, nal[best[1]]))
    only_upr = _batches(only_upr, nal, free, days_max)
    return {"only_upr": only_upr, "only_nal": [nal[i] for i in sorted(free)],
            "pairs": pairs}


# Пачка ККМ: сколько оплат управленки может лечь в одну строку налоговой.
BATCH_MAX = 4
# Оплата сети одним платежом: на сколько её точек управленка может разнести.
CHAIN_MAX = 8


def _batches(only_upr: list[dict], nal: list[dict], free: set,
             days_max: int = PAYMENT_DAYS) -> list[dict]:
    """Розничные оплаты налоговая вправе проводить пачкой через ККМ: две
    оплаты точек 12.10 — Family Home 1 749 и Альбина 874 — в налоговой
    одной строкой «ККМ 2 624». Если сумма нескольких непарных оплат
    управленки в пределах трёх дней равна непарной строке налоговой (до
    сома на оплату), это пачка, а не два пропуска и одна лишняя."""
    from itertools import combinations

    from ..routers.tax import _customer_tokens, _is_legal

    left = list(only_upr)
    for i in sorted(free, key=lambda i: nal[i]["date"]):
        n = nal[i]
        if _is_legal(n.get("party")):
            # Оплата юрлица в пачку собирается только из оплат его же точек:
            # «Умай Групп ОсОО - Глобус» 85 108 в управленке разнесён на
            # «Глобус 11», «Глобус 12», «Глобус 14», «Глобус 26». Случайно
            # совпавшая сумма чужих мелких оплат юрлицо прятать не должна.
            chain = _customer_tokens(n.get("party"))
            near = [u for u in left if abs((u["date"] - n["date"]).days) <= days_max
                    and u["amount"] < n["amount"] + AMOUNT_TOLERANCE
                    and _customer_tokens(u.get("party")) & chain]
            most = CHAIN_MAX
        else:
            near = [u for u in left if abs((u["date"] - n["date"]).days) <= days_max
                    and u["amount"] < n["amount"] + AMOUNT_TOLERANCE
                    and not _is_legal(u.get("party"))]
            most = BATCH_MAX
        if len(near) < 2 or len(near) > 12:
            continue
        hit = None
        for k in range(2, min(most, len(near)) + 1):
            for combo in combinations(near, k):
                if abs(sum(u["amount"] for u in combo) - n["amount"]) < AMOUNT_TOLERANCE * k:
                    hit = combo
                    break
            if hit:
                break
        if hit:
            free.discard(i)
            for u in hit:
                left.remove(u)
    return left


# Как документ, которого нет во второй базе, сдвигает остаток «нал − упр».
# Продажа, внесённая только в управленку, — в налоговой товар ещё на складе,
# там больше; поступление только в управленке — в налоговой меньше. Документ
# только в налоговой — наоборот.
_OUTFLOW = ("sale", "writeoff")
_INFLOW = ("purchase", "return")


def _doc_lines(db: Session, org: str, kind: str, side: str, number: str | None,
               d: date, party: str | None) -> list[tuple[str, float]]:
    """Товары и штуки одного документа — из строк выгрузки его базы."""
    if side == "upr" and kind == "return":
        # Строки возвратов управленки лежат без номера документа — только
        # дата и покупатель; по ним и находим.
        R = models.ReturnLine
        q = db.query(R.product, R.qty).filter(
            R.organization == org, R.date == d, R.client == party)
    elif side == "upr":
        model, name = {
            "sale": (models.Sale, models.Sale.product),
            "writeoff": (models.WriteOff, models.WriteOff.product),
            "purchase": (models.Purchase, models.Purchase.product),
        }.get(kind, (None, None))
        if model is None or not number:
            return []
        q = db.query(name, model.qty).filter(
            model.organization == org, model.doc_number == number,
            model.date == d)
    else:
        if not number:
            return []
        T = models.TaxOperation
        q = db.query(T.product, T.qty).filter(
            T.organization == org, T.kind == kind, T.doc_number == number,
            T.date == d,
            # Доп. расходы повторяют товары поступления — не второй приход.
            or_(T.source.is_(None), ~T.source.ilike("%доп расход%")))
    return [(p, float(qty)) for p, qty in q.all() if p and qty]


def pending_by_item(db: Session, org: str, pending: list[dict],
                    aliases: dict[str, str]) -> dict[str, dict]:
    """На сколько штук по каждому товару остаток налоговой обязан отличаться
    от управленки из-за документов, ещё не внесённых во вторую базу.

    Невнесённая реализация №134 на 150 шт — и в налоговой ровно на 150 шт
    больше: это не вторая задача, а та же самая."""
    out: dict[str, dict] = {}
    for p in pending:
        if p["org"] != org:
            continue
        kind, side = p["kind"], p["side"]
        if kind not in _OUTFLOW + _INFLOW:
            continue
        sign = (1 if kind in _OUTFLOW else -1) * (1 if side == "upr" else -1)
        for product, qty in _doc_lines(db, org, kind, side, p.get("number"),
                                   p["date"], p.get("party")):
            key = _norm(product)
            key = aliases.get(key, key)
            a = out.setdefault(key, {"qty": 0.0, "docs": []})
            a["qty"] += sign * qty
            label = p["label"]
            if label not in a["docs"]:
                a["docs"].append(label)
    return out


def _doc_number(doc: str) -> str | None:
    m = re.search(r"(\d+)\s+от", doc or "")
    return (m.group(1).lstrip("0") or "0") if m else None


def extra_events(db: Session, cfg: "models.TaskBotSettings | None",
                 today: date, pending: list[dict] | None = None) -> list[dict]:
    """Расхождения остатков и оплат — в виде событий журнала контуров.

    Считаются только для фирм, где загружен налоговый журнал: без него
    налоговой стороны нет вовсе, и «всё расходится» было бы враньём.

    pending — документы без пары во второй базе, по которым уже есть задача
    («внести реализацию №134»). Разница остатка, которую они объясняют,
    отдельной задачей не становится: внесут документ — сойдётся и остаток."""
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
            waiting = pending_by_item(db, org, pending or [], aliases)
            for d in stock_diffs(db, org, aliases):
                w = waiting.get(d["key"])
                rest = round(d["diff"] - (w["qty"] if w else 0.0), 3)
                if abs(rest) < 0.001:
                    continue
                note = f"упр {_fmt(d['upr'])} · нал {_fmt(d['nal'])}"
                if w and abs(w["qty"]) >= 0.001:
                    note += (f"; ещё {_fmt(abs(w['qty']))} — это "
                             f"{', '.join(w['docs'])}, задача уже есть")
                out.append({
                    "key": f"{org}|stock|{d['key']}",
                    "organization": org, "kind": "stock", "side": "tax",
                    "doc_date": today,
                    "doc_number": note,
                    "party": d["item"], "qty": rest, "amount": 0,
                    "is_task": True,
                })
        if org in pay_orgs:
            res = payment_diffs(db, org, since)
            age_ok = lambda d: (today - d).days >= (cfg.grace_days or 0)
            for u, n in _party_mismatch(db, org, res["pairs"]):
                num = _doc_number(n["doc"])
                out.append({
                    "key": f"{org}|payparty|{n['date'].isoformat()}|{num}|{n['amount']:.2f}",
                    "organization": org, "kind": "payparty", "side": "tax",
                    "doc_date": n["date"], "doc_number": num,
                    "party": f"{n['party']} → {u['party']}", "qty": 0,
                    "amount": round(n["amount"], 2), "is_task": age_ok(n["date"]),
                })
            other = _other_inflows(db, org, since - timedelta(days=PAYMENT_DAYS))
            res["only_upr"], wrong = _wrong_operation(res["only_upr"], other)
            for u, n in wrong:
                num = _doc_number(n["doc"])
                how = n["content"] or "другой операцией"
                out.append({
                    "key": f"{org}|payop|{n['date'].isoformat()}|{num}|{n['amount']:.2f}",
                    "organization": org, "kind": "payop", "side": "tax",
                    "doc_date": n["date"], "doc_number": num,
                    "party": f"«{how}», счёт {n['account']}, {n['party'] or '—'} → {u['party']}",
                    "qty": 0, "amount": round(n["amount"], 2), "is_task": age_ok(n["date"]),
                })
            for base, side in (("upr", "upr"), ("nal", "tax")):
                for g in salary_gaps(db, org, base, today):
                    out.append({
                        "key": f"{org}|salary|{side}|{g['employee']}",
                        "organization": org, "kind": "salary", "side": side,
                        "doc_date": g["since"], "doc_number": f"с {g['since']:%d.%m.%y}",
                        "party": g["employee"], "qty": 0,
                        "amount": g["amount"], "is_task": True,
                    })
                for g in accountable_overdue(db, org, base, today):
                    out.append({
                        "key": f"{org}|accountable|{side}|{g['person']}",
                        "organization": org, "kind": "accountable", "side": side,
                        "doc_date": g["since"], "doc_number": f"с {g['since']:%d.%m.%y}",
                        "party": g["person"], "qty": 0,
                        "amount": g["amount"], "is_task": True,
                    })
            for q in qr_missing(db, org):
                out.append({
                    "key": f"{org}|qr|{q['txn_id']}",
                    "organization": org, "kind": "qr", "side": "bank",
                    "doc_date": q["date"], "doc_number": f"{q['paid_at']:%H:%M}",
                    "party": q["party"] or "QR", "qty": 0,
                    "amount": round(q["amount"], 2), "is_task": age_ok(q["date"]),
                })
            legal, retail = _split_retail(res)
            out += _retail_months(org, retail, today)
            for side, rows in (("upr", legal["upr"]), ("tax", legal["tax"])):
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


def _split_retail(res: dict) -> tuple[dict, dict]:
    """Непарные оплаты — на юрлиц и розницу.

    Оплаты юрлиц сверяются день в день, каждая. Розница (точки, рынок; в
    налоговой — «ККМ») идёт в налоговую через ККМ пачкой в конце месяца:
    задача по каждой точке на 874 сом — шум, а не контроль."""
    from ..routers.tax import _is_legal

    legal = {"upr": [], "tax": []}
    retail = {"upr": [], "tax": []}
    for side, rows in (("upr", res["only_upr"]), ("tax", res["only_nal"])):
        for r in rows:
            (legal if _is_legal(r["party"]) else retail)[side].append(r)
    return legal, retail


def _retail_months(org: str, retail: dict, today: date) -> list[dict]:
    """Непарная розница — одной строкой за месяц: сколько оплат точек в
    управленке не нашли пары в налоговой и наоборот, на какую сумму.

    Пачки ККМ кладут в налоговую одной суммой за несколько точек и дней,
    поэтому сравниваем итог месяца, а не оплаты поштучно: сошлись суммы —
    всё проведено, как бы ни разбили. Срок — тот же, что у ККМ за месяц
    (10-й рабочий день следующего месяца); до него это не задача."""
    from ..routers.tax import _MONTHS, kkm_deadline

    months: dict = defaultdict(lambda: {"upr": [], "tax": []})
    for side in ("upr", "tax"):
        for r in retail[side]:
            months[(r["date"].year, r["date"].month)][side].append(r)
    out = []
    for (y, m), g in sorted(months.items()):
        su = sum(r["amount"] for r in g["upr"])
        sn = sum(r["amount"] for r in g["tax"])
        n = len(g["upr"]) + len(g["tax"])
        if abs(su - sn) < AMOUNT_TOLERANCE * n:
            continue
        deadline = kkm_deadline(y, m)
        out.append({
            "key": f"{org}|kkmpay|{y}-{m:02d}",
            "organization": org, "kind": "kkmpay",
            "side": "upr" if su > sn else "tax",
            "doc_date": deadline,
            "doc_number": f"{_MONTHS[m - 1]} {y}",
            "party": (f"без пары: в управленке {len(g['upr'])} опл. на "
                      f"{_fmt(round(su))} сом, в налоговой {len(g['tax'])} "
                      f"на {_fmt(round(sn))} сом"),
            "qty": len(g["upr"]) - len(g["tax"]),
            "amount": round(su - sn, 2),
            "is_task": today > deadline,
        })
    return out


def split_orgs(value: str | None) -> set[str]:
    return {o.strip() for o in (value or "").split(",") if o.strip() in models.ORGS}


def _fmt(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ") if abs(x - round(x)) < 0.001 else f"{x:,.2f}".replace(",", " ")

