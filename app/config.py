"""Configuration loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Data sources ---------------------------------------------------
    # Free key: https://developer.ticketmaster.com/  (Discovery API v2)
    ticketmaster_api_key: str = ""

    # Optional demand signal: https://developer.spotify.com/dashboard
    spotify_client_id: str = ""
    spotify_client_secret: str = ""

    # AXS has no public API and its ToS prohibits scraping. The adapter is
    # disabled by default and refuses to run against paths robots.txt blocks.
    # See docs/SOURCES.md before turning this on.
    enable_axs: bool = False

    # --- Markets to watch ----------------------------------------------
    # Ticketmaster DMA ids, comma separated. 324=Seattle-Tacoma, 345=NY,
    # 324/382 etc. `/markets` on the dashboard lists resolved names.
    market_dma_ids: str = "324"
    # Classification to poll. "Music" keeps it to concerts.
    classification_name: str = "Music"
    # How far ahead to look.
    lookahead_days: int = 240

    # --- Polling ---------------------------------------------------------
    poll_interval_minutes: int = 180
    # Ticketmaster's documented limit is 5000 requests/day, 5 req/sec.
    requests_per_second: float = 4.0

    # --- Alerting --------------------------------------------------------
    alert_score_threshold: float = 0.62
    # Never re-alert the same event inside this window.
    alert_cooldown_hours: int = 48
    max_alerts_per_email: int = 12

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    email_from: str = ""
    email_to: str = ""
    # Render emails to ./outbox/ instead of sending. Great for dry runs.
    email_dry_run: bool = True

    # --- Portfolio tracking ---------------------------------------------
    # Hour (UTC) to run the daily mark-to-market report on your holdings.
    daily_report_hour_utc: int = 15
    # Text/email you immediately when a position moves this much in a day.
    position_move_alert_pct: float = 0.15
    # Also send the daily portfolio summary as an SMS.
    sms_daily_summary: bool = False

    # --- SMS (optional, Twilio) -----------------------------------------
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    sms_to: str = ""

    # --- Storage ---------------------------------------------------------
    database_url: str = "sqlite:///./concertscraper.db"

    @property
    def dma_id_list(self) -> list[str]:
        return [p.strip() for p in self.market_dma_ids.split(",") if p.strip()]

    @property
    def recipients(self) -> list[str]:
        return [p.strip() for p in self.email_to.split(",") if p.strip()]

    @property
    def sms_recipients(self) -> list[str]:
        return [p.strip() for p in self.sms_to.split(",") if p.strip()]

    @property
    def sms_configured(self) -> bool:
        return bool(
            self.twilio_account_sid
            and self.twilio_auth_token
            and self.twilio_from_number
            and self.sms_recipients
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
