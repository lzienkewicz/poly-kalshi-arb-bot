import pytest
from pydantic import ValidationError

from src.config import Settings, get_settings


@pytest.fixture(autouse=True)
def clear_settings_cache() -> None:
    get_settings.cache_clear()


def test_defaults():
    s = Settings()
    assert s.live_trading is False
    assert s.trade_size_usdc == 10.0
    assert s.max_open_positions == 5
    assert s.max_deployed_usdc == 50.0
    assert s.max_daily_loss_usdc == 20.0
    assert s.net_edge_min == 0.02
    assert s.poly_fee_rate == 0.0
    assert s.kalshi_fee_rate == 0.02
    assert s.slip_poly == 0.005
    assert s.slip_kalshi == 0.005
    assert s.stale_penalty == 0.005
    assert s.max_price_age_s == 30
    assert s.poll_interval_s == 60
    assert s.discord_webhook_url is None


def test_live_trading_default_is_false(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LIVE_TRADING", raising=False)
    s = Settings()
    assert s.live_trading is False


def test_live_trading_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIVE_TRADING", "true")
    s = Settings()
    assert s.live_trading is True


def test_poll_interval_minimum_enforced():
    with pytest.raises(ValidationError, match="poll_interval_s"):
        Settings(poll_interval_s=9)


def test_poll_interval_floor_is_ten():
    s = Settings(poll_interval_s=10)
    assert s.poll_interval_s == 10


def test_trade_size_must_be_positive():
    with pytest.raises(ValidationError):
        Settings(trade_size_usdc=0.0)


def test_net_edge_min_must_be_positive():
    with pytest.raises(ValidationError):
        Settings(net_edge_min=0.0)


def test_fee_rate_bounds():
    with pytest.raises(ValidationError):
        Settings(kalshi_fee_rate=-0.01)
    with pytest.raises(ValidationError):
        Settings(kalshi_fee_rate=1.01)


def test_env_override_numeric(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRADE_SIZE_USDC", "25.0")
    monkeypatch.setenv("POLL_INTERVAL_S", "120")
    s = Settings()
    assert s.trade_size_usdc == 25.0
    assert s.poll_interval_s == 120


def test_get_settings_is_cached():
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2


def test_get_settings_cache_cleared(monkeypatch: pytest.MonkeyPatch):
    get_settings.cache_clear()
    monkeypatch.setenv("TRADE_SIZE_USDC", "7.5")
    s = get_settings()
    assert s.trade_size_usdc == 7.5
