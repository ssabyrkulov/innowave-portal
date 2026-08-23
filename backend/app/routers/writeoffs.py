"""Списания товаров — файл ВыгрузкаСпис.

Построчный формат 1С: Дата, Номер, Склад, Номенклатура, Количество, СчетУчета,
СчетЗатрат, Субконто, ЕдиницаИзмерения, Комментарий, ДокументGUID.

Суммы в выгрузке нет — только количество, поэтому списания закрывают товарный
баланс (остатки), но не денежный. Зато есть счёт затрат, субконто и
комментарий: по ним видно, куда ушёл товар — торговому агенту, на маркетинг,
в брак, — а это ровно то, чего не хватало расчёту остатков, где списания
приходилось считать «верхней оценкой».
"""

from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, onec
from ..database import get_db
from ..deps import get_current_user

router = APIRouter(prefix="/writeoffs", tags=["writeoffs"])

HEADERS = {
    "Дата": "date",
    "Номер": "doc_number",
    "Склад": "warehouse",
    "СкладНаименование": "warehouse",
    "Номенклатура": "product",
    "НоменклатураНаименование": "product",
    "Количество": "qty",
    "СчетУчета": "account",
    "СчетЗатрат": "cost_account",
    "Субконто": "subconto",
    "ЕдиницаИзмерения": "unit",
    "НоменклатураЕдиницаИзмеренияНаименование": "unit",
    "Комментарий": "comment",
    "ДокументGUID": "doc_guid",
    # Непроведённые и помеченные на удаление — не операции.
    **onec.header_map(),
}


def import_writeoffs_workbook(db: Session, content: bytes, filename: str,
                              user_id: int, org: str = models.DEFAULT_ORG) -> dict:
    """Импорт списаний. Файл выгружается за всю историю, поэтому загрузка
    заменяет данные организации целиком; сначала разбор, потом замена —
    битый файл не может стереть данные."""
    from ..services import xlsx  # читалка с починкой архива 1С ред. 1.7
    from .tax import _day, _num

    org = models.normalize_org(org)
    wb = xlsx.load_workbook(content)
    ws = wb[wb.sheetnames[0]]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]

    header_idx, col = None, {}
    for i, row in enumerate(rows[:10]):
        names = {str(c).strip(): j for j, c in enumerate(row) if c}
        if "Дата" in names and "Количество" in names and (
                "Номенклатура" in names or "НоменклатураНаименование" in names):
            header_idx = i
            col = {HEADERS[k]: j for k, j in names.items() if k in HEADERS}
            break
    if header_idx is None:
        raise HTTPException(status_code=400,
                            detail="Не найдены колонки списаний товаров")

    def cell(row, key):
        j = col.get(key)
        return row[j] if j is not None and j < len(row) else None

    def text(row, key):
        return str(cell(row, key) or "").strip() or None

    parsed: list[models.WriteOff] = []
    not_posted = 0
    for row in rows[header_idx + 1:]:
        if onec.skip_reason({k: cell(row, k) for k in ("_posted", "_deleted")}):
            not_posted += 1
            continue
        d = _day(cell(row, "date"))
        qty = _num(cell(row, "qty"))
        product = text(row, "product")
        # Списание без количества или без номенклатуры — не строка товара
        # (пустой хвост файла, итоговая строка); молча пропускаем.
        if d is None or qty is None or not product:
            continue
        parsed.append(models.WriteOff(
            organization=org,
            date=d,
            doc_number=text(row, "doc_number"),
            doc_guid=text(row, "doc_guid"),
            warehouse=text(row, "warehouse"),
            product=product,
            qty=qty,
            unit=text(row, "unit"),
            account=text(row, "account"),
            cost_account=text(row, "cost_account"),
            subconto=text(row, "subconto"),
            comment=text(row, "comment"),
        ))
    if not parsed:
        raise HTTPException(status_code=400,
                            detail="В файле списаний не нашлось ни одной строки")

    db.query(models.WriteOff).filter(
        models.WriteOff.organization == org).delete(synchronize_session=False)
    db.bulk_save_objects(parsed)
    db.add(models.ImportLog(
        filename=f"[списания:{org}] {filename}",
        user_id=user_id, added=len(parsed), skipped=0, errors_count=0,
    ))
    db.commit()
    return {"added": len(parsed), "skipped_not_posted": not_posted}


@router.get("/summary")
def writeoffs_summary(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    org: str = "all",
):
    """Сводка списаний: сколько документов, штук, и куда ушёл товар.

    Разрез по субконто и по комментарию — это и есть ответ «куда»: 1С пишет
    туда «Списание для торгового агента …», «Списание на маркетинг …». Сумм в
    выгрузке нет, поэтому всё считается в штуках."""
    rows = models.org_scope(db.query(models.WriteOff), models.WriteOff, org).all()
    if not rows:
        return {"org": (org or "all"), "count": 0, "docs": 0, "qty": 0.0,
                "by_subconto": [], "by_product": [], "first": None, "last": None}

    by_sub: dict[str, dict] = defaultdict(lambda: {"qty": 0.0, "lines": 0, "docs": set()})
    by_prod: dict[str, dict] = defaultdict(lambda: {"qty": 0.0, "lines": 0})
    docs = set()
    for r in rows:
        key = r.doc_guid or f"{r.doc_number}|{r.date}"
        docs.add(key)
        s = by_sub[r.subconto or "(без статьи)"]
        s["qty"] += float(r.qty or 0)
        s["lines"] += 1
        s["docs"].add(key)
        p = by_prod[r.product or "(без названия)"]
        p["qty"] += float(r.qty or 0)
        p["lines"] += 1

    return {
        "org": (org or "all"),
        "count": len(rows),
        "docs": len(docs),
        "qty": round(sum(float(r.qty or 0) for r in rows), 1),
        "first": min(r.date for r in rows).isoformat(),
        "last": max(r.date for r in rows).isoformat(),
        "by_subconto": sorted(
            ({"subconto": k, "qty": round(v["qty"], 1),
              "lines": v["lines"], "docs": len(v["docs"])}
             for k, v in by_sub.items()),
            key=lambda x: -x["qty"]),
        "by_product": sorted(
            ({"product": k, "qty": round(v["qty"], 1), "lines": v["lines"]}
             for k, v in by_prod.items()),
            key=lambda x: -x["qty"])[:100],
    }


LINES_CAP = 3000


@router.get("/lines")
def writeoffs_lines(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    org: str = "all",
):
    """Построчная детализация списаний — как в файле, в хронологическом
    порядке. Группировку по документам фронт делает сам, чтобы одна и та же
    выдача годилась и для реестра, и для раскрывающегося списка."""
    q = models.org_scope(db.query(models.WriteOff), models.WriteOff, org)
    total = q.count()
    rows = q.order_by(models.WriteOff.date.desc(),
                      models.WriteOff.id.desc()).limit(LINES_CAP).all()
    return {
        "org": (org or "all"),
        "total": total,
        "shown": len(rows),
        "capped": total > len(rows),
        "rows": [{
            "date": r.date.isoformat(),
            "doc_number": r.doc_number,
            "doc_guid": r.doc_guid,
            "warehouse": r.warehouse,
            "product": r.product,
            "qty": float(r.qty or 0),
            "unit": r.unit,
            "cost_account": r.cost_account,
            "subconto": r.subconto,
            "comment": r.comment,
        } for r in rows],
    }
