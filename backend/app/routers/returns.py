"""Возвраты товаров от покупателей (документный уровень, файл ВыгрузкаВозв:
Дата / Сумма / Валюта / Контрагент). Уменьшают долг клиента в дебиторке."""

import hashlib
from collections import defaultdict
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import models
from ..services import xlsx
from ..database import get_db
from ..deps import get_current_user

router = APIRouter(prefix="/returns", tags=["returns"])

HEADERS = {"Дата": "date", "Сумма": "amount", "Валюта": "currency",
           "Контрагент": "client",
           # Необязательная колонка обновлённых выгрузок 1С: GUID документа.
           "ДокументGUID": "doc_guid"}


def import_return_lines_workbook(
    db: Session, content: bytes, filename: str, user_id: int,
    org: str = models.DEFAULT_ORG,
) -> dict:
    """Возвраты из построчного файла ВыгрузкаТовВозв (формат продаж).

    Делает две вещи:
    1. Самолечение: удаляет из продаж строки, если они когда-то по ошибке
       туда попали (совпадение по row_hash).
    2. Записывает суммы возвратов по клиентам (заменой целиком). Сумма
       берётся по «СуммаДокумента» возврата (итог документа, как у продаж) —
       так значения совпадают с выгрузкой Возв и Excel. Если у документа нет
       итога — суммируем строки со скидкой.
    """
    from .sales import _row_hash, parse_sales_workbook

    org = models.normalize_org(org)
    parsed, errors, not_posted = parse_sales_workbook(content)

    # 1. Очистка продаж от возвратных строк (в рамках своей организации)
    hashes = [_row_hash(p, org) for p in parsed]
    removed = 0
    if hashes:
        removed = (
            db.query(models.Sale)
            .filter(models.Sale.organization == org,
                    models.Sale.row_hash.in_(hashes))
            .delete(synchronize_session=False)
        )

    # 2. Возвраты по документам: один ReturnDoc на документ по СуммаДокумента.
    db.query(models.ReturnDoc).filter(models.ReturnDoc.organization == org).delete()
    # Товарные строки возвратов — для расчётного остатка (возврат возвращает
    # товар на склад). Заменяются целиком вместе с документами.
    db.query(models.ReturnLine).filter(models.ReturnLine.organization == org).delete()
    for p in parsed:
        db.add(models.ReturnLine(
            organization=org, date=p["date"], client=p.get("client"),
            product=p.get("product"), qty=p.get("qty"), amount=p.get("amount"),
            doc_guid=p.get("doc_guid"),
        ))
    db.flush()
    seen_docs: set = set()
    fallback: dict[tuple, dict] = {}  # документы без номера/итога — по строкам
    docs: list[dict] = []
    for p in parsed:
        client = p["client"]
        if p.get("doc_number") and p.get("doc_total") is not None:
            key = (p["doc_number"], p["date"], client)
            if key in seen_docs:
                continue
            seen_docs.add(key)
            docs.append({
                "date": p["date"], "client": client,
                "amount": float(p["doc_total"]),
                "currency": p.get("currency") or "KGS",
                "doc_guid": p.get("doc_guid"),
            })
        else:
            # нет итога документа — копим сумму строк (со скидкой) по клиенту/дате
            fkey = (p["date"], client)
            f = fallback.setdefault(fkey, {
                "date": p["date"], "client": client, "amount": 0.0,
                "currency": p.get("currency") or "KGS",
                "doc_guid": p.get("doc_guid"),
            })
            f["amount"] += float(p["amount"]) * (1 - float(p.get("discount_pct") or 0) / 100)
    docs.extend(fallback.values())

    added = 0
    for i, d in enumerate(docs):
        h = hashlib.sha256(
            f"{org}|{d['date']}|{d['client']}|{d['amount']}|{i}".encode()
        ).hexdigest()
        db.add(models.ReturnDoc(
            date=d["date"], amount=round(d["amount"], 2),
            currency=d["currency"], client=d["client"],
            doc_guid=d.get("doc_guid"),
            organization=org, row_hash=h,
        ))
        added += 1

    db.add(models.ImportLog(
        filename=f"[возвраты, очистка продаж −{removed}] {filename}",
        user_id=user_id, added=added, skipped=0, errors_count=len(errors),
    ))
    db.commit()
    return {
        "added_returns": added,
        "removed_from_sales": removed,
        "total_returns_in_db": db.query(models.ReturnDoc).count(),
    }


def _parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value.split()[0], "%d.%m.%Y").date()
        except ValueError:
            return None
    return None


