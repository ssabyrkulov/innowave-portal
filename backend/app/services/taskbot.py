"""Задачи бухгалтерии в Telegram.

Портал после каждой выгрузки сверяет управленку с налоговой (журнал
контуров, см. tax.scan_contour_events). Всё, что не сходится и должно
сходиться, — задача. Бот держит их в группе бухгалтерии:

- новая задача приходит отдельным сообщением с упоминанием ответственного
  и той же строкой ложится в закреплённый список дня;
- галочку ставит следующая выгрузка: расхождение ушло — строка в списке
  зачёркивается, сообщение о задаче переписывается на «закрыто». Кнопок
  нет: отмечать руками нечего, выгрузка знает лучше;
- в 18:00 — сводка, и задачи старше срока эскалации уходят руководителю.

Бот отдельный — «Задачи бухгалтерии», токен в переменной окружения
TASKBOT_TOKEN. Бот согласований платежей (TELEGRAM_BOT_TOKEN) его не
заменяет: у бота один вебхук на всё, и кнопки задач не должны делить его с
уведомлениями о платежах. Группу бот узнаёт по команде /bind с кодом из
портала, людей — по ссылке /start из портала.
"""

import hashlib
import hmac
import html
import json
import re
import secrets
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from .. import models
from ..config import settings

# Бишкек: UTC+6 круглый год. День задач и время сводки — местные.
TZ_SHIFT = timedelta(hours=6)
DIGEST_HOUR = 18
# Сколько сообщений о новых задачах слать за один пересчёт. Однотипные
# задачи идут одним сообщением (28 оплат «Булак Маркет» — одно), а что не
# влезло — придёт следующим пересчётом: ни одна задача не теряется.
ALERTS_PER_SYNC = 8
# Telegram режет сообщение на 4096 символах — группу делим на части.
MESSAGE_LIMIT = 3800

# Пересчёт журнала и рассылка — один за раз на процесс. Кнопка «Сверить
# сейчас» сразу после деплоя попадала на фоновый пересчёт, который стартует
# при запуске: два тяжёлых прохода разом на 512 МБ инстанса, и Render
# отдавал 502 — на странице «Ошибка запроса». Второй теперь не запускается.
scan_lock = threading.Lock()
LIST_LINES = 30

FIRM = {"hygiene": "Hygiene", "innowave": "Innowave", "bluecarbon": "BC"}
KIND = {"sale": "Реализация", "return": "Возврат", "writeoff": "Списание",
        "purchase": "Поступление", "payment": "Оплата", "stock": "Остаток",
        "customer": "Покупатель", "kkm": "ККМ", "kkmpay": "Оплаты розницы",
        "payparty": "Контрагент оплаты", "payop": "Вид оплаты",
        "qr": "QR-оплата", "salary": "Зарплата"}


def local_now() -> datetime:
    return datetime.utcnow() + TZ_SHIFT


def local_today() -> date:
    return local_now().date()


def _local(dt: datetime | None) -> datetime | None:
    return dt + TZ_SHIFT if dt else None


def get_settings(db: Session) -> models.TaskBotSettings:
    cfg = db.query(models.TaskBotSettings).first()
    if cfg is None:
        cfg = models.TaskBotSettings(
            enabled=True,
            control_from=local_today().replace(day=1),
            # Остатки Хайджина в налоговой законно отстают от управленки до
            # проведения ККМ, поэтому по умолчанию остатки сверяем только у
            # Инновейва. Оплаты и документы — у обеих фирм.
            stock_orgs="innowave",
            payment_orgs="innowave,hygiene",
            doc_orgs="innowave,hygiene",
            escalate_days=2,
            grace_days=1,
            item_aliases=("Шампунь для волос с кондиционером = "
                          "Шампунь SPLASH для волос c кондиционером"),
        )
        db.add(cfg)
        db.flush()
    return cfg


# --- Telegram Bot API ------------------------------------------------------

def configured() -> bool:
    return bool(settings.taskbot_token)


