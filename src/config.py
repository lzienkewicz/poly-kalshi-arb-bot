from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Trading mode — bot refuses live trades unless explicitly enabled
    live_trading: bool = False

    # Per-trade and portfolio risk caps
    trade_size_usdc: float = Field(default=10.0, gt=0)
    max_open_positions: int = Field(default=5, gt=0)
    max_deployed_usdc: float = Field(default=50.0, gt=0)
    max_daily_loss_usdc: float = Field(default=20.0, gt=0)
    net_edge_min: float = Field(default=0.02, gt=0)

    # Fee rates (proportion of notional, e.g. 0.02 = 2%)
    poly_fee_rate: float = Field(default=0.0, ge=0, le=1)
    kalshi_fee_rate: float = Field(default=0.02, ge=0, le=1)

    # Slippage buffers added to each leg's cost
    slip_poly: float = Field(default=0.005, ge=0)
    slip_kalshi: float = Field(default=0.005, ge=0)

    # Stale-data penalty applied per stale leg
    stale_penalty: float = Field(default=0.005, ge=0)
    max_price_age_s: int = Field(default=30, gt=0)

    # Polling interval; floor enforced at 10 s to avoid rate-limit bans
    poll_interval_s: int = Field(default=60, ge=10)

    # Optional Discord webhook; absence disables notifications
    discord_webhook_url: str | None = None

    @field_validator("poll_interval_s")
    @classmethod
    def _poll_floor(cls, v: int) -> int:
        if v < 10:
            raise ValueError("poll_interval_s must be >= 10 to avoid rate-limit bans")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
