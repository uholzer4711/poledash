from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Polestar ---
    polestar_email: str
    polestar_password: str
    polestar_vins: str = ""

    # --- Polling ---
    poll_interval_min: int = Field(default=10, ge=1, le=1440)
    fast_poll_interval_min: int = Field(default=2, ge=1, le=60)

    # --- HTTP server ---
    host: str = "0.0.0.0"
    port: int = 8000

    # --- Persistence ---
    data_dir: Path = Path("/data")
    backup_keep_days: int = Field(default=14, ge=1, le=3650)

    # --- ntfy.sh ---
    ntfy_url: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_username: str = ""
    ntfy_password: str = ""

    # --- Telegram ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- Notification thresholds ---
    notify_low_soc: int = Field(default=20, ge=0, le=100)
    notify_service_days: int = Field(default=30, ge=0, le=365)

    # --- MQTT (optional, e.g. for Home Assistant) ---
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_base_topic: str = "poledash"
    mqtt_ha_discovery: bool = True

    @property
    def vin_list(self) -> list[str]:
        return [v.strip() for v in self.polestar_vins.split(",") if v.strip()]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "polestar.sqlite"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.data_dir.mkdir(parents=True, exist_ok=True)
        _settings.backup_dir.mkdir(parents=True, exist_ok=True)
    return _settings
