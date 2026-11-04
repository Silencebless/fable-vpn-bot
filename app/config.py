"""Настройки приложения из переменных окружения / .env"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


def _split(value: str) -> list[str]:
    return [x.strip() for x in value.replace(";", ",").split(",") if x.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
    )

    # --- Telegram ---
    bot_token: str
    owner_ids: str = ""            # ID владельцев через запятую (полный доступ)
    support_url: str = ""          # https://t.me/your_support

    # --- Публичный адрес (редиректы /dl и /add, вебхуки) ---
    public_base_url: str = "http://localhost:8080"
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    webhook_secret: str = "change-me"   # секретная часть URL вебхуков

    # --- Инфраструктура ---
    database_url: str = "postgresql+asyncpg://fable:fable@postgres:5432/fable"
    redis_url: str = "redis://redis:6379/0"
    timezone: str = "Europe/Moscow"
    log_level: str = "INFO"

    # --- Remnawave ---
    remnawave_url: str = "http://remnawave:3000"
    remnawave_token: str = ""
    remnawave_proxy_headers: bool = True      # слать X-Forwarded-For/Proto (нужно при доступе без reverse-proxy)
    remnawave_caddy_key: str = ""             # X-Api-Key, если панель за Caddy Security
    remnawave_cookie: str = ""                # name=value, если панель за cookie-защитой
    remnawave_webhook_secret: str = ""        # = WEBHOOK_SECRET_HEADER панели

    # --- ЮKassa ---
    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""
    yookassa_receipts: bool = True            # передавать данные чека (54-ФЗ / самозанятые)
    yookassa_vat_code: int = 1                # 1 = без НДС
    yookassa_tax_system_code: int | None = None
    yookassa_check_ip: bool = False           # проверять IP отправителя уведомлений

    # --- Начальные значения тарифов (используются только при первом запуске) ---
    lite_squads: str = ""                     # UUID внутренних сквадов через запятую
    premium_squads: str = ""

    # --- Бэкапы ---
    backup_hour: int = 4                      # час (по timezone), когда слать бэкап владельцам
    backup_chat_id: int | None = None         # куда слать бэкап (по умолчанию — владельцам)

    @property
    def owners(self) -> set[int]:
        out: set[int] = set()
        for x in _split(self.owner_ids):
            try:
                out.add(int(x))
            except ValueError:
                continue
        return out

    @property
    def base_url(self) -> str:
        return self.public_base_url.rstrip("/")

    @property
    def lite_squad_list(self) -> list[str]:
        return _split(self.lite_squads)

    @property
    def premium_squad_list(self) -> list[str]:
        return _split(self.premium_squads)

    @property
    def yookassa_enabled(self) -> bool:
        return bool(self.yookassa_shop_id and self.yookassa_secret_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
