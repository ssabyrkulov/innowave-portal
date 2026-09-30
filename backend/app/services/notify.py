"""Уведомления о заявках на платёж.

Единственный канал сейчас — Telegram: бот пишет в один чат (руководителя
или общий чат бухгалтерии). Токен и чат берутся из переменных окружения
TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID; пустые значения = уведомления
выключены, и портал работает как раньше. Отправка идёт в фоновом потоке:
согласование не должно ждать ответа Telegram и не должно падать, если
Telegram недоступен.
"""

import json
import threading
import urllib.request

from ..config import settings


def _send(text: str) -> None:
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    body = json.dumps({
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:  # noqa: BLE001 — уведомление не должно ронять запрос
        pass


def notify(text: str) -> None:
    """Отправить сообщение, если канал настроен. Никогда не бросает."""
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return
    threading.Thread(target=_send, args=(text,), daemon=True).start()
