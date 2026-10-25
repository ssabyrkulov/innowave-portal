"""Задачи бухгалтерии в Telegram: настройки, подключение группы, вебхук.

Сама логика — в services/taskbot.py; здесь только то, что видит страница
«Контуры 1С» (статус, настройки, список задач) и вход для Telegram.
"""

import hmac
from datetime import date

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import get_current_user, require_roles
from ..services import reconcile, taskbot

router = APIRouter(tags=["taskbot"])

admin_only = require_roles(models.Role.admin)


def _status(db: Session, cfg: models.TaskBotSettings, me: models.User) -> dict:
    users = db.query(models.User).filter(models.User.is_active.is_(True)) \
        .order_by(models.User.full_name).all()
    link = (f"https://t.me/{cfg.bot_username}?start={taskbot.link_token(me.id)}"
            if cfg.bot_username else None)
    return {
        "token_set": taskbot.configured(),
        "bot_username": cfg.bot_username,
        "chat_bound": bool(cfg.chat_id),
        "chat_title": cfg.chat_title,
        "bind_code": cfg.bind_code if me.role == models.Role.admin else None,
        "enabled": cfg.enabled,
        "control_from": cfg.control_from.isoformat() if cfg.control_from else None,
        "stock_orgs": sorted(reconcile.split_orgs(cfg.stock_orgs)),
        "payment_orgs": sorted(reconcile.split_orgs(cfg.payment_orgs)),
        "doc_orgs": sorted(reconcile.split_orgs(cfg.doc_orgs)),
        "upr_user_id": cfg.upr_user_id,
        "nal_user_id": cfg.nal_user_id,
        "owner_user_id": cfg.owner_user_id,
        "escalate_days": cfg.escalate_days,
        "grace_days": cfg.grace_days,
        "item_aliases": cfg.item_aliases or "",
        "last_scan_at": cfg.last_scan_at.isoformat() if cfg.last_scan_at else None,
        "nal_ledger": {o: reconcile.has_nal_ledger(db, o) for o in models.ORGS},
        "my_link": link,
        "me_linked": bool(me.telegram_id),
        "users": [{"id": u.id, "name": u.full_name, "role": u.role.value,
                   "telegram": u.telegram_name or (bool(u.telegram_id) or None)}
                  for u in users],
    }


@router.get("/taskbot/settings")
def get_settings(db: Session = Depends(get_db),
                 me: models.User = Depends(get_current_user)):
    cfg = taskbot.get_settings(db)
    db.commit()
    return _status(db, cfg, me)


class SettingsIn(BaseModel):
    enabled: bool | None = None
    control_from: date | None = None
    stock_orgs: list[str] | None = None
    payment_orgs: list[str] | None = None
    doc_orgs: list[str] | None = None
    upr_user_id: int | None = None
    nal_user_id: int | None = None
    owner_user_id: int | None = None
    escalate_days: int | None = None
    grace_days: int | None = None
    item_aliases: str | None = None


@router.put("/taskbot/settings")
def save_settings(payload: SettingsIn, db: Session = Depends(get_db),
                  me: models.User = Depends(admin_only)):
    cfg = taskbot.get_settings(db)
    data = payload.model_dump(exclude_unset=True)
    for key in ("stock_orgs", "payment_orgs", "doc_orgs"):
        if key in data:
            data[key] = ",".join(o for o in data[key] or [] if o in models.ORGS)
    for key in ("escalate_days", "grace_days"):
        if key in data and (data[key] is None or data[key] < 0):
            data.pop(key)
    for key, value in data.items():
        setattr(cfg, key, value)
    db.commit()
    return _status(db, cfg, me)


class ConnectIn(BaseModel):
    portal_url: str


