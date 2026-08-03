"""Автоприём выгрузок 1С, которые Google Apps Script пушит из папки Drive.

Скрипт (docs/AUTOSYNC.md) раз в N минут отправляет новые/изменённые файлы
на POST /integrations/inbox с токеном. Тип файла определяется по заголовкам
колонок, дубли отсекает построчная дедупликация импортёров.
"""

import hashlib
import io
import secrets
import traceback

import openpyxl
from fastapi import APIRouter, Depends, Form, Header, HTTPException, UploadFile
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..database import get_db
from ..deps import require_roles
from ..security import hash_password
from .balances import (
    import_cash_balances_workbook,
    import_stock_balances_workbook,
)
from .expenses import import_expenses_workbook
from .receipts import import_receipts_workbook
from .returns import import_return_lines_workbook, import_returns_workbook
from .sales import (
    _row_hash,
    import_sales_docs_workbook,
    import_sales_workbook,
    parse_sales_workbook,
)

router = APIRouter(prefix="/integrations", tags=["integrations"])

ROBOT_EMAIL = "robot@innowave.portal"


def _recover_filename(name: str) -> str:
    """Чинит имя файла, если оно пришло «моджибейком» (UTF-8 байты,
    прочитанные как latin-1) — типичная беда multipart-загрузок с
    кириллицей. Для корректных кириллических имён encode('latin-1')
    падает → возвращаем как есть.
    """
    try:
        return name.encode("latin-1").decode("utf-8")
    except (UnicodeError, AttributeError):
        return name


def _require_token(authorization: str | None) -> None:
    if not settings.inbox_token:
        raise HTTPException(
            status_code=503,
            detail="Автоприём выключен: не задан INBOX_TOKEN на сервере",
        )
    # Сравниваем байты, а не строки: secrets.compare_digest на str требует
    # чистого ASCII и на любом другом символе бросает TypeError — запрос падал
    # с «500 Внутренняя ошибка» вместо честного «неверный токен», из-за чего
    # причина выглядела как сбой разбора файла.
    expected = f"Bearer {settings.inbox_token}"
    ok = bool(authorization) and secrets.compare_digest(
        (authorization or "").encode("utf-8"), expected.encode("utf-8")
    )
    if not ok:
        raise HTTPException(
            status_code=401,
            detail="Неверный токен автоприёма: проверьте TOKEN в скрипте "
                   "Google Apps Script и INBOX_TOKEN на сервере (частая "
                   "причина — лишний пробел или кириллическая буква при "
                   "копировании).",
        )


