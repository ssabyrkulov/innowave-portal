import asyncio
from pathlib import Path

from bot.ai import Assistant
from bot.knowledge import Salon
from bot.storage import Booking, Storage

SALON = Path(__file__).resolve().parent.parent / "salon.json"


def test_salon_loads_and_formats():
    s = Salon.load(SALON)
    assert s.categories()[0] == "Волосы"
    assert "Маникюр" in s.price_list()
    assert s.phone in s.contacts()
    prompt = s.system_prompt()
    assert s.address in prompt and "Не придумывай" in prompt


def test_storage_roundtrip(tmp_path):
    st = Storage(str(tmp_path / "b.db"))
    b = st.add(Booking(user_id=42, username="u", service="Стрижка", when="завтра", name="Айгуль", phone="+996555"))
    assert b.id == 1 and st.status(1) == "new"
    assert st.set_status(1, "confirmed") == 42
    assert st.status(1) == "confirmed"
    assert st.set_status(99, "confirmed") is None


def test_assistant_disabled_without_key():
    a = Assistant("", "claude-haiku-5-5", "sys")
    assert not a.enabled
    assert asyncio.run(a.answer(1, "привет")) is None


def test_handlers_build():
    from bot.handlers import build_router
    s = Salon.load(SALON)
    r = build_router(s, Storage(":memory:"), Assistant("", "m", "sys"), 0)
    assert r is not None
