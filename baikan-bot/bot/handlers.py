"""Telegram-обработчики: меню, прайс, контакты, запись, свободные вопросы."""
import html
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from .ai import Assistant
from .knowledge import Salon
from .storage import Booking, Storage

log = logging.getLogger(__name__)

BTN_PRICES = "💇 Услуги и цены"
BTN_BOOK = "📝 Записаться"
BTN_CONTACTS = "📍 Адрес и часы"
BTN_ADMIN = "☎️ Связаться с администратором"
BTN_CANCEL = "✖️ Отмена"
BTN_SHARE_PHONE = "📱 Отправить мой номер"

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_PRICES), KeyboardButton(text=BTN_BOOK)],
        [KeyboardButton(text=BTN_CONTACTS), KeyboardButton(text=BTN_ADMIN)],
    ],
    resize_keyboard=True,
    input_field_placeholder="Задайте вопрос или выберите пункт меню",
)
CANCEL_KB = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_CANCEL)]], resize_keyboard=True)
PHONE_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_SHARE_PHONE, request_contact=True)], [KeyboardButton(text=BTN_CANCEL)]],
    resize_keyboard=True,
)


class BookingForm(StatesGroup):
    service = State()
    when = State()
    name = State()
    phone = State()
    confirm = State()


def build_router(salon: Salon, storage: Storage, assistant: Assistant, admin_chat_id: int) -> Router:
    r = Router()
    esc = html.escape

    # ---------- меню ----------
    @r.message(CommandStart())
    async def start(m: Message, state: FSMContext):
        await state.clear()
        await m.answer(
            f"Здравствуйте! 👋 Это бот салона <b>{esc(salon.name)}</b>.\n"
            "Здесь можно посмотреть цены, записаться или просто задать вопрос.",
            reply_markup=MAIN_MENU,
        )

    @r.message(Command("chatid"))
    async def chat_id(m: Message):
        # помогает узнать ADMIN_CHAT_ID при настройке
        await m.answer(f"ID этого чата: <code>{m.chat.id}</code>")

    @r.message(F.text == BTN_CANCEL)
    async def cancel(m: Message, state: FSMContext):
        await state.clear()
        await m.answer("Хорошо, отменили. Чем ещё помочь?", reply_markup=MAIN_MENU)

    @r.message(F.text == BTN_PRICES)
    async def prices(m: Message):
        await m.answer(salon.price_list(), reply_markup=MAIN_MENU)

    @r.message(F.text == BTN_CONTACTS)
    async def contacts(m: Message):
        await m.answer(salon.contacts(), reply_markup=MAIN_MENU, disable_web_page_preview=True)

    @r.message(F.text == BTN_ADMIN)
    async def call_admin(m: Message):
        await m.answer(
            f"Позвоните или напишите администратору: {esc(salon.phone)}\n"
            "Или оставьте заявку через «📝 Записаться» — мы перезвоним.",
            reply_markup=MAIN_MENU,
        )

    # ---------- запись ----------
    def services_kb(category: str | None) -> InlineKeyboardMarkup:
        if category is None:
            rows = [[InlineKeyboardButton(text=c, callback_data=f"cat:{i}")]
                    for i, c in enumerate(salon.categories())]
        else:
            rows = [[InlineKeyboardButton(text=f"{s.name} — {s.price}", callback_data=f"svc:{salon.services.index(s)}")]
                    for s in salon.services_in(category)]
            rows.append([InlineKeyboardButton(text="← Назад", callback_data="cat:back")])
        rows.append([InlineKeyboardButton(text="Другое / не знаю", callback_data="svc:other")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @r.message(F.text == BTN_BOOK)
    async def book_start(m: Message, state: FSMContext):
        await state.clear()
        await state.set_state(BookingForm.service)
        await m.answer("Запишем вас! Можно отменить в любой момент.", reply_markup=CANCEL_KB)
        await m.answer("Выберите категорию услуги:", reply_markup=services_kb(None))

    @r.callback_query(BookingForm.service, F.data.startswith("cat:"))
    async def pick_category(c: CallbackQuery):
        key = c.data.split(":", 1)[1]
        cats = salon.categories()
        if key == "back":
            await c.message.edit_text("Выберите категорию услуги:", reply_markup=services_kb(None))
        elif key.isdigit() and int(key) < len(cats):
            cat = cats[int(key)]
            await c.message.edit_text(f"<b>{esc(cat)}</b> — выберите услугу:", reply_markup=services_kb(cat))
        await c.answer()

    @r.callback_query(BookingForm.service, F.data.startswith("svc:"))
    async def pick_service(c: CallbackQuery, state: FSMContext):
        key = c.data.split(":", 1)[1]
        if key.isdigit() and int(key) < len(salon.services):
            service = salon.services[int(key)].name
        else:
            service = "Другое (уточнить у администратора)"
        await state.update_data(service=service)
        await state.set_state(BookingForm.when)
        await c.message.edit_text(f"Услуга: <b>{esc(service)}</b>")
        await c.message.answer(
            f"Когда вам удобно? Напишите день и время, например «завтра после 15:00».\n"
            f"Мы работаем: {esc(salon.hours)}",
            reply_markup=CANCEL_KB,
        )
        await c.answer()

    @r.message(BookingForm.service)
    async def service_typed(m: Message, state: FSMContext):
        # клиент написал услугу текстом вместо кнопки
        await state.update_data(service=(m.text or "").strip()[:200] or "Не указана")
        await state.set_state(BookingForm.when)
        await m.answer("Когда вам удобно? Напишите день и время.", reply_markup=CANCEL_KB)

    @r.message(BookingForm.when, F.text)
    async def got_when(m: Message, state: FSMContext):
        await state.update_data(when=m.text.strip()[:200])
        await state.set_state(BookingForm.name)
        await m.answer("Как к вам обращаться?", reply_markup=CANCEL_KB)

    @r.message(BookingForm.name, F.text)
    async def got_name(m: Message, state: FSMContext):
        await state.update_data(name=m.text.strip()[:100])
        await state.set_state(BookingForm.phone)
        await m.answer("Оставьте номер телефона для подтверждения — кнопкой ниже или текстом.", reply_markup=PHONE_KB)

    @r.message(BookingForm.phone)
    async def got_phone(m: Message, state: FSMContext):
        if m.contact:
            phone = m.contact.phone_number
        else:
            phone = (m.text or "").strip()
            if sum(ch.isdigit() for ch in phone) < 9:
                await m.answer("Похоже, номер неполный. Пример: +996 555 123 456", reply_markup=PHONE_KB)
                return
        await state.update_data(phone=phone[:30])
        await state.set_state(BookingForm.confirm)
        d = await state.get_data()
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить", callback_data="book:yes"),
            InlineKeyboardButton(text="✖️ Отмена", callback_data="book:no"),
        ]])
        await m.answer("Проверьте заявку:", reply_markup=MAIN_MENU)
        await m.answer(
            f"Услуга: <b>{esc(d['service'])}</b>\nКогда: {esc(d['when'])}\n"
            f"Имя: {esc(d['name'])}\nТелефон: {esc(d['phone'])}",
            reply_markup=kb,
        )

    @r.callback_query(BookingForm.confirm, F.data.startswith("book:"))
    async def confirm(c: CallbackQuery, state: FSMContext, bot: Bot):
        if c.data == "book:no":
            await state.clear()
            await c.message.edit_text("Заявка отменена.")
            await c.answer()
            return
        d = await state.get_data()
        await state.clear()
        b = storage.add(Booking(
            user_id=c.from_user.id,
            username=c.from_user.username or "",
            service=d["service"], when=d["when"], name=d["name"], phone=d["phone"],
        ))
        await c.message.edit_text(
            f"Спасибо, {esc(b.name)}! Заявка №{b.id} принята. "
            "Администратор свяжется с вами для подтверждения времени. 💐"
        )
        await c.answer()
        if admin_chat_id:
            user = f"@{esc(b.username)}" if b.username else f'<a href="tg://user?id={b.user_id}">клиент</a>'
            admin_kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"adm:ok:{b.id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm:no:{b.id}"),
            ]])
            try:
                await bot.send_message(
                    admin_chat_id,
                    f"🆕 <b>Заявка №{b.id}</b>\nУслуга: {esc(b.service)}\nКогда: {esc(b.when)}\n"
                    f"Имя: {esc(b.name)}\nТелефон: {esc(b.phone)}\nTelegram: {user}",
                    reply_markup=admin_kb,
                )
            except Exception:
                log.exception("Не удалось отправить заявку администратору")
        else:
            log.warning("ADMIN_CHAT_ID не задан — заявка №%s только сохранена в базе", b.id)

    # ---------- кнопки администратора ----------
    @r.callback_query(F.data.startswith("adm:"))
    async def admin_action(c: CallbackQuery, bot: Bot):
        if not admin_chat_id or c.message.chat.id != admin_chat_id:
            await c.answer("Недоступно", show_alert=True)
            return
        _, action, raw_id = c.data.split(":")
        booking_id = int(raw_id)
        if storage.status(booking_id) != "new":
            await c.answer("Заявка уже обработана")
            return
        status = "confirmed" if action == "ok" else "declined"
        user_id = storage.set_status(booking_id, status)
        mark = "✅ Подтверждена" if status == "confirmed" else "❌ Отклонена"
        who = esc(c.from_user.full_name)
        await c.message.edit_text(f"{c.message.html_text}\n\n<b>{mark}</b> ({who})")
        await c.answer()
        if user_id:
            text = (f"Ваша запись №{booking_id} подтверждена! Ждём вас 💐"
                    if status == "confirmed" else
                    f"К сожалению, по заявке №{booking_id} не получилось подобрать время. "
                    f"Позвоните нам: {salon.phone}")
            try:
                await bot.send_message(user_id, text)
            except Exception:
                log.exception("Не удалось уведомить клиента")

    # ---------- свободные вопросы ----------
    @r.message(F.text, F.chat.type == "private")
    async def free_text(m: Message, bot: Bot):
        await bot.send_chat_action(m.chat.id, "typing")
        reply = await assistant.answer(m.from_user.id, m.text[:2000])
        if reply is None:
            reply = ("Я пока отвечаю только через меню 🙂 Выберите пункт ниже "
                     f"или позвоните администратору: {salon.phone}")
        await m.answer(esc(reply), reply_markup=MAIN_MENU)

    return r
