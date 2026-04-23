"""
Tests for src/crypto_semantic.py

Covers:
  - Asset normalization (Bitcoin ↔ BTC, Ethereum ↔ ETH, etc.)
  - Threshold extraction and normalization ($100k = $100,000 = 100k)
  - Comparator extraction (hit / reach / close above are distinct)
  - Deadline normalization (end of year, Q4, month+year)
  - Event family routing (touch_target vs above_below_on_date vs ETF vs protocol)
  - All hard-reject rules (asset, comparator, threshold, deadline, approval_target, conditional)
  - Compatible cases that should PASS
"""

from __future__ import annotations

import pytest

from src.crypto_semantic import (
    CryptoSemantics,
    _deadlines_compatible,
    _extract_asset,
    _extract_comparator,
    _extract_deadline,
    _extract_threshold,
    _extract_event_family,
    _CURRENT_YEAR,
    crypto_semantics_compatible,
    extract_crypto_semantics,
)


# ---------------------------------------------------------------------------
# Asset normalization
# ---------------------------------------------------------------------------

class TestAssetNormalization:
    def test_bitcoin_to_btc(self):
        assert _extract_asset("Will Bitcoin hit $100k by December 2025?") == "BTC"

    def test_btc_to_btc(self):
        assert _extract_asset("Will BTC reach $100,000 by December 2025?") == "BTC"

    def test_ethereum_to_eth(self):
        assert _extract_asset("Will Ethereum hit $5000?") == "ETH"

    def test_ether_to_eth(self):
        assert _extract_asset("Will Ether reach $5000?") == "ETH"

    def test_eth_ticker_to_eth(self):
        assert _extract_asset("Will ETH close above $5000 on Dec 31?") == "ETH"

    def test_solana_to_sol(self):
        assert _extract_asset("Will Solana hit $200?") == "SOL"

    def test_ripple_to_xrp(self):
        assert _extract_asset("Will Ripple reach $5?") == "XRP"

    def test_xrp_ticker(self):
        assert _extract_asset("Will XRP hit $5 by 2025?") == "XRP"

    def test_dogecoin_to_doge(self):
        assert _extract_asset("Will Dogecoin hit $1?") == "DOGE"

    def test_unknown_asset_returns_none(self):
        assert _extract_asset("Will the market cap reach $1 trillion?") is None

    def test_bitcoin_and_btc_normalize_same(self):
        a = extract_crypto_semantics("Will Bitcoin hit $100k by December 2025?")
        b = extract_crypto_semantics("Will BTC reach $100,000 by December 2025?")
        assert a.asset == b.asset == "BTC"


# ---------------------------------------------------------------------------
# Threshold extraction and normalization
# ---------------------------------------------------------------------------

class TestThresholdNormalization:
    def test_dollar_k(self):
        assert _extract_threshold("Will BTC hit $100k?") == 100_000.0

    def test_dollar_comma(self):
        assert _extract_threshold("Will BTC hit $100,000?") == 100_000.0

    def test_bare_k(self):
        assert _extract_threshold("Will BTC reach 100k?") == 100_000.0

    def test_million(self):
        assert _extract_threshold("Will BTC hit $1M?") == 1_000_000.0

    def test_billion(self):
        assert _extract_threshold("Will crypto market cap hit $1B?") == 1_000_000_000.0

    def test_decimal(self):
        assert _extract_threshold("Will ETH hit $3.5k?") == 3_500.0

    def test_100k_equals_100000_equals_100_comma(self):
        v1 = _extract_threshold("BTC hit $100k")
        v2 = _extract_threshold("BTC hit $100,000")
        v3 = _extract_threshold("BTC hit 100k")
        assert v1 == v2 == v3 == 100_000.0

    def test_year_not_extracted_as_threshold(self):
        # "2025" is a year, not a price — no $ prefix, no k/m/b suffix
        assert _extract_threshold("Will BTC hit this by 2025?") is None

    def test_day_count_not_extracted(self):
        # "3 days" — bare integer without suffix, should not be a threshold
        assert _extract_threshold("Will BTC maintain above $50k for 3 days?") == 50_000.0


# ---------------------------------------------------------------------------
# Comparator extraction
# ---------------------------------------------------------------------------

