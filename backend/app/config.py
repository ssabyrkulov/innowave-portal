from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    secret_key: str = "change-me-to-a-long-random-string"
    access_token_expire_minutes: int = 720
    algorithm: str = "HS256"
    database_url: str = "sqlite:///./payment_calendar.db"

    # Comma-separated list of allowed frontend origins, or "*" for any.
    cors_origins: str = "*"

    # Секрет для автоприёма файлов из Google Drive (Apps Script).
    # Пустая строка = автоприём выключен.
    inbox_token: str = ""

    first_admin_email: str = "admin@innowave.group"
    first_admin_password: str = "admin123"
    first_admin_name: str = "Administrator"


settings = Settings()
