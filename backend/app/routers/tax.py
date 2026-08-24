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

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import or_
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
MATCHED_KINDS = ("sale", "return", "cash_in", "cash_out")

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

    # --- Пара в управленке для каждой операции ---
    # Контрагенты в контурах разные (в налоговой — юрлица, в управленке —
    # точки), поэтому имя ключом быть не может. Пара ищется по сумме (до
    # копеек) и близкой дате: сначала день в день, потом в пределах недели —
    # проводки в налоговую базу часто заносятся с отставанием.
    def upr_candidates() -> list[dict]:
        uq_org = o or "hygiene"  # налоговый контур пока только по Hygiene
        if kind == "sale":
            docs: dict = {}
            for s in db.query(models.Sale).filter(
                    models.Sale.organization == uq_org).all():
                key = (s.doc_number or f"~{s.client}", s.date, s.client)
                d = docs.setdefault(key, {"date": s.date, "who": s.client,
                                          "amount": 0.0, "doc_total": None,
                                          "currency": "KGS"})
                d["amount"] += float(s.amount) * (1 - float(s.discount_pct or 0) / 100)
                if s.doc_total is not None:
                    d["doc_total"] = float(s.doc_total)
            out = []
            for d in docs.values():
                out.append({"date": d["date"], "who": d["who"], "currency": "KGS",
                            "amount": round(float(d["doc_total"] or d["amount"]), 2)})
            return out
        if kind == "return":
            return [{"date": r.date, "who": r.client, "currency": r.currency,
                     "amount": round(float(r.amount), 2)}
                    for r in db.query(models.ReturnDoc).filter(
                        models.ReturnDoc.organization == uq_org).all()]
        if kind == "cash_in":
            return [{"date": r.date, "who": r.payer, "currency": r.currency,
                     "amount": round(float(r.amount), 2)}
                    for r in db.query(models.Receipt).filter(
                        models.Receipt.organization == uq_org).all()]
        return [{"date": e.date, "who": e.counterparty, "currency": e.currency,
                 "amount": round(float(e.amount), 2)}
                for e in db.query(models.Expense).filter(
                    models.Expense.organization == uq_org).all()]

    if kind not in MATCHED_KINDS:
        # Сверять не с чем: у закупок, авансовых отчётов и склада в управленке
        # нет соответствующего контура. Возвращаем чистый реестр.
        for item in items:
            item["upr"] = None
        return {
            "kind": kind, "label": KIND_LABEL[kind], "count": len(items),
            "amount": round(sum(i["amount"] for i in items), 2),
            "matched": None, "unmatched": None, "unmatched_amount": None,
            "items": items[:DOCS_CAP], "cap": DOCS_CAP,
        }

    cands = upr_candidates()
    used: set = set()
    # Связки контрагентов НАЛ ↔ УПР: для связанных имя становится ключом
    # сверки — сначала ищем пару только среди операций связанных контрагентов
    # (окно шире, две недели), и лишь потом среди всех по сумме и дате.
    # Связанных имён может быть несколько (Императив → все точки Алдей).
    name_links = _links_map(db)

    def scan(item: dict, d0, cur, who: set | None, window: int):
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

    def find_pair(item: dict):
        d0 = date.fromisoformat(item["date"])
        cur = item.get("currency") or "KGS"
        linked = set(name_links.get(item.get("counterparty") or "", []))
        best, best_days = (None, None)
        if linked:
            best, best_days = scan(item, d0, cur, linked, 14)
        if best is None:
            best, best_days = scan(item, d0, cur, None, 7)
        if best is None:
            return None
        used.add(best)
        c = cands[best]
        return {"date": c["date"].isoformat(), "who": c["who"], "days": best_days,
                "by_link": bool(linked and (c["who"] or "") in linked)}

    matched = 0
    for item in items:
        pair = find_pair(item)
        item["upr"] = pair
        if pair:
            matched += 1

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
