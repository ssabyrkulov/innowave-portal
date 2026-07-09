import hashlib
import io
from datetime import date, datetime

import openpyxl
from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import get_current_user, require_roles

router = APIRouter(prefix="/sales", tags=["sales"])

can_import = require_roles(models.Role.admin, models.Role.accountant)
admin_only = require_roles(models.Role.admin)

# Заголовки колонок 1С → поля модели. Ищем по именам, а не по позициям,
# чтобы перестановка колонок в выгрузке не ломала импорт.
HEADER_MAP = {
    "Дата": "date",
    "КонтрагентНаименование": "client",
    "Склад": "warehouse",
    "НоменклатураНаименование": "product",
    "Количество": "qty",
    "Цена": "price",
    "Сумма": "amount",
    "ВалютаДокументаНаименование": "currency",
    "Номер": "doc_number",
    "СуммаДокумента": "doc_total",
    "НоменклатураЕдиницаИзмерения": "unit",
    "КонтрагентSD_АгентНаименование": "agent",
    "ПроцентСкидкиНаценки": "discount_pct",
    "СчетУчета": "account",
    "ОтветственныйНаименование": "responsible",
}

REQUIRED_FIELDS = {"date", "client", "product", "qty", "price", "amount"}


def _parse_date(value) -> date | None:
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


def _parse_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _row_hash(d: dict) -> str:
    key = "|".join(
        str(d.get(f, ""))
        for f in (
            "date", "client", "product", "qty", "price",
            "amount", "doc_number", "warehouse", "doc_total",
        )
    )
    return hashlib.sha256(key.encode()).hexdigest()


@router.post("/import")
async def import_sales(
    file: UploadFile,
    replace_period: bool = Form(default=False),
    db: Session = Depends(get_db),
    current: models.User = Depends(can_import),
):
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ожидается файл Excel (.xlsx)",
        )
    content = await file.read()
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Не удалось открыть файл — убедитесь, что это Excel (.xlsx)",
        )

    ws = wb[wb.sheetnames[0]]
    rows = ws.iter_rows(values_only=True)

    # Ищем строку заголовков в первых 20 строках
    header_idx = None
    columns: dict[int, str] = {}
    buffered = []
    for i, row in enumerate(rows):
        buffered.append(row)
        if i >= 20:
            break
        matched = {
            j: HEADER_MAP[str(cell).strip()]
            for j, cell in enumerate(row)
            if cell is not None and str(cell).strip() in HEADER_MAP
        }
        if "date" in matched.values() and "amount" in matched.values():
            header_idx = i
            columns = matched
            break

    if header_idx is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Не найдена строка заголовков (ожидаются колонки 1С: "
            "Дата, КонтрагентНаименование, Сумма и т.д.)",
        )

    # --- Фаза 1: разбор всех строк файла ---
    parsed_rows: list[dict] = []
    errors: list[str] = []

    def process(row, line_no):
        data = {}
        for j, field in columns.items():
            data[field] = row[j] if j < len(row) else None
        if all(v is None for v in data.values()):
            return
        parsed = {
            "date": _parse_date(data.get("date")),
            "client": str(data.get("client") or "").strip() or None,
            "warehouse": str(data.get("warehouse") or "").strip() or None,
            "product": str(data.get("product") or "").strip() or None,
            "qty": _parse_float(data.get("qty")),
            "price": _parse_float(data.get("price")),
            "amount": _parse_float(data.get("amount")),
            "currency": str(data.get("currency") or "KGS").strip()[:3] or "KGS",
            "doc_number": str(data.get("doc_number") or "").strip() or None,
            "doc_total": _parse_float(data.get("doc_total")),
            "unit": str(data.get("unit") or "").strip() or None,
            "agent": str(data.get("agent") or "").strip() or None,
            "discount_pct": _parse_float(data.get("discount_pct")),
            "account": str(data.get("account") or "").strip() or None,
            "responsible": str(data.get("responsible") or "").strip() or None,
        }
        missing = [f for f in REQUIRED_FIELDS if parsed.get(f) is None]
        if missing:
            if len(errors) < 20:
                errors.append(f"Строка {line_no}: нет значений {', '.join(missing)}")
            return
        parsed_rows.append(parsed)

    line_no = header_idx + 1
    for line_no, row in enumerate(buffered[header_idx + 1:], start=header_idx + 2):
        process(row, line_no)
    for line_no, row in enumerate(rows, start=line_no + 1):
        process(row, line_no)

    replaced = 0
    if replace_period and parsed_rows:
        # Заменяем период, который покрывает файл: правки задним числом в 1С
        # корректно попадут в базу, а не останутся дублями.
        dmin = min(p["date"] for p in parsed_rows)
        dmax = max(p["date"] for p in parsed_rows)
        replaced = (
            db.query(models.Sale)
            .filter(models.Sale.date >= dmin, models.Sale.date <= dmax)
            .delete(synchronize_session=False)
        )
        db.flush()

    # --- Фаза 2: вставка с защитой от дублей ---
    existing_hashes = {h for (h,) in db.query(models.Sale.row_hash).all()}
    seen_in_file: set[str] = set()
    added = 0
    skipped = 0
    for parsed in parsed_rows:
        h = _row_hash(parsed)
        if h in existing_hashes or h in seen_in_file:
            skipped += 1
            continue
        seen_in_file.add(h)
        db.add(models.Sale(**parsed, row_hash=h))
        added += 1

    db.add(models.ImportLog(
        filename=file.filename or "upload.xlsx",
        user_id=current.id,
        added=added,
        skipped=skipped,
        errors_count=len(errors),
        replace_period=replace_period,
    ))
    db.commit()
    total = db.query(models.Sale).count()
    return {
        "added": added,
        "skipped_duplicates": skipped,
        "replaced_rows": replaced,
        "errors": errors,
        "total_in_db": total,
    }