def api(method: str, **payload) -> dict | None:
    """Вызов Bot API. Ошибку пишет в лог и возвращает None — бот не должен
    ронять ни пересчёт, ни вебхук."""
    if not configured():
        return None
    url = f"https://api.telegram.org/bot{settings.taskbot_token}/{method}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as err:
        try:
            data = json.loads(err.read().decode())
        except Exception:  # noqa: BLE001
            data = {"ok": False, "description": str(err)}
    except Exception as err:  # noqa: BLE001 — сеть, таймаут
        print(f"[taskbot] {method}: {err}", flush=True)
        return None
    if not data.get("ok"):
        desc = data.get("description", "")
        # Правка тем же текстом — не ошибка, а «ничего не изменилось».
        if "message is not modified" not in desc:
            print(f"[taskbot] {method}: {desc}", flush=True)
        return None
    return data.get("result")


def webhook_secret() -> str:
    """Секрет вебхука — производный от SECRET_KEY: отдельная переменная
    окружения не нужна, а снаружи его не подобрать."""
    return hashlib.sha256(
        (settings.secret_key + "|taskbot").encode()).hexdigest()[:48]


def link_token(user_id: int) -> str:
    """Код в ссылке t.me/бот?start=… — по нему бот узнаёт пользователя."""
    sig = hmac.new(settings.secret_key.encode(), f"tg{user_id}".encode(),
                   hashlib.sha256).hexdigest()[:12]
    return f"u{user_id}_{sig}"


def parse_link_token(token: str) -> int | None:
    try:
        uid = int(token.split("_", 1)[0][1:])
    except (ValueError, IndexError):
        return None
    return uid if hmac.compare_digest(token, link_token(uid)) else None


def new_bind_code() -> str:
    return f"{secrets.randbelow(900000) + 100000}"


# --- Тексты ----------------------------------------------------------------

def _e(s) -> str:
    return html.escape(str(s or ""), quote=False)


def _money(x) -> str:
    v = float(x or 0)
    return f"{v:,.0f}".replace(",", " ") if abs(v - round(v)) < 0.005 \
        else f"{v:,.2f}".replace(",", " ")


def responsible_id(cfg: models.TaskBotSettings, e: models.ContourEvent) -> int | None:
    """Кто исправляет. Управленка — эталон (сверена с фактом): остаток,
    разошедшийся с ней, и документ, которого нет в налоговой, — задача
    налоговой. Документ или оплата, которых нет в управленке, — управленки."""
    if e.kind == "salary":
        # Выплата без начисления — ошибка той базы, где она найдена.
        return cfg.upr_user_id if e.side == "upr" else cfg.nal_user_id
    if e.kind in ("stock", "payparty", "payop") or e.side == "upr":
        return cfg.nal_user_id
    return cfg.upr_user_id


# Вид документа в винительном падеже — задача говорит, что сделать:
# «Внести в налоговую реализацию №134».
KIND_ACC = {"sale": "реализацию", "return": "возврат от покупателя",
            "writeoff": "списание", "purchase": "поступление",
            "payment": "оплату"}


