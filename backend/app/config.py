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

    # --- Интеграция SalesDoc (сверка учёта) ---
    # Заполняются переменными окружения на Render; в репозиторий не коммитим.
    # Пустой salesdoc_url = интеграция выключена.
    salesdoc_url: str = ""          # напр. https://innowave.salesdoc.io
    salesdoc_login: str = ""
    salesdoc_password: str = ""
    salesdoc_filial: str = ""       # filial_id; пусто = без филиальной структуры
    # Статический режим: если заданы токен и userId (как в интеграции 1С),
    # портал использует их напрямую и НЕ делает login — тогда портал и 1С не
    # гасят токены друг друга (у SalesDoc один токен на аккаунт).
    salesdoc_token: str = ""
    salesdoc_user_id: str = ""


settings = Settings()
