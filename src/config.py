from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Risk limits — nested model so they can be validated as a unit
# ---------------------------------------------------------------------------


class RiskConfig(BaseSettings):
    """
    All monetary values in USD.
    All time values in seconds unless the field name says otherwise.

    These are loaded from environment variables with the RISK_ prefix.
    Every field has a default; the outer Config validates min/max bounds.
    """

    trade_size_usdc: Annotated[float, Field(gt=0.0, le=1000.0)] = Field(
        default=10.0,
        description="Notional per trade in USDC. Hard cap — never exceeded.",
    )
    max_open_positions: Annotated[int, Field(ge=1, le=100)] = Field(
        default=5,
        description="Maximum number of simultaneously open positions.",
    )
    max_deployed_usdc: Annotated[float, Field(gt=0.0)] = Field(
        default=50.0,
        description="Maximum total notional deployed across all open positions.",
    )
    max_daily_loss_usdc: Annotated[float, Field(gt=0.0)] = Field(
        default=20.0,
        description="Bot halts for the remainder of the UTC day if realized loss exceeds this.",
    )
    net_edge_min: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        default=0.02,
        description="Minimum net edge in dollars required to classify an opportunity as TRADEABLE.",
    )
    max_price_age_s: Annotated[float, Field(gt=0.0)] = Field(
        default=30.0,
        description="Price quotes older than this (in seconds) incur a stale penalty per leg.",
    )
    stale_penalty_per_leg: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        default=0.005,
        description="Cost added to net edge calculation for each stale leg.",
    )
    slippage_buffer_per_leg: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        default=0.005,
        description="Slippage allowance subtracted per leg regardless of quote freshness.",
    )

    model_config = SettingsConfigDict(env_prefix="RISK_", env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def _validate_deployed_vs_trade_size(self) -> "RiskConfig":
        if self.max_deployed_usdc < self.trade_size_usdc:
            raise ValueError(
                f"max_deployed_usdc ({self.max_deployed_usdc}) must be >= "
                f"trade_size_usdc ({self.trade_size_usdc})"
            )
        return self


# ---------------------------------------------------------------------------
# Fee configuration — one set per venue
# ---------------------------------------------------------------------------


class FeeConfig(BaseSettings):
    """
    Fee rates expressed as a fraction of notional (e.g. 0.02 = 2%).
    Loaded from environment variables.
    """

    polymarket_taker_fee: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        default=0.0,
        description="Polymarket taker fee rate. 0.0 until officially confirmed otherwise.",
    )
    kalshi_taker_fee: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        default=0.02,
        description="Kalshi taker fee rate (2% of notional by default).",
    )

    model_config = SettingsConfigDict(env_prefix="FEE_", env_file=".env", extra="ignore")


# ---------------------------------------------------------------------------
# Polling / timing
# ---------------------------------------------------------------------------


class PollingConfig(BaseSettings):
    """
    Controls how frequently the bot queries each venue.
    Floor of 10 s is enforced to avoid rate-limit bans.
    """

    poll_interval_s: Annotated[float, Field(ge=10.0)] = Field(
        default=60.0,
        description="Seconds between full market scans.",
    )
    cooldown_after_trade_s: Annotated[float, Field(ge=0.0)] = Field(
        default=30.0,
        description="Seconds to pause after executing a trade before scanning again.",
    )
    cooldown_after_error_s: Annotated[float, Field(ge=0.0)] = Field(
        default=10.0,
        description="Seconds to pause after a venue fetch error before retrying.",
    )

    model_config = SettingsConfigDict(env_prefix="POLL_", env_file=".env", extra="ignore")

    @field_validator("poll_interval_s", mode="before")
    @classmethod
    def _enforce_floor(cls, v: float) -> float:
        if float(v) < 10.0:
            raise ValueError("poll_interval_s must be >= 10 seconds to respect venue rate limits")
        return float(v)


# ---------------------------------------------------------------------------
# Secrets — API keys and webhook
# ---------------------------------------------------------------------------


class SecretsConfig(BaseSettings):
    """
    Loaded exclusively from environment variables (never from .env committed to source control).
    All fields are SecretStr so they are masked in logs and repr output.
    """

    polymarket_api_key: SecretStr | None = Field(
        default=None,
        description="Polymarket CLOB API key. Required in live mode.",
    )
    polymarket_api_secret: SecretStr | None = Field(
        default=None,
        description="Polymarket CLOB API secret. Required in live mode.",
    )
    kalshi_api_key: SecretStr | None = Field(
        default=None,
        description="Kalshi API key. Required in live mode.",
    )
    kalshi_api_secret: SecretStr | None = Field(
        default=None,
        description="Kalshi API secret. Required in live mode.",
    )
    discord_webhook_url: HttpUrl | None = Field(
        default=None,
        description="Discord webhook for trade alerts. Alerts are suppressed when None.",
    )

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


# ---------------------------------------------------------------------------
# Top-level bot configuration
# ---------------------------------------------------------------------------


class Config(BaseSettings):
    """
    Master configuration object.

    Loaded once at startup via Config.load(). The bot must not start if
    any required field is missing or any validator raises.

    Sub-configs are loaded from their own env-prefix namespaces.
    """

    # Mode
    live_trading: bool = Field(
        default=False,
        description="Paper mode when False. Must be explicitly set True to enable live order placement.",
    )
    log_level: str = Field(default="INFO")

    # Paths
    approved_pairs_path: Path = Field(
        default=Path("config/approved_pairs.json"),
        description="Path to JSON file listing pre-approved market pairs.",
    )
    trades_db_path: Path = Field(
        default=Path("data/trades.db"),
        description="SQLite database for trade records.",
    )
    log_dir: Path = Field(
        default=Path("logs"),
        description="Directory for daily JSONL decision logs.",
    )

    # Sub-configs — instantiated via model_validator so their own validators run
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
        """
        Entry point for the bot.  Call once at startup.
        Raises ValidationError (which exits the process) on any misconfiguration.
        """
        return cls()

    @property
    def mode_label(self) -> str:
        return "LIVE" if self.live_trading else "PAPER"
