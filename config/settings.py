"""
Trillionaire Algo Engine - Global Configuration & Settings
Loads runtime environment variables and YAML configurations using pydantic-settings.
"""

from datetime import datetime, time
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from shared.constants import IST_TZ
from shared.enums import TradingMode
from shared.exceptions import ConfigurationError

CONFIG_DIR = Path(__file__).parent.resolve()
CAPITAL_TIERS_FILE = CONFIG_DIR / "capital_tiers.yaml"
SECTOR_MAP_FILE = CONFIG_DIR / "sector_map.yaml"


class Settings(BaseSettings):
    """Production runtime settings loaded from environment and .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Core Application Identity
    app_name: str = Field(default="Trillionaire Algo Engine", description="Application display name")
    environment: str = Field(default="development", description="Environment: development, staging, production")
    trading_mode: TradingMode = Field(default=TradingMode.PAPER, description="PAPER, LIVE, or REPLAY")
    active_capital_tier: str = Field(default="TIER_0_PAPER", description="Key for active tier in capital_tiers.yaml")

    # Zerodha Kite Connect Credentials
    kite_api_key: str = Field(default="", description="Zerodha Kite API Key")
    kite_api_secret: str = Field(default="", description="Zerodha Kite API Secret")
    kite_access_token: Optional[str] = Field(default=None, description="Active session access token")
    kite_totp_secret: str = Field(default="", description="Base32 TOTP secret for headless 2FA auth")
    kite_user_id: str = Field(default="", description="Zerodha Client Login ID")
    kite_password: str = Field(default="", description="Zerodha Client Login Password for Headless Auth")

    # Infrastructure Connection URIs
    redis_url: str = Field(default="redis://localhost:6379/0", description="Redis pub/sub & caching URI")
    postgres_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/trillionaire",
        description="Async PostgreSQL connection URI",
    )

    # Sentinel AI & News API Keys
    gemini_api_key: str = Field(default="", description="Google Gemini API key for pre-market headline triage")
    news_api_key: str = Field(default="", description="Financial news feed API key (optional)")

    # Telegram Bot & Operator Security
    telegram_bot_token: str = Field(default="", description="Telegram Bot API token for mobile push alerts")
    telegram_chat_id: str = Field(default="", description="Authorized Telegram Chat ID for alerts and /kill command")
    trillionaire_admin_key: str = Field(
        default="trillionaire-dev-secret-key-change-in-prod",
        description="Admin API Secret Key for Mutating /control/* Endpoints",
    )

    # IST Algorithmic Session Boundaries
    no_entry_before: str = Field(default="09:25", description="Earliest time (IST HH:MM) for new entries")
    no_entry_after: str = Field(default="14:30", description="Latest time (IST HH:MM) for new entries")
    force_square_off: str = Field(default="15:10", description="Mandatory time (IST HH:MM) to square off all MIS")

    # Logging & Observability
    log_level: str = Field(default="INFO", description="Loguru logging level")

    @field_validator("no_entry_before", "no_entry_after", "force_square_off")
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        try:
            parts = [int(p) for p in v.split(":")]
            if len(parts) != 2 or not (0 <= parts[0] <= 23 and 0 <= parts[1] <= 59):
                raise ValueError
        except Exception:
            raise ValueError(f"Invalid time format '{v}', expected HH:MM (e.g., 09:25)")
        return v

    def get_time(self, field_val: str) -> time:
        """Parse 'HH:MM' string to datetime.time."""
        hour, minute = map(int, field_val.split(":"))
        return time(hour, minute, 0)

    @property
    def no_entry_before_time(self) -> time:
        return self.get_time(self.no_entry_before)

    @property
    def no_entry_after_time(self) -> time:
        return self.get_time(self.no_entry_after)

    @property
    def force_square_off_time(self) -> time:
        return self.get_time(self.force_square_off)

    def is_entry_allowed(self, current_dt: Optional[datetime] = None) -> bool:
        """Check if current IST timestamp falls within the allowed entry window."""
        if current_dt is None:
            current_dt = datetime.now(IST_TZ)
        elif current_dt.tzinfo is None:
            current_dt = current_dt.replace(tzinfo=IST_TZ)
        else:
            current_dt = current_dt.astimezone(IST_TZ)

        curr_time = current_dt.time()
        return self.no_entry_before_time <= curr_time <= self.no_entry_after_time

    def is_force_square_off_due(self, current_dt: Optional[datetime] = None) -> bool:
        """Check if current IST timestamp is at or past force square-off time."""
        if current_dt is None:
            current_dt = datetime.now(IST_TZ)
        elif current_dt.tzinfo is None:
            current_dt = current_dt.replace(tzinfo=IST_TZ)
        else:
            current_dt = current_dt.astimezone(IST_TZ)

        curr_time = current_dt.time()
        return curr_time >= self.force_square_off_time

    def load_capital_tiers(self) -> Dict[str, Any]:
        """Load and return the raw capital tiers mapping from capital_tiers.yaml."""
        if not CAPITAL_TIERS_FILE.exists():
            raise ConfigurationError(f"Capital tiers config file not found: {CAPITAL_TIERS_FILE}")
        with open(CAPITAL_TIERS_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data.get("tiers", {})

    def get_active_capital_tier_config(self) -> Dict[str, Any]:
        """Fetch the dictionary configuration for the currently active capital tier."""
        tiers = self.load_capital_tiers()
        if self.active_capital_tier not in tiers:
            raise ConfigurationError(
                f"Active capital tier '{self.active_capital_tier}' not defined in {CAPITAL_TIERS_FILE}."
            )
        return tiers[self.active_capital_tier]

    def load_sector_map(self) -> Dict[str, Any]:
        """Load and return the sector index and stock mappings from sector_map.yaml."""
        if not SECTOR_MAP_FILE.exists():
            raise ConfigurationError(f"Sector map config file not found: {SECTOR_MAP_FILE}")
        with open(SECTOR_MAP_FILE, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def get_tracked_symbols(self) -> List[str]:
        """Retrieve list of all active tradingsymbols defined in sector_map.yaml."""
        sector_data = self.load_sector_map()
        return [stock["tradingsymbol"] for stock in sector_data.get("stocks", [])]


# Singleton instance
settings = Settings()
