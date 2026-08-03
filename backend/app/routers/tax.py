"""Налоговый контур (черновик): выгрузки из налоговой базы 1С (ред. 1.7).

Пока Эрмек не довёл выгрузку до конца (нет метки НАЛ в именах, нет банка и
остатков), файлы грузятся вручную на отдельной странице и живут в своей
таблице — с управленческими данными не пересекаются вообще. Когда формат
устаканится, эти же импортёры подключатся к автоприёму.
"""

import io
import zipfile
from collections import defaultdict
from datetime import date, datetime

import openpyxl
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import get_current_user, require_roles

router = APIRouter(prefix="/tax", tags=["tax"])

can_edit = require_roles(models.Role.admin, models.Role.accountant)


def _load_wb(content: bytes):
    """openpyxl с починкой архива 1С: ред. 1.7 пишет SharedStrings.xml с
    большой буквы, а openpyxl ищет строчную — без переупаковки файл не
    открывается вовсе."""
    try:
        return openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except KeyError:
        zin = zipfile.ZipFile(io.BytesIO(content))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
            for it in zin.infolist():
                name = it.filename
                if name.lower().endswith("sharedstrings.xml"):
                    name = "xl/sharedStrings.xml"
                zout.writestr(name, zin.read(it.filename))
        return openpyxl.load_workbook(io.BytesIO(buf.getvalue()), data_only=True, read_only=True)


def _rows(content: bytes) -> list[list]:
    wb = _load_wb(content)
    ws = wb[wb.sheetnames[0]]
    return [list(r) for r in ws.iter_rows(values_only=True)]


def _day(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.strptime(value.split()[0], "%d.%m.%Y").date()
        except ValueError:
            return None
    return None


def _num(value) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).replace("\xa0", "").replace(" ", "").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _header_map(rows: list[list]) -> tuple[int, dict]:
    """Строка заголовков и карта «имя колонки → индекс» (первые 5 строк)."""
    for i, row in enumerate(rows[:5]):
        names = {str(c).strip(): j for j, c in enumerate(row) if c}
        if "Дата" in names and ("Сумма" in names or "СуммаДокумента" in names):
            return i, names
    raise HTTPException(status_code=400, detail="Не нашёл строку заголовков (нужны «Дата» и «Сумма»)")


def _detect_kind(names: dict, filename: str) -> str:
    """Тип файла по заголовкам; имя — только чтобы отличить ПКО от РКО,
    когда колонка вида операции называется одинаково."""
    if "НоменклатураНаименование" in names:
        return "sale"
    if "ВидОперации" in names:
        return "cash_in"      # так называется колонка в ПКО
    if "Основание" in names:
        return "cash_out"     # а так — в РКО
    low = (filename or "").lower()
    if any(t in low for t in ("пко", "pko", "приход", "prihod", "вход", "vhod")):
        return "cash_in"
    if any(t in low for t in ("рко", "rko", "расход", "rashod", "исход", "ishod")):
        return "cash_out"
    return "return"  # Дата/Сумма/Валюта/Контрагент — документные возвраты


def _parse(content: bytes, filename: str) -> tuple[str, list[dict]]:
    rows = _rows(content)
    hi, names = _header_map(rows)
    kind = _detect_kind(names, filename)

    def cell(row, name):
        j = names.get(name)
        return row[j] if j is not None and j < len(row) else None

    out: list[dict] = []
    for row in rows[hi + 1:]:
        d = _day(cell(row, "Дата"))
        if d is None:
            continue
        if kind == "sale":
            amount = _num(cell(row, "Сумма"))
            if amount is None:
                continue
            out.append({
                "kind": kind, "date": d,
                "counterparty": str(cell(row, "КонтрагентНаименование") or "").strip() or None,
                "amount": amount,
                "currency": str(cell(row, "ВалютаДокументаНаименование") or "KGS").strip() or "KGS",
                "doc_number": str(cell(row, "Номер") or "").strip() or None,
                "doc_total": _num(cell(row, "СуммаДокумента")),
                "warehouse": str(cell(row, "Склад") or "").strip() or None,
                "product": str(cell(row, "НоменклатураНаименование") or "").strip() or None,
                "qty": _num(cell(row, "Количество")),
            })
        else:
            amount = _num(cell(row, "Сумма"))
            if amount is None:
                continue
            out.append({
                "kind": kind, "date": d,
                "counterparty": str(cell(row, "Контрагент") or "").strip() or None,
                "amount": amount,
                "currency": str(cell(row, "Валюта") or "KGS").strip() or "KGS",
                "doc_number": str(cell(row, "Номер") or "").strip() or None,
                "operation": str(
                    cell(row, "ВидОперации") or cell(row, "Основание") or ""
                ).strip() or None,
            })
    return kind, out


