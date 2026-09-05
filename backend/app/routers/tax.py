"""Налоговый контур: выгрузки из налоговых баз 1С (ред. 1.7) обеих фирм.

Живут в своей таблице и с управленческими данными не пересекаются вообще —
иначе задвоились бы и деньги, и продажи. Грузятся автоматически из общей
папки Drive: фирму даёт первое слово имени файла, налоговый контур — метка
«НАЛОГОВАЯ», вид документа — остаток имени.

Вид операции определяется ТОЛЬКО по имени файла. Заголовки не различают ни
приход от расхода денег, ни закупку от продажи: у «Поступление товары» те же
Контрагент/Номенклатура/Количество/Сумма, что у «Реализация товары». Файл с
незнакомым именем отбивается ошибкой — молча угадать значит уложить закупки
в реализации и сломать сверку.
"""

import re
from collections import defaultdict
from datetime import date, datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import get_current_user, require_roles
from ..services import xlsx

router = APIRouter(prefix="/tax", tags=["tax"])

can_edit = require_roles(models.Role.admin, models.Role.accountant)


def _rows(content: bytes) -> list[list]:
    # xlsx.load_workbook чинит архив 1С ред. 1.7: она пишет SharedStrings.xml
    # с большой буквы, а openpyxl ищет строчную.
    wb = xlsx.load_workbook(content)
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
        if "Дата" in names and (_AMOUNT_FIELDS & set(names)
                                or "Количество" in names or "Номенклатура" in names):
            return i, names
    raise HTTPException(
        status_code=400,
        detail="Не нашёл строку заголовков (нужны «Дата» и сумма либо количество)")


# Явная карта «имя файла налоговой → вид операции». Вид берём ТОЛЬКО из имени:
# в новом формате заголовки не различают ни приход от расхода денег (у ПКО и
# РКО колонки почти одинаковы), ни закупку от продажи (у «Поступление товары»
# те же Контрагент/Номенклатура/Количество/Сумма, что у «Реализация товары»).
# Угадывание по заголовкам укладывало 600 строк закупок в реализации и ломало
# сверку, поэтому неизвестное имя честно отбивается ошибкой.
# Порядок важен: частные токены идут раньше общих.
_TAX_BY_NAME = (
    ("возврат поставщику", "return_supplier"),
    ("возврат от покупателя", "return"),
    ("реализация", "sale"),
    ("поступление доп расходов", "purchase"),
    ("поступление товары", "purchase"),
    ("поступление услуги", "purchase"),
    ("поступление товаров и услуг", "purchase"),
    ("пко выдача в подотчет", "cash_in"),
    ("приходный кассовый ордер", "cash_in"),
    ("пп входящее", "cash_in"),
    ("платежный ордер поступление", "cash_in"),
    ("рко выдача в подотчет", "cash_out"),
    ("рко выплата зарплаты", "cash_out"),
    ("расходный кассовый ордер", "cash_out"),
    ("пп исходящее", "cash_out"),          # покрывает и «ПП исходящее налоги»
    ("платежный ордер списание", "cash_out"),
    ("авансовый отчет", "advance"),
    ("списание товаров", "writeoff"),
    ("оприходование товаров", "stock_in"),
    ("перемещение товаров", "transfer"),
    ("инвентаризация товаров", "inventory"),
    # Ниже — файлы, которые в налоговый контур пока не грузятся. Держим их в
    # той же карте, чтобы отказ был осмысленным, а не «не нашёл заголовков».
    ("журнал проводок", "unsupported"),
    ("гтд", "unsupported"),
    ("конвертация", "unsupported"),
    ("начисление зарплаты", "unsupported"),
    ("сф выданные", "unsupported"),
    ("сф полученные", "unsupported"),
    ("эсф выписанные", "unsupported"),
    ("эсф полученные", "unsupported"),
    ("бланки счетов-фактур", "unsupported"),
    ("ручные операции", "unsupported"),
    ("проблемные документы", "unsupported"),
    ("контрагенты", "unsupported"),
    ("номенклатура", "unsupported"),
    ("остатки", "unsupported"),
)

# Виды, которые участвуют в сверке налоговой с управленкой. Остальные лежат
# справочно: закупки, подотчёт, склад и возвраты поставщикам — управленческого
# контура под них в портале нет, сверять не с чем.
# Виды, у которых в управленке есть свой контур и пара ищется. Закупки и
# списания попали сюда, когда сверка научилась искать пару по количеству:
# до этого их сверять было нечем.
MATCHED_KINDS = ("sale", "return", "cash_in", "cash_out", "purchase",
                 "writeoff")

# Товарные виды: у них есть номенклатура, количество и склад.
_GOODS_KINDS = ("sale", "return", "return_supplier", "purchase",
                "writeoff", "stock_in", "transfer", "inventory")

# Сумма товарной строки: сначала строка табличной части, потом итог документа.
# Табличная часть товарных документов выгружена корректно — по всем 175
# реализациям Хайджина и 191 Инновейва строки сходятся с итогом до копейки.
_AMOUNT_ORDER = ("Сумма", "СуммаРасхода", "СуммаФакт", "СуммаДокумента",
                 "СуммаОперации")
_AMOUNT_FIELDS = set(_AMOUNT_ORDER) | {"СуммаПлатежа", "СуммаКВыплате"}

# Денежные виды: одна строка файла = один документ, сумма берётся из шапки.
_CASH_KINDS = ("cash_in", "cash_out")

# Плательщик/получатель. У ПКО и РКО подотчёта нет колонки «Контрагент» —
# там физлицо в «ПринятоОт», «Выдать» или «ФизЛицо».
_PARTY_ORDER = ("Контрагент", "КонтрагентНаименование", "Поставщик",
                "Покупатель", "ПринятоОт", "Выдать", "ФизЛицо", "Сотрудник")


def _detect_kind(filename: str) -> str:
    """Вид операции по имени файла. Неизвестное имя — ошибка, а не догадка."""
    low = (filename or "").lower().replace("\u0451", "\u0435")
    for token, kind in _TAX_BY_NAME:
        if token in low:
            return kind
    raise HTTPException(
        status_code=400,
        detail=f"Не знаю, к какому виду отнести файл «{filename}». "
               "Вид определяется по имени файла из выгрузки 1С.",
    )


def _tax_source(filename: str) -> str:
    """Вид документа из имени файла — «Хайджин_НАЛОГОВАЯ_ПП входящее» → «ПП
    входящее». Нужен, чтобы снапшоты разных файлов одного вида не затирали
    друг друга."""
    base = (filename or "").rsplit("/", 1)[-1]
    for suffix in (".xlsx", ".xls"):
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)]
    parts = base.split("_")
    return (parts[-1] if len(parts) > 1 else base).strip() or "без имени"


