"""Server-authoritative paper opens for cached carry-scanner candidates."""
from __future__ import annotations

import asyncio
import multiprocessing
import sys
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
for path in (str(ROOT), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

from execution.cross_venue_executor import CrossVenueResult, load_positions, open_cross_venue_position
from server.routes import positions, scanner

HOUR_MS = 3_600_000
SCAN_ID = "a" * 64
CANDIDATE_ID = "b" * 64


def _cached_scan(now_ms: int | None = None, *, overrides=None):
    now = now_ms or int(time.time() * 1000)
    candidate = {
        "venue": "binance",
        "base": "BTC",
        "symbol": "BTCUSDT",
        "direction": "forward",
        "snapshot_id": CANDIDATE_ID,
        "snapshot_estimate": True,
        "snapshot_ts_ms": now,
        "snapshot_observed_at_ms": now,
        "funding_observed_at_ms": now,
        "next_funding_ts": now + HOUR_MS,
        "interval_h": 8.0,
        "history_samples": 3,
        "history_first_ts": now - 24 * HOUR_MS,
        "history_latest_ts": now - 8 * HOUR_MS,
        "spot_book_observed_at_ms": now,
        "perp_book_observed_at_ms": now,
        "spot_book_source_ts_ms": now,
        "perp_book_source_ts_ms": now,
        "spot_book_request_started_at_ms": now,
        "perp_book_request_started_at_ms": now,
        "spot_book_snapshot_id": "spot-book-1",
        "perp_book_snapshot_id": "perp-book-1",
        "spot_instrument_snapshot_observed_at_ms": now,
        "perp_instrument_snapshot_observed_at_ms": now,
        "spot_instrument_snapshot_id": "spot-instruments-1",
        "perp_instrument_snapshot_id": "perp-instruments-1",
        "source_timestamp_skew_ms": 0,
        "notional_usd_requested": 100.0,
        "horizon_hours": 24.0,
        "rate_pct": 0.05,
        "historical_median_rate_pct": 0.04,
        "expected_rate_pct": 0.04,
        "funding_estimator": "min(current_rate, recent_median_rate)",
        "funding_payments_estimated": 3,
        "quantity_base": 1.0,
        "spot_notional_usd": 100.0,
        "perp_notional_usd": 100.1,
        "mark_price": 100.0,
        "spot_entry_vwap": 100.0,
        "spot_exit_vwap": 99.9,
        "perp_entry_vwap": 100.1,
        "perp_exit_vwap": 100.2,
        "spot_fee_pct": 0.1,
        "perp_fee_pct": 0.05,
        "futures_fee_pct": 0.05,
        "fee_pct": 0.15,
        "entry_fee_usd": 0.15005,
        "exit_fee_usd": 0.15003,
        "observed_round_trip_book_cost_usd": 0.2,
        "exit_slippage_bps_per_leg": 10.0,
        "exit_slippage_assumption_usd": 0.2,
        "basis_buffer_bps": 10.0,
        "basis_buffer_usd": 0.1,
        "gross_funding_usd": 1.2,
        "total_estimated_cost_usd": 0.80008,
        "net_horizon_earnings_usd": 0.39992,
        "short_margin_multiplier": 1.0,
        "short_margin_usd": 100.1,
        "capital_required_usd": 200.70008,
        "snapshot_estimate": True,
    }
    candidate.update(overrides or {})
    return {
        "venue": "binance",
        "schema_version": 3,
        "snapshot_id": SCAN_ID,
        "timestamp_ms": now,
        "completed_at_ms": now,
        "notional_usd": 100.0,
        "horizon_hours": 24.0,
        "fee_source": "static_vip_tier_assumption",
        "fee_tier": "vip0",
        "spot_fee_pct": 0.1,
        "futures_fee_pct": 0.05,
        "two_leg_fee_pct": 0.15,
        "assumptions": {
            "spot_taker_fee_pct": 0.1,
            "perp_taker_fee_pct": 0.05,
            "fees_are_taker": True,
            "fee_application": "entry and exit on each spot/perpetual leg",
            "source": "static_vip_tier_assumption",
            "tier": "vip0",
            "private_fee_api_used": False,
            "funding_rate_estimator": "min(current positive rate, median of recent signed settlements)",
            "maximum_funding_snapshot_age_sec": 60.0,
            "maximum_order_book_age_sec": 5.0,
            "maximum_source_timestamp_skew_sec": 5.0,
            "maximum_instrument_snapshot_age_sec": 300.0,
            "maximum_history_gap_intervals": 1.5,
        },
        "forward": [candidate],
        "reverse": [],
    }


def _request(**overrides):
    body = {
        "strategy": "carry",
        "base": "BTC",
        "symbol": "BTCUSDT",
        "amount_usd": 100.0,
        "horizon_hours": 24.0,
        "scan_snapshot_id": SCAN_ID,
        "candidate_snapshot_id": CANDIDATE_ID,
        "direction": "forward",
        "futures_venue": "binance",
        "spot_venue": "binance",
        "dry_run": True,
    }
    body.update(overrides)
    return positions.OpenPositionRequest(**body)


def _setup(monkeypatch, *, scan=None, used_rows=None):
    calls = []
    ledger_rows = list(used_rows or [])
    monkeypatch.setattr(scanner, "_carry_results", [scan or _cached_scan()])
    monkeypatch.setattr(positions, "_check_venues_tradeable", lambda *_a, **_kw: None)
    monkeypatch.setattr(positions, "_load_cross_positions_fn", lambda: list(ledger_rows))

    def fake_open(*args, **kwargs):
        calls.append((args, kwargs))
        ledger_rows.append({
            "status": "open",
            "dry_run": True,
            "scanner_provenance": dict(kwargs["scanner_provenance"]),
        })
        return CrossVenueResult(True, "simulated", "paper-test-1", [], [])

    monkeypatch.setattr(positions, "_open_cross_fn", fake_open)
    if hasattr(positions, "_CARRY_OPEN_IN_PROGRESS"):
        monkeypatch.setattr(positions, "_CARRY_OPEN_IN_PROGRESS", set())
    return calls


def test_carry_paper_open_requires_both_server_known_snapshot_ids(monkeypatch):
    calls = _setup(monkeypatch)

    with pytest.raises(HTTPException) as error:
        asyncio.run(positions.open_position(_request(scan_snapshot_id=None, candidate_snapshot_id=None)))

    assert error.value.status_code == 409
    assert calls == []


def test_carry_paper_open_binds_exact_cached_candidate_and_server_derived_provenance(monkeypatch):
    calls = _setup(monkeypatch)

    response = asyncio.run(positions.open_position(_request(
        net_horizon_earnings_usd=999_999.0,
        gross_funding_usd=999_999.0,
        pnl_usd=999_999.0,
    )))

    assert response["success"] is True
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[:4] == ("BTC", "forward", "binance", "binance")
    assert args[4] == 100.0
    provenance = kwargs["scanner_provenance"]
    assert provenance == {
        "scan_snapshot_id": SCAN_ID,
        "candidate_snapshot_id": CANDIDATE_ID,
        "market_snapshot_id": CANDIDATE_ID,
        "economics_revision_id": None,
        "venue": "binance",
        "symbol": "BTCUSDT",
        "direction": "forward",
        "scanned_notional_usd": 100.0,
        "horizon_hours": 24.0,
        "snapshot_ts_ms": scanner._carry_results[0]["forward"][0]["snapshot_ts_ms"],
        "funding_observed_at_ms": scanner._carry_results[0]["forward"][0]["funding_observed_at_ms"],
        "history_latest_ts": scanner._carry_results[0]["forward"][0]["history_latest_ts"],
        "interval_hours": 8.0,
        "market_snapshot_ids": {
            "spot_book_snapshot_id": "spot-book-1",
            "perp_book_snapshot_id": "perp-book-1",
            "spot_instrument_snapshot_id": "spot-instruments-1",
            "perp_instrument_snapshot_id": "perp-instruments-1",
        },
        "execution_mode": "paper",
        "fee_assumptions": {
            "spot_taker_fee_pct": 0.1,
            "perp_taker_fee_pct": 0.05,
            "two_leg_taker_fee_pct": 0.15,
            "fees_are_taker": True,
            "fee_application": "entry and exit on each spot/perpetual leg",
            "source": "static_vip_tier_assumption",
            "tier": "vip0",
            "private_fee_api_used": False,
        },
        "estimate_assumptions": scanner._carry_results[0]["assumptions"],
        "candidate_estimate": scanner._carry_results[0]["forward"][0],
    }
    assert "pnl_usd" not in provenance
    assert "net_horizon_earnings_usd" not in provenance


@pytest.mark.parametrize(
    "overrides",
    [
        {"scan_snapshot_id": "c" * 64},
        {"candidate_snapshot_id": "d" * 64},
        {"base": "ETH", "symbol": "ETHUSDT"},
        {"symbol": "BTC-USDT"},
        {"futures_venue": "bybit", "spot_venue": "bybit"},
        {"direction": "reverse"},
        {"amount_usd": 100.01},
        {"horizon_hours": 48.0},
    ],
)
def test_carry_paper_open_rejects_snapshot_candidate_or_input_mismatch(monkeypatch, overrides):
    calls = _setup(monkeypatch)

    with pytest.raises(HTTPException):
        asyncio.run(positions.open_position(_request(**overrides)))

    assert calls == []


def test_carry_paper_open_rejects_stale_candidate_and_overdue_settlement(monkeypatch):
    now = int(time.time() * 1000)
    stale_scan = _cached_scan(now - 61_000)
    calls = _setup(monkeypatch, scan=stale_scan)

    with pytest.raises(HTTPException):
        asyncio.run(positions.open_position(_request()))
    assert calls == []

    overdue_scan = _cached_scan(now)
    overdue_scan["forward"][0]["next_funding_ts"] = now - 1
    monkeypatch.setattr(scanner, "_carry_results", [overdue_scan])
    with pytest.raises(HTTPException):
        asyncio.run(positions.open_position(_request()))
    assert calls == []


def test_carry_paper_open_rejects_stale_books_and_missing_market_snapshot_identity(monkeypatch):
    now = int(time.time() * 1000)
    stale_book_scan = _cached_scan(
        now,
        overrides={"spot_book_observed_at_ms": now - 6_000},
    )
    calls = _setup(monkeypatch, scan=stale_book_scan)

    with pytest.raises(HTTPException, match="scanner candidate is stale"):
        asyncio.run(positions.open_position(_request()))
    assert calls == []

    incomplete_scan = _cached_scan(
        now,
        overrides={"spot_book_snapshot_id": None},
    )
    monkeypatch.setattr(scanner, "_carry_results", [incomplete_scan])
    with pytest.raises(HTTPException, match="snapshot identity is incomplete"):
        asyncio.run(positions.open_position(_request()))
    assert calls == []


def test_carry_paper_history_freshness_accepts_exactly_one_and_a_half_intervals(monkeypatch):
    now = int(time.time() * 1000)
    scan = _cached_scan(now)
    candidate = scan["forward"][0]
    candidate["history_latest_ts"] = now - int(1.5 * 8 * HOUR_MS)
    _setup(monkeypatch, scan=scan)

    validated, provenance = positions._carry_paper_context(_request(), now_ms=now)

    assert validated is candidate
    assert provenance["candidate_snapshot_id"] == CANDIDATE_ID

    candidate["history_latest_ts"] -= 1
    with pytest.raises(HTTPException, match="settlement history is stale"):
        positions._carry_paper_context(_request(), now_ms=now)


def test_carry_paper_open_rejects_replay_after_successful_paper_open(monkeypatch):
    calls = _setup(monkeypatch)

    first = asyncio.run(positions.open_position(_request()))
    assert first["success"] is True
    assert len(calls) == 1

    with pytest.raises(HTTPException) as error:
        asyncio.run(positions.open_position(_request()))

    assert error.value.status_code == 409
    assert len(calls) == 1


def test_carry_paper_open_rejects_replay_of_already_recorded_snapshot_candidate(monkeypatch):
    existing = {
        "id": "paper-prior",
        "status": "closed",
        "scanner_provenance": {
            "scan_snapshot_id": SCAN_ID,
            "candidate_snapshot_id": CANDIDATE_ID,
        },
    }
    calls = _setup(monkeypatch, used_rows=[existing])

    with pytest.raises(HTTPException) as error:
        asyncio.run(positions.open_position(_request()))

    assert error.value.status_code == 409
    assert calls == []


def test_paper_executor_persists_scanner_provenance_without_claiming_profit(tmp_path):
    class PaperVenue:
        def __init__(self):
            self.calls = []

        def fetch_symbol_rules(self, _pair):
            return {
                "quantity_precision": 6,
                "quote_precision": 2,
                "min_trade_usdt": 5.0,
                "min_trade_base": 0.001,
                "qty_step": 0.000001,
            }

        def fetch_futures_symbol_rules(self, pair):
            return self.fetch_symbol_rules(pair)

        def get_ticker(self, _pair):
            return 100.0

        def get_futures_ticker(self, _pair):
            return 100.0

        def execute_trades(self, trades, markets, *, dry_run):
            assert dry_run is True
            self.calls.extend(trades)
            return [
                {"symbol": trade["symbol"], "status": "simulated"}
                for trade in trades
            ]

    venue = PaperVenue()
    ledger = tmp_path / "paper-positions.json"
    provenance = {
        "scan_snapshot_id": SCAN_ID,
        "candidate_snapshot_id": CANDIDATE_ID,
        "fee_assumptions": {"spot_taker_fee_pct": 0.1, "perp_taker_fee_pct": 0.05},
        "candidate_estimate": {"net_horizon_earnings_usd": 12.34},
    }

    result = open_cross_venue_position(
        "BTC",
        "forward",
        "binance",
        "binance",
        100.0,
        dry_run=True,
        futures_venue=venue,
        spot_venue=venue,
        positions_path=ledger,
        scanner_provenance=provenance,
    )

    assert result.ok is True
    assert result.state == "simulated"
    assert all(item["status"] == "simulated" for item in result.executed)
    saved = load_positions(ledger)
    assert len(saved) == 1
    assert saved[0]["scanner_provenance"] == provenance
    assert saved[0]["dry_run"] is True
    assert saved[0]["scanner_provenance"]["candidate_estimate"]["net_horizon_earnings_usd"] == 12.34
    assert "net_horizon_earnings_usd" not in saved[0]
    assert "total_pnl_usd" not in saved[0]
    assert saved[0]["simulation_execution"]["qty_base"] == saved[0]["qty"]
    assert saved[0]["simulation_execution"]["spot"]["ticker_price"] == 100.0
    assert saved[0]["simulation_execution"]["spot"]["rules"]["quantity_precision"] == 6
    assert saved[0]["simulation_execution"]["perpetual"]["ticker_price"] == 100.0
    assert "pnl_usd" not in saved[0]
    assert "realized_pnl_usd" not in saved[0]
    assert "net_horizon_earnings_usd" not in saved[0]


def _multiprocess_paper_open_worker(
    ledger_path,
    start_barrier,
    simulation_calls,
    results,
    candidate_id=CANDIDATE_ID,
    market_snapshot_id="market-candidate-1",
):
    class NoNetworkPaperVenue:
        def fetch_symbol_rules(self, _pair):
            return {
                "quantity_precision": 6,
                "quote_precision": 2,
                "min_trade_usdt": 5.0,
                "min_trade_base": 0.0,
            }

        def fetch_futures_symbol_rules(self, pair):
            return self.fetch_symbol_rules(pair)

        def get_ticker(self, _pair):
            return 100.0

        def get_futures_ticker(self, _pair):
            return 100.0

        def execute_trades(self, trades, _markets, *, dry_run):
            assert dry_run is True
            with simulation_calls.get_lock():
                simulation_calls.value += len(trades)
            return [{"symbol": trade["symbol"], "status": "simulated"} for trade in trades]

    if start_barrier is not None:
        start_barrier.wait(timeout=15)
    venue = NoNetworkPaperVenue()
    result = open_cross_venue_position(
        "BTC",
        "forward",
        "binance",
        "binance",
        100.0,
        dry_run=True,
        futures_venue=venue,
        spot_venue=venue,
        positions_path=Path(ledger_path),
        scanner_provenance={
            "scan_snapshot_id": SCAN_ID,
            "candidate_snapshot_id": candidate_id,
            "market_snapshot_id": market_snapshot_id,
        },
    )
    results.put({"ok": result.ok, "state": result.state, "logs": result.logs})


def test_duplicate_scanner_candidate_is_claimed_once_across_processes(tmp_path):
    context = multiprocessing.get_context("fork")
    ledger = tmp_path / "multiprocess-paper-positions.json"
    barrier = context.Barrier(2)
    simulation_calls = context.Value("i", 0)
    results = context.Queue()
    workers = [
        context.Process(
            target=_multiprocess_paper_open_worker,
            args=(str(ledger), barrier, simulation_calls, results),
        )
        for _ in range(2)
    ]

    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)
        if worker.is_alive():
            worker.terminate()
            worker.join()
            pytest.fail("paper open worker hung while coordinating duplicate candidate")
        assert worker.exitcode == 0

    outcomes = [results.get(timeout=5) for _ in workers]
    assert sum(outcome["ok"] for outcome in outcomes) == 1
    assert sorted(outcome["state"] for outcome in outcomes) == ["aborted", "simulated"]
    assert simulation_calls.value == 2
    saved = load_positions(ledger)
    assert len(saved) == 1
    assert saved[0]["status"] == "open"
    assert saved[0]["scanner_provenance"]["scan_snapshot_id"] == SCAN_ID
    assert saved[0]["scanner_provenance"]["candidate_snapshot_id"] == CANDIDATE_ID
    assert saved[0]["scanner_provenance"]["market_snapshot_id"] == "market-candidate-1"

    # A fee-revised candidate has a new economics identity but must still be
    # unable to consume the same immutable market snapshot a second time.
    _multiprocess_paper_open_worker(
        str(ledger),
        None,
        simulation_calls,
        results,
        candidate_id="c" * 64,
        market_snapshot_id="market-candidate-1",
    )
    revised_outcome = results.get(timeout=5)
    assert revised_outcome["ok"] is False
    assert revised_outcome["state"] == "aborted"
    assert simulation_calls.value == 2
    assert len(load_positions(ledger)) == 1


