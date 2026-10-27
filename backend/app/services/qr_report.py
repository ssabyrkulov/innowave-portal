"""QR-отчёт банка — третий источник оплат.

Отчёт выгружают из кабинета банка (роль SHOP_OWNER) за период: шапка
«Компания / Время создания от / до / Статус транзакции», дальше по строке
на транзакцию. Портал сверяет с ним управленку: QR-оплата, которой на
следующий день нет в управленке, — задача управленке. Без отчёта такую
оплату не видно, если её не внесли ни в одну базу: сверка баз сравнивает
базы между собой.

Файл можно загрузить на странице «Контуры 1С» или положить в папку Drive
с выгрузками 1С: автосинк узнает его по «SHOP_OWNER» в имени.
"""
import hashlib
import io
from datetime import date, datetime

import openpyxl
from sqlalchemy.orm import Session

from .. import models

HEADER = "ID Транзакции в системе"
# Отчёт выгружают с фильтром «Оплата прошла», но фильтр может быть и
# другим — в сверку идут только прошедшие оплаты.
PAID = "оплата прошла"


def is_qr_name(filename: str) -> bool:
    """Имя файла отчёта: «…-SHOP_OWNER-27.10.2026_17_01-73073.xlsx»."""
    name = (filename or "").lower()
    return "shop_owner" in name or name.startswith("qr")


def org_of(company: str | None) -> str | None:
    """Фирма по полю «Компания». «Хайджин» — первым: полное название
    «Инновейв Хайджин» содержит и «Инновейв»."""
    name = (company or "").lower().replace("ё", "е")
    if "хайджин" in name or "hygiene" in name:
        return "hygiene"
    if "инновейв" in name or "innowave" in name:
        return "innowave"
    if "карбон" in name or "carbon" in name:
        return "bluecarbon"
    return None


def _when(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    for fmt in ("%d-%m-%Y %H:%M:%S", "%d.%m.%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%d-%m-%Y %H:%M", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _day(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse(content: bytes) -> dict:
    """Шапка и строки отчёта. ValueError — файл не похож на QR-отчёт."""
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        meta: dict[str, object] = {}
        col: dict[str, int] | None = None
        rows = []
        for raw in wb.worksheets[0].iter_rows(values_only=True):
            cells = [str(c).strip() if c is not None else "" for c in raw]
            if col is None:
                if HEADER in cells:
                    col = {name: i for i, name in enumerate(cells) if name}
                elif cells and cells[0]:
                    meta[cells[0]] = raw[1] if len(raw) > 1 else None
                continue
            get = lambda name: raw[col[name]] if name in col and col[name] < len(raw) else None
            txn = str(get(HEADER) or "").strip()
            at = _when(get("Дата и время транзакции"))
            if not txn or at is None:
                continue
            try:
                amount = float(get("Сумма") or 0)
            except (TypeError, ValueError):
                continue
            rows.append({
                "txn_id": txn, "paid_at": at, "amount": amount,
                "status": str(get("Статус") or "").strip() or None,
                "method": str(get("Способ оплаты") or "").strip() or None,
                "point": str(get("Торговая точка") or "").strip() or None,
                "comment": str(get("Комментарий") or "").strip() or None,
            })
    finally:
        wb.close()
    if col is None:
        raise ValueError(f"Не найдена строка заголовков («{HEADER}»): это не QR-отчёт банка")
    return {
        "org": org_of(str(meta.get("Компания") or "")),
        "date_from": _day(meta.get("Время создания от")),
        "date_to": _day(meta.get("Время создания до")),
        "rows": rows,
    }


def import_report(db: Session, content: bytes, filename: str, user_id: int,
                  org: str | None = None) -> dict:
    """Загружает отчёт: новые транзакции добавляет, известные обновляет
    (статус мог смениться — возврат оплаты). Отчёты за пересекающиеся
    периоды не задваиваются: строка одна на ID транзакции банка."""
    rep = parse(content)
    org = rep["org"] or org
    if org not in models.ORGS:
        raise ValueError("Не понять фирму: в шапке отчёта нет «Компания»")
    rows = rep["rows"]
    known = {p.txn_id: p for p in db.query(models.QrPayment).filter(
        models.QrPayment.txn_id.in_([r["txn_id"] for r in rows])).all()} if rows else {}
    added = updated = 0
    for r in rows:
        p = known.get(r["txn_id"])
        if p is None:
            p = models.QrPayment(txn_id=r["txn_id"], organization=org)
            db.add(p)
            known[r["txn_id"]] = p
            added += 1
        elif (p.status, float(p.amount or 0)) != (r["status"], r["amount"]):
            updated += 1
        p.organization = org
        p.paid_at, p.date = r["paid_at"], r["paid_at"].date()
        p.amount = r["amount"]
        p.status, p.method = r["status"], r["method"]
        p.point, p.comment = r["point"], r["comment"]
    period = " – ".join(d.strftime("%d.%m.%Y") for d in (rep["date_from"], rep["date_to"]) if d)
    paid = [r for r in rows if (r["status"] or "").lower() == PAID]
    db.add(models.ImportLog(
        filename=f"[qr:{org}] {filename}" + (f" · {period}" if period else ""),
        user_id=user_id, added=added, skipped=len(rows) - added,
        errors_count=0, file_hash=hashlib.sha256(content).hexdigest()))
    db.commit()
    return {
        "organization": org,
        "date_from": rep["date_from"].isoformat() if rep["date_from"] else None,
        "date_to": rep["date_to"].isoformat() if rep["date_to"] else None,
        "total": len(rows), "paid": len(paid),
        "paid_amount": round(sum(r["amount"] for r in paid), 2),
        "added": added, "updated": updated,
    }