def _robot_user(db: Session) -> models.User:
    """Служебный пользователь для журнала автозагрузок (вход запрещён)."""
    user = db.query(models.User).filter_by(email=ROBOT_EMAIL).first()
    if user is None:
        user = models.User(
            email=ROBOT_EMAIL,
            full_name="Автозагрузка (Drive)",
            role=models.Role.viewer,
            is_active=False,
            hashed_password=hash_password(secrets.token_urlsafe(24)),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


# Каноничный файл на каждый тип данных. У некоторых выгрузок есть несколько
# вариантов (Реал/Реал2, Возв/ТовВозв) — это разные форматы одних и тех же
# данных, которые тестировали с 1С. Грузим ТОЛЬКО каноничный, дубли-варианты
# явно пропускаем, чтобы не задваивать выручку/возвраты.
# Чтобы сменить каноничный файл — поправьте условия ниже.
def classify_by_name(filename: str, org: str = models.DEFAULT_ORG) -> str | None:
    """Тип выгрузки по имени файла — самый надёжный сигнал. None → sniff_kind.

    Каноничные файлы зависят от организации: у Hygiene реализация = Реал2, а
    возвраты — построчный ТовВозв; у Innowave выгружается только Реал (без «2»)
    и документный Возв. Поэтому логика ветвится по org.
    """
    name = (filename or "").lower()
    if name.startswith("~$"):
        return "ignore"  # временный файл Excel

    def has(*tokens: str) -> bool:
        return any(t in name for t in tokens)

    # Налоговый контур портал пока не ведёт. Файлы по схеме
    # «[Фирма][NAL][Тип]» нельзя смешивать с управленческими: имя
    # «…[NAL][Реализация…]» иначе распозналось бы как обычная реализация и
    # задвоило бы данные. Пропускаем осознанно, с внятной причиной в ответе.
    # Короткую метку ловим только в скобках: голое «nal» встречается внутри
    # обычных слов (analiz, nalichnie) и как подстрока опасно.
    if has("налог", "nalog", "[nal]", "[нал]", "_nal_", "_нал_"):
        return "tax_skip"

    # --- Новая схема имён: «Фирма_Управленка_ТипВыгрузки» полными словами ---
    # Понимаем кириллицу и транслит. Эти правила стоят РАНЬШЕ старых коротких
    # токенов, и это важно: «Реализация» иначе попала бы в правило «реал»,
    # которое для Hygiene считает файл без «2» дублем и молча пропускает.
    if has("реализац", "realizac"):
        # Формат (построчный/документный) импортёр продаж определяет сам
        # по содержимому — одно имя работает для обеих фирм.
        return "sales"
    if has("возврат", "vozvrat"):
        # «Возврат товаров ПОСТАВЩИКУ» — это закупки, портал их не ведёт;
        # клиентский возврат в 1С называется «…от покупателя».
        if has("поставщик", "postavshik", "postavshchik"):
            return "unsupported"
        # У Hygiene возвраты выгружаются построчно, у Innowave — документами.
        return "return_docs" if org == "innowave" else "return_lines"
    if has("остатк", "ostatk"):
        # «Остатки …»: деньги, если названы деньги/банк/касса, иначе товары.
        if has("денег", "денежн", "deneg", "denejn", "банк", "bank", "касс", "kass"):
            return "cash_balances"
        return "stock_balances"
    # «Поступление ТОВАРОВ» — закупка у поставщика, а не деньги. Правило стоит
    # раньше денежного «поступления», иначе закупки грузились бы как оплаты.
    if has("поступлен", "postuplen") and has("товар", "tovar"):
        return "unsupported"
    # Виды, для которых импортёров пока нет. Ловим по имени осознанно, а не
    # отдаём угадыванию по колонкам: «Оприходование» содержит «приход» и без
    # этого правила уехало бы в денежные поступления. Блок стоит раньше правил
    # поступлений/расходов именно из-за таких пересечений.
    if has("оприходован", "oprihodovan",
           "списан", "spisan",
           "перемещен", "peremeshen", "peremeshch",
           "инвентариз", "inventariz",
           "корректировк", "korrektirovk",
           "взаимозач", "vzaimozach",
           "счет на оплату", "счёт на оплату", "schet na oplatu",
           "контрагент", "kontragent",
           "номенклатур", "nomenklatur",
           "оборотно", "oborotno"):
        return "unsupported"
    # Поступления денег: «Платёжное поручение ВХОДЯЩЕЕ» (банк) и «ПРИХОДНЫЙ
    # кассовый ордер» (касса). Проверяется раньше расходов: слово «платёжное»
    # есть в обоих поручениях, направление решают «входящее»/«исходящее».
    if has("входящ", "vhodyash", "vkhodyash", "приход", "prihod",
           "поступлен", "postuplen"):
        return "receipts"  # банк или касса — решается ниже по слову в имени
    if has("исходящ", "ishodyash", "расход", "rashod",
           "платеж", "платёж", "platej", "poruchenie"):
        return "expense"  # банк или касса — решается ниже по слову в имени

    # --- Продажи ---
    if "реал" in name:
        if org == "innowave":
            return "sales"  # у Innowave каноничен обычный Реал
        return "sales" if "реал2" in name else "dup_sales"

    # --- Возвраты ---
    if "товвозв" in name:
        return "return_lines"
    if "возв" in name:
        # У Innowave нет построчного ТовВозв — значит документный Возв каноничен
        return "return_docs" if org == "innowave" else "dup_returns"

    # --- Остальные типы (по одному файлу) ---
    if "банккасса" in name:
        return "cash_balances"
    if "пписход" in name or "рко" in name:
        return "expense"
    if "банквх" in name or "пко" in name:
        return "receipts"
    if "ост" in name and "прост" not in name:
        return "stock_balances"
    return None


def _is_line_sales(content: bytes) -> bool:
    """True — реализация построчная (есть «НоменклатураНаименование»), False —
    документная (Дата/Сумма/Контрагент). Определяет, каким импортёром грузить."""
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except Exception:
        return True
    ws = wb[wb.sheetnames[0]]
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i >= 20:
            break
        if any(c is not None and str(c).strip() == "НоменклатураНаименование" for c in row):
            return True
    return False


def sniff_kind(content: bytes) -> str:
    """Определяет тип выгрузки по заголовкам колонок в первых 20 строках."""
    try:
        wb = openpyxl.load_workbook(
            io.BytesIO(content), data_only=True, read_only=True
        )
    except Exception:
        return "not_excel"
    ws = wb[wb.sheetnames[0]]
    first_rows: list[set] = []
    all_text = ""
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i >= 20:
            break
        cells = {str(c).strip() for c in row if c is not None}
        first_rows.append(cells)
        all_text += " " + " ".join(cells)

    # Возвраты выгружаются с теми же колонками, что продажи — отличаем по
    # служебной строке 1С «Запрос: Документ.ВозвратТоваров…» или по слову
    # «Возврат» в шапке. Это спасает выручку от чужих строк.
    is_return_marked = "Возврат" in all_text

    for cells in first_rows:
        if "НоменклатураНаименование" in cells and "Сумма" in cells:
            return "return_lines" if is_return_marked else "sales"
        if {"Дата", "Сумма", "Контрагент", "ВидОперации"} <= cells:
            return "receipts"
        if "Основание" in cells or {"Документ", "Номер"} <= cells:
            return "expense"  # РКО/платёжки — импортёр в разработке
        if "Касса_Банк" in cells and "СуммаОстаток" in cells:
            return "cash_balances"
        if "СуммаОстаток" in cells and "КоличествоОстаток" in cells:
            return "stock_balances"
        if {"Дата", "Сумма", "Валюта", "Контрагент"} <= cells:
            return "return_docs"
    return "unknown"


@router.post("/inbox")
async def inbox(
    file: UploadFile,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
    fname: str | None = Form(default=None),
    org: str = Form(default=models.DEFAULT_ORG),
):
    _require_token(authorization)

    org = models.normalize_org(org)
    content = await file.read()
    # Имя из поля формы (fname) — приходит корректным UTF-8, в отличие от
    # имени в заголовке multipart, где кириллица портится. Заголовок —
    # запасной вариант с попыткой восстановления.
    filename = fname or _recover_filename(file.filename or "file.xlsx")
    # Имя файла — первичный сигнал; колонки — запасной для незнакомых имён.
    kind = classify_by_name(filename, org) or sniff_kind(content)
    if kind == "ignore":
        return {"type": "ignore", "status": "skipped", "detail": "Временный файл"}
    if kind == "tax_skip":
        return {
            "type": kind,
            "status": "skipped",
            "detail": "Налоговая выгрузка: портал ведёт только управленческий "
                      "контур — файл пропущен осознанно, данные не задвоены",
        }
    if kind == "unsupported":
        return {
            "type": kind,
            "status": "skipped",
            "detail": "Этот вид выгрузки портал пока не ведёт — файл пропущен "
                      "осознанно и данные не искажает; когда добавим "
                      "поддержку, начнём загружать автоматически",
        }
    if kind in ("dup_sales", "dup_returns"):
        # Дубль-вариант выгрузки (напр. старый Реал при наличии Реал2) —
        # не грузим, чтобы не задваивать данные.
        return {
            "type": kind,
            "status": "skipped",
            "detail": "Дубль-вариант выгрузки — грузится каноничный файл",
        }
    robot = _robot_user(db)

    # Раньше здесь стоял короткий выход «файл уже обработан» по хэшу
    # содержимого. Он мешал переразложить файлы после смены логики
    # маршрутизации, поэтому убран: все импортёры идемпотентны — снапшоты
    # (остатки) грузятся заменой, продажи/оплаты/расходы/возвраты
    # дедуплицируются построчно или заменой периода. Хэш по-прежнему
    # пишем в журнал для аудита.
    file_hash = hashlib.sha256(content).hexdigest()

    auto_name = f"[авто:{org}] {filename}"
    try:
        return _dispatch_import(db, kind, content, auto_name, robot, filename,
                                org, file_hash)
    except HTTPException:
        raise
    except Exception as err:  # noqa: BLE001 — важно назвать место сбоя
        # Ответ уходит в журнал Apps Script, а туда traceback целиком не
        # влезает. Отдаём тип, текст и последний кадр из нашего кода — по ним
        # сразу видно, какой импортёр и какая строка споткнулись.
        tb = traceback.extract_tb(err.__traceback__)
        ours = [f for f in tb if "/app/" in f.filename] or tb
        last = ours[-1] if ours else None
        where = (f"{last.filename.split('/')[-1]}:{last.lineno} в {last.name}()"
                 if last else "неизвестно")
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail=f"Не удалось загрузить «{filename}» ({kind}): "
                   f"{type(err).__name__}: {err} — {where}",
        ) from err