class TestComparatorExtraction:
    def test_hit_to_reach(self):
        assert _extract_comparator("Will BTC hit $100k?") == "reach"

    def test_reach_to_reach(self):
        assert _extract_comparator("Will BTC reach $100k?") == "reach"

    def test_surpass_to_reach(self):
        assert _extract_comparator("Will BTC surpass $100k?") == "reach"

    def test_close_above_to_close_above(self):
        assert _extract_comparator("Will BTC close above $100k on Dec 31?") == "close_above"

    def test_close_below_to_close_below(self):
        assert _extract_comparator("Will BTC close below $50k?") == "close_below"

    def test_above_to_above(self):
        assert _extract_comparator("Will BTC be above $100k by December?") == "above"

    def test_below_to_below(self):
        assert _extract_comparator("Will BTC fall below $20k?") == "below"

    def test_at_least_to_at_least(self):
        assert _extract_comparator("Will ETH be at least $3000?") == "at_least"

    def test_close_above_beats_above(self):
        # "close above" should win over plain "above"
        comp = _extract_comparator("Will BTC close above $100k?")
        assert comp == "close_above", f"Expected close_above, got {comp!r}"


# ---------------------------------------------------------------------------
# Deadline extraction and normalization
# ---------------------------------------------------------------------------

class TestDeadlineExtraction:
    def test_end_of_specific_year(self):
        assert _extract_deadline("Will BTC hit $100k by end of 2025?") == "2025-12"

    def test_end_of_year_no_year(self):
        result = _extract_deadline("Will BTC hit $100k by end of year?")
        assert result == f"{_CURRENT_YEAR}-12"

    def test_eoy_abbreviation(self):
        result = _extract_deadline("Will BTC hit $100k by EOY?")
        assert result == f"{_CURRENT_YEAR}-12"

    def test_quarter_maps_to_last_month(self):
        assert _extract_deadline("Will BTC hit $100k by Q4 2025?") == "2025-12"
        assert _extract_deadline("Will BTC hit $100k by Q1 2025?") == "2025-03"
        assert _extract_deadline("Will BTC hit $100k by Q2 2025?") == "2025-06"
        assert _extract_deadline("Will BTC hit $100k by Q3 2025?") == "2025-09"

    def test_month_day_year(self):
        assert _extract_deadline("Will BTC hit $100k by December 31, 2025?") == "2025-12"

    def test_month_year(self):
        assert _extract_deadline("Will BTC hit $100k by December 2025?") == "2025-12"

    def test_month_only_uses_current_year(self):
        result = _extract_deadline("Will BTC hit $100k by December?")
        assert result == f"{_CURRENT_YEAR}-12"

    def test_bare_year(self):
        assert _extract_deadline("Will BTC hit $100k in 2025?") == "2025"

    def test_no_deadline(self):
        assert _extract_deadline("Will BTC ever hit $100k?") is None

    def test_eoy_equals_december(self):
        d1 = _extract_deadline("Will BTC hit $100k by end of year 2025?")
        d2 = _extract_deadline("Will BTC hit $100k by December 2025?")
        assert d1 == d2 == "2025-12"


# ---------------------------------------------------------------------------
# Deadline compatibility
# ---------------------------------------------------------------------------

class TestDeadlineCompatibility:
    def test_same_deadline(self):
        assert _deadlines_compatible("2025-12", "2025-12")

    def test_both_none(self):
        assert _deadlines_compatible(None, None)

    def test_one_none(self):
        assert _deadlines_compatible("2025-12", None)
        assert _deadlines_compatible(None, "2025-12")

    def test_different_year_incompatible(self):
        assert not _deadlines_compatible("2025-12", "2026-12")

    def test_same_year_one_month_level(self):
        # "2025" (year-only) vs "2025-12" — compatible since month info missing for one
        assert _deadlines_compatible("2025", "2025-12")

    def test_different_months_same_year_incompatible(self):
        assert not _deadlines_compatible("2025-11", "2025-12")

    def test_different_months_incompatible(self):
        assert not _deadlines_compatible("2025-06", "2025-09")


# ---------------------------------------------------------------------------
# Event family routing
# ---------------------------------------------------------------------------

class TestEventFamilyRouting:
    def test_touch_target_routing_hit(self):
        assert _extract_event_family("Will BTC hit $100k by December 2025?") == "touch_target_by_date"

    def test_touch_target_routing_reach(self):
        assert _extract_event_family("Will BTC reach $100,000 by December?") == "touch_target_by_date"

    def test_above_below_close_above(self):
        assert _extract_event_family("Will BTC close above $100k on Dec 31?") == "above_below_on_date"

    def test_above_below_close_below(self):
        assert _extract_event_family("Will BTC close below $50k?") == "above_below_on_date"

    def test_above_below_plain_above(self):
        assert _extract_event_family("Will BTC be above $100k by December?") == "above_below_on_date"

    def test_above_below_plain_below(self):
        assert _extract_event_family("Will BTC fall below $20k?") == "above_below_on_date"

    def test_etf_approval_routing(self):
        assert _extract_event_family("Will a spot Bitcoin ETF be approved by the SEC?") == "ETF_approval"

    def test_etf_with_price_language_still_etf(self):
        # ETF routing takes priority over price checks
        assert _extract_event_family("Will a spot ETF be launched at a price above NAV?") == "ETF_approval"

    def test_protocol_halving(self):
        assert _extract_event_family("Will the Bitcoin halving occur before April 2024?") == "protocol_event"

    def test_protocol_merge(self):
        assert _extract_event_family("Will Ethereum complete the Merge?") == "protocol_event"

    def test_protocol_upgrade(self):
        assert _extract_event_family("Will the Ethereum Pectra upgrade happen in 2025?") == "protocol_event"

    def test_other_family(self):
        # No price, no ETF, no protocol — routes to other
        assert _extract_event_family("Will Coinbase be the largest crypto exchange?") == "other"

    def test_touch_vs_above_are_different_families(self):
        # hit → touch_target_by_date; close above → above_below_on_date
        assert _extract_event_family("Will BTC hit $100k by December?") == "touch_target_by_date"
        assert _extract_event_family("Will BTC close above $100k by December?") == "above_below_on_date"