def task_text(e: models.ContourEvent) -> str:
    """Одна строка задачи — что сделать, без фирмы (её добавляет вызывающий).

    Не «остаток упр 43, нал 193», а действие: внести документ, найти
    лишнее. Управленка сверена с фактом, поэтому лишнее или недостающее в
    налоговой ищется в налоговой."""
    if e.kind == "kkm":
        # doc_number: «сентябрь 2026: ONE размер L — должно быть 1 240
        # (сейчас 0); …», party: итог розницы и налоговой.
        period, _, items = (e.doc_number or "").partition(": ")
        return (f"Провести ККМ за {_e(period)} по товарам и штукам: "
                f"{_e(items)} ({_e(e.party)})")
    if e.kind == "kkmpay":
        # Розница за месяц одной строкой: party — сколько и на какую сумму.
        amount = _money(abs(float(e.amount or 0)))
        if e.side == "upr":
            return (f"Провести через ККМ оплаты розницы за "
                    f"{_e(e.doc_number)}: в налоговой не хватает {amount} сом "
                    f"({_e(e.party)})")
        return (f"Проверить оплаты розницы за {_e(e.doc_number)}: в "
                f"налоговой на {amount} сом больше, чем в управленке — "
                f"внести оплаты точек в управленку или исправить налоговую "
                f"({_e(e.party)})")
    if e.kind in ("payparty", "payop"):
        # party: «что в налоговой → покупатель по управленке».
        from ..routers.tax import _clean_customer, _is_legal
        tax_side, _, upr_party = (e.party or "").partition(" → ")
        who = _clean_customer(upr_party)
        need = f"«{who}»" if _is_legal(upr_party) else "«ККМ» (розница)"
        what = (f"№{_e(e.doc_number)} от {e.doc_date:%d.%m.%y} на "
                f"{_money(e.amount)}")
        if e.kind == "payparty":
            return (f"Исправить контрагента в оплате {what}: в налоговой "
                    f"«{_e(_clean_customer(tax_side))}», а по управленке это "
                    f"оплата «{_e(who)}» — должно быть {_e(need)}")
        return (f"Провести как оплату покупателя поступление {what}: в "
                f"налоговой оно проведено как {_e(tax_side)}, а по управленке "
                f"это оплата покупателя «{_e(who)}» — покупатель {_e(need)}")
    if e.kind == "qr":
        # doc_number — время оплаты, party — комментарий плательщика.
        note = "" if e.party in (None, "", "QR") else f" (комментарий: «{_e(e.party)}»)"
        return (f"Внести в управленку QR-оплату от {e.doc_date:%d.%m.%y} "
                f"{_e(e.doc_number)} на {_money(e.amount)} сом{note}: деньги "
                f"на счёт пришли (отчёт банка), а оплаты в управленке нет — "
                f"найти точку и провести")
    if e.kind == "salary":
        where = "управленке" if e.side == "upr" else "налоговой"
        return (f"Зарплата в {where}: «{_e(e.party)}» выплачено на "
                f"{_money(e.amount)} сом больше, чем начислено (выплаты "
                f"{_e(e.doc_number)}) — доначислить за месяцы выплат, а если "
                f"это не зарплата (услуги, возврат), перенести со счёта 3520")
    if e.kind == "customer":
        n = _money(abs(float(e.qty or 0)))
        if e.side == "upr":
            return (f"Выписать ЭСФ: {_e(e.party)} — в налоговой не хватает "
                    f"{n} шт. ({_e(e.doc_number)})")
        return (f"Проверить {_e(e.party)}: в налоговой на {n} шт. больше, "
                f"чем отгружено в управленке ({_e(e.doc_number)}) — внести "
                f"отгрузку в управленку или исправить налоговую")
    if e.kind == "stock":
        q = float(e.qty or 0)
        # Дробное количество — не штуки (бензин в литрах): единицу не пишем.
        n = _money(abs(q)) + (" шт." if abs(q - round(q)) < 0.001 else "")
        if q > 0:
            act = (f"Найти, почему в налоговой лишние {n} «{_e(e.party)}»: "
                   f"не проведён расход — продажа или списание")
        else:
            act = (f"Найти, почему в налоговой не хватает {n} "
                   f"«{_e(e.party)}»: не проведён приход — поступление или "
                   f"возврат")
        return f"{act} ({_e(e.doc_number)})"
    # «0000-000134» в управленке и «134» в налоговой — один номер.
    number = re.sub(r"^[0\-]+(?=\d)", "", e.doc_number or "")
    num = f" №{_e(number)}" if number else ""
    what = (f"{KIND_ACC.get(e.kind, e.kind)}{num} от "
            f"{e.doc_date.strftime('%d.%m.%y')}")
    # У списаний суммы нет, зато есть состав — его и показываем.
    tail = []
    if e.party:
        tail.append(_e(e.party))
    if float(e.amount or 0):
        tail.append(_money(e.amount))
    elif (float(e.qty or 0) and e.kind != "payment"
          and not re.search(r"\d", e.party or "")):
        # У списания состав уже с количеством — второй раз не пишем.
        tail.append(f"{_money(e.qty)} шт.")
    tail = f" · {' · '.join(tail)}" if tail else ""
    if e.side == "upr":
        return f"Внести в налоговую {what}{tail}"
    return (f"Внести в управленку {what}{tail} "
            f"(или убрать из налоговой, если там ошибка)")


