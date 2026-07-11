from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, overridable via COLDWATCH_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="COLDWATCH_", env_file=".env", extra="ignore")

    # --- database ---
    database_url: str = "postgresql+asyncpg://coldwatch:coldwatch@localhost:5432/coldwatch"

    # --- MQTT (внешнее, неизменяемое железо: контроллеры публикуют float в топики) ---
    mqtt_enabled: bool = True
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    # Фильтр подписки. Топики, не привязанные к датчикам, попадают в очередь
    # обнаружения, поэтому широкий фильтр безопасен.
    mqtt_topic_filter: str = "#"
    mqtt_reconnect_min_delay: float = 1.0
    mqtt_reconnect_max_delay: float = 60.0

    # --- ingest ---
    # Физически правдоподобный диапазон температур; всё вне его — неисправность датчика.
    plausible_min_c: float = -60.0
    plausible_max_c: float = 60.0
    ingest_flush_interval_s: float = 1.0
    ingest_max_buffer: int = 500

    # --- rule engine ---
    rules_enabled: bool = True
    offline_check_interval_s: float = 5.0

    # --- notifications ---
    telegram_bot_token: str | None = None
    # SOCKS5-прокси для api.telegram.org (например socks5://user:pass@host:1080);
    # обязателен на площадках, где прямой доступ к Telegram блокируется
    telegram_proxy: str | None = None
    notify_retry_attempts: int = 3
    notify_retry_delay_s: float = 2.0

    # --- auth ---
    # пароль первичного администратора (логин admin); используется один раз,
    # при пустой таблице пользователей. Не задан — пароль генерируется в лог.
    admin_password: str | None = None
    # включить при работе через reverse-proxy с TLS
    session_cookie_secure: bool = False

    # --- display ---
    display_timezone: str = "Europe/Moscow"
    # каталог собранного web-интерфейса (frontend/dist)
    static_dir: str = "static"


@lru_cache
def get_settings() -> Settings:
    return Settings()
