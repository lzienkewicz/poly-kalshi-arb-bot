import pytest
from pydantic import ValidationError

from src.config import Config, FeeConfig, PollingConfig, RiskConfig, SecretsConfig


def test_config_defaults():
    c = Config()
    assert c.live_trading is False
    assert c.log_level == "INFO"
    assert c.risk.trade_size_usdc == 10.0
    assert c.risk.max_open_positions == 5
    assert c.risk.max_deployed_usdc == 50.0
    assert c.risk.max_daily_loss_usdc == 20.0
    assert c.risk.net_edge_min == 0.02
    assert c.risk.max_price_age_s == 30.0
    assert c.risk.stale_penalty_per_leg == 0.005
    assert c.risk.slippage_buffer_per_leg == 0.005
    assert c.fees.polymarket_taker_fee == 0.0
    assert c.fees.kalshi_taker_fee == 0.02
    assert c.polling.poll_interval_s == 60.0
    assert c.secrets.discord_webhook_url is None


def test_live_trading_default_false():
    c = Config()
    assert c.live_trading is False


def test_mode_label_paper():
    c = Config()
    assert c.mode_label == "PAPER"


def test_risk_deployed_must_be_gte_trade_size():
    with pytest.raises(ValidationError, match="max_deployed_usdc"):
        RiskConfig(trade_size_usdc=20.0, max_deployed_usdc=10.0)


def test_risk_trade_size_positive():
    with pytest.raises(ValidationError):
        RiskConfig(trade_size_usdc=0.0)


def test_risk_net_edge_min_bounds():
    with pytest.raises(ValidationError):
        RiskConfig(net_edge_min=-0.01)
    with pytest.raises(ValidationError):
        RiskConfig(net_edge_min=1.01)


def test_fee_rate_bounds():
    with pytest.raises(ValidationError):
        FeeConfig(kalshi_taker_fee=-0.01)
    with pytest.raises(ValidationError):
        FeeConfig(kalshi_taker_fee=1.01)


def test_poll_interval_floor():
    with pytest.raises(ValidationError, match="poll_interval_s"):
        PollingConfig(poll_interval_s=9.0)


def test_poll_interval_floor_exact():
    p = PollingConfig(poll_interval_s=10.0)
    assert p.poll_interval_s == 10.0


def test_live_trading_requires_secrets():
    with pytest.raises(ValidationError, match="secrets are missing"):
        Config(live_trading=True)


def test_config_load():
    c = Config.load()
    assert isinstance(c, Config)


def test_env_override_risk(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RISK_TRADE_SIZE_USDC", "25.0")
    r = RiskConfig()
    assert r.trade_size_usdc == 25.0


def test_env_override_poll(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("POLL_POLL_INTERVAL_S", "120.0")
    p = PollingConfig()
    assert p.poll_interval_s == 120.0