def _who(user: models.User | None) -> str:
    if user is None:
        return "не назначен"
    if user.telegram_id:
        return f'<a href="tg://user?id={_e(user.telegram_id)}">{_e(user.full_name)}</a>'
    return _e(user.full_name)


def age_days(e: models.ContourEvent, today: date) -> int:
    seen = _local(e.first_seen)
    return (today - seen.date()).days if seen else 0


def is_open(e: models.ContourEvent) -> bool:
    return e.resolved_at is None and e.acked_at is None


def _portal_link(cfg: models.TaskBotSettings) -> str | None:
    base = (cfg.portal_url or settings.portal_url or "").rstrip("/")
    return f"{base}/contours" if base else None


# Пустая клавиатура: при правке старых сообщений снимает с них кнопки
# «Внесла» и «Не нужно», которые были в первой версии бота.
NO_BUTTONS = {"inline_keyboard": []}


def alert_text(cfg, e, users: dict[int, models.User]) -> str:
    who = users.get(responsible_id(cfg, e))
    return (f"🔴 <b>{FIRM.get(e.organization, e.organization)}</b> · "
            f"{task_text(e)}\nОтветственный: {_who(who)}")


def group_key(e: models.ContourEvent) -> tuple:
    """Задачи, которые идут одним сообщением и одной строкой списка: та же
    фирма, тот же вид, та же сторона; у ошибок оплат в налоговой — ещё и тот
    же контрагент, на которого их записали."""
    label = ""
    if e.kind in ("payparty", "payop"):
        label = (e.party or "").partition(" → ")[0]
    return (e.organization, e.kind, e.side, label)


def _item_text(e: models.ContourEvent) -> str:
    """Строка задачи внутри группы — без того, что уже сказано в заголовке."""
    if e.kind in ("payparty", "payop"):
        from ..routers.tax import _clean_customer
        upr = _clean_customer((e.party or "").partition(" → ")[2])
        return (f"№{_e(e.doc_number)} от {e.doc_date:%d.%m} на "
                f"{_money(e.amount)} — по управленке «{_e(upr)}»")
    if e.kind == "salary":
        return f"{_e(e.party)} — {_money(e.amount)} сом ({_e(e.doc_number)})"
    if e.kind == "qr":
        note = "" if e.party in (None, "", "QR") else f" — «{_e(e.party)}»"
        return f"{e.doc_date:%d.%m} {_e(e.doc_number)} — {_money(e.amount)} сом{note}"
    return task_text(e)


def group_head(e: models.ContourEvent, n: int) -> str:
    """Заголовок группы из n задач вида e."""
    if e.kind == "payparty":
        tax = (e.party or "").partition(" → ")[0]
        from ..routers.tax import _clean_customer
        return (f"Исправить контрагента в оплатах ({n}): в налоговой они "
                f"записаны на «{_e(_clean_customer(tax))}», а по управленке это "
                f"оплаты других покупателей — для розницы должно быть «ККМ»")
    if e.kind == "payop":
        tax = (e.party or "").partition(" → ")[0]
        return (f"Провести как оплату покупателя ({n}): в налоговой эти "
                f"поступления проведены как {_e(tax)}")
    if e.kind == "stock":
        return f"Остатки: найти, почему налоговая не сходится с управленкой ({n})"
    if e.kind == "salary":
        where = "управленке" if e.side == "upr" else "налоговой"
        return (f"Зарплата в {where}: выплачено больше, чем начислено ({n}) — "
                f"доначислить за месяцы выплат, а если это не зарплата, "
                f"перенести со счёта 3520")
    if e.kind == "qr":
        return (f"Внести в управленку QR-оплаты ({n}): деньги на счёт пришли "
                f"(отчёт банка), а оплат в управленке нет — найти точки и провести")
    if e.kind == "customer":
        return (f"Выписать ЭСФ покупателям-юрлицам ({n})" if e.side == "upr"
                else f"Проверить покупателей-юрлиц: в налоговой больше ({n})")
    what = KIND_PL.get(e.kind, KIND.get(e.kind, e.kind))
    if e.side == "upr":
        return f"Внести в налоговую: {what} ({n})"
    return f"Внести в управленку или убрать из налоговой: {what} ({n})"


