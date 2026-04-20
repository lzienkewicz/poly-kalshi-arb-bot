from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field, computed_field, model_validator


class BookLevel(BaseModel):
    """A single price level in an order book."""

    price: float = Field(ge=0.0, le=1.0, description="Price in [0, 1] dollars per contract")
    size: float = Field(ge=0.0, description="Available contracts at this price level")

    model_config = {"frozen": True}


class BookSide(BaseModel):
    """One side (bids or asks) of a binary contract's order book."""

    levels: list[BookLevel] = Field(default_factory=list, description="Price levels, best price first")

    @property
    def best_price(self) -> float | None:
        return self.levels[0].price if self.levels else None

    @property
    def best_size(self) -> float | None:
        return self.levels[0].size if self.levels else None

    @property
    def total_available(self) -> float:
        return sum(lvl.size for lvl in self.levels)

    def available_at_price(self, price: float) -> float:
        """Return total size available at exactly this price level."""
        return sum(lvl.size for lvl in self.levels if lvl.price == price)

    model_config = {"frozen": True}


class OrderBook(BaseModel):
    """
    Top-of-book and depth snapshot for one side (YES or NO) of a binary contract.

    Prices are always expressed in dollars in [0, 1].
    Kalshi prices (in cents) must be converted before construction.
    """

    venue_market_id: str = Field(description="Venue-native market identifier this book belongs to")
    outcome: str = Field(description="Which outcome this book represents: 'YES' or 'NO'")

    bids: BookSide = Field(default_factory=BookSide, description="Resting buy orders, best bid first")
    asks: BookSide = Field(default_factory=BookSide, description="Resting sell orders, best ask first")

    # Snapshot timing
    snapshot_ts: datetime = Field(description="UTC timestamp when this snapshot was taken")

    @computed_field  # type: ignore[misc]
    @property
    def best_bid(self) -> float | None:
        return self.bids.best_price

    @computed_field  # type: ignore[misc]
    @property
    def best_ask(self) -> float | None:
        return self.asks.best_price

    @computed_field  # type: ignore[misc]
    @property
    def mid(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None

    @computed_field  # type: ignore[misc]
    @property
    def spread(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None

    def age_seconds(self, now: datetime | None = None) -> float:
        """Seconds elapsed since this snapshot was taken."""
        if now is None:
            now = datetime.now(tz=timezone.utc)
        return (now - self.snapshot_ts).total_seconds()

    def is_stale(self, max_age_s: float, now: datetime | None = None) -> bool:
        return self.age_seconds(now) > max_age_s

    def available_at_ask(self) -> float:
        """Contracts available at the current best ask."""
        if self.best_ask is None:
            return 0.0
        return self.asks.available_at_price(self.best_ask)

    @model_validator(mode="after")
    def _validate_prices_consistent(self) -> "OrderBook":
        bid = self.best_bid
        ask = self.best_ask
        if bid is not None and ask is not None and bid > ask:
            raise ValueError(f"Best bid {bid} exceeds best ask {ask} — crossed book")
        return self

    model_config = {"frozen": True}
