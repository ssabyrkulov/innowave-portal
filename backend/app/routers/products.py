"""Справочник номенклатуры — файл «…_Номенклатура».

Формат 1С: Код, Наименование, НаименованиеПолное, Артикул, ЭтоГруппа,
Родитель, РодительGUID, НоменклатурнаяГруппа, ЕдИзм, ЭтоУслуга, КодТНВЭД,
СтранаПроисхождения, ПометкаУдаления, НоменклатураGUID.

Зачем он порталу: до сих пор товары склеивались по нормализованному
названию. Та же позиция в закупке зовётся «Подгузники StarKid размер L*4», в
продаже — «Детские подгузники StarKid размер L», и совпадение строк было
догадкой. Отсюда позиции с отрицательным расчётным остатком и строки
«продано то, чего не закупали».

Справочник даёт GUID — точный ключ. Движения тоже несут НоменклатураGUID, и
там, где он есть, склейка перестаёт быть угадыванием. Для строк, загруженных
до появления GUID, справочник работает мостом: имя → GUID.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import get_current_user

router = APIRouter(prefix="/products", tags=["products"])

HEADERS = {
    "НоменклатураGUID": "guid",
    "Код": "code",
    "Наименование": "name",
    "НаименованиеПолное": "name_full",
    "Артикул": "article",
    "НоменклатурнаяГруппа": "group_name",
    "РодительGUID": "parent_guid",
    "ЕдИзм": "unit",
    "ЕдиницаИзмерения": "unit",
    "ЭтоГруппа": "is_group",
    "ЭтоУслуга": "is_service",
    "СтранаПроисхождения": "country",
    "ПометкаУдаления": "deleted",
}

_TRUE = {"да", "истина", "true", "1", "yes", "y", "+"}


def _flag(value) -> bool:
    """Признак 1С в булево. Пустое и непонятное — «нет»: справочник читается
    ради классификации, и превращать неизвестность в «да» опаснее."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in _TRUE


def import_products_workbook(db: Session, content: bytes, filename: str,
                             user_id: int, org: str = models.DEFAULT_ORG) -> dict:
    """Импорт справочника заменой целиком.

    Организация игнорируется намеренно: номенклатура в 1С общая, и держать её
    копии по фирмам значило бы плодить конфликтующие названия одного GUID."""
    from ..services import xlsx  # читалка с починкой архива 1С ред. 1.7

    wb = xlsx.load_workbook(content)
    ws = wb[wb.sheetnames[0]]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]

    header_idx, col = None, {}
    for i, row in enumerate(rows[:10]):
        names = {str(c).strip(): j for j, c in enumerate(row) if c}
        if "НоменклатураGUID" in names and "Наименование" in names:
            header_idx = i
            col = {HEADERS[k]: j for k, j in names.items() if k in HEADERS}
            break
    if header_idx is None:
        found = ", ".join(str(c).strip() for c in (rows[0] if rows else [])
                          if c is not None)[:300]
        raise HTTPException(
            status_code=400,
            detail="Не найдены колонки справочника номенклатуры (ожидаются "
                   f"НоменклатураGUID и Наименование). В файле: {found or 'пусто'}")

    def cell(row, key):
        j = col.get(key)
        return row[j] if j is not None and j < len(row) else None

    def text(row, key):
        return str(cell(row, key) or "").strip() or None

    parsed: dict[str, models.Product] = {}
    for row in rows[header_idx + 1:]:
        guid = text(row, "guid")
        if not guid:
            continue  # хвост файла или итоговая строка
        # Один GUID — одна позиция. Если 1С отдала дубль, побеждает последняя
        # строка: справочник выгружается целиком, и свежая версия каноничнее.
        parsed[guid.lower()] = models.Product(
            guid=guid.lower(),
            code=text(row, "code"),
            name=text(row, "name"),
            name_full=text(row, "name_full"),
            article=text(row, "article"),
            group_name=text(row, "group_name"),
            parent_guid=(text(row, "parent_guid") or "").lower() or None,
            unit=text(row, "unit"),
            is_group=_flag(cell(row, "is_group")),
            is_service=_flag(cell(row, "is_service")),
            country=text(row, "country"),
            deleted=_flag(cell(row, "deleted")),
        )
    if not parsed:
        # Пустой справочник не бывает: это сбой выгрузки. Стирать по нему
        # рабочий справочник нельзя — молча выходим, оставив прежний.
        return {"added": 0, "empty": True}

    db.query(models.Product).delete(synchronize_session=False)
    db.bulk_save_objects(list(parsed.values()))
    db.add(models.ImportLog(
        filename=f"[номенклатура] {filename}",
        user_id=user_id, added=len(parsed), skipped=0, errors_count=0,
    ))
    db.commit()
    goods = sum(1 for p in parsed.values() if not p.is_group and not p.is_service)
    return {"added": len(parsed), "goods": goods,
            "groups": sum(1 for p in parsed.values() if p.is_group),
            "services": sum(1 for p in parsed.values() if p.is_service)}


