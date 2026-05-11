"""Environment-level settings (read from .env / OS env)."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class BotMode(StrEnum):
    DRY = "dry"
    LIVE = "live"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class Settings(BaseSettings):
    """Process-wide settings loaded from environment / .env."""

    mode: BotMode = Field(default=BotMode.DRY, validation_alias="BOT_MODE")
    log_level: LogLevel = Field(default=LogLevel.INFO, validation_alias="BOT_LOG_LEVEL")

    data_dir: Path = Field(default=Path("./data"), validation_alias="BOT_DATA_DIR")
    reports_dir: Path = Field(default=Path("./reports"), validation_alias="BOT_REPORTS_DIR")
    journals_dir: Path = Field(default=Path("./journals"), validation_alias="BOT_JOURNALS_DIR")
    logs_dir: Path = Field(default=Path("./logs"), validation_alias="BOT_LOGS_DIR")
    config_dir: Path = Field(default=Path("./config"), validation_alias="BOT_CONFIG_DIR")
    runtime_dir: Path = Field(default=Path("./runtime"), validation_alias="BOT_RUNTIME_DIR")

    db_url: str = Field(
        default="sqlite:///./data/bot.sqlite",
        validation_alias="DB_URL",
    )

    delta_base_url: HttpUrl = Field(
        default=HttpUrl("https://api.india.delta.exchange"),
        validation_alias="DELTA_BASE_URL",
    )
    delta_ws_url: str = Field(
        default="wss://socket.india.delta.exchange",
        validation_alias="DELTA_WS_URL",
    )
    delta_api_key: str = Field(default="", validation_alias="DELTA_API_KEY")
    delta_api_secret: str = Field(default="", validation_alias="DELTA_API_SECRET")

    delta_rest_rps: int = Field(default=10, validation_alias="DELTA_REST_RPS", ge=1)
    delta_order_rps: int = Field(default=5, validation_alias="DELTA_ORDER_RPS", ge=1)
    delta_ws_mps: int = Field(default=20, validation_alias="DELTA_WS_MPS", ge=1)

    prom_textfile_path: Path = Field(
        default=Path("/var/lib/node_exporter/textfile_collector/bot.prom"),
        validation_alias="PROM_TEXTFILE_PATH",
    )
    prom_http_port: int = Field(default=9091, validation_alias="PROM_HTTP_PORT", ge=1024, le=65535)

    nav_inr_override: float | None = Field(default=None, validation_alias="BOT_NAV_INR")
    usd_inr_rate_override: float | None = Field(default=None, validation_alias="BOT_USD_INR_RATE")

    backblaze_b2_key: str = Field(default="", validation_alias="BACKBLAZE_B2_KEY")
    backblaze_b2_secret: str = Field(default="", validation_alias="BACKBLAZE_B2_SECRET")
    backblaze_b2_bucket: str = Field(default="", validation_alias="BACKBLAZE_B2_BUCKET")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @property
    def is_live(self) -> bool:
        return self.mode == BotMode.LIVE

    @property
    def is_dry(self) -> bool:
        return self.mode == BotMode.DRY


def load_settings(_env_file: str | Path | None = ".env") -> Settings:
    """Return a fresh Settings instance, optionally pointing to a non-default env file.

    `_env_file=None` disables env-file loading (useful in tests where env vars are set directly).
    """
    if _env_file is None:
        return Settings(_env_file=None)  # type: ignore[call-arg]
    return Settings(_env_file=_env_file)  # type: ignore[call-arg]