# Вид документа во множественном — заголовок группы задач.
KIND_PL = {"sale": "реализации", "return": "возвраты от покупателей",
           "writeoff": "списания", "purchase": "поступления", "payment": "оплаты"}


def group_text(cfg, evs: list, users: dict[int, models.User]) -> list[str]:
    """Сообщение о группе задач; длинная группа — несколько частей.
    Возвращает тексты частей по порядку, части делят evs подряд."""
    e0 = evs[0]
    if len(evs) == 1:
        return [alert_text(cfg, e0, users)]
    head = (f"🔴 <b>{FIRM.get(e0.organization, e0.organization)}</b> · "
            f"{group_head(e0, len(evs))}")
    foot = f"Ответственный: {_who(users.get(responsible_id(cfg, e0)))}"
    parts, cur = [], [head]
    for e in evs:
        line = f"• {_item_text(e)}"
        if len("\n".join(cur + [line, foot])) > MESSAGE_LIMIT:
            parts.append(cur)
            cur = [head + " (продолжение)"]
        cur.append(line)
    parts.append(cur)
    return ["\n".join(p + [foot]) for p in parts]


def _chunks(evs: list, texts: list[str]) -> list[list]:
    """Какие задачи вошли в какую часть сообщения (по числу строк «•»)."""
    out, i = [], 0
    for t in texts:
        k = max(1, t.count("\n• "))
        out.append(evs[i:i + k])
        i += k
    return out


def tasks_for_list(db: Session, today: date) -> list[models.ContourEvent]:
    """Открытые задачи и закрытые сегодня — то, что видно в списке дня."""
    start = datetime.combine(today, datetime.min.time()) - TZ_SHIFT
    q = db.query(models.ContourEvent).filter(models.ContourEvent.is_task.is_(True))
    return [e for e in q.all()
            if is_open(e)
            or (e.resolved_at and e.resolved_at >= start)
            or (e.acked_at and e.acked_at >= start)]


def waiting_reason(db: Session, cfg: models.TaskBotSettings) -> str | None:
    """Почему сверять пока нечего. Пустой список без причины читается как
    «всё сходится» — а это неправда, пока налоговых журналов нет или
    сверка ещё ни разу не прошла."""
    from . import reconcile

    if cfg.last_scan_at is None:
        return "⏳ Сверка ещё не запускалась — список заполнится после неё."
    orgs = reconcile.split_orgs(cfg.stock_orgs) | reconcile.split_orgs(cfg.payment_orgs)
    missing = [FIRM.get(o, o) for o in sorted(orgs)
               if not reconcile.has_nal_ledger(db, o)]
    if missing:
        return ("⏳ Жду налоговый журнал проводок из 1С: "
                + ", ".join(missing)
                + ". Остатки и оплаты сверю, когда он придёт с выгрузкой.")
    return None