def import_tax_workbook(db: Session, content: bytes, filename: str,
                        user_id: int, org: str = "hygiene") -> dict:
    """Импорт файла налоговой базы. Каждая загрузка заменяет данные своего
    вида целиком: выгрузки идут за всю историю, дедупликация не нужна.
    Используется и ручной кнопкой, и автоприёмом из папки Drive."""
    org = models.normalize_org(org)
    kind, parsed = _parse(content, filename or "")
    if not parsed:
        raise HTTPException(status_code=400, detail="В файле не нашлось ни одной строки с датой и суммой")
    # Снапшот-замена: сначала парсим (выше), только потом удаляем старое —
    # битый файл не может стереть данные.
    db.query(models.TaxOperation).filter(
        models.TaxOperation.organization == org,
        models.TaxOperation.kind == kind,
    ).delete(synchronize_session=False)
    for p in parsed:
        db.add(models.TaxOperation(organization=org, **p))
    db.add(models.ImportLog(
        filename=f"[налоговая:{org}:{kind}] {filename}",
        user_id=user_id, added=len(parsed), skipped=0, errors_count=0,
    ))
    db.commit()
    return {"kind": kind, "added": len(parsed)}


@router.post("/import")
async def tax_import(
    db: Session = Depends(get_db),
    user: models.User = Depends(can_edit),
    file: UploadFile = File(...),
    org: str = Form(default="hygiene"),
):
    content = await file.read()
    return import_tax_workbook(db, content, file.filename or "", user.id, org)


KIND_LABEL = {"sale": "Реализации", "return": "Возвраты",
              "cash_in": "Касса · приход", "cash_out": "Касса · расход"}


@router.get("/summary")
def tax_summary(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    org: str = "all",
):
    """Сводка налогового контура: выручка по годам, касса по видам операций,
    подотчёт по людям. Всё считается из своей таблицы — управленка не трогается."""
    q = db.query(models.TaxOperation)
    o = models.normalize_org(org) if (org or "").lower() in models.ORGS else None
    if o:
        q = q.filter(models.TaxOperation.organization == o)
    ops = q.all()

    kinds: dict[str, dict] = {}
    sales_by_year: dict[int, float] = defaultdict(float)
    cash_by_op: dict[tuple, float] = defaultdict(float)
    podotchet: dict[str, dict] = {}
    clients: dict[str, float] = defaultdict(float)
    last_date: dict[str, str] = {}

    for op in ops:
        k = kinds.setdefault(op.kind, {"count": 0, "amount": 0.0})
        amt = float(op.amount)
        k["count"] += 1
        k["amount"] += amt
        if op.date and (op.kind not in last_date or op.date.isoformat() > last_date[op.kind]):
            last_date[op.kind] = op.date.isoformat()
        if op.kind == "sale":
            sales_by_year[op.date.year] += amt
            if op.counterparty:
                clients[op.counterparty] += amt
        elif op.kind in ("cash_in", "cash_out"):
            cash_by_op[(op.kind, op.operation or "(без вида)")] += amt
            oper = (op.operation or "").lower()
            if "подотчет" in oper or "подотчёт" in oper:
                p = podotchet.setdefault(op.counterparty or "(не указан)",
                                         {"issued": 0.0, "returned": 0.0})
                if op.kind == "cash_out":
                    p["issued"] += amt
                else:
                    p["returned"] += amt

    return {
        "org": o or "all",
        "kinds": [
            {"kind": k, "label": KIND_LABEL.get(k, k),
             "count": v["count"], "amount": round(v["amount"], 2),
             "last_date": last_date.get(k)}
            for k, v in sorted(kinds.items())
        ],
        "sales_by_year": [
            {"year": y, "amount": round(a, 2)}
            for y, a in sorted(sales_by_year.items())
        ],
        "cash_by_operation": sorted(
            ({"direction": k[0], "operation": k[1], "amount": round(a, 2)}
             for k, a in cash_by_op.items()),
            key=lambda x: -x["amount"],
        ),
        "podotchet": sorted(
            ({"person": name, "issued": round(v["issued"], 2),
              "returned": round(v["returned"], 2),
              "hanging": round(v["issued"] - v["returned"], 2)}
             for name, v in podotchet.items()),
            key=lambda x: -x["hanging"],
        ),
        "top_clients": sorted(
            ({"client": c, "amount": round(a, 2)} for c, a in clients.items()),
            key=lambda x: -x["amount"],
        )[:15],
    }