def _dispatch_import(db, kind, content, auto_name, robot, filename, org, file_hash):
    """Разбор файла нужным импортёром по определённому типу выгрузки."""
    if kind == "sales":
        # Формат реализации: построчный (есть «НоменклатураНаименование») или
        # документный (Дата/Сумма/Контрагент, как у Innowave).
        if _is_line_sales(content):
            result = import_sales_workbook(db, content, auto_name, robot.id, org=org)
        else:
            result = import_sales_docs_workbook(db, content, auto_name, robot.id, org=org)
    elif kind == "receipts":
        # Касса: старое «ПКО» или слово «касса» в новой схеме имён; иначе банк.
        low = filename.lower()
        rcpt_kind = "cash" if any(
            t in low for t in ("пко", "pko", "касс", "kass")) else "bank"
        result = import_receipts_workbook(
            db, content, auto_name, robot.id, kind=rcpt_kind, org=org
        )
    elif kind == "return_docs":
        result = import_returns_workbook(db, content, auto_name, robot.id, org=org)
    elif kind == "cash_balances":
        result = import_cash_balances_workbook(db, content, auto_name, robot.id, org=org)
    elif kind == "stock_balances":
        result = import_stock_balances_workbook(db, content, auto_name, robot.id, org=org)
    elif kind == "expense":
        # Касса: старое «РКО» или слово «касса» в новой схеме имён; иначе банк.
        low = filename.lower()
        exp_kind = "cash" if any(
            t in low for t in ("рко", "rko", "касс", "kass")) else "bank"
        result = import_expenses_workbook(db, content, auto_name, robot.id, exp_kind, org=org)
    elif kind == "return_lines":
        # ТовВозв: очистка продаж + запись сумм возвратов по клиентам.
        result = import_return_lines_workbook(db, content, auto_name, robot.id, org=org)
        log = db.query(models.ImportLog).order_by(models.ImportLog.id.desc()).first()
        if log and log.file_hash is None:
            log.file_hash = file_hash
            db.commit()
        return {"type": kind, "status": "imported", **result}
    else:
        db.add(models.ImportLog(
            filename=f"[авто, не распознан] {filename}",
            user_id=robot.id,
            added=0,
            skipped=0,
            errors_count=0,
        ))
        db.commit()
        return {
            "type": kind,
            "status": "skipped",
            "detail": "Формат пока не поддерживается — файл записан в журнал",
        }

    # Проставляем хэш файла в свежую запись журнала
    log = (
        db.query(models.ImportLog)
        .order_by(models.ImportLog.id.desc())
        .first()
    )
    if log and log.file_hash is None:
        log.file_hash = file_hash
        db.commit()

    return {"type": kind, "status": "imported", **result}


admin_only = require_roles(models.Role.admin)


@router.post("/reset")
def reset_imported_data(
    db: Session = Depends(get_db),
    _: models.User = Depends(admin_only),
):
    """Полная очистка импортированных из 1С данных для чистого переимпорта.

    Удаляет только то, что грузится из 1С. Ручные данные портала —
    пользователи, платежи календаря, планы агентов, сопоставления имён,
    принятые нарушения — сохраняются.
    """
    cleared = {}
    for model in (
        models.Sale,
        models.Receipt,
        models.Expense,
        models.ReturnDoc,
        models.CashBalance,
        models.StockBalance,
        models.ImportLog,
    ):
        cleared[model.__tablename__] = db.query(model).delete()
    db.commit()
    return {"status": "reset", "cleared": cleared}
