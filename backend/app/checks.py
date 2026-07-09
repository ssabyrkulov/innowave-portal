"""Правила контроля данных бухгалтерии.

Каждое правило — чистая функция над загруженными продажами, возвращающая
список нарушений. vhash стабилен между запусками: одно и то же нарушение
получает один и тот же ключ, поэтому пометка «принято» переживает
переимпорты.
"""

import hashlib
import re
import statistics
from collections import defaultdict
from datetime import date

from sqlalchemy.orm import Session

from . import models

# R3: допустимое отклонение цены от медианы. Порог подобран по реальным
# данным: у компании два легальных уровня цен (опт/розница) с разбегом до
# ~50% — сигналим только на то, что выходит и за него.
PRICE_DEVIATION_LIMIT = 0.50
DISCOUNT_LIMIT = 15.0          # R8: максимальная скидка, %
# R1: допуск расхождения сумм — 1 сом на документ (округления 1С при
# применении скидок к строкам).
DOC_TOTAL_TOLERANCE = 1.0
BASE_CURRENCY = "KGS"

RULES = {
    "doc_total_mismatch": {
        "title": "Сумма строк ≠ сумме документа",
        "severity": "critical",
        "hint": "Строки накладной (с учётом скидок) не сходятся с полем "
                "«СуммаДокумента» — часть позиций удалена или внесена с ошибкой.",
    },
    "numbering_gap": {
        "title": "Пропуск в нумерации накладных",
        "severity": "warning",
        "hint": "Между соседними номерами накладных есть дыра — возможно, "
                "документ не внесён или удалён.",
    },
    "price_anomaly": {
        "title": "Аномальная цена",
        "severity": "warning",
        "hint": f"Цена отличается от обычной цены этого товара более чем "
                f"на {int(PRICE_DEVIATION_LIMIT*100)}%.",
    },
    "missing_fields": {
        "title": "Не заполнены ключевые поля",
        "severity": "warning",
        "hint": "У продажи не указан агент или склад — данные внесены небрежно.",
    },
    "nonpositive": {
        "title": "Нулевые или отрицательные значения",
        "severity": "critical",
        "hint": "Количество или сумма ≤ 0 — техническая ошибка ввода.",
    },
    "foreign_currency": {
        "title": "Продажа не в базовой валюте",
        "severity": "warning",
        "hint": f"Валюта документа отличается от {BASE_CURRENCY} — проверьте, "
                "не ошибка ли это.",
    },
    "doc_double_total": {
        "title": "Один номер — разные суммы документа",
        "severity": "critical",
        "hint": "Накладная с одним номером встречается с разными итогами — "
                "признак правок задним числом.",
    },
    "discount_over_limit": {
        "title": "Скидка выше лимита",
        "severity": "warning",
        "hint": f"Скидка превышает {int(DISCOUNT_LIMIT)}% — требуется "
                "подтверждение руководителя.",
    },
}


def _vhash(rule: str, key: str) -> str:
    return hashlib.sha256(f"{rule}|{key}".encode()).hexdigest()


def _v(rule, key, doc, dt, client, detail):
    return {
        "rule": rule,
        "severity": RULES[rule]["severity"],
        "doc_number": doc,
        "date": dt.isoformat() if isinstance(dt, date) else str(dt or ""),
        "client": client,
        "detail": detail,
        "vhash": _vhash(rule, key),
    }


