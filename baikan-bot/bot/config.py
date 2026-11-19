import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_chat_id: int
    anthropic_api_key: str
    claude_model: str
    salon_file: str
    db_path: str


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN не задан (см. .env.example)")
    return Config(
        bot_token=token,
        admin_chat_id=int(os.getenv("ADMIN_CHAT_ID", "0") or 0),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
        claude_model=os.getenv("CLAUDE_MODEL", "claude-haiku-5-5").strip(),
        salon_file=os.getenv("SALON_FILE", "salon.json"),
        db_path=os.getenv("DB_PATH", "bookings.db"),
    )