@router.get("/imports")
def list_imports(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    limit: int = Query(default=20, le=100),
):
    logs = (
        db.query(models.ImportLog)
        .order_by(models.ImportLog.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": l.id,
            "filename": l.filename,
            "user": l.user.full_name if l.user else None,
            "added": l.added,
            "skipped": l.skipped,
            "errors_count": l.errors_count,
            "replace_period": l.replace_period,
            "created_at": l.created_at.isoformat(),
        }
        for l in logs
    ]


@router.get("/summary")
def sales_summary(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    top: int = Query(default=10, le=50),
):
    query = db.query(models.Sale)
    if date_from:
        query = query.filter(models.Sale.date >= date_from)
    if date_to:
        query = query.filter(models.Sale.date <= date_to)
    sales = query.all()

    revenue = 0.0
    docs: set[str] = set()
    clients: dict[str, dict] = {}
    products: dict[str, dict] = {}
    agents: dict[str, dict] = {}
    monthly: dict[str, dict] = {}
    min_date = max_date = None

    for s in sales:
        amt = float(s.amount)
        revenue += amt
        if s.doc_number:
            docs.add(f"{s.doc_number}|{s.date}")
        c = clients.setdefault(s.client, {"name": s.client, "revenue": 0.0, "docs": set()})
        c["revenue"] += amt
        if s.doc_number:
            c["docs"].add(s.doc_number)
        p = products.setdefault(s.product, {"name": s.product, "revenue": 0.0, "qty": 0.0})
        p["revenue"] += amt
        p["qty"] += float(s.qty)
        if s.agent:
            a = agents.setdefault(s.agent, {"name": s.agent, "revenue": 0.0, "docs": set()})
            a["revenue"] += amt
            if s.doc_number:
                a["docs"].add(s.doc_number)
        mkey = s.date.strftime("%Y-%m")
        m = monthly.setdefault(mkey, {"month": mkey, "revenue": 0.0, "docs": set()})
        m["revenue"] += amt
        if s.doc_number:
            m["docs"].add(s.doc_number)
        if min_date is None or s.date < min_date:
            min_date = s.date
        if max_date is None or s.date > max_date:
            max_date = s.date

    def finalize(items, sort_key="revenue"):
        out = []
        for it in items:
            it = dict(it)
            if isinstance(it.get("docs"), set):
                it["docs"] = len(it["docs"])
            out.append(it)
        return sorted(out, key=lambda x: -x[sort_key])

    return {
        "lines": len(sales),
        "revenue": revenue,
        "docs": len(docs),
        "clients": len(clients),
        "avg_doc": revenue / len(docs) if docs else 0,
        "date_from": min_date.isoformat() if min_date else None,
        "date_to": max_date.isoformat() if max_date else None,
        "monthly": sorted(
            ({"month": m["month"], "revenue": m["revenue"], "docs": len(m["docs"])}
             for m in monthly.values()),
            key=lambda x: x["month"],
        ),
        "top_clients": finalize(clients.values())[:top],
        "top_products": finalize(products.values())[:top],
        "top_agents": finalize(agents.values())[:top],
    }


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
def clear_sales(
    db: Session = Depends(get_db),
    _: models.User = Depends(admin_only),
):
    """Полная очистка данных продаж (только админ) — для переимпорта."""
    db.query(models.Sale).delete()
    db.commit()