def render_list(db: Session, cfg: models.TaskBotSettings) -> str:
    today = local_today()
    users = {u.id: u for u in db.query(models.User).all()}
    tasks = tasks_for_list(db, today)
    sections: dict[int | None, list] = {}
    for e in tasks:
        sections.setdefault(responsible_id(cfg, e), []).append(e)

    def title(uid):
        role = ("Налоговая" if uid == cfg.nal_user_id
                else "Управленка" if uid == cfg.upr_user_id else "Задачи")
        u = users.get(uid)
        return f"<b>{role} — {_e(u.full_name) if u else 'не назначен'}</b>"

    def order(e):
        # Сначала открытые (старые выше), потом закрытые.
        return (0 if is_open(e) else 1, e.first_seen or datetime.min)

    lines = [f"📌 <b>Задачи бухгалтерии · {today.strftime('%d.%m')}</b>"]
    n_open = n_done = 0
    for uid in sorted(sections, key=lambda u: (u != cfg.nal_user_id, u or 0)):
        rows = sorted(sections[uid], key=order)
        lines += ["", title(uid)]
        shown = 0
        # Однотипные задачи — одной строкой: «Исправить контрагента в
        # оплатах (28)…», подробности — в сообщении и в портале.
        groups: dict = {}
        for e in rows:
            state = "open" if is_open(e) else "ack" if e.acked_at else "done"
            groups.setdefault((state, group_key(e)), []).append(e)
        for (state, _), evs in groups.items():
            e = evs[0]
            body = task_text(e) if len(evs) == 1 else group_head(e, len(evs))
            text = f"<b>{FIRM.get(e.organization, e.organization)}</b>: {body}"
            if state == "open":
                age = max(age_days(x, today) for x in evs)
                late = f" ⚠️{age} дн." if age >= (cfg.escalate_days or 2) else ""
                n_open += len(evs)
                line = f"🔴 {text}{late}"
            elif state == "ack":
                n_done += len(evs)
                line = f"✅ {text} — норма: {_e(e.note or 'не нужно')}"
            else:
                n_done += len(evs)
                last = max(x.resolved_at for x in evs)
                line = f"✅ {text} · {_local(last):%H:%M}"
            if shown < LIST_LINES:
                lines.append(line)
            shown += 1
        if shown > LIST_LINES:
            lines.append(f"…ещё {shown - LIST_LINES} — в портале")
    waiting = waiting_reason(db, cfg)
    if waiting:
        lines += ["", waiting]
    elif not tasks:
        lines += ["", "✅ Всё сходится: управленка и налоговая совпадают."]
    lines += ["", f"<i>Обновлено {local_now():%H:%M} · открыто {n_open} · "
                  f"закрыто сегодня {n_done}</i>"]
    link = _portal_link(cfg)
    if link:
        lines.append(f'<a href="{_e(link)}">Подробнее в портале</a>')
    text = "\n".join(lines)
    return text[:4000]


# --- Синхронизация с группой ------------------------------------------------

def _ready(cfg) -> bool:
    return configured() and cfg.enabled and bool(cfg.chat_id)


def refresh_list(db: Session, cfg: models.TaskBotSettings, force_new: bool = False) -> None:
    """Переписать закреплённый список; с новым днём — завести новый."""
    if not _ready(cfg):
        return
    text = render_list(db, cfg)
    today = local_today()
    if cfg.list_message_id and cfg.list_day == today and not force_new:
        if text == cfg.list_text:
            return
        if api("editMessageText", chat_id=cfg.chat_id,
               message_id=cfg.list_message_id, text=text, parse_mode="HTML",
               disable_web_page_preview=True) is not None:
            cfg.list_text = text
            return
    msg = api("sendMessage", chat_id=cfg.chat_id, text=text, parse_mode="HTML",
              disable_web_page_preview=True, disable_notification=True)
    if msg:
        if cfg.list_message_id:
            api("unpinChatMessage", chat_id=cfg.chat_id,
                message_id=cfg.list_message_id)
        api("pinChatMessage", chat_id=cfg.chat_id,
            message_id=msg["message_id"], disable_notification=True)
        cfg.list_message_id = msg["message_id"]
        cfg.list_day = today
        cfg.list_text = text