def _parse(content: bytes, filename: str) -> tuple[str, list[dict]]:
    kind = _detect_kind(filename)
    if kind == "unsupported":
        raise HTTPException(
            status_code=400,
            detail=f"Файл «{filename}» в налоговый контур пока не грузится "
                   "(проводки, ГТД, счета-фактуры, зарплата и справочники).",
        )
    source = _tax_source(filename)
    rows = _rows(content)
    hi, names = _header_map(rows)

    def cell(row, name):
        j = names.get(name)
        return row[j] if j is not None and j < len(row) else None

    out: list[dict] = []
    for row in rows[hi + 1:]:
        d = _day(cell(row, "Дата"))
        if d is None:
            continue

        # Имена колонок различаются у старого и нового формата: короткие
        # «Контрагент», «Номенклатура», «Валюта» пришли на смену длинным.
        def first(*fields):
            for f in fields:
                v = cell(row, f)
                if v not in (None, ""):
                    return v
            return None

        if kind in _CASH_KINDS:
            # У ПКО, РКО и платёжек колонка «СуммаПлатежа» выгружается
            # несвязанной с шапкой: ПКО Хайджина по «СуммаДокумента» даёт
            # 40 467 679,11, а по «СуммаПлатежа» — 104 458 284, причём у
            # документа на 89 сом там стоит 3 200 000 от чужой строки.
            # Деньги берём только из шапки документа.
            amount = _num(first("СуммаДокумента", "Сумма"))
        else:
            amount = _num(first(*_AMOUNT_ORDER))
        if amount is None:
            # У складских документов суммы нет вовсе — «Списание товаров»
            # выгружается с пустой СуммаДокумента и одним количеством.
            # Отбрасывать такие строки нельзя: тогда весь файл теряется.
            if kind not in ("writeoff", "stock_in", "transfer", "inventory"):
                continue
            amount = 0.0
        common = {
            "kind": kind, "date": d, "amount": amount, "source": source,
            "currency": str(first("Валюта", "ВалютаДокументаНаименование")
                            or "KGS").strip() or "KGS",
            "doc_number": str(first("Номер") or "").strip() or None,
            "doc_guid": str(first("ДокументGUID") or "").strip() or None,
            "counterparty": str(first(*_PARTY_ORDER) or "").strip() or None,
            "comment": str(first("Комментарий") or "").strip() or None,
        }
        if kind in _GOODS_KINDS:
            out.append({
                **common,
                "doc_total": _num(first("СуммаДокумента", "СуммаРасходаВсего")),
                "warehouse": str(first("Склад", "СкладНаименование",
                                       "СкладПолучатель") or "").strip() or None,
                "product": str(first("Номенклатура", "НоменклатураНаименование")
                               or "").strip() or None,
                "qty": _num(first("Количество", "КоличествоФакт", "КоличествоУчет")),
                "account": str(first("СчетУчетаБУ", "СчетУчета") or "").strip() or None,
            })
        else:
            out.append({
                **common,
                "operation": str(first("ВидОперации", "Основание",
                                       "НазначениеПлатежа", "Содержание",
                                       "СтатьяДДС") or "").strip() or None,
            })

    if kind in _CASH_KINDS:
        # Страховка: если 1С выгрузит платёжку несколькими строками (у
        # «ПП исходящее налоги» табличная часть уже есть), сумма шапки
        # повторится на каждой и задвоится.
        seen: set = set()
        uniq = []
        for op in out:
            key = op["doc_guid"] or (op["doc_number"], op["date"], op["amount"])
            if key in seen:
                continue
            seen.add(key)
            uniq.append(op)
        out = uniq
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
    source = parsed[0].get("source")
    # Заменяем снапшот по паре «вид + источник». По одному виду замена стирала
    # бы соседний файл: приход дают и ПКО, и ПП входящее, и платёжный ордер.
    db.query(models.TaxOperation).filter(
        models.TaxOperation.organization == org,
        models.TaxOperation.kind == kind,
        models.TaxOperation.source == source,
    ).delete(synchronize_session=False)
    # Наследство старого формата. Файлы со скобочными именами
    # («[Hygiene][NAL][Realizaciya tovarov i uslug]») новый разбор уже не
    # принимает, но строки от них лежат в базе с пустым источником: колонка
    # появилась вместе с новым пакетом. Оставить их рядом с новыми — значит
    # задвоить налоговые реализации в сверке, поэтому первый же файл нового
    # формата убирает старый снапшот своего вида.
    db.query(models.TaxOperation).filter(
        models.TaxOperation.organization == org,
        models.TaxOperation.kind == kind,
        or_(models.TaxOperation.source.is_(None),
            models.TaxOperation.source.like("%[%")),
    ).delete(synchronize_session=False)
    for p in parsed:
        db.add(models.TaxOperation(organization=org, **p))
    db.add(models.ImportLog(
        filename=f"[налоговая:{org}:{kind}:{source}] {filename}",
        user_id=user_id, added=len(parsed), skipped=0, errors_count=0,
    ))
    db.commit()
    return {"kind": kind, "source": source, "added": len(parsed)}


@router.post("/import")
async def tax_import(
    db: Session = Depends(get_db),
    user: models.User = Depends(can_edit),
    file: UploadFile = File(...),
    org: str = Form(default="hygiene"),
):
    content = await file.read()
    return import_tax_workbook(db, content, file.filename or "", user.id, org)


KIND_LABEL = {
    "sale": "Реализации",
    "return": "Возвраты от покупателей",
    "return_supplier": "Возвраты поставщикам",
    "cash_in": "Деньги · приход",
    "cash_out": "Деньги · расход",
    "purchase": "Закупки",
    "advance": "Авансовые отчёты",
    "writeoff": "Списания",
    "stock_in": "Оприходование",
    "transfer": "Перемещения",
    "inventory": "Инвентаризация",
}


from pydantic import BaseModel


class TaxLinkItem(BaseModel):
    tax_name: str
    upr_names: list[str] | None = None  # пустой список/None — удалить связки


def _links_map(db: Session) -> dict[str, list[str]]:
    """Карта «налоговое имя → имена управленки» (их может быть несколько)."""
    out: dict[str, list[str]] = {}
    for l in db.query(models.TaxClientLink).all():
        out.setdefault(l.tax_name, []).append(l.upr_name)
    return out