@router.post("/taskbot/connect")
def connect(payload: ConnectIn, db: Session = Depends(get_db),
            me: models.User = Depends(admin_only)):
    """Подключить бота: вебхук на портал и код для команды /bind в группе."""
    if not taskbot.configured():
        raise HTTPException(status_code=400, detail=(
            "Не задан токен бота «Задачи бухгалтерии»: добавьте TASKBOT_TOKEN "
            "в переменные окружения на Render."))
    cfg = taskbot.get_settings(db)
    bot = taskbot.api("getMe")
    if not bot:
        raise HTTPException(status_code=502, detail=(
            "Telegram не ответил на getMe — проверьте токен бота."))
    base = payload.portal_url.strip().rstrip("/")
    if not base.startswith("https://"):
        raise HTTPException(status_code=400, detail=(
            "Telegram принимает вебхук только по https — откройте портал по "
            "его публичному адресу."))
    ok = taskbot.api("setWebhook", url=f"{base}/telegram/webhook",
                     secret_token=taskbot.webhook_secret(),
                     allowed_updates=["message", "callback_query"],
                     drop_pending_updates=True)
    if ok is None:
        raise HTTPException(status_code=502, detail="Telegram не принял вебхук.")
    cfg.bot_username = bot.get("username")
    cfg.portal_url = base
    cfg.bind_code = taskbot.new_bind_code()
    db.commit()
    return _status(db, cfg, me)


@router.post("/taskbot/sync")
def sync_now(db: Session = Depends(get_db),
             _: models.User = Depends(require_roles(models.Role.admin,
                                                    models.Role.accountant))):
    """Пересчитать расхождения и обновить Telegram сейчас, не дожидаясь
    фонового цикла."""
    import gc
    import time

    from .tax import scan_contour_events
    if not taskbot.scan_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail=(
            "Сверка уже идёт (фоновая, после выгрузки или запуска сервиса) — "
            "через минуту обновите страницу."))
    try:
        t0 = time.time()
        res = scan_contour_events(db)
        res["telegram"] = taskbot.sync(db)
        print(f"[taskbot] сверка по кнопке: {res}, {time.time() - t0:.1f} c",
              flush=True)
        return res
    finally:
        taskbot.scan_lock.release()
        db.expunge_all()
        gc.collect()


@router.get("/taskbot/tasks")
def tasks(db: Session = Depends(get_db),
          _: models.User = Depends(get_current_user)):
    """Задачи дня — то же, что в закреплённом списке группы."""
    cfg = taskbot.get_settings(db)
    db.commit()
    today = taskbot.local_today()
    users = {u.id: u.full_name for u in db.query(models.User).all()}
    out = []
    for e in taskbot.tasks_for_list(db, today):
        state = ("open" if taskbot.is_open(e)
                 else "acked" if e.acked_at else "done")
        out.append({
            "id": e.id, "state": state, "organization": e.organization,
            "kind": e.kind, "side": e.side,
            "text": taskbot.html.unescape(taskbot.task_text(e)),
            "responsible": users.get(taskbot.responsible_id(cfg, e)),
            "age_days": taskbot.age_days(e, today),
            "claimed_by": e.claimed_by, "note": e.note,
            "closed_at": (e.resolved_at or e.acked_at).isoformat()
            if (e.resolved_at or e.acked_at) else None,
        })
    order = {"open": 0, "done": 1, "acked": 2}
    out.sort(key=lambda t: (order[t["state"]], -t["age_days"]))
    return {"rows": out, "escalate_days": cfg.escalate_days}


@router.post("/telegram/webhook")
async def webhook(request: Request, db: Session = Depends(get_db),
                  secret: str | None = Header(
                      default=None, alias="X-Telegram-Bot-Api-Secret-Token")):
    """Вход для Telegram: команды, ответы и нажатия кнопок."""
    if not secret or not hmac.compare_digest(secret, taskbot.webhook_secret()):
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        upd = await request.json()
        taskbot.handle_update(db, upd)
    except Exception as err:  # noqa: BLE001 — Telegram повторяет при ошибке
        db.rollback()
        print(f"[taskbot] вебхук: {err}", flush=True)
    # Всегда 200: иначе Telegram будет слать то же обновление снова и снова.
    return {"ok": True}
