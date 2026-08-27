"""Ручные операции 1С и сторно — файл «…_Ручные операции».

Обычная проводка в 1С рождается из документа: продали, оплатили, списали.
Ручная операция — проводка в обход документов, вписанная бухгалтером. Иногда
это норма (перенос между счетами), иногда так правят то, что не сходится, — и
тогда баланс 1С и расчёт портала расходятся, а причины в выгрузках документов
не видно вовсе.

Отдельный случай — сторно. Операция отменяет ранее проведённый документ, но
сам документ остаётся в базе и приходит в выгрузку как обычный. Портал считает
его целиком, не зная, что 1С его сняла. Поэтому СторноGUID проверяется по
нашим таблицам: если сторнированный документ у нас есть, значит именно на его
сумму мы ошибаемся.

Формат управленческой выгрузки: Дата, Номер, Организация, Содержание,
СуммаОперации, СуммаДокумента, ТиповаяОперация, СпособЗаполнения,
СторнируемыйДокумент, СторноGUID, Ответственный, Комментарий, Автор,
ДокументGUID. Налоговая версия короче (без сторно-колонок) и уходит своим
импортёром — здесь принимаются обе, чтобы файл не падал из-за лишней колонки.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import get_current_user

router = APIRouter(prefix="/manual-entries", tags=["manual-entries"])

HEADERS = {
    "Дата": "date",
    "Номер": "doc_number",
    "Содержание": "content",
    "СуммаОперации": "amount",
    "СуммаДокумента": "doc_amount",
    "ТиповаяОперация": "typical",
    "СпособЗаполнения": "fill_method",
    "СторнируемыйДокумент": "reversed_doc",
    "СторноGUID": "reversed_guid",
    "Ответственный": "responsible",
    "Автор": "author",
    "Комментарий": "comment",
    "ДокументGUID": "doc_guid",
}


def import_manual_entries_workbook(db: Session, content: bytes, filename: str,
                                   user_id: int,
                                   org: str = models.DEFAULT_ORG) -> dict:
    """Импорт ручных операций заменой данных организации целиком.

    Как и проблемные документы, это снимок: операцию удалили — и она из
    выгрузки исчезла. Пустой файл поэтому очищает список, а не пропускается:
    иначе портал показывал бы отменённые правки как действующие."""
    from ..services import xlsx  # читалка с починкой архива 1С ред. 1.7
    from .tax import _day, _num

    org = models.normalize_org(org)
    wb = xlsx.load_workbook(content)
    ws = wb[wb.sheetnames[0]]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]

    header_idx, col = None, {}
    for i, row in enumerate(rows[:10]):
        names = {str(c).strip(): j for j, c in enumerate(row) if c}
        if "Дата" in names and "Содержание" in names and "СуммаОперации" in names:
            header_idx = i
            col = {HEADERS[k]: j for k, j in names.items() if k in HEADERS}
            break
    if header_idx is None:
        found = ", ".join(str(c).strip() for c in (rows[0] if rows else [])
                          if c is not None)[:300]
        raise HTTPException(
            status_code=400,
            detail="Не найдены колонки ручных операций (ожидаются Дата, "
                   f"Содержание, СуммаОперации). В файле: {found or 'пусто'}")

    def cell(row, key):
        j = col.get(key)
        return row[j] if j is not None and j < len(row) else None

    def text(row, key):
        return str(cell(row, key) or "").strip() or None

    parsed: list[models.ManualEntry] = []
    for row in rows[header_idx + 1:]:
        d = _day(cell(row, "date"))
        if d is None:
            continue  # пустой хвост файла или итоговая строка
        parsed.append(models.ManualEntry(
            organization=org,
            date=d,
            doc_number=text(row, "doc_number"),
            doc_guid=(text(row, "doc_guid") or "").lower() or None,
            content=text(row, "content"),
            amount=_num(cell(row, "amount")),
            doc_amount=_num(cell(row, "doc_amount")),
            typical=text(row, "typical"),
            fill_method=text(row, "fill_method"),
            reversed_doc=text(row, "reversed_doc"),
            reversed_guid=(text(row, "reversed_guid") or "").lower() or None,
            responsible=text(row, "responsible"),
            author=text(row, "author"),
            comment=text(row, "comment"),
        ))

    db.query(models.ManualEntry).filter(
        models.ManualEntry.organization == org).delete(synchronize_session=False)
    if parsed:
        db.bulk_save_objects(parsed)
    db.add(models.ImportLog(filename=f"[ручные операции:{org}] {filename}",
                            user_id=user_id, added=len(parsed),
                            skipped=0, errors_count=0))
    db.commit()
    return {"added": len(parsed),
            "reversals": sum(1 for p in parsed if p.reversed_guid)}


@router.get("")
def manual_entries(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    org: str = "all",
    limit: int = Query(default=300, le=2000),
):
    """Ручные операции, сторно и проверка сторнированных документов."""
    # Где искать сторнированный документ — тот же список таблиц, что у
    # проблемных документов. Вопрос один и тот же: лежит ли у нас документ,
    # которого в расчёте быть не должно.
    from .problem_docs import LEAK_SOURCES

    entries = (models.org_scope(db.query(models.ManualEntry),
                                models.ManualEntry, org)
               .order_by(models.ManualEntry.date.desc(),
                         models.ManualEntry.id.desc())
               .all())
    if not entries:
        return {"rows": [], "shown": 0, "total_rows": 0, "amount": 0.0,
                "by_author": [], "reversals": [], "reversals_total": 0,
                "counted_anyway": 0, "counted_amount": 0.0}

    guids = {e.reversed_guid for e in entries if e.reversed_guid}
    found: dict[str, list[str]] = {}
    if guids:
        for label, model in LEAK_SOURCES:
            hits = (db.query(model.doc_guid)
                    .filter(model.doc_guid.in_(guids))
                    .distinct().all())
            for (g,) in hits:
                found.setdefault((g or "").lower(), []).append(label)

    def row(e):
        return {
            "date": e.date.isoformat(),
            "doc_number": e.doc_number,
            "content": e.content,
            "amount": float(e.amount) if e.amount is not None else None,
            "typical": e.typical,
            "reversed_doc": e.reversed_doc,
            "author": e.author or e.responsible,
            "comment": e.comment,
            # Непусто — сторнированный документ лежит у нас и считается.
            "in_portal": found.get(e.reversed_guid or "", []),
        }

    rows = [row(e) for e in entries]
    reversals = [r for r in rows if r["reversed_doc"] or r["in_portal"]]
    counted = [r for r in rows if r["in_portal"]]

    by_author: dict[str, dict] = {}
    for e in entries:
        key = e.author or e.responsible or "(не указан)"
        a = by_author.setdefault(key, {"author": key, "docs": 0, "amount": 0.0})
        a["docs"] += 1
        a["amount"] += float(e.amount or 0)
    for a in by_author.values():
        a["amount"] = round(a["amount"], 2)

    return {
        "rows": rows[:limit],
        "shown": min(len(rows), limit),
        "total_rows": len(rows),
        "amount": round(sum(float(e.amount or 0) for e in entries), 2),
        "by_author": sorted(by_author.values(), key=lambda a: -a["docs"]),
        "reversals": reversals,
        "reversals_total": len(reversals),
        # Сторно, чей документ портал всё-таки считает: 1С операцию сняла, а
        # у нас она в расчёте целиком. Ровно на эту сумму мы ошибаемся.
        "counted_anyway": len(counted),
        "counted_amount": round(sum(abs(r["amount"] or 0) for r in counted), 2),
    }