@router.get("/links")
def tax_links(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    """Контрагенты налоговой базы с оборотами + текущие связки с управленкой."""
    links = _links_map(db)
    agg: dict[str, dict] = {}
    for op in db.query(models.TaxOperation).all():
        if not op.counterparty:
            continue
        a = agg.setdefault(op.counterparty, {"amount": 0.0, "count": 0})
        a["amount"] += float(op.amount)
        a["count"] += 1
    # Кандидаты со стороны управленки: клиенты продаж, плательщики, контрагенты
    # расходов — всё, с чем налоговую операцию можно связать.
    upr: set[str] = set()
    for (c,) in db.query(models.Sale.client).distinct():
        upr.add(c)
    for (c,) in db.query(models.Receipt.payer).distinct():
        upr.add(c)
    for (c,) in db.query(models.Expense.counterparty).distinct():
        upr.add(c)
    return {
        "clients": sorted(
            ({"tax_name": name, "amount": round(v["amount"], 2),
              "count": v["count"], "upr_names": links.get(name, [])}
             for name, v in agg.items()),
            key=lambda x: -x["amount"],
        ),
        "upr_options": sorted(n for n in upr if n),
    }


@router.post("/links")
def tax_link_save(
    payload: TaxLinkItem,
    db: Session = Depends(get_db),
    _: models.User = Depends(can_edit),
):
    """Сохранить связки контрагента: список имён управленки заменяется целиком
    (пустой список — удалить все)."""
    tax_name = payload.tax_name.strip()
    if not tax_name:
        raise HTTPException(status_code=400, detail="Пустое имя контрагента")
    db.query(models.TaxClientLink).filter_by(tax_name=tax_name).delete(
        synchronize_session=False)
    names = [n.strip() for n in (payload.upr_names or []) if n and n.strip()]
    for n in dict.fromkeys(names):  # без дублей, порядок сохранён
        db.add(models.TaxClientLink(tax_name=tax_name, upr_name=n))
    db.commit()
    return {"status": "ok", "count": len(set(names))}


@router.get("/groups")
def tax_groups(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    org: str = "all",
):
    """Сверка по группам связок: суммарные обороты налогового контрагента
    против СОВОКУПНОСТИ связанных контрагентов управленки.

    Построчная сверка операций тут и не должна сходиться: Императив платит
    одной суммой за несколько точек Алдей, дробление разное. Сходиться должны
    итоги группы. Сравниваем всю историю обеих сторон без выравнивания
    периодов: если в управленке есть операции старше налоговой базы, разница
    покажет реальный объём непроведённого — прятать его отсечкой по дате было
    бы искажением."""
    from .receipts import CUSTOMER_PAYMENT_PREFIX

    from .purchases import _norm_product, _group_of, _size_of

    links = _links_map(db)
    if not links:
        return {"groups": []}
    o = models.normalize_org(org) if (org or "").lower() in models.ORGS else None
    uq_org = o or "hygiene"  # налоговый контур пока только по Hygiene
    aliases = {a.payer: a.client for a in db.query(models.ClientAlias).all()}

    # Один реальный партнёр — это связная группа имён: шесть налоговых ИП и
    # один контрагент управленки образуют одну группу, а не шесть. Раньше на
    # каждое налоговое имя строилась своя строка с ОДНИМ И ТЕМ ЖЕ итогом
    # управленки — управленческая сторона считалась шесть раз, и разница
    # получалась бессмысленной. Собираем компоненты связности.
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for tax_name, upr_names in links.items():
        for upr in upr_names:
            union(("T", tax_name), ("U", upr))

    comps: dict = {}
    for node in list(parent):
        c = comps.setdefault(find(node), {"tax": set(), "upr": set()})
        c["tax" if node[0] == "T" else "upr"].add(node[1])

    groups = []
    for comp in comps.values():
        tax_names, upr_names = sorted(comp["tax"]), sorted(comp["upr"])
        if not tax_names or not upr_names:
            continue

        # --- Налоговая сторона ---
        ops = db.query(models.TaxOperation).filter(
            models.TaxOperation.organization == uq_org,
            models.TaxOperation.counterparty.in_(tax_names)).all()
        if not ops:
            continue
        nal_sales = sum(float(op.amount) for op in ops if op.kind == "sale")
        nal_pay = sum(float(op.amount) for op in ops if op.kind == "cash_in")
        nal_ret = sum(float(op.amount) for op in ops if op.kind == "return")
        nal_qty = sum(float(op.qty or 0) for op in ops if op.kind == "sale")

        # --- Управленка: продажи (итог документа — один раз) ---
        upr_sales, upr_qty, seen_docs = 0.0, 0.0, set()
        sales_rows = db.query(models.Sale).filter(
            models.Sale.organization == uq_org,
            models.Sale.client.in_(upr_names)).all()
        for s in sales_rows:
            upr_qty += float(s.qty or 0)
            if s.doc_number and s.doc_total is not None:
                key = (s.doc_number, s.date, s.client)
                if key in seen_docs:
                    continue
                seen_docs.add(key)
                upr_sales += float(s.doc_total)
            else:
                upr_sales += float(s.amount) * (1 - float(s.discount_pct or 0) / 100)

        # Оплаты: плательщик приводится к клиенту через алиасы, как в дебиторке.
        upr_pay = 0.0
        for r in db.query(models.Receipt).filter(
                models.Receipt.organization == uq_org).all():
            if not r.operation.startswith(CUSTOMER_PAYMENT_PREFIX):
                continue
            client = aliases.get(r.payer, r.payer)
            if client in upr_names or r.payer in upr_names:
                upr_pay += float(r.amount_kgs)

        upr_ret = sum(
            float(rd.amount) for rd in db.query(models.ReturnDoc).filter(
                models.ReturnDoc.organization == uq_org).all()
            if aliases.get(rd.client, rd.client) in upr_names
            or rd.client in upr_names
        )

        # --- По товарам: штуки и суммы с обеих сторон ---
        # Названия номенклатуры в контурах пишутся по-разному, поэтому ключ —
        # нормализованное имя (как в расчёте остатков).
        prod: dict = {}

        def cell(name):
            key = _norm_product(name)
            e = prod.get(key)
            if e is None:
                e = prod[key] = {"product": name or "—", "nal_qty": 0.0,
                                 "upr_qty": 0.0, "nal_amount": 0.0,
                                 "upr_amount": 0.0}
            return e

        for op in ops:
            if op.kind == "sale":
                e = cell(op.product)
                e["nal_qty"] += float(op.qty or 0)
                e["nal_amount"] += float(op.amount)
        for s in sales_rows:
            e = cell(s.product)
            e["upr_qty"] += float(s.qty or 0)
            e["upr_amount"] += float(s.amount) * (1 - float(s.discount_pct or 0) / 100)

        products = sorted(
            ({**e,
              "diff_qty": round(e["nal_qty"] - e["upr_qty"], 1),
              "nal_qty": round(e["nal_qty"], 1),
              "upr_qty": round(e["upr_qty"], 1),
              "nal_amount": round(e["nal_amount"], 2),
              "upr_amount": round(e["upr_amount"], 2)}
             for e in prod.values()),
            key=lambda x: (_group_of(x["product"]), _size_of(x["product"]),
                           x["product"] or ""),
        )

        groups.append({
            "name": upr_names[0] if len(upr_names) == 1 else f"{upr_names[0]} +{len(upr_names) - 1}",
            "tax_names": tax_names,
            "upr_names": upr_names,
            "sales": {"nal": round(nal_sales, 2), "upr": round(upr_sales, 2),
                      "diff": round(nal_sales - upr_sales, 2)},
            "qty": {"nal": round(nal_qty, 1), "upr": round(upr_qty, 1),
                    "diff": round(nal_qty - upr_qty, 1)},
            "pay": {"nal": round(nal_pay, 2), "upr": round(upr_pay, 2),
                    "diff": round(nal_pay - upr_pay, 2)},
            "returns": {"nal": round(nal_ret, 2), "upr": round(upr_ret, 2),
                        "diff": round(nal_ret - upr_ret, 2)},
            "products": products,
        })
    groups.sort(key=lambda g: -g["sales"]["upr"])
    return {"groups": groups}


DOCS_CAP = 500


# Окно поиска пары между базами. Документ проводят во второй базе не в тот
# же день: встречаются и 140 дней позже, и 38 дней раньше. Шире брать нельзя —
# одинаковых по количеству отгрузок много, и пары станут случайными.
PAIR_WINDOW = 150


def _doc_rows(rows, number_of, party_of, amount_of, qty_of,
              total_of=None) -> list[dict]:
    """Строки выгрузки сворачиваются в документы.

    Итог документа (СуммаДокумента) повторяется в каждой строке, поэтому он
    не складывается, а запоминается один раз: иначе документ из двух строк
    выглядел бы вдвое дороже, чем он есть.
    """
    acc: dict = {}
    for r in rows:
        key = (number_of(r) or getattr(r, "doc_guid", None) or f"~{r.id}", r.date)
        e = acc.setdefault(key, {
            "date": r.date, "number": number_of(r), "party": party_of(r),
            "amount": 0.0, "qty": 0.0, "total": None})
        e["amount"] += amount_of(r)
        e["qty"] += qty_of(r)
        if total_of is not None:
            t = total_of(r)
            if t is not None:
                e["total"] = t
    out = []
    for e in acc.values():
        if e["total"] is not None:
            e["amount"] = e["total"]
        e.pop("total")
        out.append(e)
    return out


def _upr_docs(db: Session, org: str, kind: str) -> list[dict]:
    """Документы управленки того же вида, что и налоговые.

    Берём только те колонки, из которых собирается документ: на живой базе
    продаж десятки тысяч строк, и поднимать их в память объектами ORM —
    верный способ уронить сервис на маленьком инстансе.
    """
    if kind == "sale":
        rows = db.query(
            models.Sale.id, models.Sale.date, models.Sale.doc_number,
            models.Sale.client, models.Sale.amount, models.Sale.discount_pct,
            models.Sale.qty, models.Sale.doc_total,
        ).filter(models.Sale.organization == org).all()
        # Сумма документа — после скидки: в налоговой она такая же. Где итога
        # нет, складываем строки с учётом процента скидки.
        return _doc_rows(rows, lambda r: r.doc_number, lambda r: r.client,
                         lambda r: (float(r.amount or 0)
                                    * (1 - float(r.discount_pct or 0) / 100)),
                         lambda r: float(r.qty or 0),
                         total_of=lambda r: (float(r.doc_total)
                                             if r.doc_total is not None else None))
    if kind == "return":
        rows = db.query(
            models.ReturnDoc.id, models.ReturnDoc.date,
            models.ReturnDoc.client, models.ReturnDoc.amount,
        ).filter(models.ReturnDoc.organization == org).all()
        return _doc_rows(rows, lambda r: None, lambda r: r.client,
                         lambda r: float(r.amount or 0), lambda r: 0.0)
    if kind == "writeoff":
        rows = db.query(
            models.WriteOff.id, models.WriteOff.date,
            models.WriteOff.doc_number, models.WriteOff.subconto,
            models.WriteOff.qty,
        ).filter(models.WriteOff.organization == org).all()
        return _doc_rows(rows, lambda r: r.doc_number, lambda r: r.subconto,
                         lambda r: 0.0, lambda r: float(r.qty or 0))
    if kind == "purchase":
        rows = db.query(
            models.Purchase.id, models.Purchase.date,
            models.Purchase.doc_number, models.Purchase.supplier,
            models.Purchase.amount_kgs, models.Purchase.qty,
        ).filter(models.Purchase.organization == org).all()
        return _doc_rows(rows, lambda r: r.doc_number, lambda r: r.supplier,
                         lambda r: float(r.amount_kgs or 0),
                         lambda r: float(r.qty or 0))
    return []


def _tax_doc_rows(db: Session, org: str, kind: str) -> list[dict]:
    """Документы налогового контура одного вида, собранные из строк."""
    acc: dict = {}
    for t in db.query(
            models.TaxOperation.date, models.TaxOperation.doc_number,
            models.TaxOperation.counterparty, models.TaxOperation.amount,
            models.TaxOperation.qty, models.TaxOperation.comment,
            models.TaxOperation.doc_guid,
    ).filter(models.TaxOperation.organization == org,
             models.TaxOperation.kind == kind).all():
        key = t.doc_guid or f"{t.doc_number}|{t.date}|{t.counterparty}"
        e = acc.setdefault(key, {
            "date": t.date, "number": t.doc_number, "party": t.counterparty,
            "amount": 0.0, "qty": 0.0, "comment": ""})
        e["amount"] += float(t.amount or 0)
        e["qty"] += float(t.qty or 0)
        if t.comment:
            e["comment"] = t.comment
    return list(acc.values())


def _pair_docs(upr: list[dict], tax: list[dict]) -> tuple[dict, int]:
    """Сопоставляет документы двух баз. Возвращает {индекс налогового: документ
    управленки} и число пар.

    Ключ сверки — количество, а не сумма: штуки в базах одинаковы, а цены
    разные (в налоговой трансфертные — у Байго те же 20 312 шт стоят 7,1 млн
    в управленке и 5,6 млн в налоговой). Дата тоже не совпадает: документ
    проводят во второй базе позже, иногда через месяцы, а иногда раньше —
    поэтому окно широкое и симметричное, а из нескольких кандидатов берётся
    ближайший по дате.
    """
    used_u: set = set()
    pairs: dict = {}

    # 1) По номеру управленки из комментария налогового документа — прямая
    #    ссылка, поставленная руками бухгалтера, сильнее любых догадок.
    by_number: dict = defaultdict(list)
    for i, u in enumerate(upr):
        if u["number"]:
            by_number[u["number"]].append(i)
    for j, t in enumerate(tax):
        for n in _UPR_NUMBER.findall(t.get("comment") or ""):
            free = [i for i in by_number.get(n, []) if i not in used_u]
            if not free:
                continue
            # Номера повторяются по годам — берём ближайший по дате.
            i = min(free, key=lambda i: abs((upr[i]["date"] - t["date"]).days))
            used_u.add(i)
            pairs[j] = upr[i]

    # 2) По количеству, а где количества нет (возвраты приходят документом,
    #    без товарных строк) — по сумме.
    def greedy(field: str, tol: float) -> None:
        index: dict = defaultdict(list)
        for i, u in enumerate(upr):
            if i not in used_u and u[field]:
                index[round(u[field], 3)].append(i)
        cands = []
        for j, t in enumerate(tax):
            if j in pairs or not t[field]:
                continue
            for i in index.get(round(t[field], 3), []):
                dist = abs((upr[i]["date"] - t["date"]).days)
                if dist > PAIR_WINDOW:
                    continue
                # При равном расстоянии по дате вперёд идёт пара, у которой
                # сошлась ещё и сумма.
                cands.append((dist, abs(upr[i]["amount"] - t["amount"]) > 1, i, j))
        cands.sort()
        for _, _, i, j in cands:
            if i in used_u or j in pairs:
                continue
            if abs(upr[i][field] - tax[j][field]) > tol:
                continue
            used_u.add(i)
            pairs[j] = upr[i]

    greedy("qty", 0.001)
    greedy("amount", 1.0)
    return pairs, len(pairs)


def _money_pairs(db: Session, items: list[dict], kind: str, org: str | None) -> None:
    """Пара для денежных операций: у платежа нет количества, ключ — сумма.

    Контрагенты в контурах разные (в налоговой юрлицо, в управленке точка
    или плательщик), поэтому имя ключом быть не может. Сначала ищем среди
    операций связанных контрагентов (связки заданы руками, окно шире), потом
    среди всех — по сумме до копейки и близкой дате.
    """
    uq_org = org or models.DEFAULT_ORG
    if kind == "cash_in":
        cands = [{"date": r.date, "who": r.payer, "currency": r.currency,
                  "amount": round(float(r.amount), 2)}
                 for r in db.query(models.Receipt).filter(
                     models.Receipt.organization == uq_org).all()]
    else:
        cands = [{"date": e.date, "who": e.counterparty, "currency": e.currency,
                  "amount": round(float(e.amount), 2)}
                 for e in db.query(models.Expense).filter(
                     models.Expense.organization == uq_org).all()]

    used: set = set()
    name_links = _links_map(db)

    def scan(item, d0, cur, who, window):
        best, best_days = None, None
        for j, c in enumerate(cands):
            if j in used or (c["currency"] or "KGS") != cur:
                continue
            if who is not None and (c["who"] or "") not in who:
                continue
            if abs(c["amount"] - item["amount"]) >= 0.5:
                continue
            days = abs((c["date"] - d0).days)
            if days > window:
                continue
            if best is None or days < best_days:
                best, best_days = j, days
                if days == 0:
                    break
        return best, best_days

    for item in items:
        d0 = date.fromisoformat(item["date"])
        cur = item.get("currency") or "KGS"
        linked = set(name_links.get(item.get("counterparty") or "", []))
        best, best_days = (None, None)
        if linked:
            best, best_days = scan(item, d0, cur, linked, 14)
        if best is None:
            best, best_days = scan(item, d0, cur, None, 7)
        if best is None:
            item["upr"] = None
            continue
        used.add(best)
        c = cands[best]
        item["upr"] = {"date": c["date"].isoformat(), "who": c["who"],
                       "days": best_days, "amount": c["amount"],
                       "by_link": bool(linked and (c["who"] or "") in linked)}


@router.get("/docs")
def tax_docs(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    kind: str = "sale",
    org: str = "all",
):
    """Реестр операций налогового контура — каждый документ строкой.

    Товарные виды (реализации, возвраты, закупки, списания) собираются из
    строк в документы: номер + дата + контрагент, итоговая сумма и число
    позиций. Денежные — одна строка файла и есть одна операция.

    Пара в управленке ищется только для видов сверки. У закупок, подотчёта и
    склада управленческой пары нет — показываем реестром, без пустой колонки
    «не найдено», которая читалась бы как расхождение."""
    if kind not in KIND_LABEL:
        raise HTTPException(status_code=400, detail="Неизвестный вид операций")
    q = db.query(models.TaxOperation).filter(models.TaxOperation.kind == kind)
    o = models.normalize_org(org) if (org or "").lower() in models.ORGS else None
    if o:
        q = q.filter(models.TaxOperation.organization == o)
    rows = q.all()

    if kind in _GOODS_KINDS:
        docs: dict = {}
        for r in rows:
            key = (r.doc_number, r.date, r.counterparty)
            d = docs.get(key)
            if d is None:
                d = docs[key] = {
                    "date": r.date.isoformat(),
                    "doc_number": r.doc_number,
                    "counterparty": r.counterparty,
                    "warehouse": r.warehouse,
                    "amount": 0.0,
                    "doc_total": None,
                    "positions": 0,
                    "currency": r.currency,
                }
            d["positions"] += 1
            d["amount"] += float(r.amount)
            if r.doc_total is not None:
                d["doc_total"] = float(r.doc_total)
        items = []
        for d in docs.values():
            amt = d.pop("doc_total") or d["amount"]
            items.append({**d, "amount": round(float(amt), 2)})
    else:
        items = [
            {
                "date": r.date.isoformat(),
                "doc_number": r.doc_number,
                "counterparty": r.counterparty,
                "operation": r.operation,
                "amount": round(float(r.amount), 2),
                "currency": r.currency,
            }
            for r in rows
        ]

    items.sort(key=lambda x: (x["date"], x.get("doc_number") or ""), reverse=True)

    if kind not in MATCHED_KINDS:
        # Сверять не с чем: у авансовых отчётов и склада в управленке нет
        # соответствующего контура. Возвращаем чистый реестр.
        for item in items:
            item["upr"] = None
        return {
            "kind": kind, "label": KIND_LABEL[kind], "count": len(items),
            "amount": round(sum(i["amount"] for i in items), 2),
            "matched": None, "unmatched": None, "unmatched_amount": None,
            "items": items[:DOCS_CAP], "cap": DOCS_CAP,
        }

    # --- Пара в управленке для каждой операции ---
    # Товарные документы ищутся тем же способом, что и на странице
    # «Контуры 1С»: по количеству в широком окне. Раньше здесь был свой
    # поиск — по сумме до копейки в окне недели, — и он терял пары там, где
    # в налоговой трансфертная цена или документ проведён месяцем позже: у
    # Инновейва из 193 документов «без пары» оказывалось 22 вместо пяти.
    # Деньги ищутся по-прежнему по сумме: у платежа нет количества, а сумма
    # в контурах одна и та же.
    by_key: dict = {}
    if kind in _GOODS_KINDS:
        for firm in ([o] if o else list(models.ORGS)):
            upr = _upr_docs(db, firm, kind)
            tax = _tax_doc_rows(db, firm, kind)
            pairs, _n = _pair_docs(upr, tax)
            for j, t in enumerate(tax):
                u = pairs.get(j)
                if u is None:
                    continue
                key = (t["number"], t["date"].isoformat(), t["party"])
                by_key[key] = {
                    "date": u["date"].isoformat(), "who": u["party"],
                    "number": u["number"],
                    "days": abs((u["date"] - t["date"]).days),
                    # Сумма второй базы: разница в цене — не ошибка, а
                    # трансфертная наценка, и прятать её незачем.
                    "amount": round(u["amount"], 2),
                }
        for item in items:
            item["upr"] = by_key.get(
                (item["doc_number"], item["date"], item["counterparty"]))
    else:
        _money_pairs(db, items, kind, o)

    matched = sum(1 for i in items if i["upr"])


    unmatched_amount = round(sum(i["amount"] for i in items if not i["upr"]), 2)
    return {
        "kind": kind,
        "label": KIND_LABEL[kind],
        "count": len(items),
        "amount": round(sum(i["amount"] for i in items), 2),
        "matched": matched,
        "unmatched": len(items) - matched,
        "unmatched_amount": unmatched_amount,
        "items": items[:DOCS_CAP],
        "cap": DOCS_CAP,
    }


@router.get("/compare")
def tax_compare(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    org: str = "all",
):
    """Сверка трёх контуров по месяцам: управленка ↔ налоговая ↔ SalesDoc.

    Поклиентно контуры не сопоставить — в налоговой базе продажи проведены на
    юрлица, а в управленке и SalesDoc клиенты — розничные точки. Поэтому
    сверяем агрегатами: выручка и поступления денег по месяцам. Это отвечает
    на два вопроса сразу: какая доля оборота проведена официально (НАЛ/УПР) и
    насколько SalesDoc совпадает с управленкой (Δ SD)."""
    from .receipts import CUSTOMER_PAYMENT_PREFIX
    from ..services import salesdoc as sd

    o = models.normalize_org(org) if (org or "").lower() in models.ORGS else None

    sales: dict[str, dict] = defaultdict(lambda: {"upr": 0.0, "nal": 0.0, "sd": 0.0})
    money: dict[str, dict] = defaultdict(lambda: {"upr": 0.0, "nal": 0.0, "sd": 0.0})

    def mk(d) -> str:
        return f"{d.year:04d}-{d.month:02d}"

    # --- Управленка: продажи документами (итог дока — один раз) ---
    q = db.query(models.Sale)
    if o:
        q = q.filter(models.Sale.organization == o)
    seen_docs: set = set()
    for s in q.all():
        if s.doc_number and s.doc_total is not None:
            key = (s.doc_number, s.date, s.client)
            if key in seen_docs:
                continue
            seen_docs.add(key)
            sales[mk(s.date)]["upr"] += float(s.doc_total)
        else:
            sales[mk(s.date)]["upr"] += float(s.amount) * (
                1 - float(s.discount_pct or 0) / 100)

    # --- Управленка: поступления от покупателей ---
    rq = db.query(models.Receipt)
    if o:
        rq = rq.filter(models.Receipt.organization == o)
    for r in rq.all():
        if r.operation.startswith(CUSTOMER_PAYMENT_PREFIX):
            money[mk(r.date)]["upr"] += float(r.amount_kgs)

    # --- Налоговая ---
    tq = db.query(models.TaxOperation)
    if o:
        tq = tq.filter(models.TaxOperation.organization == o)
    for t in tq.all():
        if t.kind == "sale":
            sales[mk(t.date)]["nal"] += float(t.amount)
        elif t.kind == "cash_in" and "покупател" in (t.operation or "").lower():
            money[mk(t.date)]["nal"] += float(t.amount)

    # --- SalesDoc (зеркало): отгрузки по складам фирмы + оплаты ---
    store_ids = None
    if o:
        rows = [s for s in db.query(models.SalesDocStore).all() if s.store_id]
        mine = {s.store_id.lower() for s in rows if s.organization == o}
        unmapped = {s.store_id.lower() for s in rows if not s.organization}
        store_ids = (mine | unmapped) if mine else None
    oq = db.query(models.SalesDocOrder).filter(
        models.SalesDocOrder.status.in_(sorted(sd.SHIPPED_STATUSES)))
    if store_ids:
        oq = oq.filter(models.SalesDocOrder.store_sd_id.in_(store_ids))
    for r in oq.all():
        if r.date:
            sales[mk(r.date)]["sd"] += float(r.amount or 0)
    for p in db.query(models.SalesDocPayment).filter(
            models.SalesDocPayment.txn == sd.PAYMENT_TXN).all():
        if p.date:
            money[mk(p.date)]["sd"] += float(p.amount or 0)

    def table(agg: dict) -> list[dict]:
        out = []
        for m in sorted(agg, reverse=True):
            v = agg[m]
            out.append({
                "month": m,
                "upr": round(v["upr"], 2),
                "nal": round(v["nal"], 2),
                "sd": round(v["sd"], 2),
                # Доля официально проведённого от управленческого оборота.
                "nal_share": round(v["nal"] / v["upr"] * 100, 1) if v["upr"] else None,
                "sd_diff": round(v["sd"] - v["upr"], 2),
            })
        return out

    def totals(agg: dict) -> dict:
        u = sum(v["upr"] for v in agg.values())
        n = sum(v["nal"] for v in agg.values())
        s_ = sum(v["sd"] for v in agg.values())
        return {"upr": round(u, 2), "nal": round(n, 2), "sd": round(s_, 2),
                "nal_share": round(n / u * 100, 1) if u else None,
                "sd_diff": round(s_ - u, 2)}

    return {
        "org": o or "all",
        # Оплаты SalesDoc по фирмам не делятся идеально (аванс без заказов
        # виден в обеих) — честно предупреждаем в интерфейсе.
        "sales": {"rows": table(sales), "totals": totals(sales)},
        "money": {"rows": table(money), "totals": totals(money)},
    }


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
    # Связки: обороты раздробленных юрлиц собираются под общим именем
    # управленки («Байго Трейд» вместо шести отдельных ИП). Склеиваем только
    # однозначные связки: если у налогового имени несколько управленческих
    # (Императив → точки Алдей), оборот остаётся под налоговым именем —
    # разделить его по точкам не по чему.
    name_links = _links_map(db)

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
                targets = name_links.get(op.counterparty) or []
                name = targets[0] if len(targets) == 1 else op.counterparty
                clients[name] += amt
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


# Номер документа управленки в комментарии налогового: «0000-000760».
_UPR_NUMBER = re.compile(r"\d{4}-\d{6}")


def _pick_twin(dates: dict, tax_doc: dict):
    """Из одноимённых документов управленки выбирает тот, что имеет в виду
    налоговый.

    Номера повторяются по годам — у Хайджина 391 номер из 988 встречается с
    разными датами. Одной близости даты мало: «0000-000061» есть 20.01.2025
    (Булак, 20 шт на 10 710) и 05.02.2026 (Чынар оптомаркет, 100 шт на
    37 170), и до налогового документа от 31.07.2025 второй ближе на три дня.
    По дате выбирался он — и отгрузка Булака показывалась расхождением на
    −80 штук у чужого клиента.

    Поэтому сначала смотрим на деньги и количество: совпавший итог документа
    — куда более сильный признак, чем несколько дней разницы.
    """
    def rank(d):
        u = dates[d]
        total = u["doc_total"] if u["doc_total"] is not None else u["amount"]
        return (
            abs(total - tax_doc["amount"]) > 0.01,   # сумма совпала — вперёд
            abs(u["qty"] - tax_doc["qty"]) > 0.01,   # затем количество
            abs((d - tax_doc["date"]).days),         # и лишь потом дата
        )
    return min(dates, key=rank)


@router.get("/by-comment")
def tax_by_comment(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    org: str = "hygiene",
    only_diff: bool = False,
):
    """Сверка налоговых реализаций с управленческими по номерам из комментария.

    Прямой связи между контурами нет: GUID у баз свои, контрагенты разные —
    в управленке торговая точка, в налоговой юрлицо или ИП. Единственный
    мостик — номер документа управленки, который бухгалтер вписывает в
    комментарий налогового документа.

    Связь не «один к одному»: одну отгрузку управленки в налоговой разбивают
    на несколько документов по разным ИП, а один налоговый документ может
    покрывать несколько управленческих. Поэтому считаем связными группами:
    все налоговые документы, ссылающиеся на общий номер, и все упомянутые
    документы управленки — это одна группа, и сходиться должны её итоги.

    Номера повторяются по годам: «0000-000001» есть и в октябре 2024, и в
    январе 2026. Из одноимённых берём тот, что ближе по дате к налоговому
    документу, — иначе отгрузка на 10,5 млн сверяется с документом на 8 907
    сом, и расхождение выглядит катастрофой на ровном месте.
    """
    o = models.normalize_org(org) if (org or "").lower() in models.ORGS else "hygiene"

    # --- Управленка: документы (номер + дата) с итогами ---
    upr: dict = defaultdict(lambda: defaultdict(
        lambda: {"qty": 0.0, "amount": 0.0, "doc_total": None, "client": None}))
    for s in db.query(models.Sale).filter(models.Sale.organization == o).all():
        if not s.doc_number:
            continue
        e = upr[s.doc_number][s.date]
        e["qty"] += float(s.qty or 0)
        e["amount"] += float(s.amount or 0)
        e["client"] = s.client
        if s.doc_total is not None:
            e["doc_total"] = float(s.doc_total)

    # --- Налоговая: документы реализации с комментарием ---
    tax: dict = defaultdict(lambda: {"qty": 0.0, "amount": 0.0, "date": None,
                                     "number": None, "counterparty": None,
                                     "comment": ""})
    for t in db.query(models.TaxOperation).filter(
            models.TaxOperation.organization == o,
            models.TaxOperation.kind == "sale").all():
        key = t.doc_guid or f"{t.doc_number}|{t.date}|{t.counterparty}"
        e = tax[key]
        e["qty"] += float(t.qty or 0)
        e["amount"] += float(t.amount or 0)
        e["date"] = t.date
        e["number"] = t.doc_number
        e["counterparty"] = t.counterparty
        if t.comment:
            e["comment"] = t.comment

    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    chosen: dict = {}
    no_link = []
    for key, e in tax.items():
        nums = _UPR_NUMBER.findall(e["comment"] or "")
        hit = False
        for n in nums:
            if n not in upr:
                continue
            best = _pick_twin(upr[n], e)
            chosen[(n, best)] = upr[n][best]
            union(("T", key), ("U", (n, best)))
            hit = True
        if not hit:
            no_link.append(e)

    comps: dict = defaultdict(lambda: {"t": set(), "u": set()})
    for node in list(parent):
        comps[find(node)]["t" if node[0] == "T" else "u"].add(node[1])

    groups = []
    for c in comps.values():
        uq = sum(chosen[k]["qty"] for k in c["u"])
        ua = sum((chosen[k]["doc_total"] if chosen[k]["doc_total"] is not None
                  else chosen[k]["amount"]) for k in c["u"])
        nq = sum(tax[g]["qty"] for g in c["t"])
        na = sum(tax[g]["amount"] for g in c["t"])
        dates = [d for _, d in c["u"]]
        groups.append({
            "date": min(dates).isoformat(),
            "upr_numbers": sorted({n for n, _ in c["u"]}),
            "client": next((chosen[k]["client"] for k in c["u"]
                            if chosen[k]["client"]), None),
            "tax_docs": sorted(
                ({"number": str(tax[g]["number"] or ""),
                  "date": tax[g]["date"].isoformat(),
                  "counterparty": tax[g]["counterparty"],
                  "qty": round(tax[g]["qty"], 1),
                  "amount": round(tax[g]["amount"], 2)} for g in c["t"]),
                key=lambda x: x["date"]),
            "upr_qty": round(uq, 1), "tax_qty": round(nq, 1),
            "diff_qty": round(nq - uq, 1),
            "upr_amount": round(ua, 2), "tax_amount": round(na, 2),
            # Разница в цене между контурами — не ошибка, а трансфертная
            # наценка. Показываем процентом, чтобы выбросы были заметны.
            "price_pct": round((na / ua - 1) * 100, 1) if ua else None,
        })
    groups.sort(key=lambda g: g["date"], reverse=True)
    matched = sum(1 for g in groups if abs(g["diff_qty"]) < 1)
    if only_diff:
        groups = [g for g in groups if abs(g["diff_qty"]) >= 1]

    return {
        "org": o,
        "groups": groups,
        "total_groups": len(comps),
        "matched": matched,
        "diff_qty": round(sum(g["diff_qty"] for g in groups), 1),
        # Документы налоговой, в комментарии которых номера управленки нет.
        # Их не с чем сверять — это розница на физлиц («ИНН Иванова») и
        # документы с комментарием вроде «Подгузники».
        "unlinked": len(no_link),
        "unlinked_qty": round(sum(e["qty"] for e in no_link), 1),
    }


@router.get("/unposted")
def tax_unposted(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    org: str = "hygiene",
    limit: int = Query(default=50, le=500),
):
    """Документы, проведённые в одном контуре и не проведённые в другом.

    Две базы 1С живут порознь: одну ведёт управленческий учёт, другую —
    налоговый, и документ попадает во вторую руками, когда бухгалтер до него
    дойдёт. Пока он не дошёл, товар в налоговой числится на складе, деньги —
    не в выручке, и это видно только косвенно: расхождение в остатках, в
    обороте, в ЭСФ. Причину каждый раз ищут раскопками в выгрузках.

    Здесь она названа прямо: по каждому виду документов — что есть в
    управленке и нет в налоговой, и наоборот.

    Пара ищется двумя способами. Для реализаций — по номеру управленки,
    который бухгалтер вписывает в комментарий налогового документа (это
    единственная прямая связь между базами: GUID у баз свои, контрагенты
    разные). Если номера нет — по дате и сумме с допуском: контуры ведут
    один и тот же документ, и совпадение даты с точностью до пары дней и
    суммы до копейки — надёжный признак.

    Отдельно помечается «хвост» — документы свежее последнего документа
    другого контура. Это обычное отставание: бухгалтерия ещё не дошла.
    А вот документ без пары внутри уже закрытого периода — настоящая дыра,
    и именно он должен попадаться на глаза.
    """
    # Фирма берётся из переключателя в шапке. «Обе» — не смесь: базы 1С у
    # фирм разные, и «нет пары» у Хайджина ничего не говорит про Инновейв,
    # поэтому каждая считается отдельно и показывается своим блоком.
    firms = ([models.normalize_org(org)] if (org or "").lower() in models.ORGS
             else list(models.ORGS))

    def block(key: str, label: str, upr: list[dict], tax: list[dict]) -> dict:
        upr_last = max((u["date"] for u in upr), default=None)
        tax_last = max((t["date"] for t in tax), default=None)
        pairs, paired = _pair_docs(upr, tax)
        only_u = [u for u in upr if u not in pairs.values()]
        only_t = [t for j, t in enumerate(tax) if j not in pairs]

        def out(rows, other_last):
            return [{
                "date": r["date"].isoformat(),
                "number": r["number"],
                "party": r["party"],
                "amount": round(r["amount"], 2),
                "qty": round(r["qty"], 3),
                # Свежее последнего документа второй базы — документ просто
                # ждёт очереди, бухгалтерия ещё не дошла. Если во второй базе
                # документов этого вида нет вовсе, она его не ведёт (налоговая,
                # например, не ведёт списания на маркетинг) — тогда и
                # просроченных тут не бывает.
                "tail": bool(other_last is None or r["date"] > other_last),
            } for r in sorted(rows, key=lambda x: x["date"], reverse=True)]

        rows_u, rows_t = out(only_u, tax_last), out(only_t, upr_last)
        # Налоговая ведёт не всё: у Хайджина на 1 539 отгрузок управленки
        # приходится 184 налоговых документа — ЭСФ на юрлиц и сводные.
        # Записывать полторы тысячи документов в пропущенные бессмысленно:
        # это не потери, а устройство учёта. Поэтому «просрочено» считаем
        # только там, где база ведёт документы вида почти целиком.
        FULL, ENOUGH = 0.7, 20
        cover_u = paired / len(upr) if upr else 1.0
        cover_t = paired / len(tax) if tax else 1.0
        # На горстке документов доля ничего не значит: один непарный из трёх
        # даст «покрытие 67%» и молча спрячет настоящий пропуск.
        partial_u = cover_u < FULL and len(upr) >= ENOUGH
        partial_t = cover_t < FULL and len(tax) >= ENOUGH
        gaps_u = [] if partial_u else [r for r in rows_u if not r["tail"]]
        gaps_t = [] if partial_t else [r for r in rows_t if not r["tail"]]
        for rows, partial in ((rows_u, partial_u), (rows_t, partial_t)):
            for r in rows:
                r["gap"] = not r["tail"] and not partial
        return {
            "key": key,
            "label": label,
            "upr_last": upr_last.isoformat() if upr_last else None,
            "tax_last": tax_last.isoformat() if tax_last else None,
            "upr_docs": len(upr),
            "tax_docs": len(tax),
            "paired": paired,
            # Доля документов, у которых пара нашлась. Низкая — база ведёт
            # лишь часть, и список «без пары» справочный, а не тревожный.
            "cover_upr": round(cover_u * 100),
            "cover_tax": round(cover_t * 100),
            "partial_upr": partial_u,
            "partial_tax": partial_t,
            # База вообще не ведёт этот вид документов — расхождение
            # структурное, а не потерянный документ.
            "upr_absent": not upr,
            "tax_absent": not tax,
            "only_upr": rows_u[:limit],
            "only_tax": rows_t[:limit],
            "only_upr_count": len(rows_u),
            "only_tax_count": len(rows_t),
            "gaps_upr": len(gaps_u),
            "gaps_tax": len(gaps_t),
            "gaps_upr_amount": round(sum(r["amount"] for r in gaps_u), 2),
            "gaps_tax_amount": round(sum(r["amount"] for r in gaps_t), 2),
        }

    LABELS = {"sale": "Реализации", "return": "Возвраты от покупателей",
              "writeoff": "Списания", "purchase": "Поступления товаров"}

    def firm_types(o: str) -> list[dict]:
        return [block(k, label, _upr_docs(db, o, k), _tax_doc_rows(db, o, k))
                for k, label in LABELS.items()]

    out_firms = []
    for o in firms:
        out_firms.append({"org": o, "types": firm_types(o)})
    return {
        "firms": out_firms,
        # Сумма дыр по всем фирмам и видам — одно число для «Контроля».
        "gaps": sum(t["gaps_upr"] + t["gaps_tax"]
                    for f in out_firms for t in f["types"]),
        "tail": sum(t["only_upr_count"] - t["gaps_upr"]
                    + t["only_tax_count"] - t["gaps_tax"]
                    for f in out_firms for t in f["types"]),
    }


def scan_contour_events(db: Session) -> dict:
    """Заносит в журнал расхождения контуров: новые — заводит, ушедшие —
    закрывает.

    Сверка сама по себе показывает только «сейчас»: документ без пары
    сегодня есть, завтра бухгалтер провёл его во второй базе — и он исчез,
    будто ничего не было. Для «ничего не забываем» этого мало, поэтому
    каждое расхождение живёт отдельной записью: замечено такого-то числа,
    закрылось такого-то. Повтор той же истории виден как повтор.

    Считается по всем фирмам сразу — журнал общий, а фильтруют его при
    показе.
    """
    from datetime import datetime

    data = tax_unposted(db=db, _=None, org="all", limit=100000)
    now = datetime.utcnow()
    seen: set[str] = set()
    added = closed = 0
    existing = {e.key: e for e in db.query(models.ContourEvent).all()}

    for firm in data["firms"]:
        o = firm["org"]
        for t in firm["types"]:
            for side, rows in (("upr", t["only_upr"]), ("tax", t["only_tax"])):
                # Порядок фиксируем: по нему нумеруются одинаковые документы,
                # и при следующем пересчёте нумерация должна получиться той
                # же, иначе события заводились бы заново каждый раз.
                rows = sorted(rows, key=lambda x: (
                    x["date"], str(x["number"] or ""), str(x["party"] or ""),
                    x["qty"], x["amount"]))
                repeats: dict[str, int] = defaultdict(int)
                for r in rows:
                    # Ключ документа: номер бывает пустым (у возвратов его
                    # нет вовсе), поэтому в ключ идут ещё дата и суммы. Но и
                    # этого мало: два возврата одному клиенту в один день на
                    # одну сумму — обычное дело, и такие документы разводятся
                    # порядковым номером. Первый остаётся без суффикса, чтобы
                    # уже заведённые события не потерялись.
                    base = "|".join([
                        o, t["key"], side, r["date"], str(r["number"] or ""),
                        str(r["party"] or ""), f'{r["qty"]:.3f}',
                        f'{r["amount"]:.2f}',
                    ])
                    repeats[base] += 1
                    key = base if repeats[base] == 1 else f"{base}#{repeats[base]}"
                    seen.add(key)
                    e = existing.get(key)
                    if e is None:
                        db.add(models.ContourEvent(
                            key=key, organization=o, kind=t["key"], side=side,
                            doc_date=date.fromisoformat(r["date"]),
                            doc_number=r["number"], party=r["party"],
                            qty=r["qty"], amount=r["amount"],
                            gap=bool(r.get("gap")),
                            first_seen=now, last_seen=now))
                        added += 1
                    else:
                        e.last_seen = now
                        # Документ снова без пары — событие снова открыто.
                        e.resolved_at = None
                        e.gap = bool(r.get("gap"))

    for key, e in existing.items():
        if key not in seen and e.resolved_at is None:
            # Пара нашлась — расхождение закрылось само.
            e.resolved_at = now
            closed += 1
    try:
        db.commit()
    except IntegrityError as err:
        # Журнал — вспомогательная вещь: его сбой не должен ни ронять
        # страницу, ни мешать приёму файлов.
        db.rollback()
        print(f"[contours] журнал не записан: {err}", flush=True)
        return {"added": 0, "closed": 0, "error": "не удалось записать журнал"}
    open_gaps = (db.query(models.ContourEvent)
                 .filter(models.ContourEvent.resolved_at.is_(None),
                         models.ContourEvent.acked_at.is_(None),
                         models.ContourEvent.gap.is_(True)).count())
    return {"added": added, "closed": closed, "open_gaps": open_gaps}


@router.post("/contour-events/scan")
def contour_events_scan(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
):
    """Пересчитать журнал расхождений (обычно вызывается после импорта)."""
    return scan_contour_events(db)


@router.get("/contour-events")
def contour_events(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    org: str = "all",
    state: str = Query(default="open", pattern="^(open|resolved|acked|all)$"),
    limit: int = Query(default=200, le=1000),
):
    """Журнал расхождений: что заметили, когда и чем закончилось."""
    q = db.query(models.ContourEvent)
    o = (org or "").strip().lower()
    if o in models.ORGS:
        q = q.filter(models.ContourEvent.organization == o)
    if state == "open":
        q = q.filter(models.ContourEvent.resolved_at.is_(None),
                     models.ContourEvent.acked_at.is_(None))
    elif state == "resolved":
        q = q.filter(models.ContourEvent.resolved_at.isnot(None))
    elif state == "acked":
        q = q.filter(models.ContourEvent.acked_at.isnot(None))
    rows = (q.order_by(models.ContourEvent.first_seen.desc(),
                       models.ContourEvent.id.desc()).limit(limit).all())
    return {
        "rows": [{
            "id": e.id,
            "organization": e.organization,
            "kind": e.kind,
            "side": e.side,
            "date": e.doc_date.isoformat(),
            "number": e.doc_number,
            "party": e.party,
            "qty": float(e.qty or 0),
            "amount": float(e.amount or 0),
            "gap": bool(e.gap),
            "first_seen": e.first_seen.isoformat(),
            "resolved_at": e.resolved_at.isoformat() if e.resolved_at else None,
            "acked_at": e.acked_at.isoformat() if e.acked_at else None,
            "note": e.note,
        } for e in rows],
        "open_gaps": (db.query(models.ContourEvent)
                      .filter(models.ContourEvent.resolved_at.is_(None),
                              models.ContourEvent.acked_at.is_(None),
                              models.ContourEvent.gap.is_(True)).count()),
        "open_total": (db.query(models.ContourEvent)
                       .filter(models.ContourEvent.resolved_at.is_(None),
                               models.ContourEvent.acked_at.is_(None)).count()),
    }


@router.post("/contour-events/{event_id}/ack")
def contour_event_ack(
    event_id: int,
    db: Session = Depends(get_db),
    current: models.User = Depends(can_edit),
):
    """«Это норма» — событие уходит из открытых, но остаётся в журнале."""
    from datetime import datetime

    e = db.query(models.ContourEvent).get(event_id)
    if e is None:
        raise HTTPException(status_code=404, detail="Событие не найдено")
    e.acked_at = datetime.utcnow()
    e.acked_by = current.id
    db.commit()
    return {"status": "ok"}


# --- Товарные движения обоих контуров, по видам и по размерам ---------------
#
# Остаток товара — это не одна цифра, а результат всех движений: поступило,
# оприходовали, вернули покупатели, продали, списали, вернули поставщику,
# переместили. Пока хоть одно звено в двух контурах разное, остатки не
# сойдутся, а по итоговой сумме не видно, какое именно звено разъехалось.
# Здесь оба контура считаются одинаково — по штукам, по каждой позиции с её
# размером, — и сразу видно, где именно и на сколько штук расхождение.
#
# Управленческую сторону двух видов портал не получает вовсе: инвентаризация
# в 1С проводок не делает (излишки идут оприходованием, недостачи списанием),
# а возврат поставщику выгружается только по налоговому контуру. Такие виды
# помечаются отдельно, а не показываются как расхождение на весь объём.
GOODS_FLOW: tuple[tuple[str, str, int, str | None], ...] = (
    ("purchase", "Поступления", 1, "Purchase"),
    ("stock_in", "Оприходования", 1, "StockReceipt"),
    ("return", "Возвраты от покупателей", 1, "ReturnLine"),
    ("sale", "Реализации", -1, "Sale"),
    ("writeoff", "Списания", -1, "WriteOff"),
    ("return_supplier", "Возвраты поставщикам", -1, None),
    ("transfer", "Перемещения", 0, "StockTransfer"),
    ("inventory", "Инвентаризация", 0, None),
)

# Счета товаров для перепродажи. Бензин, дизтопливо и мебель приходуются тем
# же документом «Поступление товары», но на счета материалов и МБП — по
# количеству от подгузников их не отличить, только по счёту.
_GOODS_ACCOUNTS = ("161", "162", "163", "164")


def _goods_account_ok(account) -> bool:
    """Счёт пустой — берём (у части выгрузок его нет вовсе)."""
    acc = str(account or "").strip()
    return not acc or acc.startswith(_GOODS_ACCOUNTS)


@router.get("/goods-flow")
def goods_flow(
    db: Session = Depends(get_db),
    _: models.User = Depends(get_current_user),
    org: str = Query(default="all"),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    min_diff: float = Query(default=0.001, description="Порог значимой разницы, шт"),
):
    """Управленка ↔ налоговая по всем товарным движениям, в штуках и размерах.

    Одна строка верхнего уровня — вид движения; внутри — позиции с размерами.
    Считаем только количество: цены между контурами трансфертные и совпадать
    не обязаны, а штуки обязаны совпасть всегда.

    Итог внизу — расчётный остаток по движениям каждого контура: приход минус
    расход. Он и отвечает на вопрос «бьются ли остатки»: пока список видов
    чист, остаток сойдётся сам.
    """
    from .purchases import _group_of, _norm_product, _size_of

    o = models.normalize_org(org) if (org or "").lower() in models.ORGS else None

    # Ключ сопоставления — нормализованное имя: в двух базах одну и ту же
    # позицию пишут по-разному, а после нормализации имена совпадают.
    def bucket(store: dict, name: str | None) -> dict:
        key = _norm_product(name)
        e = store.get(key)
        if e is None:
            e = store[key] = {"product": name or "—", "upr": 0.0, "nal": 0.0}
        elif name and (e["product"] == "—" or len(name) < len(e["product"])):
            # Держим самое короткое написание: оно обычно и есть каноничное.
            e["product"] = name
        return e

    def in_range(d) -> bool:
        if d is None:
            return False
        if date_from and d < date_from:
            return False
        if date_to and d > date_to:
            return False
        return True

    # --- Управленка: по одной таблице на вид, только нужные колонки ---
    upr_models = {
        "Purchase": models.Purchase, "StockReceipt": models.StockReceipt,
        "ReturnLine": models.ReturnLine, "Sale": models.Sale,
        "WriteOff": models.WriteOff, "StockTransfer": models.StockTransfer,
    }
    per_kind: dict[str, dict] = {k: {} for k, *_ in GOODS_FLOW}
    # Охват выгрузки по каждому виду и контуру: сколько строк и за какой
    # период. Без него не отличить «в налоговой так не проводят» от «файл
    # перестал приходить»: и там и там просто мало штук, а решения нужны
    # разные. Оборвавшийся период виден сразу.
    span: dict[tuple[str, str], dict] = {}

    def touch(kind: str, side: str, d) -> None:
        e = span.get((kind, side))
        if e is None:
            e = span[(kind, side)] = {"rows": 0, "first": None, "last": None}
        e["rows"] += 1
        if d is not None:
            if e["first"] is None or d < e["first"]:
                e["first"] = d
            if e["last"] is None or d > e["last"]:
                e["last"] = d

    for kind, _label, _sign, model_name in GOODS_FLOW:
        model = upr_models.get(model_name or "")
        if model is None:
            continue
        has_account = hasattr(model, "account")
        cols = [model.date, model.product, model.qty]
        if has_account:
            cols.append(model.account)
        q = db.query(*cols)
        if o:
            q = q.filter(model.organization == o)
        if date_from:
            q = q.filter(model.date >= date_from)
        if date_to:
            q = q.filter(model.date <= date_to)
        for row in q.all():
            if has_account and not _goods_account_ok(row[3]):
                continue
            bucket(per_kind[kind], row[1])["upr"] += float(row[2] or 0)
            touch(kind, "upr", row[0])

    # --- Налоговая: одна таблица на все виды ---
    tq = db.query(models.TaxOperation.kind, models.TaxOperation.date,
                  models.TaxOperation.product, models.TaxOperation.qty,
                  models.TaxOperation.account, models.TaxOperation.source)
    if o:
        tq = tq.filter(models.TaxOperation.organization == o)
    tq = tq.filter(models.TaxOperation.kind.in_([k for k, *_ in GOODS_FLOW]))
    if date_from:
        tq = tq.filter(models.TaxOperation.date >= date_from)
    if date_to:
        tq = tq.filter(models.TaxOperation.date <= date_to)
    for kind, _d, product, qty, account, source in tq.all():
        if not product:
            continue
        # «Поступление услуги» — не товар, а «Поступление доп расходов»
        # перечисляет ТЕ ЖЕ товары второй раз, чтобы разнести на них таможню.
        src = (source or "").lower()
        if "услуг" in src or "доп расход" in src:
            continue
        if not _goods_account_ok(account):
            continue
        bucket(per_kind[kind], product)["nal"] += float(qty or 0)
        touch(kind, "nal", _d)

    # --- Сборка ответа ---
    stock: dict = {}
    kinds_out = []
    for kind, label, sign, model_name in GOODS_FLOW:
        store = per_kind[kind]
        products = []
        upr_total = nal_total = 0.0
        for e in store.values():
            upr, nal = round(e["upr"], 3), round(e["nal"], 3)
            if abs(upr) < 0.001 and abs(nal) < 0.001:
                continue
            upr_total += upr
            nal_total += nal
            products.append({
                "product": e["product"],
                "upr": round(upr, 1), "nal": round(nal, 1),
                "diff": round(nal - upr, 1),
            })
            # В расчётный остаток идут только виды, которые есть в обоих
            # контурах. Возврат поставщику выгружается лишь по налоговой, и
            # если считать его, разница окажется не расхождением учёта, а
            # следствием отсутствующей выгрузки.
            if sign and model_name:
                s = bucket(stock, e["product"])
                s["upr"] += sign * upr
                s["nal"] += sign * nal
        products.sort(key=lambda x: (_group_of(x["product"]),
                                     _size_of(x["product"]),
                                     x["product"] or ""))
        def cover(side: str) -> dict:
            e = span.get((kind, side)) or {"rows": 0, "first": None, "last": None}
            return {
                "rows": e["rows"],
                "first": e["first"].isoformat() if e["first"] else None,
                "last": e["last"].isoformat() if e["last"] else None,
            }

        upr_cover, nal_cover = cover("upr"), cover("nal")
        kinds_out.append({
            "kind": kind,
            "label": label,
            # Строки и период с каждой стороны — чем именно объясняется
            # разница: в базе так не проводят или выгрузка оборвалась.
            "upr_cover": upr_cover,
            "nal_cover": nal_cover,
            # Знак движения для остатка: приход, расход или внутреннее.
            "sign": sign,
            "upr": round(upr_total, 1),
            "nal": round(nal_total, 1),
            "diff": round(nal_total - upr_total, 1),
            # Контур, которого у этого вида нет вовсе: сравнивать не с чем,
            # и показывать полный объём как расхождение было бы враньём.
            "upr_absent": model_name is None,
            "nal_absent": abs(nal_total) < 0.001 and abs(upr_total) >= 0.001,
            "mismatch": sorted(
                (p for p in products if abs(p["diff"]) >= min_diff),
                key=lambda x: -abs(x["diff"]),
            ) if model_name else [],
            "products": products,
        })

    stock_rows = []
    for e in stock.values():
        upr, nal = round(e["upr"], 1), round(e["nal"], 1)
        if abs(upr) < 0.001 and abs(nal) < 0.001:
            continue
        stock_rows.append({"product": e["product"], "upr": upr, "nal": nal,
                           "diff": round(nal - upr, 1)})
    stock_rows.sort(key=lambda x: (_group_of(x["product"]),
                                   _size_of(x["product"]),
                                   x["product"] or ""))

    matched = [k for k in kinds_out if not k["upr_absent"]]
    return {
        "org": o or "all",
        "from": date_from.isoformat() if date_from else None,
        "to": date_to.isoformat() if date_to else None,
        "kinds": kinds_out,
        # Расчётный остаток по движениям каждого контура: приход минус расход.
        # Виды без управленческого контура в него не идут — иначе разница
        # была бы не расхождением, а следствием отсутствующей выгрузки.
        "stock": {
            "upr": round(sum(r["upr"] for r in stock_rows), 1),
            "nal": round(sum(r["nal"] for r in stock_rows), 1),
            "diff": round(sum(r["diff"] for r in stock_rows), 1),
            "rows": stock_rows,
            "mismatch": sorted((r for r in stock_rows
                                if abs(r["diff"]) >= min_diff),
                               key=lambda x: -abs(x["diff"])),
        },
        # Движения, которых в управленке нет вовсе: в остаток не вошли, но
        # знать о них надо — на складе они товар двигали.
        # Инвентаризация сюда не идёт: она товар не двигает, а фиксирует
        # факт — движение делают созданные по ней оприходование и списание.
        "outside": [{"kind": k["kind"], "label": k["label"], "nal": k["nal"]}
                    for k in kinds_out
                    if k["upr_absent"] and k["sign"] and abs(k["nal"]) >= 0.001],
        "kinds_with_diff": sum(1 for k in matched if abs(k["diff"]) >= min_diff),
        "positions_with_diff": sum(len(k["mismatch"]) for k in matched),
    }