# ---------------------------------------------------------------------------
# Compatibility — cases that should PASS
# ---------------------------------------------------------------------------

class TestCompatiblePairs:
    def test_bitcoin_btc_same_threshold_same_deadline(self):
        s1 = extract_crypto_semantics("Will Bitcoin hit $100k by December 2025?")
        s2 = extract_crypto_semantics("Will BTC reach $100,000 by December 2025?")
        ok, detail = crypto_semantics_compatible(s1, s2)
        assert ok, detail

    def test_hit_reach_synonyms_compatible(self):
        s1 = extract_crypto_semantics("Will BTC hit $100k by December 2025?")
        s2 = extract_crypto_semantics("Will BTC reach $100k by December 2025?")
        assert s1.comparator == s2.comparator == "reach"
        ok, detail = crypto_semantics_compatible(s1, s2)
        assert ok, detail

    def test_eoy_equals_december_compatible(self):
        s1 = extract_crypto_semantics("Will BTC hit $100k by end of year 2025?")
        s2 = extract_crypto_semantics("Will BTC hit $100k by December 2025?")
        ok, detail = crypto_semantics_compatible(s1, s2)
        assert ok, detail

    def test_q4_equals_december_compatible(self):
        s1 = extract_crypto_semantics("Will BTC hit $100k by Q4 2025?")
        s2 = extract_crypto_semantics("Will BTC reach $100,000 by December 2025?")
        ok, detail = crypto_semantics_compatible(s1, s2)
        assert ok, detail

    def test_same_etf_approval_target_compatible(self):
        s1 = extract_crypto_semantics("Will a spot Bitcoin ETF be approved by the SEC?")
        s2 = extract_crypto_semantics("Will the SEC approve a spot BTC ETF in 2025?")
        ok, detail = crypto_semantics_compatible(s1, s2)
        assert ok, detail

    def test_no_asset_specified_compatible(self):
        # If neither side specifies an asset, skip the asset check
        s1 = extract_crypto_semantics("Will the crypto market cap hit $1T by 2025?")
        s2 = extract_crypto_semantics("Will crypto reach $1 trillion by 2025?")
        ok, detail = crypto_semantics_compatible(s1, s2)
        assert ok, detail


# ---------------------------------------------------------------------------
# Compatibility — cases that should REJECT
# ---------------------------------------------------------------------------

