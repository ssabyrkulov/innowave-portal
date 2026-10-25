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
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from .. import models
from ..config import settings

# Бишкек: UTC+6 круглый год. День задач и время сводки — местные.
TZ_SHIFT = timedelta(hours=6)
DIGEST_HOUR = 18
# Сколько отдельных сообщений о новых задачах слать за один пересчёт.
# Остальные — только в закреплённом списке, чтобы первый запуск или большая
# выгрузка не завалили группу сотней сообщений.
ALERTS_PER_SYNC = 8
LIST_LINES = 30

FIRM = {"hygiene": "Hygiene", "innowave": "Innowave", "bluecarbon": "BC"}
KIND = {"sale": "Реализация", "return": "Возврат", "writeoff": "Списание",
        "purchase": "Поступление", "payment": "Оплата", "stock": "Остаток"}


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
    if e.kind == "stock" or e.side == "upr":
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
        for e in rows:
            text = f"{FIRM.get(e.organization, e.organization)}: {task_text(e)}"
            if is_open(e):
                age = age_days(e, today)
                late = f" ⚠️{age} дн." if age >= (cfg.escalate_days or 2) else ""
                n_open += 1
                line = f"☐ {text}{late}"
            elif e.acked_at:
                n_done += 1
                line = f"🚫 <s>{text}</s> — {_e(e.note or 'не нужно')}"
            else:
                n_done += 1
                line = f"✅ <s>{text}</s> · {_local(e.resolved_at):%H:%M}"
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
    fresh = [e for e in tasks if is_open(e) and e.tg_message_id is None]
    fresh.sort(key=lambda e: e.first_seen or datetime.min)
    for e in fresh[:ALERTS_PER_SYNC]:
        msg = api("sendMessage", chat_id=cfg.chat_id,
                  text=alert_text(cfg, e, users), parse_mode="HTML",
                  disable_web_page_preview=True)
        e.tg_message_id = msg["message_id"] if msg else 0
        sent += 1 if msg else 0
    rest = fresh[ALERTS_PER_SYNC:]
    for e in rest:
        e.tg_message_id = 0  # только в списке
    if rest:
        api("sendMessage", chat_id=cfg.chat_id, disable_notification=True,
            text=f"…и ещё {len(rest)} новых расхождений — они в закреплённом "
                 f"списке задач.")

    for e in tasks:
        if is_open(e) or not e.tg_message_id or e.tg_closed:
            continue
        if e.resolved_at:
            head = f"✅ <b>Закрыто</b> · {_local(e.resolved_at):%d.%m %H:%M}"
            tail = "Расхождение ушло: найдено в выгрузке."
        else:
            head = "🚫 <b>Не нужно</b>"
            tail = _e(e.note or "")
        api("editMessageText", chat_id=cfg.chat_id, message_id=e.tg_message_id,
            text=(f"{head}\n<s>{FIRM.get(e.organization, e.organization)}: "
                  f"{task_text(e)}</s>\n{tail}").strip(),
            parse_mode="HTML", disable_web_page_preview=True,
            reply_markup=NO_BUTTONS)
        e.tg_closed = True
        closed += 1

    refresh_list(db, cfg)
    db.commit()
    return {"sent": sent, "closed": closed, "ready": True}


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
            lines.append(f"• {FIRM.get(e.organization, e.organization)}: "
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
