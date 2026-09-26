"""Offline tests for bounded public-data paper validation and replay."""
from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from market.carry_scanner import scan_carry_venue  # noqa: E402
import tools.paper_validation_acquire as acquisition  # noqa: E402
from tools.paper_validation_acquire import AllowlistViolation, validate_public_url  # noqa: E402
from tools.paper_validation_replay import (  # noqa: E402
    SnapshotBindingError,
    SnapshotReplayFundingProvider,
    SnapshotReplayMarketProvider,
    SnapshotReplayPaperTransport,
    run_replay_validation,
)

NOW = 1_700_000_000_000
HOUR_MS = 3_600_000


def _book(mid: float = 100.0, qty: float = 100.0) -> dict:
    return {
        "bids": [[mid - 0.01, qty], [mid - 0.02, qty]],
        "asks": [[mid + 0.01, qty], [mid + 0.02, qty]],
        "observed_at_ms": NOW,
        "exchange_ts_ms": NOW,
        "request_started_at_ms": NOW,
        "snapshot_id": f"book-{mid}",
    }


def _exchange_snapshot() -> dict:
    limits = {
        "qty_step": 0.001,
        "min_qty": 0.001,
        "max_qty": 1000.0,
        "min_notional_usd": 1.0,
    }
    return {
        "market_universe": {
            "spot": {"BTCUSDT": dict(limits)},
            "perpetual": {"BTCUSDT": dict(limits)},
            "snapshot_metadata": {
                "spot": {"observed_at_ms": NOW, "snapshot_id": "spot-inst-1"},
                "perpetual": {"observed_at_ms": NOW, "snapshot_id": "perp-inst-1"},
            },
        },
        "funding": {
            "current": [
                {
                    "symbol": "BTCUSDT",
                    "rate_pct": 0.20,
                    "next_funding_ts": NOW + HOUR_MS,
                    "mark_price": 100.0,
                    "observed_at_ms": NOW,
                }
            ],
            "intervals": {"BTCUSDT": 8.0},
            "history": {
                "BTCUSDT": [
                    {"ts": NOW - 24 * HOUR_MS, "rate_pct": 0.25},
                    {"ts": NOW - 16 * HOUR_MS, "rate_pct": 0.30},
                    {"ts": NOW - 8 * HOUR_MS, "rate_pct": 0.35},
                ]
            },
        },
        "books": {
            "spot": {"BTCUSDT": _book(100.0)},
            "perpetual": {"BTCUSDT": _book(100.02)},
        },
    }


def _snapshot() -> dict:
    exchange = _exchange_snapshot()
    return {
        "schema_version": 1,
        "snapshot_id": "fixture-snapshot",
        "captured_at_ms": NOW,
        "captured_at_utc": "2023-11-14T22:13:20Z",
        "exchanges": {"binance": copy.deepcopy(exchange), "bybit": copy.deepcopy(exchange)},
    }


def test_public_allowlist_rejects_private_paths_and_other_hosts():
    validate_public_url("https://api.binance.com/api/v3/exchangeInfo")
    validate_public_url(
        "https://api.binance.com/api/v3/exchangeInfo?symbols=%5B%22BTCUSDT%22%5D"
    )
    validate_public_url("https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT")
    validate_public_url("https://api.bybit.com/v5/market/orderbook?category=linear&symbol=BTCUSDT")

    for url in (
        "https://api.binance.com/api/v3/order",
        "https://api.bybit.com/v5/order/create",
        "https://example.com/api/v3/exchangeInfo",
        "http://api.binance.com/api/v3/exchangeInfo",
        "https://user:pass@api.binance.com/api/v3/exchangeInfo",
    ):
        with pytest.raises(AllowlistViolation):
            validate_public_url(url)


def test_public_fetcher_uses_get_only_and_enforces_response_bound(monkeypatch):
    calls = []

    class FakeResponse:
        headers = {"Content-Length": "2"}

        def __init__(self):
            self._sent = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def geturl(self):
            return "https://api.binance.com/api/v3/exchangeInfo"

        def getcode(self):
            return 200

        def read(self, _size=-1):
            if self._sent:
                return b""
            self._sent = True
            return b"{}"

    class FakeOpener:
        def open(self, request, timeout):
            calls.append((request.get_method(), request.full_url, timeout))
            return FakeResponse()

    monkeypatch.setattr(acquisition, "_PUBLIC_OPENER", FakeOpener())
    payload, raw, evidence = acquisition.fetch_public_json(
        "https://api.binance.com/api/v3/exchangeInfo"
    )
    assert payload == {}
    assert raw == b"{}"
    assert evidence["status"] == 200
    assert calls and calls[0][0] == "GET"

    with pytest.raises(AllowlistViolation):
        acquisition.fetch_public_json("https://api.binance.com/api/v3/order")
    assert len(calls) == 1

    class TooLargeResponse(FakeResponse):
        headers = {"Content-Length": str(acquisition.MAX_RESPONSE_BYTES + 1)}

    class TooLargeOpener(FakeOpener):
        def open(self, request, timeout):
            return TooLargeResponse()

    monkeypatch.setattr(acquisition, "_PUBLIC_OPENER", TooLargeOpener())
    with pytest.raises(acquisition.PublicFetchError, match="bounded size"):
        acquisition.fetch_public_json("https://api.binance.com/api/v3/exchangeInfo")

    with pytest.raises(ValueError, match="max_bytes"):
        acquisition.fetch_public_json(
            "https://api.binance.com/api/v3/exchangeInfo",
            max_bytes=acquisition.MAX_RESPONSE_BYTES + 1,
        )