def run_checks(db: Session, date_from: date | None = None,
               date_to: date | None = None) -> list[dict]:
    query = db.query(models.Sale)
    if date_from:
        query = query.filter(models.Sale.date >= date_from)
    if date_to:
        query = query.filter(models.Sale.date <= date_to)
    sales = query.all()
    if not sales:
        return []

    violations: list[dict] = []

    # Группировка по документам (номер + дата — как в summary)
    docs: dict[str, list] = defaultdict(list)
    for s in sales:
        if s.doc_number:
            docs[f"{s.doc_number}|{s.date}"].append(s)

    # R1: сумма строк vs СуммаДокумента. В выгрузке 1С «Сумма» строки — ДО
    # скидки, а «СуммаДокумента» — ПОСЛЕ, поэтому сверяем оба варианта:
    # напрямую и с применением «ПроцентСкидкиНаценки» к каждой строке.
    for key, lines in docs.items():
        doc_totals = {float(l.doc_total) for l in lines if l.doc_total is not None}
        if len(doc_totals) != 1:
            continue  # разные итоги ловит R7
        doc_total = doc_totals.pop()
        lines_sum = sum(float(l.amount) for l in lines)
        discounted_sum = sum(
            float(l.amount) * (1 - float(l.discount_pct or 0) / 100)
            for l in lines
        )
        diff = min(abs(lines_sum - doc_total), abs(discounted_sum - doc_total))
        if diff > DOC_TOTAL_TOLERANCE:
            s0 = lines[0]
            violations.append(_v(
                "doc_total_mismatch", key, s0.doc_number, s0.date, s0.client,
                f"строки: {lines_sum:,.2f}, со скидками: {discounted_sum:,.2f}, "
                f"документ: {doc_total:,.2f}",
            ))

    # R7: один номер накладной — разные СуммаДокумента.
    # Группируем по (номер, год): нумерация 1С обнуляется ежегодно,
    # одинаковые номера в разных годах — разные документы, не нарушение.
    by_number: dict[tuple, set] = defaultdict(set)
    number_meta: dict[tuple, models.Sale] = {}
    for s in sales:
        if s.doc_number and s.doc_total is not None:
            k = (s.doc_number, s.date.year)
            by_number[k].add(float(s.doc_total))
            number_meta.setdefault(k, s)
    for (num, year), totals in by_number.items():
        if len(totals) > 1:
            s0 = number_meta[(num, year)]
            violations.append(_v(
                "doc_double_total", f"{num}|{year}", num, s0.date, s0.client,
                "итоги: " + ", ".join(f"{t:,.2f}" for t in sorted(totals)),
            ))

    # R2: пропуски в нумерации (по числовой части номера)
    numeric_docs = []
    for num in {s.doc_number for s in sales if s.doc_number}:
        m = re.match(r"^(\D*)(\d+)$", num)
        if m:
            numeric_docs.append((m.group(1), int(m.group(2)), num))
    by_prefix: dict[str, list] = defaultdict(list)
    for prefix, n, raw in numeric_docs:
        by_prefix[prefix].append((n, raw))
    for prefix, nums in by_prefix.items():
        if len(nums) < 10:
            continue  # мало данных — пропуски не показательны
        seq = sorted(set(n for n, _ in nums))
        for a, b in zip(seq, seq[1:]):
            gap = b - a - 1
            if 0 < gap <= 20:  # огромные дыры = другой диапазон, не сигналим
                violations.append(_v(
                    "numbering_gap", f"{prefix}{a}-{b}", f"{prefix}…", None, None,
                    f"между №{a} и №{b} отсутствует документов: {gap}",
                ))

    # R3: аномальные цены относительно медианы по товару в рамках года
    # (цены законно меняются со временем — сравнение со «всей историей»
    # даёт ложные срабатывания).
    prices: dict[tuple, list[float]] = defaultdict(list)
    for s in sales:
        if s.qty and float(s.qty) > 0 and s.price:
            prices[(s.product, s.date.year)].append(float(s.price))
    medians = {
        k: statistics.median(v) for k, v in prices.items() if len(v) >= 5
    }
    for s in sales:
        med = medians.get((s.product, s.date.year))
        if not med or not s.price:
            continue
        deviation = abs(float(s.price) - med) / med
        if deviation > PRICE_DEVIATION_LIMIT:
            violations.append(_v(
                "price_anomaly",
                f"{s.row_hash}", s.doc_number, s.date, s.client,
                f"{s.product}: цена {float(s.price):,.2f} при обычной "
                f"~{med:,.2f} ({deviation:+.0%})",
            ))

    # R4: незаполненные agent/warehouse (по документам, чтобы не спамить).
    # Продавец может прийти либо в колонке агента (старая выгрузка), либо
    # в «ОтветственныйФИО» (новая) — жалуемся, только если нет обоих.
    for key, lines in docs.items():
        s0 = lines[0]
        missing = []
        if all(not (l.agent or l.responsible) for l in lines):
            missing.append("агент/ответственный")
        if all(not l.warehouse for l in lines):
            missing.append("склад")
        if missing:
            violations.append(_v(
                "missing_fields", key, s0.doc_number, s0.date, s0.client,
                "не указан: " + ", ".join(missing),
            ))

    # R5: нулевые/отрицательные значения
    for s in sales:
        if float(s.qty) <= 0 or float(s.amount) <= 0:
            violations.append(_v(
                "nonpositive", s.row_hash, s.doc_number, s.date, s.client,
                f"{s.product}: количество {float(s.qty):g}, "
                f"сумма {float(s.amount):,.2f}",
            ))

    # R6: не базовая валюта
    for s in sales:
        if s.currency != BASE_CURRENCY:
            violations.append(_v(
                "foreign_currency", s.row_hash, s.doc_number, s.date, s.client,
                f"{s.product}: валюта {s.currency}, "
                f"сумма {float(s.amount):,.2f}",
            ))

    # R8: скидка выше лимита
    for s in sales:
        if s.discount_pct is not None and float(s.discount_pct) > DISCOUNT_LIMIT:
            violations.append(_v(
                "discount_over_limit", s.row_hash, s.doc_number, s.date, s.client,
                f"{s.product}: скидка {float(s.discount_pct):g}%",
            ))

    severity_rank = {"critical": 0, "warning": 1}
    violations.sort(key=lambda v: (severity_rank[v["severity"]], v["date"]), reverse=False)
    return violations