class TestIncompatiblePairs:
    def test_hit_vs_close_above_reject(self):
        """'hit 100k' (touch_target_by_date) vs 'close above 100k' (above_below_on_date) must reject."""
        s1 = extract_crypto_semantics("Will BTC hit $100k by December 2025?")
        s2 = extract_crypto_semantics("Will BTC close above $100,000 on December 31, 2025?")
        assert s1.event_family == "touch_target_by_date"
        assert s2.event_family == "above_below_on_date"
        ok, detail = crypto_semantics_compatible(s1, s2)
        assert not ok, "Expected rejection: different event families"
        assert "event_family" in detail

    def test_same_asset_different_deadline_reject(self):
        """Same asset and threshold, different months → reject."""
        s1 = extract_crypto_semantics("Will BTC hit $100k by November 2025?")
        s2 = extract_crypto_semantics("Will BTC hit $100k by December 2025?")
        assert s1.deadline == "2025-11"
        assert s2.deadline == "2025-12"
        ok, detail = crypto_semantics_compatible(s1, s2)
        assert not ok, "Expected rejection: different deadline months"
        assert "deadline" in detail

    def test_same_asset_different_year_reject(self):
        s1 = extract_crypto_semantics("Will BTC hit $100k by December 2025?")
        s2 = extract_crypto_semantics("Will BTC hit $100k by December 2026?")
        ok, detail = crypto_semantics_compatible(s1, s2)
        assert not ok
        assert "deadline" in detail

    def test_same_asset_different_comparator_reject(self):
        """above vs below — different comparator → reject."""
        s1 = extract_crypto_semantics("Will BTC be above $100k by December 2025?")
        s2 = extract_crypto_semantics("Will BTC be below $100k by December 2025?")
        ok, detail = crypto_semantics_compatible(s1, s2)
        assert not ok
        assert "comparator" in detail

    def test_threshold_mismatch_reject(self):
        """Same asset and comparator, different price → reject."""
        s1 = extract_crypto_semantics("Will BTC hit $100k by December 2025?")
        s2 = extract_crypto_semantics("Will BTC hit $80k by December 2025?")
        ok, detail = crypto_semantics_compatible(s1, s2)
        assert not ok
        assert "threshold" in detail

    def test_btc_vs_eth_reject(self):
        """Different assets → reject even if price and deadline match."""
        s1 = extract_crypto_semantics("Will BTC hit $100k by December 2025?")
        s2 = extract_crypto_semantics("Will ETH hit $100k by December 2025?")
        ok, detail = crypto_semantics_compatible(s1, s2)
        assert not ok
        assert "asset" in detail

    def test_etf_spot_vs_futures_reject(self):
        """Spot ETF vs futures ETF → reject."""
        s1 = extract_crypto_semantics("Will a spot Bitcoin ETF be approved by the SEC?")
        s2 = extract_crypto_semantics("Will a Bitcoin futures ETF be approved?")
        assert s1.approval_target == "spot"
        assert s2.approval_target == "futures"
        ok, detail = crypto_semantics_compatible(s1, s2)
        assert not ok
        assert "approval_target" in detail

    def test_conditional_vs_unconditional_reject(self):
        """Conditional market vs unconditional → reject.

        Uses a non-ETF conditional to avoid ETF routing interfering with the
        family classification of the conditional question.
        """
        s1 = extract_crypto_semantics("Will BTC hit $100k by December 2025?")
        s2 = extract_crypto_semantics(
            "Will BTC hit $100k by December 2025 assuming Bitcoin dominance exceeds 60%?"
        )
        assert not s1.has_conditional
        assert s2.has_conditional
        ok, detail = crypto_semantics_compatible(s1, s2)
        assert not ok
        assert "conditional" in detail

    def test_etf_approval_vs_price_target_reject(self):
        """ETF approval market vs price target market → reject on family."""
        s1 = extract_crypto_semantics("Will a spot Bitcoin ETF be approved by the SEC?")
        s2 = extract_crypto_semantics("Will BTC hit $100k by December 2025?")
        ok, detail = crypto_semantics_compatible(s1, s2)
        assert not ok
        assert "event_family" in detail

    def test_protocol_vs_price_reject(self):
        """Protocol event vs price target → reject on family."""
        s1 = extract_crypto_semantics("Will the Bitcoin halving occur in April 2024?")
        s2 = extract_crypto_semantics("Will BTC hit $100k by April 2024?")
        assert s1.event_family == "protocol_event"
        assert s2.event_family == "touch_target_by_date"
        ok, detail = crypto_semantics_compatible(s1, s2)
        assert not ok
        assert "event_family" in detail


# ---------------------------------------------------------------------------
# Full extract_crypto_semantics integration
# ---------------------------------------------------------------------------

class TestExtractIntegration:
    def test_full_btc_price_market(self):
        s = extract_crypto_semantics("Will Bitcoin hit $100k by December 31, 2025?")
        assert s.asset == "BTC"
        assert s.threshold == 100_000.0
        assert s.comparator == "reach"
        assert s.deadline == "2025-12"
        assert s.event_family == "touch_target_by_date"
        assert not s.has_conditional

    def test_full_etf_market(self):
        s = extract_crypto_semantics(
            "Will the SEC approve a spot Bitcoin ETF in 2025?"
        )
        assert s.asset == "BTC"
        assert s.event_family == "ETF_approval"
        assert s.approval_target == "spot"
        assert s.jurisdiction == "us"

    def test_full_close_above_market(self):
        s = extract_crypto_semantics(
            "Will ETH close above $3000 on December 31, 2025?"
        )
        assert s.asset == "ETH"
        assert s.threshold == 3_000.0
        assert s.comparator == "close_above"
        assert s.deadline == "2025-12"
        assert s.event_family == "above_below_on_date"

    def test_full_protocol_event(self):
        s = extract_crypto_semantics("Will the Bitcoin halving occur before April 2024?")
        assert s.asset == "BTC"
        assert s.event_family == "protocol_event"
        assert s.deadline == "2024-04"

    def test_conditional_detected(self):
        s = extract_crypto_semantics(
            "Will BTC hit $100k by December if the ETF is approved?"
        )
        assert s.has_conditional