def test_snapshot_replay_providers_feed_the_real_scanner_without_network():
    doc = _snapshot()
    fp = SnapshotReplayFundingProvider(doc["exchanges"]["binance"])
    mp = SnapshotReplayMarketProvider(doc["exchanges"]["binance"])
    result = scan_carry_venue(
        "binance",
        funding_provider=fp,
        market_provider=mp,
        now_ms=NOW,
        max_workers=1,
        spot_fee_pct=0.10,
        perp_fee_pct=0.05,
    )

    assert result["forward_candidates"]
    row = result["forward_candidates"][0]
    assert row["symbol"] == "BTCUSDT"
    assert row["snapshot_estimate"] is True
    assert row["spot_book_snapshot_id"] == "book-100.0"
    assert row["perp_book_snapshot_id"] == "book-100.02"
    assert result["assumptions"]["public_market_data_only"] is True
    assert result["assumptions"]["private_fee_api_used"] is False


def test_replay_preserves_stale_depth_fee_and_snapshot_binding_exclusions():
    stale = _snapshot()
    stale["exchanges"]["binance"]["books"]["spot"]["BTCUSDT"]["observed_at_ms"] = NOW - 6_000
    stale_scan = scan_carry_venue(
        "binance",
        funding_provider=SnapshotReplayFundingProvider(stale["exchanges"]["binance"]),
        market_provider=SnapshotReplayMarketProvider(stale["exchanges"]["binance"]),
        now_ms=NOW,
        max_workers=1,
        spot_fee_pct=0.10,
        perp_fee_pct=0.05,
    )
    assert stale_scan["forward_candidates"] == []
    assert "stale_order_book" in stale_scan["exclusion_counts"]

    thin = _snapshot()
    thin["exchanges"]["binance"]["books"]["spot"]["BTCUSDT"] = _book(qty=0.1)
    thin["exchanges"]["binance"]["books"]["perpetual"]["BTCUSDT"] = _book(100.02, qty=0.1)
    thin_scan = scan_carry_venue(
        "binance",
        funding_provider=SnapshotReplayFundingProvider(thin["exchanges"]["binance"]),
        market_provider=SnapshotReplayMarketProvider(thin["exchanges"]["binance"]),
        now_ms=NOW,
        max_workers=1,
        spot_fee_pct=0.10,
        perp_fee_pct=0.05,
    )
    assert thin_scan["forward_candidates"] == []
    assert "insufficient_depth" in thin_scan["exclusion_counts"]

    low_fee = scan_carry_venue(
        "binance",
        funding_provider=SnapshotReplayFundingProvider(_snapshot()["exchanges"]["binance"]),
        market_provider=SnapshotReplayMarketProvider(_snapshot()["exchanges"]["binance"]),
        now_ms=NOW,
        max_workers=1,
        spot_fee_pct=0.01,
        perp_fee_pct=0.01,
    )["forward_candidates"][0]
    high_fee = scan_carry_venue(
        "binance",
        funding_provider=SnapshotReplayFundingProvider(_snapshot()["exchanges"]["binance"]),
        market_provider=SnapshotReplayMarketProvider(_snapshot()["exchanges"]["binance"]),
        now_ms=NOW,
        max_workers=1,
        spot_fee_pct=0.50,
        perp_fee_pct=0.50,
    )["forward_candidates"][0]
    assert high_fee["net_horizon_earnings_usd"] < low_fee["net_horizon_earnings_usd"]


def test_paper_transport_rejects_candidate_bound_to_different_snapshot():
    doc = _snapshot()
    result = scan_carry_venue(
        "binance",
        funding_provider=SnapshotReplayFundingProvider(doc["exchanges"]["binance"]),
        market_provider=SnapshotReplayMarketProvider(doc["exchanges"]["binance"]),
        now_ms=NOW,
        max_workers=1,
        spot_fee_pct=0.10,
        perp_fee_pct=0.05,
    )
    candidate = result["forward_candidates"][0]
    candidate["spot_book_snapshot_id"] = "book-from-another-snapshot"

    with pytest.raises(SnapshotBindingError, match="spot book identity"):
        SnapshotReplayPaperTransport(doc["exchanges"]["binance"], candidate, phase="open")


