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
    # 2-min floor (ge=2): the README rate-limit invariant — never poll the
    # Polestar API faster than ~60 s; throttling shows up as "not authorized".
    poll_interval_min: int = Field(default=10, ge=2, le=1440)
    fast_poll_interval_min: int = Field(default=2, ge=2, le=60)

    # --- C3 (Volvo Cloud Connectivity) polling ---
    # A second library (unofficial-polestar-api) hitting the same account, so
    # the cadence is deliberately slower than the main poll and staggered
    # against it (see scheduler._reschedule_c3) to spare the shared rate limit.
    c3_enabled: bool = True
    # Base (idle) cadence. Kept short enough that a typical drive spans at least
    # one poll, so the movement heuristic in scheduler.c3_poll_once can flip into
    # the faster drive cadence below. Going lower raises throttle risk on the
    # shared account ("not authorized") — tune via C3_POLL_INTERVAL_MIN.
    c3_poll_interval_min: int = Field(default=5, ge=2, le=1440)
    c3_fast_poll_interval_min: int = Field(default=5, ge=2, le=60)
    # Faster cadence while the car is *driving* so the GPS trail is dense enough
    # to draw a real route line. Raises API load on the shared account — set
    # high (≈ c3_poll_interval_min) to effectively disable.
    c3_drive_poll_interval_min: int = Field(default=2, ge=2, le=60)

    # --- Adaptive idle / night throttling ---
    # Stretch the poll cadence when the car is at rest, to spare the shared
    # account and the 12V battery. Only ever slows down — never applied while
    # charging or driving (those keep their fast/drive cadence so we never miss
    # a charge-complete or a route). Two triggers:
    #   * hard night window (local time) → strong slowdown, car is parked anyway
    #   * car locked & parked during the day → moderate slowdown ("not in use")
    # "Locked" is read from the latest C3 snapshot; with C3 off only the night
    # rule applies. The dashboard/Test live reads still fetch fresh on demand,
    # so this only affects background persistence cadence.
    adaptive_idle_enabled: bool = True
    # Local hours [from, to) treated as hard night. Wraps midnight when from>to.
    night_hour_from: int = Field(default=23, ge=0, le=23)
    night_hour_to: int = Field(default=5, ge=0, le=23)
    # Slow cadences (minutes). Night is the strongest; idle is the daytime
    # locked-and-parked rate. Each is a floor — a longer base interval wins.
    night_poll_interval_min: int = Field(default=30, ge=2, le=1440)
    c3_night_poll_interval_min: int = Field(default=30, ge=2, le=1440)
    idle_poll_interval_min: int = Field(default=20, ge=2, le=1440)
    c3_idle_poll_interval_min: int = Field(default=15, ge=2, le=1440)

    notify_door_open: bool = True
    notify_unlocked: bool = True
    # Opt-in: send a notification when the app logs an ERROR/CRITICAL. Deduped
    # to at most one per logger per hour so a recurring fault can't spam. Off by
    # default — most users watch the /log tab instead.
    notify_log_errors: bool = False
    # Optional URL to a real Polestar 2 image shown on the dashboard vehicle
    # card (e.g. an official press/gallery render). Empty ⇒ the built-in
    # recolorable SVG top-view is used. Kept as config (not committed) so we
    # never bundle third-party copyrighted imagery into the repo.
    car_image_url: str = ""

    # --- HTTP server ---
    host: str = "0.0.0.0"
    port: int = 8000
    # Public, externally-reachable base URL of this app (e.g.
    # "https://poledash.example.com"), used to build the one-tap confirm links
    # in automation "suggest"-mode push notifications. Empty ⇒ the notification
    # degrades to a text hint ("open poledash to confirm") with no button.
    public_base_url: str = ""

    # --- Persistence ---
    data_dir: Path = Path("/data")
    backup_keep_days: int = Field(default=14, ge=1, le=3650)
    # Activity-log history: the in-memory ring (last 2000 lines) is flushed to
    # the LogEntry table every N minutes and pruned after this many days.
    log_persist_interval_min: int = Field(default=2, ge=1, le=60)
    log_retention_days: int = Field(default=90, ge=1, le=3650)

    # --- Petrol comparison (Insights: cost & CO2 vs an equivalent ICE) ---
    # Defaults: a typical petrol car ~7 l/100 km at €1.75/l, ~150 g CO2/km
    # tank-to-wheel. Runtime-overridable via /settings (AppSetting).
    petrol_price_eur_per_l: float = Field(default=1.75, ge=0, le=10)
    petrol_l_per_100km: float = Field(default=7.0, ge=0, le=30)
    petrol_co2_g_per_km: int = Field(default=150, ge=0, le=500)

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

    # --- Email (SMTP) ---
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_to: str = ""

    # --- Software update watch ---
    software_manual_url: str = (
        "https://www.polestar.com/uk/manual/polestar-2/2027/software-updates/"
    )

    # --- Demo mode (public showcase instance) ---
    demo_mode: bool = False
    demo_banner_text: str = (
        "DEMO INSTANCE — synthetic data, coordinates München-shifted."
    )

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
