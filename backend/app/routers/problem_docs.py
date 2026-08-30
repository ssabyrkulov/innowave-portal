"""Проблемные документы 1С: непроведённые и помеченные на удаление.

1С сама составляет этот список — файл «…_Проблемные документы» с колонками
Дата, Номер, ВидДокумента, Статус, Организация, Контрагент, СуммаДокумента,
Автор, Комментарий, ДокументGUID. Портал его до сих пор выбрасывал, хотя это
готовый перечень того, что в учёте не доделано: незакрытый авансовый отчёт,
реализация, которую решили удалить, платёжка, которую не провели.

Сам список — половина пользы. Вторая половина — сверка. Документ, помеченный
на удаление, не должен участвовать в наших расчётах, и импортёры такие строки
отбрасывают. Но отбрасывают они только там, где 1С отдаёт признаки
«Проведён» и «ПометкаУдаления»; в остальных выгрузках документ приходит как
обычный. Поэтому портал ищет ДокументGUID каждого проблемного документа в
своих таблицах и показывает найденное отдельно: вот эти строки считаются, а
не должны.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import get_current_user

router = APIRouter(prefix="/problem-docs", tags=["problem-docs"])

HEADERS = {
    "Дата": "date",
    "Номер": "doc_number",
    "ВидДокумента": "kind",
    "Статус": "status",
    "Контрагент": "counterparty",
    "СуммаДокумента": "amount",
    "Автор": "author",
    "Комментарий": "comment",
    "ДокументGUID": "doc_guid",
}

# Где искать просочившийся документ. Порядок — от товарных таблиц к
# денежным: если помеченная на удаление реализация попала в продажи, это
# бьёт и по остатку, и по выручке, и увидеть её надо первой.
LEAK_SOURCES: tuple[tuple[str, type], ...] = (
    ("продажи", models.Sale),
    ("закупки", models.Purchase),
    ("списания", models.WriteOff),
    ("оприходования", models.StockReceipt),
    ("перемещения", models.StockTransfer),
    ("возвраты", models.ReturnLine),
    ("возвраты (документы)", models.ReturnDoc),
    ("оплаты", models.Receipt),
    ("расходы", models.Expense),
    ("ГТД", models.ImportCost),
    ("доп. расходы", models.ExtraCost),
)


def import_problem_docs_workbook(db: Session, content: bytes, filename: str,
                                 user_id: int,
                                 org: str = models.DEFAULT_ORG) -> dict:
    """Импорт списка проблемных документов заменой данных организации.

    Это снимок состояния 1С на момент выгрузки, а не журнал: документ
    провели — и он из списка исчез. Поэтому загрузка заменяет прежний список
    целиком, иначе исправленное осталось бы висеть вечно."""
    from ..services import xlsx  # читалка с починкой архива 1С ред. 1.7
    from .tax import _day, _num

    org = models.normalize_org(org)
    wb = xlsx.load_workbook(content)
    ws = wb[wb.sheetnames[0]]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]

    header_idx, col = None, {}
    for i, row in enumerate(rows[:10]):
        names = {str(c).strip(): j for j, c in enumerate(row) if c}
        if "Дата" in names and "ВидДокумента" in names and "Статус" in names:
            header_idx = i
            col = {HEADERS[k]: j for k, j in names.items() if k in HEADERS}
            break
    if header_idx is None:
        found = ", ".join(str(c).strip() for c in (rows[0] if rows else [])
                          if c is not None)[:300]
        raise HTTPException(
            status_code=400,
            detail="Не найдены колонки проблемных документов (ожидаются Дата, "
                   f"ВидДокумента, Статус). В файле: {found or 'пусто'}")

    def cell(row, key):
        j = col.get(key)
        return row[j] if j is not None and j < len(row) else None

    def text(row, key):
        return str(cell(row, key) or "").strip() or None

    parsed: list[models.ProblemDoc] = []
    for row in rows[header_idx + 1:]:
        d = _day(cell(row, "date"))
        kind = text(row, "kind")
        if d is None or not kind:
            continue  # пустой хвост файла или итоговая строка
        parsed.append(models.ProblemDoc(
            organization=org,
            date=d,
            doc_number=text(row, "doc_number"),
            doc_guid=(text(row, "doc_guid") or "").lower() or None,
            kind=kind,
            status=text(row, "status"),
            counterparty=text(row, "counterparty"),
            amount=_num(cell(row, "amount")),
            author=text(row, "author"),
            comment=text(row, "comment"),
        ))

    # Список грузится заменой целиком, а «в списке с» надо сохранить: иначе
    # каждая выгрузка обнуляла бы возраст проблемы и все документы выглядели
    # бы появившимися сегодня. Переносим по ДокументGUID, а если его нет —
    # по дате, номеру, виду и контрагенту.
    def key_of(p) -> tuple:
        if p.doc_guid:
            return ("g", p.doc_guid)
        return ("k", p.date, p.doc_number, p.kind, p.counterparty)

    known = {key_of(old): old.first_seen
             for old in db.query(models.ProblemDoc)
             .filter(models.ProblemDoc.organization == org).all()}
    now = datetime.utcnow()
    for p in parsed:
        p.first_seen = known.get(key_of(p)) or now

    # Пустой список — хорошая новость, а не сбой: в 1С не осталось
    # проблемных документов. Стираем прежний, иначе портал будет показывать
    # уже исправленное.
    db.query(models.ProblemDoc).filter(
        models.ProblemDoc.organization == org).delete(synchronize_session=False)
    if parsed:
        db.bulk_save_objects(parsed)
    db.add(models.ImportLog(filename=f"[проблемные:{org}] {filename}",
                            user_id=user_id, added=len(parsed),
                            skipped=0, errors_count=0))
    db.commit()
    return {"added": len(parsed),
            "kinds": len({p.kind for p in parsed if p.kind})}


@router.get("")
def problem_docs(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    org: str = "all",
    limit: int = Query(default=300, le=2000),
):
    """Список проблемных документов 1С и проверка, не попали ли они в расчёты."""
    docs = (models.org_scope(db.query(models.ProblemDoc), models.ProblemDoc, org)
            .order_by(models.ProblemDoc.date.desc(),
                      models.ProblemDoc.id.desc())
            .all())
    if not docs:
        return {"rows": [], "shown": 0, "total_rows": 0, "by_kind": [],
                "by_status": [], "leaked": [], "leaked_total": 0}

    # Ищем GUID проблемных документов в рабочих таблицах — одним запросом на
    # таблицу, а не по документу: списки короткие, а обращений к базе иначе
    # были бы сотни.
    # Заодно берём самую раннюю дату загрузки строк документа: 1С не отдаёт,
    # когда поставили пометку удаления, но день, с которого документ считается
    # у нас, известен точно — и пометку поставили уже после него.
    guids = {d.doc_guid for d in docs if d.doc_guid}
    found: dict[str, list[str]] = {}
    since: dict[str, datetime] = {}
    if guids:
        for label, model in LEAK_SOURCES:
            hits = (db.query(model.doc_guid, func.min(model.imported_at))
                    .filter(model.doc_guid.in_(guids))
                    .group_by(model.doc_guid).all())
            for g, first in hits:
                g = (g or "").lower()
                if not g:
                    continue
                found.setdefault(g, []).append(label)
                if first is not None and (g not in since or first < since[g]):
                    since[g] = first

    def row(d):
        return {
            "date": d.date.isoformat(),
            "doc_number": d.doc_number,
            "kind": d.kind,
            "status": d.status,
            "counterparty": d.counterparty,
            "amount": float(d.amount) if d.amount is not None else None,
            "author": d.author,
            "comment": d.comment,
            # Пусто — документ никуда не просочился, и это норма.
            "in_portal": found.get(d.doc_guid or "", []),
            # С какого дня документ числится проблемным у нас.
            "first_seen": (d.first_seen.date().isoformat()
                           if d.first_seen else None),
            # С какого дня его строки лежат в расчётах (только для утечек).
            "in_portal_since": (since[d.doc_guid].date().isoformat()
                                if d.doc_guid in since else None),
        }

    rows = [row(d) for d in docs]

    def group(field):
        agg: dict[str, dict] = {}
        for d in docs:
            key = getattr(d, field) or "(не указан)"
            e = agg.setdefault(key, {field: key, "docs": 0, "amount": 0.0})
            e["docs"] += 1
            e["amount"] += float(d.amount or 0)
        out = sorted(agg.values(), key=lambda e: -e["docs"])
        for e in out:
            e["amount"] = round(e["amount"], 2)
        return out

    leaked = [r for r in rows if r["in_portal"]]
    return {
        "rows": rows[:limit],
        "shown": min(len(rows), limit),
        "total_rows": len(rows),
        "by_kind": group("kind"),
        "by_status": group("status"),
        # Главное в отчёте: документы, которые 1С считает недействующими, а
        # портал всё-таки посчитал. Их надо не читать, а чинить.
        "leaked": leaked,
        "leaked_total": len(leaked),
    }
