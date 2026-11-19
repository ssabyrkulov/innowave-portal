"""Ответы на свободные вопросы через Claude."""
import logging
from collections import defaultdict, deque

from anthropic import AsyncAnthropic

log = logging.getLogger(__name__)

HISTORY_TURNS = 6  # сколько последних пар «вопрос–ответ» помнить на клиента


class Assistant:
    def __init__(self, api_key: str, model: str, system_prompt: str):
        self.client = AsyncAnthropic(api_key=api_key) if api_key else None
        self.model = model
        self.system_prompt = system_prompt
        self.history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_TURNS * 2))

    @property
    def enabled(self) -> bool:
        return self.client is not None

    async def answer(self, user_id: int, text: str) -> str | None:
        if not self.client:
            return None
        hist = self.history[user_id]
        messages = [*hist, {"role": "user", "content": text}]
        try:
            resp = await self.client.messages.create(
                model=self.model,
                max_tokens=500,
                system=self.system_prompt,
                messages=messages,
            )
        except Exception:  # сеть, лимиты, неверный ключ — клиенту отвечаем запасным текстом
            log.exception("Claude API error")
            return None
        reply = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not reply:
            return None
        hist.append({"role": "user", "content": text})
        hist.append({"role": "assistant", "content": reply})
        return reply