@router.get("")
def list_products(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    q: str = "",
    limit: int = Query(default=200, le=2000),
):
    """Позиции справочника; q — поиск по названию, коду или артикулу."""
    query = db.query(models.Product).filter(models.Product.is_group.is_(False))
    if q.strip():
        # Шаблон берём как есть: ilike сам приводит регистр обеих сторон.
        # Ручной .lower() здесь всё ломал — на SQLite, где ilike кириллицу не
        # понижает, запрос «Глобус» превращался в «глобус» и не находил
        # ничего вообще. На Postgres (там портал и работает) регистр не важен
        # в любом случае, на SQLite поиск по кириллице теперь чувствителен к
        # регистру — но находит.
        like = f"%{q.strip()}%"
        query = query.filter(models.Product.name.ilike(like)
                             | models.Product.code.ilike(like)
                             | models.Product.article.ilike(like))
    rows = query.order_by(models.Product.name).limit(limit).all()
    return [{
        "guid": p.guid, "code": p.code, "name": p.name,
        "group_name": p.group_name, "unit": p.unit,
        "is_service": p.is_service, "deleted": p.deleted,
    } for p in rows]


@router.get("/coverage")
def guid_coverage(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    org: str = "all",
):
    """Насколько движения опираются на GUID, а не на догадку по названию.

    Считается и по товару, и по контрагенту: вопрос один и тот же, а ответы
    в двух разных отчётах пришлось бы сводить глазами.

    Три состояния у каждой строки: GUID пришёл из выгрузки (точно), GUID
    подобран по справочнику через название (надёжно), не подобран вовсе
    (склейка остаётся догадкой). Последнее — очередь на разбор: именно эти
    строки дают «продано то, чего не закупали»."""
    from .purchases import _norm_product

    by_name: dict[str, str] = {}
    known_guids: set[str] = set()
    goods = 0
    for p in db.query(models.Product).filter(
            models.Product.is_group.is_(False)).all():
        goods += 1
        known_guids.add(p.guid)
        for candidate in (p.name, p.name_full):
            if candidate:
                by_name.setdefault(_norm_product(candidate), p.guid)

    # Контрагенты меряются тем же способом: справочник даёт мост по имени,
    # выгрузка — GUID. Разные сущности, но вопрос один — на чём держится
    # склейка, и держать ответы в двух разных отчётах незачем.
    parties_by_name: dict[str, str] = {}
    party_guids: set[str] = set()
    parties = 0
    for c in db.query(models.Counterparty).filter(
            models.Counterparty.is_group.is_(False)).all():
        parties += 1
        party_guids.add(c.guid)
        for candidate in (c.name, c.name_full):
            if candidate:
                parties_by_name.setdefault(_norm_product(candidate), c.guid)

    out = []
    MODELS = (("Товар · продажи", models.Sale, "product", "product_guid",
               by_name),
              ("Товар · закупки", models.Purchase, "product", "product_guid",
               by_name),
              ("Товар · возвраты", models.ReturnLine, "product",
               "product_guid", by_name),
              ("Товар · списания", models.WriteOff, "product", "product_guid",
               by_name),
              ("Товар · оприходования", models.StockReceipt, "product",
               "product_guid", by_name),
              ("Клиент · продажи", models.Sale, "client", "client_guid",
               parties_by_name),
              ("Клиент · возвраты", models.ReturnDoc, "client", "client_guid",
               parties_by_name),
              ("Поставщик · закупки", models.Purchase, "supplier",
               "supplier_guid", parties_by_name))
    guid_index = {id(by_name): known_guids, id(parties_by_name): party_guids}
    for label, model, name_field, guid_field, index in MODELS:
        known = guid_index[id(index)]
        rows = models.org_scope(
            db.query(getattr(model, name_field), getattr(model, guid_field)),
            model, org).all()
        direct = bridged = orphan = 0
        unmatched: dict[str, int] = {}
        for name, guid in rows:
            # GUID засчитывается точным ключом, только если справочник его
            # знает: незнакомый ключом не становится (иначе позиция разъедется
            # надвое), и писать про него «GUID из выгрузки» значило бы
            # показывать надёжность, которой нет.
            if guid and guid in known:
                direct += 1
            elif index and _norm_product(name) in index:
                bridged += 1
            else:
                orphan += 1
                unmatched[name or "(без названия)"] = \
                    unmatched.get(name or "(без названия)", 0) + 1
        out.append({
            "source": label, "rows": len(rows), "direct": direct,
            "bridged": bridged, "orphan": orphan,
            "top_unmatched": sorted(unmatched.items(), key=lambda kv: -kv[1])[:5],
        })
    return {"products": goods, "names": len(by_name),
            "counterparties": parties, "sources": out}
