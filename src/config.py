from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RiskConfig(BaseSettings):
    trade_size_usdc: Annotated[float, Field(gt=0.0, le=1000.0)] = Field(default=10.0)
    max_open_positions: Annotated[int, Field(ge=1, le=100)] = Field(default=5)
    max_deployed_usdc: Annotated[float, Field(gt=0.0)] = Field(default=50.0)
    max_daily_loss_usdc: Annotated[float, Field(gt=0.0)] = Field(default=20.0)
    net_edge_min: Annotated[float, Field(ge=0.0, le=1.0)] = Field(default=0.02)
    max_price_age_s: Annotated[float, Field(gt=0.0)] = Field(default=30.0)
    stale_penalty_per_leg: Annotated[float, Field(ge=0.0, le=1.0)] = Field(default=0.005)
    slippage_buffer_per_leg: Annotated[float, Field(ge=0.0, le=1.0)] = Field(default=0.005)

    model_config = SettingsConfigDict(env_prefix="RISK_", env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def _validate_deployed_vs_trade_size(self) -> "RiskConfig":
        if self.max_deployed_usdc < self.trade_size_usdc:
            raise ValueError(
                f"max_deployed_usdc ({self.max_deployed_usdc}) must be >= "
                f"trade_size_usdc ({self.trade_size_usdc})"
            )
        return self


class FeeConfig(BaseSettings):
    polymarket_taker_fee: Annotated[float, Field(ge=0.0, le=1.0)] = Field(default=0.0)
    kalshi_taker_fee: Annotated[float, Field(ge=0.0, le=1.0)] = Field(default=0.02)

    model_config = SettingsConfigDict(env_prefix="FEE_", env_file=".env", extra="ignore")


class PollingConfig(BaseSettings):
    poll_interval_s: Annotated[float, Field(ge=10.0)] = Field(default=60.0)
    cooldown_after_trade_s: Annotated[float, Field(ge=0.0)] = Field(default=30.0)
    cooldown_after_error_s: Annotated[float, Field(ge=0.0)] = Field(default=10.0)

    model_config = SettingsConfigDict(env_prefix="POLL_", env_file=".env", extra="ignore")

    @field_validator("poll_interval_s", mode="before")
    @classmethod
    def _enforce_floor(cls, v: float) -> float:
        if float(v) < 10.0:
            raise ValueError("poll_interval_s must be >= 10 seconds to respect venue rate limits")
        return float(v)


class SecretsConfig(BaseSettings):
    polymarket_api_key: SecretStr | None = Field(default=None)
    polymarket_api_secret: SecretStr | None = Field(default=None)
    kalshi_api_key: SecretStr | None = Field(default=None)
    kalshi_api_secret: SecretStr | None = Field(default=None)
    discord_webhook_url: HttpUrl | None = Field(default=None)

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


class Config(BaseSettings):
    live_trading: bool = Field(default=False)
    log_level: str = Field(default="INFO")
    match_domain: str = Field(default="sports")  # sports | crypto | politics | entertainment | all

    approved_pairs_path: Path = Field(default=Path("config/approved_pairs.json"))
    trades_db_path: Path = Field(default=Path("data/trades.db"))
    log_dir: Path = Field(default=Path("logs"))

    risk: RiskConfig = Field(default_factory=RiskConfig)
    fees: FeeConfig = Field(default_factory=FeeConfig)
    polling: PollingConfig = Field(default_factory=PollingConfig)
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def _validate_live_mode_requirements(self) -> "Config":
        if self.live_trading:
            missing: list[str] = []
            s = self.secrets
            if s.polymarket_api_key is None:
                missing.append("POLYMARKET_API_KEY")
            if s.polymarket_api_secret is None:
                missing.append("POLYMARKET_API_SECRET")
            if s.kalshi_api_key is None:
                missing.append("KALSHI_API_KEY")
            if s.kalshi_api_secret is None:
                missing.append("KALSHI_API_SECRET")
            if missing:
                raise ValueError(
                    f"LIVE_TRADING=true but the following secrets are missing: {', '.join(missing)}"
                )
        return self

    @model_validator(mode="after")
    def _ensure_directories(self) -> "Config":
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.trades_db_path.parent.mkdir(parents=True, exist_ok=True)
        return self

    @classmethod
    def load(cls) -> "Config":
        return cls()

    @property
    def mode_label(self) -> str:
        return "LIVE" if self.live_trading else "PAPER"