def import_returns_workbook(
    db: Session,
    content: bytes,
    filename: str,
    user_id: int,
    org: str = models.DEFAULT_ORG,
) -> dict:
    org = models.normalize_org(org)
    try:
        wb = xlsx.load_workbook(content)
    except Exception:
        raise HTTPException(status_code=400, detail="Не удалось открыть файл Excel")

    ws = wb[wb.sheetnames[0]]
    rows_iter = ws.iter_rows(values_only=True)

    header_idx, columns, buffered = None, {}, []
    for i, row in enumerate(rows_iter):
        buffered.append(row)
        if i >= 20:
            break
        matched = {
            j: HEADERS[str(c).strip()]
            for j, c in enumerate(row)
            if c is not None and str(c).strip() in HEADERS
        }
        if {"date", "amount", "client"} <= set(matched.values()):
            header_idx, columns = i, matched
            break
    if header_idx is None:
        raise HTTPException(
            status_code=400,
            detail="Не найдены колонки возвратов (Дата, Сумма, Валюта, Контрагент)",
        )

    parsed, errors = [], []

    def process(row, line_no):
        data = {f: (row[j] if j < len(row) else None) for j, f in columns.items()}
        if all(v is None for v in data.values()):
            return
        dt = _parse_date(data.get("date"))
        try:
            amount = float(str(data.get("amount")).replace(" ", "").replace(",", "."))
        except (TypeError, ValueError):
            amount = None
        client = str(data.get("client") or "").strip()
        if not dt or amount is None or not client:
            if len(errors) < 20:
                errors.append(f"Строка {line_no}: нет даты, суммы или контрагента")
            return
        parsed.append({
            "src_dt": str(data.get("date")),
            "date": dt,
            "amount": amount,
            "currency": (str(data.get("currency") or "KGS").strip() or "KGS")[:3],
            "client": client,
            "doc_guid": str(data.get("doc_guid") or "").strip() or None,
        })

    line_no = header_idx + 1
    for line_no, row in enumerate(buffered[header_idx + 1:], start=header_idx + 2):
        process(row, line_no)
    for line_no, row in enumerate(rows_iter, start=line_no + 1):
        process(row, line_no)

    # Возвраты — полная выгрузка за всю историю, поэтому грузим «заменой
    # целиком» в рамках своей организации: это самоисцеляет таблицу, если в
    # неё раньше по ошибке попал чужой файл (напр. исходящие платежи).
    db.query(models.ReturnDoc).filter(models.ReturnDoc.organization == org).delete()
    db.flush()
    existing: set[str] = set()
    seen: set[str] = set()
    occurrences: dict[str, int] = defaultdict(int)
    added = skipped = 0
    for p in parsed:
        base = "|".join(str(p[f]) for f in ("src_dt", "amount", "currency", "client"))
        occurrences[base] += 1
        h = hashlib.sha256(f"{org}|{base}#{occurrences[base]}".encode()).hexdigest()
        p = {k: v for k, v in p.items() if k != "src_dt"}
        if h in existing or h in seen:
            skipped += 1
            continue
        seen.add(h)
        db.add(models.ReturnDoc(**p, organization=org, row_hash=h))
        added += 1

    db.add(models.ImportLog(
        filename=f"[возвраты] {filename}",
        user_id=user_id,
        added=added,
        skipped=skipped,
        errors_count=len(errors),
    ))
    db.commit()
    return {
        "added": added,
        "skipped_duplicates": skipped,
        "errors": errors,
        "total_in_db": db.query(models.ReturnDoc).count(),
    }


def returns_without_lines(db: Session, org: str) -> dict:
    """Возвраты, у которых нет товарных строк — их товар не вернулся в расчёт.

    Документный формат выгрузки (ВыгрузкаВозв: дата, сумма, контрагент) даёт
    только ReturnDoc. Для дебиторки этого хватает, для остатков — нет:
    расчёт «поступило − продано + возвраты − списано» берёт возвраты из
    ReturnLine, и документ без строк в него не попадает. Товар при этом в 1С
    на склад вернулся, поэтому расчётный остаток оказывается занижен, а Δ
    против 1С — отрицательной. Молчать об этом нельзя: карточка остатков
    показывает, сколько возвратов прошло мимо товара."""
    # Тянем только нужные колонки: таблицы возвратов растут всю жизнь портала,
    # а проверке хватает даты, контрагента и GUID.
    lines_by_guid: set[str] = set()
    lines_by_doc: set[tuple] = set()
    for r in models.org_scope(
        db.query(models.ReturnLine.date, models.ReturnLine.client,
                 models.ReturnLine.doc_guid), models.ReturnLine, org
    ).all():
        if r.doc_guid:
            lines_by_guid.add(r.doc_guid.lower())
        lines_by_doc.add((r.date, (r.client or "").strip()))

    docs = models.org_scope(
        db.query(models.ReturnDoc.date, models.ReturnDoc.client,
                 models.ReturnDoc.amount, models.ReturnDoc.doc_guid),
        models.ReturnDoc, org
    ).all()
    missing = []
    for d in docs:
        if d.doc_guid and d.doc_guid.lower() in lines_by_guid:
            continue
        # Документы без GUID сопоставляем по дате и контрагенту: шапки и
        # строки приходят из одного файла, так что пара совпадает точно.
        if (d.date, (d.client or "").strip()) in lines_by_doc:
            continue
        missing.append(d)
    missing.sort(key=lambda d: d.date, reverse=True)
    return {
        "docs": len(docs),
        "missing": len(missing),
        "missing_amount": round(sum(float(d.amount or 0) for d in missing), 2),
        "sample": [{"date": d.date.isoformat(), "client": d.client,
                    "amount": round(float(d.amount or 0), 2)}
                   for d in missing[:5]],
    }


@router.get("")
def list_returns(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    limit: int = Query(default=300, le=1000),
    org: str = Query(default="all"),
):
    rows = (
        models.org_scope(db.query(models.ReturnDoc), models.ReturnDoc, org)
        .order_by(models.ReturnDoc.date.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "date": r.date.isoformat(),
            "amount": float(r.amount),
            "currency": r.currency,
            "client": r.client,
        }
        for r in rows
    ]
