import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from .ai import Assistant
from .config import load_config
from .handlers import build_router
from .knowledge import Salon
from .storage import Storage


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    salon = Salon.load(cfg.salon_file)
    storage = Storage(cfg.db_path)
    assistant = Assistant(cfg.anthropic_api_key, cfg.claude_model, salon.system_prompt())
    if not assistant.enabled:
        logging.warning("ANTHROPIC_API_KEY не задан — свободные вопросы отключены, работает только меню")

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(build_router(salon, storage, assistant, cfg.admin_chat_id))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