def test_replay_runs_both_exchanges_and_marks_open_close_as_simulated():
    result = run_replay_validation(
        [_snapshot()],
        run_paper_lifecycle=True,
        run_negative_plumbing_lifecycle=True,
    )

    assert result["summary"]["exchange_scans"] == 2
    assert result["summary"]["candidate_count"] == 2
    assert (
        result["summary"]["positive_net_estimate_count"]
        + result["summary"]["non_positive_net_estimate_count"]
        == result["summary"]["candidate_count"]
    )
    assert result["summary"]["paper_lifecycle_attempts"] == 2
    assert result["summary"]["paper_lifecycle_completed"] == 2
    assert result["summary"]["real_orders_submitted"] == 0
    for scan in result["scans"]:
        assert scan["venue"] in {"binance", "bybit"}
        lifecycle = scan["paper_lifecycle"]["candidates"][0]
        assert lifecycle["open"]["state"] == "simulated"
        assert lifecycle["close"]["state"] == "simulated"
        assert lifecycle["fill_mode"] == "replayed_simulated"
        assert lifecycle["execution_purpose"] == "plumbing_only_negative_estimate"
        assert lifecycle["strategy_decision"] == "no_trade"
        assert lifecycle["realized_pnl_claimed"] is False


def test_zero_candidate_replay_reports_exclusions_without_fabricating_a_positive():
    doc = _snapshot()
    for venue in doc["exchanges"].values():
        venue["funding"]["history"]["BTCUSDT"] = []
    result = run_replay_validation([doc], run_paper_lifecycle=True)

    assert result["summary"]["candidate_count"] == 0
    assert result["summary"]["paper_lifecycle_attempts"] == 0
    assert result["summary"]["real_orders_submitted"] == 0
    assert all(scan["forward_candidates"] == [] for scan in result["scans"])
    assert all("missing_funding_history" in scan["exclusion_counts"] for scan in result["scans"])
    assert result["no_profitability_claim"] is True


def test_replay_transport_uses_side_correct_quantity_vwap_for_open_and_close():
    exchange = _exchange_snapshot()
    exchange["market_universe"]["snapshot_metadata"]["spot"]["snapshot_id"] = "spot-inst-side"
    exchange["market_universe"]["snapshot_metadata"]["perpetual"]["snapshot_id"] = "perp-inst-side"
    exchange["books"]["spot"]["BTCUSDT"] = {
        "bids": [[99.0, 1.0], [98.0, 2.0]],
        "asks": [[101.0, 1.0], [102.0, 2.0]],
        "observed_at_ms": NOW,
        "exchange_ts_ms": NOW,
        "request_started_at_ms": NOW,
        "snapshot_id": "spot-book-side",
    }
    exchange["books"]["perpetual"]["BTCUSDT"] = {
        "bids": [[100.0, 1.0], [99.0, 2.0]],
        "asks": [[101.0, 1.0], [102.0, 2.0]],
        "observed_at_ms": NOW,
        "exchange_ts_ms": NOW,
        "request_started_at_ms": NOW,
        "snapshot_id": "perp-book-side",
    }
    candidate = {
        "symbol": "BTCUSDT",
        "snapshot_id": "candidate-side",
        "spot_book_snapshot_id": "spot-book-side",
        "perp_book_snapshot_id": "perp-book-side",
        "spot_instrument_snapshot_id": "spot-inst-side",
        "perp_instrument_snapshot_id": "perp-inst-side",
        "quantity_base": 1.5,
    }

    opened = SnapshotReplayPaperTransport(exchange, candidate, phase="open")
    open_fills = opened.execute_trades(
        [
            {"symbol": "BTC", "type": "buy", "amount_base": 1.5},
            {"symbol": "BTC", "type": "open_short", "amount_base": 1.5},
        ],
        {},
        dry_run=True,
    )
    assert open_fills[0]["exec_price"] == pytest.approx((101.0 + 102.0 * 0.5) / 1.5)
    assert open_fills[1]["exec_price"] == pytest.approx((100.0 + 99.0 * 0.5) / 1.5)
    assert open_fills[0]["book_side"] == "asks"
    assert open_fills[1]["book_side"] == "bids"

    closed = SnapshotReplayPaperTransport(exchange, candidate, phase="close")
    close_fills = closed.execute_trades(
        [
            {"symbol": "BTC", "type": "sell", "amount_base": 1.5},
            {"symbol": "BTC", "type": "close_short", "amount_base": 1.5},
        ],
        {},
        dry_run=True,
    )
    assert close_fills[0]["exec_price"] == pytest.approx((99.0 + 98.0 * 0.5) / 1.5)
    assert close_fills[1]["exec_price"] == pytest.approx((101.0 + 102.0 * 0.5) / 1.5)
    assert close_fills[0]["book_side"] == "bids"
    assert close_fills[1]["book_side"] == "asks"


