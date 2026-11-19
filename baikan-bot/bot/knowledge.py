"""Информация о салоне: загрузка из JSON и форматирование для пользователя и для ИИ."""
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Service:
    category: str
    name: str
    price: str
    duration: str = ""


@dataclass
class Salon:
    name: str
    address: str
    phone: str
    hours: str
    map_url: str = ""
    instagram: str = ""
    services: list[Service] = field(default_factory=list)
    faq: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "Salon":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        data.pop("_note", None)
        services = [Service(**s) for s in data.pop("services", [])]
        return cls(services=services, **data)

    def categories(self) -> list[str]:
        seen: list[str] = []
        for s in self.services:
            if s.category not in seen:
                seen.append(s.category)
        return seen

    def services_in(self, category: str) -> list[Service]:
        return [s for s in self.services if s.category == category]

    def price_list(self) -> str:
        lines = [f"💇 <b>Услуги и цены — {self.name}</b>"]
        for cat in self.categories():
            lines.append(f"\n<b>{cat}</b>")
            for s in self.services_in(cat):
                dur = f" · {s.duration}" if s.duration else ""
                lines.append(f"• {s.name} — {s.price}{dur}")
        return "\n".join(lines)

    def contacts(self) -> str:
        lines = [
            f"📍 <b>{self.name}</b>",
            f"Адрес: {self.address}",
            f"Часы работы: {self.hours}",
            f"Телефон: {self.phone}",
        ]
        if self.instagram:
            lines.append(f"Instagram: {self.instagram}")
        if self.map_url:
            lines.append(f'<a href="{self.map_url}">Открыть на карте</a>')
        return "\n".join(lines)

    def system_prompt(self) -> str:
        services = "\n".join(
            f"- {s.category}: {s.name} — {s.price}" + (f", {s.duration}" if s.duration else "")
            for s in self.services
        )
        faq = "\n".join(f"В: {f['q']}\nО: {f['a']}" for f in self.faq)
        return f"""Ты — вежливый администратор салона красоты «{self.name}» в Telegram.
Отвечай кратко (1–4 предложения), дружелюбно, на языке клиента (русский или кыргызский).
Используй ТОЛЬКО сведения ниже. Если ответа нет — честно скажи, что уточнит администратор,
и дай телефон {self.phone}. Не придумывай цены, скидки, имена мастеров и свободное время.
Не используй markdown-разметку.
Записать клиента ты не можешь сам: если клиент хочет записаться, предложи нажать кнопку «📝 Записаться».

Адрес: {self.address}
Часы работы: {self.hours}
Телефон: {self.phone}
Instagram: {self.instagram}

Услуги и цены:
{services}

Частые вопросы:
{faq}
"""