def test_fee_recalculation_revises_candidate_identity_and_preserves_new_estimate(monkeypatch):
    import core.fee_providers as fee_providers

    now = int(time.time() * 1000)
    scan = _cached_scan(now)
    fees = {"spot": 0.2, "futures": 0.1}
    monkeypatch.setattr(scanner, "_carry_results", [scan])
    monkeypatch.setattr(scanner, "_fee_policy", lambda: {"mode": "vip_tier", "venue_tiers": {"binance": "vip0"}})
    monkeypatch.setattr(
        fee_providers,
        "resolve_venue_fee",
        lambda _venue, *, leg, **_kwargs: {
            "taker_pct": fees[leg],
            "source": "tier",
            "tier": "vip0",
        },
    )

    async def no_broadcast(_event, _data):
        return None

    monkeypatch.setattr(scanner, "_broadcast", no_broadcast)
    first = asyncio.run(scanner.scanner_recalc_fees())
    revised_candidate = first["data"]["carry"][0]["forward"][0]
    revised_id = revised_candidate["snapshot_id"]

    assert revised_id != CANDIDATE_ID
    with pytest.raises(HTTPException, match="unavailable"):
        positions._carry_paper_context(_request(), now_ms=now)

    revised_request = _request(candidate_snapshot_id=revised_id)
    _, revised_provenance = positions._carry_paper_context(revised_request, now_ms=now)
    assert revised_provenance["fee_assumptions"]["spot_taker_fee_pct"] == 0.2
    assert revised_provenance["fee_assumptions"]["perp_taker_fee_pct"] == 0.1
    assert revised_provenance["market_snapshot_id"] == CANDIDATE_ID
    assert revised_provenance["candidate_estimate"] == revised_candidate
    assert revised_provenance["candidate_estimate"]["net_horizon_earnings_usd"] < 0.39992

    repeated = asyncio.run(scanner.scanner_recalc_fees())
    assert repeated["data"]["carry"][0]["forward"][0]["snapshot_id"] == revised_id
    fees["spot"] = 0.3
    changed = asyncio.run(scanner.scanner_recalc_fees())
    assert changed["data"]["carry"][0]["forward"][0]["snapshot_id"] != revised_id