def test_strategy_gate_skips_non_positive_estimates_and_optional_engine_run_is_labeled():
    expensive = {"binance": {"spot": 1.0, "perp": 1.0}, "bybit": {"spot": 1.0, "perp": 1.0}}
    result = run_replay_validation(
        [_snapshot()],
        fee_assumptions=expensive,
        run_paper_lifecycle=True,
    )

    assert result["summary"]["positive_net_estimate_count"] == 0
    assert result["summary"]["non_positive_net_estimate_count"] == 2
    assert result["summary"]["paper_lifecycle_attempts"] == 0
    assert result["summary"]["strategy_no_trade_count"] == 2
    for scan in result["scans"]:
        assert scan["strategy_decisions"][0]["action"] == "no_trade"
        assert scan["paper_lifecycle"]["status"] == "not_run_strategy_rejected"

    plumbing = run_replay_validation(
        [_snapshot()],
        fee_assumptions=expensive,
        run_paper_lifecycle=True,
        run_negative_plumbing_lifecycle=True,
    )
    assert plumbing["summary"]["paper_lifecycle_attempts"] == 2
    assert plumbing["summary"]["strategy_no_trade_count"] == 2
    assert plumbing["summary"]["paper_lifecycle_plumbing_only_attempts"] == 2
    for scan in plumbing["scans"]:
        lifecycle = scan["paper_lifecycle"]["candidates"][0]
        assert lifecycle["execution_purpose"] == "plumbing_only_negative_estimate"
        assert lifecycle["strategy_decision"] == "no_trade"
        assert lifecycle["realized_pnl_claimed"] is False


def test_discovery_ranks_positive_funding_smallcoins_on_same_venue_without_majors():
    spot = {symbol: {} for symbol in ("BTCUSDT", "DOGEUSDT", "PEPEUSDT", "MISSINGUSDT")}
    perpetual = {symbol: {} for symbol in ("BTCUSDT", "DOGEUSDT", "PEPEUSDT", "SHIBUSDT")}
    funding = [
        {"symbol": "BTCUSDT", "rate_pct": 9.0},
        {"symbol": "DOGEUSDT", "rate_pct": 2.0},
        {"symbol": "PEPEUSDT", "rate_pct": 3.0},
        {"symbol": "SHIBUSDT", "rate_pct": 4.0},
        {"symbol": "MISSINGUSDT", "rate_pct": 8.0},
        {"symbol": "NEGUSDT", "rate_pct": -1.0},
    ]

    selected, ranking = acquisition.select_smallcoin_symbols(
        spot_markets=spot,
        perpetual_markets=perpetual,
        funding_rows=funding,
        excluded_symbols={"BTCUSDT", "ETHUSDT", "SOLUSDT"},
        pool_size=3,
    )

    assert selected == ["PEPEUSDT", "DOGEUSDT"]
    assert [row["symbol"] for row in ranking[:3]] == ["PEPEUSDT", "DOGEUSDT"]
    assert all(row["symbol"] not in {"BTCUSDT", "SHIBUSDT", "MISSINGUSDT"} for row in ranking)


def test_replay_module_imports_without_live_transport_optional_dependency():
    module_path = Path(__file__).resolve().parents[1] / "tools" / "paper_validation_replay.py"
    probe = (
        "import runpy, sys; "
        "sys.modules['requests'] = None; "
        "runpy.run_path(sys.argv[1], run_name='paper_validation_replay_without_requests')"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe, str(module_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_acquisition_raw_paths_remain_unique_for_non_ascii_symbols(monkeypatch, tmp_path):
    collector = acquisition._Collector(tmp_path, 0, timeout=1.0)

    def fake_fetch(_url, *, timeout):
        del timeout
        return {}, b"{}", {"status": 200, "request_started_at_ms": NOW, "observed_at_ms": NOW}

    monkeypatch.setattr(acquisition, "fetch_public_json", fake_fetch)
    collector.fetch("binance", "book-spot-牛来USDT", "https://api.binance.com/api/v3/depth")
    collector.fetch("binance", "book-spot-牛龙USDT", "https://api.binance.com/api/v3/depth")

    assert len(collector.raw_files) == 2
    assert len(set(collector.raw_files)) == 2
    assert all("%" in path for path in collector.raw_files)
