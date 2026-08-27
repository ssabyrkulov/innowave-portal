"""Справочник контрагентов — файл «…_Контрагенты».

Формат 1С: Код, Наименование, НаименованиеПолное, ЭтоГруппа, Родитель,
РодительGUID, ИНН, ОКПО, ЮрФизЛицо, ГоловнойКонтрагент, ПометкаУдаления,
КонтрагентGUID.

Та же болезнь, что была с номенклатурой, только про клиентов: портал сводит
плательщика к клиенту вручную, через таблицу ClientAlias — кто-то садится и
сопоставляет «ОсОО Глобус» с «Глобус ОсОО». КонтрагентGUID идёт в выгрузках
и до сих пор нигде не сохранялся, а справочник пропускался как вид без
импортёра.

Отдельно ценен «ГоловнойКонтрагент»: филиалы сети висят под ним, и без него
дебиторка считается по каждой точке, а не по сети целиком.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import get_current_user

router = APIRouter(prefix="/counterparties", tags=["counterparties"])

HEADERS = {
    "КонтрагентGUID": "guid",
    "Код": "code",
    "Наименование": "name",
    "НаименованиеПолное": "name_full",
    "ИНН": "inn",
    "ОКПО": "okpo",
    "ГоловнойКонтрагент": "head_name",
    "РодительGUID": "parent_guid",
    "ЮрФизЛицо": "legal_type",
    "ЭтоГруппа": "is_group",
    "ПометкаУдаления": "deleted",
}

_TRUE = {"да", "истина", "true", "1", "yes", "y", "+"}


def _flag(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in _TRUE


def import_counterparties_workbook(db: Session, content: bytes, filename: str,
                                   user_id: int,
                                   org: str = models.DEFAULT_ORG) -> dict:
    """Импорт справочника заменой целиком.

    Организация игнорируется намеренно: контрагенты в 1С общие, и держать их
    копии по фирмам значило бы плодить конфликтующие имена одного GUID."""
    from ..services import xlsx  # читалка с починкой архива 1С ред. 1.7

    wb = xlsx.load_workbook(content)
    ws = wb[wb.sheetnames[0]]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]

    header_idx, col = None, {}
    for i, row in enumerate(rows[:10]):
        names = {str(c).strip(): j for j, c in enumerate(row) if c}
        if "КонтрагентGUID" in names and "Наименование" in names:
            header_idx = i
            col = {HEADERS[k]: j for k, j in names.items() if k in HEADERS}
            break
    if header_idx is None:
        found = ", ".join(str(c).strip() for c in (rows[0] if rows else [])
                          if c is not None)[:300]
        raise HTTPException(
            status_code=400,
            detail="Не найдены колонки справочника контрагентов (ожидаются "
                   f"КонтрагентGUID и Наименование). В файле: {found or 'пусто'}")

    def cell(row, key):
        j = col.get(key)
        return row[j] if j is not None and j < len(row) else None

    def text(row, key):
        return str(cell(row, key) or "").strip() or None

    parsed: dict[str, models.Counterparty] = {}
    for row in rows[header_idx + 1:]:
        guid = text(row, "guid")
        if not guid:
            continue  # хвост файла или итоговая строка
        parsed[guid.lower()] = models.Counterparty(
            guid=guid.lower(),
            code=text(row, "code"),
            name=text(row, "name"),
            name_full=text(row, "name_full"),
            inn=text(row, "inn"),
            okpo=text(row, "okpo"),
            head_name=text(row, "head_name"),
            parent_guid=(text(row, "parent_guid") or "").lower() or None,
            legal_type=text(row, "legal_type"),
            is_group=_flag(cell(row, "is_group")),
            deleted=_flag(cell(row, "deleted")),
        )
    if not parsed:
        # Пустой справочник — сбой выгрузки, а не «контрагентов не стало».
        # Стирать по нему рабочий справочник нельзя.
        return {"added": 0, "empty": True}

    db.query(models.Counterparty).delete(synchronize_session=False)
    db.bulk_save_objects(list(parsed.values()))
    db.add(models.ImportLog(
        filename=f"[контрагенты] {filename}",
        user_id=user_id, added=len(parsed), skipped=0, errors_count=0,
    ))
    db.commit()
    return {
        "added": len(parsed),
        "groups": sum(1 for c in parsed.values() if c.is_group),
        "with_inn": sum(1 for c in parsed.values() if c.inn),
        "in_networks": sum(1 for c in parsed.values() if c.head_name),
    }


def _norm_name(name: str | None) -> str:
    """Нормализация имени контрагента — та же, что в дебиторке."""
    import re
    return re.sub(r"\s+", " ", (name or "").lower().replace("ё", "е")).strip()


def client_matcher(db: Session, known_names: dict[str, str],
                   guid_names: dict[str, str], aliases: dict[str, str]):
    """Возвращает resolve(имя, GUID) → каноничное имя клиента или None.

    known_names — нормализованное имя → как клиент называется в отгрузках;
    guid_names — GUID → то же имя; aliases — ручные сопоставления плательщик
    → клиент.

    Цепочка только дополняет прежнюю, ничего из неё не убирая: ручной алиас,
    затем GUID, затем точное имя, нормализованное и напоследок мост через
    справочник (имя → GUID → клиент). Порядок важен дважды. Алиас идёт
    первым, потому что это решение живого человека, и молча переигрывать его
    машинным ключом нельзя. Всё остальное — после GUID, потому что имя врёт
    чаще. Ни одна оплата, которая раньше находила клиента, после этой правки
    его не потеряет: старые шаги остались на месте, добавились новые.

    Возвращает None, если не нашлось ничего — как и раньше: такая оплата
    попадает в «нераспознанные», и это честнее, чем повесить её наугад."""
    by_name: dict[str, str] = {}
    if guid_names:
        for c in db.query(models.Counterparty.guid, models.Counterparty.name,
                          models.Counterparty.name_full,
                          models.Counterparty.is_group).all():
            if c.is_group:
                continue
            for candidate in (c.name, c.name_full):
                if candidate:
                    by_name.setdefault(_norm_name(candidate), c.guid)

    def resolve(name: str | None, guid: str | None = None) -> str | None:
        if name is not None and name in aliases:
            return aliases[name]
        g = (guid or "").strip().lower()
        if g and g in guid_names:
            return guid_names[g]
        norm = _norm_name(name)
        hit = known_names.get(norm)
        if hit is not None:
            return hit
        # Мост: имени нет среди отгрузок, но справочник знает такой GUID, и
        # под ним отгрузки есть — значит это тот же клиент, просто написан
        # иначе (филиал, сокращение, старое название).
        bridged = by_name.get(norm)
        if bridged and bridged in guid_names:
            return guid_names[bridged]
        return None


    return resolve


def head_by_client(db: Session) -> dict[str, str]:
    """Нормализованное имя клиента → головной контрагент (сеть), если есть."""
    out: dict[str, str] = {}
    for c in db.query(models.Counterparty.name, models.Counterparty.name_full,
                      models.Counterparty.head_name,
                      models.Counterparty.is_group).all():
        if c.is_group or not c.head_name:
            continue
        for candidate in (c.name, c.name_full):
            if candidate:
                out.setdefault(_norm_name(candidate), c.head_name)
    return out


@router.get("")
def list_counterparties(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    q: str = "",
    limit: int = Query(default=200, le=2000),
):
    """Контрагенты справочника; q — поиск по названию, коду или ИНН."""
    query = db.query(models.Counterparty).filter(
        models.Counterparty.is_group.is_(False))
    if q.strip():
        # Шаблон берём как есть: ilike сам приводит регистр обеих сторон.
        # Ручной .lower() здесь всё ломал — на SQLite, где ilike кириллицу не
        # понижает, запрос «Глобус» превращался в «глобус» и не находил
        # ничего вообще. На Postgres (там портал и работает) регистр не важен
        # в любом случае, на SQLite поиск по кириллице теперь чувствителен к
        # регистру — но находит.
        like = f"%{q.strip()}%"
        query = query.filter(models.Counterparty.name.ilike(like)
                             | models.Counterparty.code.ilike(like)
                             | models.Counterparty.inn.ilike(like))
    rows = query.order_by(models.Counterparty.name).limit(limit).all()
    return [{
        "guid": c.guid, "code": c.code, "name": c.name, "inn": c.inn,
        "head_name": c.head_name, "legal_type": c.legal_type,
        "deleted": c.deleted,
    } for c in rows]


@router.get("/networks")
def networks(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    """Сети: головной контрагент и его точки.

    Пока не используется в расчётах — это разведка перед тем, как переводить
    дебиторку на сети. Сначала надо увидеть, сколько их и как они выглядят:
    цифры про деньги нельзя менять вслепую."""
    rows = (db.query(models.Counterparty)
            .filter(models.Counterparty.is_group.is_(False),
                    models.Counterparty.head_name.isnot(None))
            .order_by(models.Counterparty.head_name,
                      models.Counterparty.name).all())
    groups: dict[str, list[str]] = {}
    for c in rows:
        groups.setdefault(c.head_name, []).append(c.name or c.code or c.guid)
    return {
        "networks": len(groups),
        "members": len(rows),
        "rows": [{"head": head, "members": members, "count": len(members)}
                 for head, members in sorted(groups.items(),
                                             key=lambda kv: -len(kv[1]))],
    }