def sync(db: Session) -> dict:
    """После пересчёта журнала: новые задачи — в группу, закрытые —
    отметить, список — переписать. Вызывается из фонового цикла."""
    cfg = get_settings(db)
    cfg.last_scan_at = datetime.utcnow()
    if not _ready(cfg):
        db.commit()
        return {"sent": 0, "closed": 0, "ready": False}
    users = {u.id: u for u in db.query(models.User).all()}
    tasks = (db.query(models.ContourEvent)
             .filter(models.ContourEvent.is_task.is_(True)).all())

    sent = closed = 0
    # Новая — ещё не отправленная задача. Отозванная, которая снова стала
    # задачей, — тоже. Нулевой номер ставила прежняя версия задачам «только в
    # списке» — теперь и они уходят сообщением.
    fresh = [e for e in tasks if is_open(e)
             and (not e.tg_message_id or e.tg_closed)]
    fresh.sort(key=lambda e: e.first_seen or datetime.min)
    groups: dict = {}
    for e in fresh:
        groups.setdefault(group_key(e), []).append(e)
    pending = 0
    for evs in groups.values():
        texts = group_text(cfg, evs, users)
        for part, text in zip(_chunks(evs, texts), texts):
            if sent >= ALERTS_PER_SYNC:
                pending += len(part)  # уйдёт следующим пересчётом
                continue
            msg = api("sendMessage", chat_id=cfg.chat_id, text=text,
                      parse_mode="HTML", disable_web_page_preview=True)
            if not msg:
                continue
            sent += 1
            for e in part:
                e.tg_message_id = msg["message_id"]
                e.tg_closed = False
    if pending:
        api("sendMessage", chat_id=cfg.chat_id, disable_notification=True,
            text=f"…и ещё задач: {pending}. Придут следующим пересчётом, "
                 f"а пока они в закреплённом списке.")

    # Сообщение о группе живёт, пока в ней есть открытая задача: сделанные
    # из него уходят (сообщение переписывается), последняя — удаляет его.
    # Отозванная задача (правило уточнили — уже не задача) уходит так же.
    posted = (db.query(models.ContourEvent)
              .filter(models.ContourEvent.tg_message_id > 0,
                      models.ContourEvent.tg_closed.isnot(True)).all())
    by_msg: dict = {}
    for e in posted:
        by_msg.setdefault(e.tg_message_id, []).append(e)
    withdrawn = 0
    for mid, evs in by_msg.items():
        live = [e for e in evs if e.is_task and is_open(e)]
        gone = [e for e in evs if not (e.is_task and is_open(e))]
        if not gone:
            continue
        if live:
            text = group_text(cfg, live, users)[0]
            api("editMessageText", chat_id=cfg.chat_id, message_id=mid,
                text=text, parse_mode="HTML", disable_web_page_preview=True,
                reply_markup=NO_BUTTONS)
        elif not api("deleteMessage", chat_id=cfg.chat_id, message_id=mid):
            # Удалить можно только сообщение моложе 48 часов (так устроен
            # Telegram); старое переписываем коротким «Закрыто».
            e = gone[0]
            if e.acked_at:
                head = f"✅ <b>Норма</b> · {_e(e.note or '')}"
            else:
                head = f"✅ <b>Закрыто</b> · {local_now():%d.%m %H:%M}"
            body = task_text(e) if len(gone) == 1 else group_head(e, len(gone))
            api("editMessageText", chat_id=cfg.chat_id, message_id=mid,
                text=(f"{head}\n<b>{FIRM.get(e.organization, e.organization)}</b>: "
                      f"{body}"),
                parse_mode="HTML", disable_web_page_preview=True,
                reply_markup=NO_BUTTONS)
        for e in gone:
            e.tg_closed = True
            if e.is_task:
                closed += 1
            else:
                withdrawn += 1

    refresh_list(db, cfg)
    db.commit()
    return {"sent": sent, "closed": closed, "withdrawn": withdrawn,
            "pending": pending, "ready": True}


def periodic(db: Session) -> None:
    """Раз в несколько минут: новый день — новый список; в 18:00 — сводка."""
    cfg = get_settings(db)
    if not _ready(cfg):
        db.commit()
        return
    now = local_now()
    if cfg.list_day != now.date():
        refresh_list(db, cfg, force_new=True)
    if now.hour >= DIGEST_HOUR and cfg.last_digest_day != now.date():
        digest(db, cfg)
        cfg.last_digest_day = now.date()
    db.commit()


def digest(db: Session, cfg: models.TaskBotSettings) -> None:
    today = local_today()
    users = {u.id: u for u in db.query(models.User).all()}
    open_tasks = [e for e in tasks_for_list(db, today) if is_open(e)]
    if not open_tasks:
        waiting = waiting_reason(db, cfg)
        api("sendMessage", chat_id=cfg.chat_id, parse_mode="HTML",
            text=f"📋 <b>Сводка на {today:%d.%m}</b>\n"
                 + (waiting or "✅ Всё сходится, открытых задач нет."))
        return
    by_user: dict[int | None, list] = {}
    for e in open_tasks:
        by_user.setdefault(responsible_id(cfg, e), []).append(e)
    lines = [f"📋 <b>Сводка на {today:%d.%m}</b> · открыто {len(open_tasks)}", ""]
    for uid, rows in by_user.items():
        lines.append(f"{_who(users.get(uid))} — {len(rows)}")
    late = [e for e in open_tasks if age_days(e, today) >= (cfg.escalate_days or 2)]
    if late:
        lines += ["", f"⚠️ <b>Не закрыто дольше {cfg.escalate_days} дн.:</b>"]
        for e in sorted(late, key=lambda x: x.first_seen or datetime.min)[:15]:
            lines.append(f"• <b>{FIRM.get(e.organization, e.organization)}</b>: "
                         f"{task_text(e)} · {age_days(e, today)} дн.")
        owner = users.get(cfg.owner_user_id)
        if owner:
            lines += ["", f"Руководитель: {_who(owner)}"]
    api("sendMessage", chat_id=cfg.chat_id, parse_mode="HTML",
        text="\n".join(lines)[:4000], disable_web_page_preview=True)


