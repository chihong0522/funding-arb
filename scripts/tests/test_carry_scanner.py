#!/usr/bin/env python3
"""Hermetic tests for the phase-3 same-venue forward carry scanner."""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
ROOT = SCRIPTS.parent
for path in (str(ROOT), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

from market.carry_scanner import _history_rate, book_vwap, reprice_candidate, scan_carry_venue

NOW = 1_700_000_000_000
HOUR_MS = 3_600_000


def _market(step: float = 0.001, minimum: float = 0.001, maximum: float = 1000.0):
    return {
        "qty_step": step,
        "min_qty": minimum,
        "max_qty": maximum,
        "min_notional_usd": 1.0,
    }


def _book(mid: float = 100.0, observed_at_ms: int = NOW, qty: float = 100.0):
    return {
        "bids": [[mid - 0.01, qty], [mid - 0.02, qty]],
        "asks": [[mid + 0.01, qty], [mid + 0.02, qty]],
        "observed_at_ms": observed_at_ms,
        "exchange_ts_ms": observed_at_ms,
        "snapshot_id": f"book-{observed_at_ms}",
    }


class FakeFundingProvider:
    def __init__(self, rate_pct: float = 0.2, history=None, interval_h: float = 8.0):
        self.rate_pct = rate_pct
        self.history = list(history if history is not None else [0.25, 0.30, 0.35])
        self.interval_h = interval_h

    def fetch_all(self, quote="USDT"):
        return [{
            "symbol": "BTCUSDT",
            "rate_pct": self.rate_pct,
            "next_funding_ts": NOW + HOUR_MS,
            "mark_price": 100.0,
            "observed_at_ms": NOW,
        }]

    def fetch_interval_map(self, quote="USDT"):
        return {"BTCUSDT": self.interval_h}

    def fetch_since(self, symbol, start_ms, max_pages=10):
        return [
            {"ts": NOW - (len(self.history) - i) * int(self.interval_h * HOUR_MS), "rate_pct": rate}
            for i, rate in enumerate(self.history)
        ]


class FakeMarketProvider:
    def __init__(self, *, spot_book=None, perp_book=None, spot=None, perp=None):
        self.universe = {
            "spot": {"BTCUSDT": dict(spot or _market())},
            "perpetual": {"BTCUSDT": dict(perp or _market())},
            "snapshot_metadata": {
                "spot": {"observed_at_ms": NOW, "snapshot_id": "spot-instruments-1"},
                "perpetual": {"observed_at_ms": NOW, "snapshot_id": "perp-instruments-1"},
            },
        }
        self.books = {
            ("spot", "BTCUSDT"): spot_book or _book(),
            ("perpetual", "BTCUSDT"): perp_book or _book(),
        }

    def fetch_market_universe(self, venue):
        return self.universe

    def fetch_order_book(self, venue, market_type, symbol):
        key = (market_type, symbol)
        value = self.books[key]
        if isinstance(value, Exception):
            raise value
        return value


def _scan(funding=None, market=None, **kwargs):
    notional_usd = kwargs.pop("notional_usd", 100.0)
    horizon_hours = kwargs.pop("horizon_hours", 24.0)
    return scan_carry_venue(
        "binance",
        funding_provider=funding or FakeFundingProvider(),
        market_provider=market or FakeMarketProvider(),
        notional_usd=notional_usd,
        horizon_hours=horizon_hours,
        now_ms=NOW,
        spot_fee_pct=0.10,
        perp_fee_pct=0.05,
        exit_slippage_bps=5.0,
        basis_buffer_bps=10.0,
        **kwargs,
    )


def test_book_vwap_consumes_requested_base_quantity_across_levels():
    result = book_vwap(
        {"asks": [[100.0, 2.0], [101.0, 3.0]], "bids": []},
        "asks",
        3.0,
    )
    assert result["filled_qty"] == 3.0
    assert result["quote_usd"] == 301.0
    assert result["vwap"] == 301.0 / 3.0


def test_scanner_uses_same_venue_intersection_and_returns_forward_snapshot_estimate():
    result = _scan()
    assert result["venue"] == "binance"
    assert result["direction"] == "forward_only"
    assert result["reverse_candidates"] == []
    assert len(result["forward_candidates"]) == 1

    row = result["forward_candidates"][0]
    assert row["symbol"] == "BTCUSDT"
    assert row["interval_h"] == 8.0
    assert row["funding_payments_estimated"] == 3
    assert row["funding_estimator"] == "min(current_rate, recent_median_rate)"
    assert row["spot_entry_vwap"] > 0
    assert row["spot_exit_vwap"] > 0
    assert row["perp_entry_vwap"] > 0
    assert row["perp_exit_vwap"] > 0
    assert row["entry_fee_usd"] > 0
    assert row["exit_fee_usd"] > 0
    assert row["exit_slippage_assumption_usd"] > 0
    assert row["basis_buffer_usd"] > 0
    assert row["net_horizon_earnings_usd"] == row["gross_funding_usd"] - row["total_estimated_cost_usd"]
    assert row["breakeven_rate_pct"] == (
        row["total_estimated_cost_usd"]
        / (row["funding_payments_estimated"] * row["mark_price"] * row["quantity_base"])
        * 100.0
    )
    assert row["spot_fee_pct"] == 0.10
    assert row["futures_fee_pct"] == 0.05
    assert row["fee_pct"] == 0.15
    assert row["has_spot"] is True
    assert row["next_ts"] == row["next_funding_ts"]
    assert row["annual_pct"] > 0
    assert row["spot_price"] == row["spot_entry_vwap"]
    assert "net_edge_pct" in row
    assert row["breakeven_funding_payments"] >= 1
    assert row["net_horizon_notional_roi_pct"] == row["net_horizon_earnings_usd"] / row["notional_usd_requested"] * 100.0
    assert row["capital_fee_buffer_usd"] == row["entry_fee_usd"] + row["exit_fee_usd"]
    assert row["short_margin_multiplier"] == 1.0
    assert row["borrowing_used"] is False
    assert row["capital_required_usd"] == row["spot_notional_usd"] + row["perp_notional_usd"] + row["capital_fee_buffer_usd"]
    assert row["net_horizon_capital_roi_pct"] == row["net_horizon_earnings_usd"] / row["capital_required_usd"] * 100.0
    assert row["funding_observed_at_ms"] == NOW
    assert row["spot_instrument_snapshot_observed_at_ms"] == NOW
    assert row["perp_instrument_snapshot_observed_at_ms"] == NOW
    assert row["spot_book_observed_at_ms"] == NOW
    assert row["perp_book_observed_at_ms"] == NOW
    assert row["spot_book_snapshot_id"] == f"book-{NOW}"
    assert row["perp_book_snapshot_id"] == f"book-{NOW}"
    assert row["source_timestamp_skew_ms"] == 0
    assert row["snapshot_observed_at_ms"] == NOW
    assert len(row["snapshot_id"]) == 64
    assert len(result["snapshot_id"]) == 64
    assert result["assumptions"]["capital_model"] == "illustrative_prefunded_spot_plus_1x_short_margin"
    assert result["assumptions"]["borrowing_used"] is False
    assert "not approval of leverage or live execution" in result["disclaimer"].lower()
    assert "not guaranteed" in result["disclaimer"].lower()
    assert result["assumptions"]["fees_are_taker"] is True


def test_candidate_audits_exact_funding_history_used_in_snapshot_id():
    history_rates = [i / 10 for i in range(1, 11)]
    first = _scan(funding=FakeFundingProvider(rate_pct=2.0, history=history_rates))
    changed_rates = history_rates.copy()
    changed_rates[2] = 0.31  # Same recent median, but different underlying sample.
    second = _scan(funding=FakeFundingProvider(rate_pct=2.0, history=changed_rates))

    row = first["forward_candidates"][0]
    observations = row["funding_history_observations"]
    assert len(observations) == 8
    assert row["history_samples"] == 10
    assert row["history_first_ts"] == NOW - 10 * 8 * HOUR_MS
    assert row["history_latest_ts"] == NOW - 8 * HOUR_MS
    assert row["history_estimator_first_ts"] == NOW - 8 * 8 * HOUR_MS
    assert row["history_estimator_samples"] == 8
    assert observations[0] == {
        "ts_ms": NOW - 8 * 8 * HOUR_MS,
        "rate_pct": history_rates[2],
    }
    other = second["forward_candidates"][0]
    assert other["historical_median_rate_pct"] == row["historical_median_rate_pct"]
    assert other["snapshot_id"] != row["snapshot_id"]


def test_negative_current_funding_is_excluded_not_reverse_scanned():
    result = _scan(funding=FakeFundingProvider(rate_pct=-0.2))
    assert result["forward_candidates"] == []
    assert result["reverse_candidates"] == []
    assert "non_positive_funding_rate" in {x["reason"] for x in result["excluded"]}


def test_missing_interval_fails_closed():
    funding = FakeFundingProvider()
    funding.fetch_interval_map = lambda quote="USDT": {}
    result = _scan(funding=funding)
    assert result["forward_candidates"] == []
    assert "missing_funding_interval" in {x["reason"] for x in result["excluded"]}


def test_missing_funding_snapshot_timestamp_fails_closed():
    funding = FakeFundingProvider()
    original_fetch_all = funding.fetch_all
    funding.fetch_all = lambda quote="USDT": [
        {key: value for key, value in row.items() if key != "observed_at_ms"}
        for row in original_fetch_all(quote)
    ]
    result = _scan(funding=funding)
    assert result["forward_candidates"] == []
    assert "missing_funding_timestamp" in {x["reason"] for x in result["excluded"]}


def test_missing_history_fails_closed():
    result = _scan(funding=FakeFundingProvider(history=[]))
    assert result["forward_candidates"] == []
    assert "missing_funding_history" in {x["reason"] for x in result["excluded"]}


def test_gapped_funding_history_fails_closed():
    funding = FakeFundingProvider()
    funding.fetch_since = lambda symbol, start_ms, max_pages=10: [
        {"ts": NOW - 24 * HOUR_MS, "rate_pct": 0.25},
        {"ts": NOW - 16 * HOUR_MS, "rate_pct": 0.30},
        {"ts": NOW, "rate_pct": 0.35},
    ]
    result = _scan(funding=funding)
    assert result["forward_candidates"] == []
    assert "funding_history_gap" in {x["reason"] for x in result["excluded"]}


def test_fractional_history_timestamp_is_not_counted_as_a_settlement():
    funding = FakeFundingProvider()
    funding.fetch_since = lambda symbol, start_ms, max_pages=10: [
        {"ts": NOW - 16 * HOUR_MS, "rate_pct": 0.25},
        {"ts": NOW - 8 * HOUR_MS, "rate_pct": 0.30},
        {"ts": NOW - 1.5, "rate_pct": 0.35},
    ]
    result = _scan(funding=funding)
    assert result["forward_candidates"] == []
    assert "missing_funding_history" in {x["reason"] for x in result["excluded"]}


def test_non_positive_historical_estimate_fails_closed():
    result = _scan(funding=FakeFundingProvider(history=[-0.1, -0.2, 0.1]))
    assert result["forward_candidates"] == []
    assert "non_positive_historical_funding" in {x["reason"] for x in result["excluded"]}


def test_symbol_without_same_venue_spot_market_is_excluded():
    market = FakeMarketProvider()
    market.universe["spot"].clear()
    result = _scan(market=market)
    assert result["forward_candidates"] == []
    assert "spot_market_not_listed" in {x["reason"] for x in result["excluded"]}


def test_stale_funding_snapshot_fails_closed():
    funding = FakeFundingProvider()
    original_fetch_all = funding.fetch_all
    funding.fetch_all = lambda quote="USDT": [
        {**row, "observed_at_ms": NOW - 61_000}
        for row in original_fetch_all(quote)
    ]
    result = _scan(funding=funding, max_funding_age_sec=60.0)
    assert result["forward_candidates"] == []
    assert "stale_funding_snapshot" in {x["reason"] for x in result["excluded"]}


def test_stale_order_book_fails_closed():
    market = FakeMarketProvider(spot_book=_book(observed_at_ms=NOW - 6_000))
    result = _scan(market=market, max_book_age_sec=5.0)
    assert result["forward_candidates"] == []
    assert "stale_order_book" in {x["reason"] for x in result["excluded"]}


def test_funding_spot_perp_source_timestamp_skew_fails_closed():
    perp = _book()
    perp["exchange_ts_ms"] = NOW - 6_000
    result = _scan(
        market=FakeMarketProvider(perp_book=perp),
        max_book_age_sec=10.0,
    )
    assert result["forward_candidates"] == []
    assert "source_timestamp_skew_exceeded" in {x["reason"] for x in result["excluded"]}


def test_funding_to_spot_source_timestamp_skew_fails_closed():
    funding = FakeFundingProvider()
    original_fetch_all = funding.fetch_all
    funding.fetch_all = lambda quote="USDT": [
        {**row, "observed_at_ms": NOW - 6_000}
        for row in original_fetch_all(quote)
    ]
    result = _scan(
        funding=funding,
        max_book_age_sec=10.0,
        max_funding_age_sec=10.0,
    )
    assert result["forward_candidates"] == []
    assert "source_timestamp_skew_exceeded" in {x["reason"] for x in result["excluded"]}


def test_spot_to_perpetual_source_timestamp_skew_fails_closed():
    spot = _book()
    spot["exchange_ts_ms"] = NOW - 6_000
    result = _scan(
        market=FakeMarketProvider(spot_book=spot),
        max_book_age_sec=10.0,
    )
    assert result["forward_candidates"] == []
    assert "source_timestamp_skew_exceeded" in {x["reason"] for x in result["excluded"]}


def test_stale_instrument_snapshot_fails_closed():
    market = FakeMarketProvider()
    market.universe["snapshot_metadata"]["spot"]["observed_at_ms"] = NOW - 301_000
    result = _scan(market=market)
    assert result["forward_candidates"] == []
    assert "stale_instrument_snapshot" in {x["reason"] for x in result["excluded"]}


def test_missing_instrument_snapshot_timestamp_fails_closed():
    market = FakeMarketProvider()
    market.universe["snapshot_metadata"]["perpetual"].pop("observed_at_ms")
    result = _scan(market=market)
    assert result["forward_candidates"] == []
    assert "missing_instrument_snapshot_timestamp" in {
        x["reason"] for x in result["excluded"]
    }


def test_boolean_instrument_snapshot_id_fails_closed():
    market = FakeMarketProvider()
    market.universe["snapshot_metadata"]["spot"]["snapshot_id"] = True
    result = _scan(market=market)
    assert result["forward_candidates"] == []
    assert "missing_instrument_snapshot_id" in {
        x["reason"] for x in result["excluded"]
    }


def test_missing_or_thin_order_book_fails_closed():
    market = FakeMarketProvider()
    market.books[("perpetual", "BTCUSDT")] = RuntimeError("public depth unavailable")
    result = _scan(market=market)
    assert result["forward_candidates"] == []
    assert "missing_order_book" in {x["reason"] for x in result["excluded"]}

    thin = _book(qty=0.1)
    result = _scan(market=FakeMarketProvider(spot_book=thin, perp_book=thin))
    assert result["forward_candidates"] == []
    assert "insufficient_depth" in {x["reason"] for x in result["excluded"]}


def test_reprice_candidate_applies_updated_taker_fees_to_both_round_trip_legs():
    result = _scan()
    original = result["forward_candidates"][0]
    repriced = reprice_candidate(original, spot_fee_pct=0.20, perp_fee_pct=0.10)
    assert repriced["entry_fee_usd"] > original["entry_fee_usd"]
    assert repriced["exit_fee_usd"] > original["exit_fee_usd"]
    assert repriced["net_horizon_earnings_usd"] < original["net_horizon_earnings_usd"]
    assert repriced["futures_fee_pct"] == 0.10
    assert repriced["fee_pct"] == 0.30
    assert repriced["capital_fee_buffer_usd"] == repriced["entry_fee_usd"] + repriced["exit_fee_usd"]
    assert repriced["capital_required_usd"] == (
        repriced["spot_notional_usd"] + repriced["short_margin_usd"] + repriced["capital_fee_buffer_usd"]
    )
    assert repriced["net_horizon_capital_roi_pct"] == (
        repriced["net_horizon_earnings_usd"] / repriced["capital_required_usd"] * 100.0
    )
    assert original["fee_pct"] == 0.15


def test_missing_price_and_quantity_limits_fail_closed():
    funding = FakeFundingProvider()
    original_fetch_all = funding.fetch_all
    funding.fetch_all = lambda quote="USDT": [
        {**row, "mark_price": 0}
        for row in original_fetch_all(quote)
    ]
    result = _scan(funding=funding)
    assert result["forward_candidates"] == []
    assert "missing_price" in {x["reason"] for x in result["excluded"]}

    market = FakeMarketProvider()
    market.books[("spot", "BTCUSDT")] = {
        "bids": [], "asks": [], "observed_at_ms": NOW, "exchange_ts_ms": NOW,
    }
    result = _scan(market=market)
    assert result["forward_candidates"] == []
    assert "missing_book_side_or_price" in {x["reason"] for x in result["excluded"]}

    market = FakeMarketProvider(spot={"qty_step": 0, "min_qty": 0, "max_qty": 0})
    result = _scan(market=market)
    assert result["forward_candidates"] == []
    assert "missing_quantity_limits" in {x["reason"] for x in result["excluded"]}


def test_missing_minimum_notional_metadata_fails_closed():
    market = FakeMarketProvider()
    market.universe["spot"]["BTCUSDT"].pop("min_notional_usd")
    result = _scan(market=market)
    assert result["forward_candidates"] == []
    assert "missing_quantity_limits" in {x["reason"] for x in result["excluded"]}


def test_crossed_order_book_fails_closed():
    crossed = _book()
    crossed["bids"] = [[100.1, 100.0]]
    crossed["asks"] = [[99.9, 100.0]]
    result = _scan(market=FakeMarketProvider(spot_book=crossed))
    assert result["forward_candidates"] == []
    assert "invalid_order_book" in {x["reason"] for x in result["excluded"]}


def test_market_order_quantity_caps_are_enforced():
    market = FakeMarketProvider(spot=_market(maximum=0.5), perp=_market(maximum=0.5))
    result = _scan(market=market)
    assert result["forward_candidates"] == []
    assert "quantity_limit_exceeded" in {x["reason"] for x in result["excluded"]}


def test_scan_rejects_unsupported_venues_and_invalid_notional():
    try:
        scan_carry_venue("okx", funding_provider=FakeFundingProvider(), market_provider=FakeMarketProvider())
    except ValueError:
        pass
    else:
        raise AssertionError("unsupported venues must be rejected")

    for invalid_notional in (0, -1, 501, True, float("nan"), float("inf"), -float("inf"), 10**400):
        try:
            _scan(notional_usd=invalid_notional)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid notional must be rejected: {invalid_notional!r}")

    for invalid_horizon in (0, -1, 169, True, float("nan"), float("inf"), -float("inf")):
        try:
            _scan(horizon_hours=invalid_horizon)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid horizon must be rejected: {invalid_horizon!r}")


def test_scan_accepts_notional_and_horizon_upper_bounds():
    result = _scan(notional_usd=500.0, horizon_hours=168.0)
    assert result["notional_usd"] == 500.0
    assert result["horizon_hours"] == 168.0
    assert result["max_notional_usd"] == 500.0
    assert result["max_horizon_hours"] == 168.0

    defaults = scan_carry_venue(
        "binance",
        funding_provider=FakeFundingProvider(),
        market_provider=FakeMarketProvider(),
        now_ms=NOW,
    )
    assert defaults["notional_usd"] == 100.0
    assert defaults["horizon_hours"] == 24.0


def test_latest_funding_settlement_tail_gap_uses_one_and_a_half_intervals():
    interval_ms = 8 * HOUR_MS
    rows = [
        {"ts": NOW - 3 * interval_ms, "rate_pct": 0.25},
        {"ts": NOW - 2 * interval_ms, "rate_pct": 0.30},
        {"ts": NOW - int(1.5 * interval_ms), "rate_pct": 0.35},
    ]

    at_boundary = _history_rate(rows, now_ms=NOW, interval_h=8.0)
    assert at_boundary[2] is None
    assert at_boundary[1] == 3

    just_over_boundary = [
        *rows[:-1],
        {"ts": rows[-1]["ts"] - 1, "rate_pct": rows[-1]["rate_pct"]},
    ]
    beyond_boundary = _history_rate(just_over_boundary, now_ms=NOW, interval_h=8.0)
    assert beyond_boundary[2] == "funding_history_gap"
    assert beyond_boundary[-1] == []