# --- Входящие от Telegram ---------------------------------------------------

def _reply(chat_id, text, **extra):
    return api("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
               disable_web_page_preview=True, **extra)


def handle_update(db: Session, upd: dict) -> None:
    cfg = get_settings(db)
    if "callback_query" in upd:
        _on_button(db, cfg, upd["callback_query"])
    elif "message" in upd:
        _on_message(db, cfg, upd["message"])
    db.commit()


def _on_message(db: Session, cfg, msg: dict) -> None:
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    text = (msg.get("text") or "").strip()
    parts = text.split()
    cmd = parts[0].split("@", 1)[0].lower() if parts else ""
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "/bind":
        if chat.get("type") not in ("group", "supergroup"):
            _reply(chat.get("id"), "Команду /bind нужно отправить в группе "
                                   "бухгалтерии, а не в личке.")
            return
        if not cfg.bind_code or arg != cfg.bind_code:
            _reply(chat.get("id"), "Код не подошёл. Возьмите свежий код в "
                                   "портале: «Контуры 1С» → Telegram.")
            return
        cfg.chat_id = str(chat["id"])
        cfg.chat_title = chat.get("title")
        cfg.bind_code = None
        cfg.list_message_id = None
        # Всё, что накопилось до подключения, — в список, а не сотней
        # отдельных сообщений.
        for e in db.query(models.ContourEvent).filter(
                models.ContourEvent.is_task.is_(True),
                models.ContourEvent.tg_message_id.is_(None)).all():
            e.tg_message_id = 0
        _reply(chat["id"], "✅ Группа подключена. Ниже — закреплённый список "
                           "задач: он обновляется после каждой выгрузки 1С.")
        refresh_list(db, cfg, force_new=True)
        return

    if cmd == "/start" and chat.get("type") == "private":
        uid = parse_link_token(arg) if arg else None
        user = db.get(models.User, uid) if uid else None
        if user is None:
            _reply(chat["id"], "Чтобы привязать Telegram, откройте ссылку из "
                               "портала: «Контуры 1С» → Telegram → «Привязать "
                               "мой Telegram».")
            return
        user.telegram_id = str(sender.get("id"))
        user.telegram_name = sender.get("username") or sender.get("first_name")
        _reply(chat["id"], f"✅ Telegram привязан к пользователю портала "
                           f"<b>{_e(user.full_name)}</b>. Теперь бот будет "
                           f"отмечать вас в задачах, за которые вы отвечаете.")
        return

    if cmd in ("/list", "/tasks", "/задачи") and str(chat.get("id")) == cfg.chat_id:
        refresh_list(db, cfg, force_new=True)
        return


def _on_button(db: Session, cfg, cq: dict) -> None:
    """Кнопки «Внесла» и «Не нужно» были в первой версии бота. Их убрали:
    галочку ставит выгрузка. На старых сообщениях кнопки ещё видны — нажатие
    объясняет это и снимает их."""
    msg = cq.get("message") or {}
    api("answerCallbackQuery", callback_query_id=cq["id"],
        text="Отмечать не нужно: галочка появится сама, когда исправление "
             "придёт в выгрузке 1С.")
    if msg.get("message_id"):
        api("editMessageReplyMarkup", chat_id=(msg.get("chat") or {}).get("id"),
            message_id=msg["message_id"], reply_markup=NO_BUTTONS)
