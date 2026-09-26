#!/usr/bin/env python3
"""Hermetic tests for pure futures cross-venue executor."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import execution.pure_futures_executor as pure_futures_executor  # noqa: E402
from core.execution_policy import LiveExecutionDisabled  # noqa: E402
from execution.pure_futures_executor import (  # noqa: E402
    close_pure_futures_leg,
    close_pure_futures_pair,
    load_pure_futures_positions,
    open_pure_futures_pair,
    rebalance_pure_futures_pair,
)

TMP = Path(tempfile.gettempdir()) / "funding-arb-test-pure-futures"


class FakeFuturesVenue:
    def __init__(
        self,
        venue_id: str,
        price: float = 100.0,
        fail_types: set[str] | None = None,
        balances: dict[str, float] | None = None,
    ):
        self.venue_id = venue_id
        self.price = price
        self.fail_types = fail_types or set()
        self.trades: list[dict] = []
        self.initialized: list[str] = []
        self.transfers: list[tuple] = []
        self.balances = (
            balances
            if balances is not None
            else {
                "spot": 100000.0,
                "futures": 100000.0,
            }
        )

    def fetch_futures_symbol_rules(self, pair: str, cache_sec: int = 3600):
        return {
            "symbol": pair,
            "quantity_precision": 4,
            "quote_precision": 2,
            "min_trade_usdt": 5.0,
            "min_trade_base": 0.0,
        }

    def fetch_symbol_rules(self, pair: str, cache_sec: int = 3600):
        return self.fetch_futures_symbol_rules(pair, cache_sec)

    def get_ticker(self, pair: str):
        return self.price

    def initialize_futures_symbol(self, pair: str):
        self.initialized.append(pair)

    def fetch_usdt_account_balances(self):
        return dict(self.balances)

    def transfer_asset(
        self, asset: str, amount: float, from_account: str, to_account: str
    ):
        self.transfers.append((asset, amount, from_account, to_account))
        if self.balances.get(from_account, 0.0) < amount:
            return False
        self.balances[from_account] -= amount
        self.balances[to_account] = self.balances.get(to_account, 0.0) + amount
        return True

    def execute_trades(self, trades, market, dry_run=True):
        out = []
        for t in trades:
            self.trades.append(dict(t, dry_run=dry_run))
            typ = t["type"]
            if typ in self.fail_types and not dry_run:
                out.append(
                    {
                        "symbol": t["symbol"],
                        "type": typ,
                        "status": "failed",
                        "error": f"fail {typ}",
                    }
                )
            else:
                out.append(
                    {
                        "symbol": t["symbol"],
                        "type": typ,
                        "status": "simulated" if dry_run else "filled",
                        "exec_qty": t["amount_base"],
                        "exec_price": self.price,
                    }
                )
        return out


class FakeSafeExecutionJournal:
    """Local journal double for state-machine tests; not live reconciliation."""

    def __init__(self, ledger_path: Path):
        self.ledger_path = ledger_path
        self.events: list[tuple] = []

    def has_unresolved_work(self) -> bool:
        return False

    def blocking_reasons(self) -> list[str]:
        return []

    def begin_operation(self, operation_id: str, kind: str, **details) -> None:
        self.events.append(("begin", operation_id, kind, details))

    def finish_operation(self, operation_id: str, outcome: str) -> None:
        self.events.append(("finish", operation_id, outcome))

    def record_incident(self, operation_id: str, reason: str, details: dict) -> str:
        self.events.append(("incident", operation_id, reason, details))
        return f"fake-incident-{len(self.events)}"


def _allow_fake_state_machine(monkeypatch, *venues) -> None:
    """Patch local execution seams only after every injected venue is a fake."""
    if not venues or any(not isinstance(venue, FakeFuturesVenue) for venue in venues):
        raise AssertionError("state-machine tests must inject only FakeFuturesVenue objects")

    def submit_fake(venue, trade, market, **_kwargs):
        if not isinstance(venue, FakeFuturesVenue):
            raise AssertionError("fake journaled submit received a non-fake venue")
        return venue.execute_trades([trade], market, dry_run=False)

    monkeypatch.setattr(pure_futures_executor, "require_dry_run", lambda *_: None)
    monkeypatch.setattr(pure_futures_executor, "execute_journaled_trade", submit_fake)
    monkeypatch.setattr(pure_futures_executor, "SafeExecutionJournal", FakeSafeExecutionJournal)


def _path(name: str) -> Path:
    TMP.mkdir(parents=True, exist_ok=True)
    p = TMP / f"{name}.json"
    if p.exists():
        p.unlink()
    return p


def test_dry_run_open_and_close_records_position():
    path = _path("dry")
    lv, sv = FakeFuturesVenue("okx"), FakeFuturesVenue("bybit")
    res = open_pure_futures_pair(
        "BTC",
        "okx",
        "bybit",
        500,
        dry_run=True,
        long_venue=lv,
        short_venue=sv,
        positions_path=path,
    )
    assert res.ok and res.state == "simulated"
    rows = load_pure_futures_positions(path)
    assert len(rows) == 1 and rows[0]["status"] == "open"
    assert rows[0]["direction"] == "forward"
    assert [t["type"] for t in lv.trades + sv.trades] == ["open_long", "open_short"]

    res2 = close_pure_futures_pair(
        res.position_id,
        dry_run=True,
        long_venue=lv,
        short_venue=sv,
        positions_path=path,
    )
    assert res2.ok and res2.state == "simulated"
    rows = load_pure_futures_positions(path)
    assert rows[0]["status"] == "closed"


def test_fake_transport_open_both_legs_filled(monkeypatch):
    path = _path("fake_open")
    lv, sv = FakeFuturesVenue("okx"), FakeFuturesVenue("bybit")
    _allow_fake_state_machine(monkeypatch, lv, sv)
    res = open_pure_futures_pair(
        "ETH",
        "okx",
        "bybit",
        500,
        dry_run=False,
        long_venue=lv,
        short_venue=sv,
        positions_path=path,
    )
    assert res.ok and res.state == "filled"
    assert [t["type"] for t in lv.trades] == ["open_long"]
    assert [t["type"] for t in sv.trades] == ["open_short"]
    rows = load_pure_futures_positions(path)
    assert rows[0]["dry_run"] is False
    assert rows[0]["qty"] > 0


def test_live_single_leg_close_is_blocked_before_fake_venue_side_effects():
    """Emergency single-leg closes remain unavailable even with injected fakes."""
    path = _path("single_leg")
    lv, sv = FakeFuturesVenue("okx"), FakeFuturesVenue("bybit")
    res = open_pure_futures_pair(
        "BTC",
        "okx",
        "bybit",
        500,
        dry_run=True,
        long_venue=lv,
        short_venue=sv,
        positions_path=path,
    )
    assert res.ok
    lv.trades.clear()
    sv.trades.clear()

    with pytest.raises(LiveExecutionDisabled):
        close_pure_futures_leg(
            res.position_id,
            "short",
            long_venue=lv,
            short_venue=sv,
            positions_path=path,
            close_reason="synthetic single-leg safety check",
        )
    assert lv.trades == [] and sv.trades == []
    assert lv.initialized == [] and sv.initialized == []
    assert load_pure_futures_positions(path)[0]["status"] == "open"


def test_fake_transport_short_leg_failure_rolls_back_long(monkeypatch):
    path = _path("rollback_open")
    lv = FakeFuturesVenue("okx")
    sv = FakeFuturesVenue("bybit", fail_types={"open_short"})
    _allow_fake_state_machine(monkeypatch, lv, sv)
    res = open_pure_futures_pair(
        "BTC",
        "okx",
        "bybit",
        500,
        dry_run=False,
        long_venue=lv,
        short_venue=sv,
        positions_path=path,
    )
    assert not res.ok and res.state == "rolled_back"
    assert [t["type"] for t in lv.trades] == ["open_long", "close_long"]
    assert load_pure_futures_positions(path) == []


def test_fake_transport_rollback_failure_persists_recovery_required_position(monkeypatch):
    path = _path("naked_open")
    lv = FakeFuturesVenue("okx", fail_types={"close_long"})
    sv = FakeFuturesVenue("bybit", fail_types={"open_short"})
    _allow_fake_state_machine(monkeypatch, lv, sv)
    res = open_pure_futures_pair(
        "BTC",
        "okx",
        "bybit",
        500,
        dry_run=False,
        long_venue=lv,
        short_venue=sv,
        positions_path=path,
    )
    assert not res.ok and res.state == "recovery_required"
    rows = load_pure_futures_positions(path)
    assert len(rows) == 1
    assert rows[0]["status"] == "recovery_required"
    assert rows[0]["recovery_required"] is True
    assert rows[0]["long_qty"] > 0 and rows[0]["short_qty"] == 0


def test_fake_transport_close_failure_reopens_short(monkeypatch):
    path = _path("rollback_close")
    lv = FakeFuturesVenue("okx")
    sv = FakeFuturesVenue("bybit")
    _allow_fake_state_machine(monkeypatch, lv, sv)
    res = open_pure_futures_pair(
        "BTC",
        "okx",
        "bybit",
        500,
        dry_run=False,
        long_venue=lv,
        short_venue=sv,
        positions_path=path,
    )
    assert res.ok
    lv.fail_types.add("close_long")
    res2 = close_pure_futures_pair(
        res.position_id,
        dry_run=False,
        long_venue=lv,
        short_venue=sv,
        positions_path=path,
    )
    assert not res2.ok and res2.state == "rolled_back"
    assert [t["type"] for t in sv.trades] == ["open_short", "close_short", "open_short"]
    assert load_pure_futures_positions(path)[0]["status"] == "open"


def test_mark_spread_gate_aborts():
    path = _path("spread_gate")
    lv = FakeFuturesVenue("okx", price=100.0)
    sv = FakeFuturesVenue("bybit", price=103.0)
    res = open_pure_futures_pair(
        "BTC",
        "okx",
        "bybit",
        500,
        dry_run=True,
        max_mark_spread_pct=1.0,
        long_venue=lv,
        short_venue=sv,
        positions_path=path,
    )
    assert not res.ok and res.state == "aborted"
    assert "mark spread" in res.logs[0]


def _open_with_fake_transports(path: Path, monkeypatch) -> tuple:
    lv, sv = FakeFuturesVenue("okx"), FakeFuturesVenue("bybit")
    _allow_fake_state_machine(monkeypatch, lv, sv)
    res = open_pure_futures_pair(
        "BTC",
        "okx",
        "bybit",
        500,
        dry_run=False,
        long_venue=lv,
        short_venue=sv,
        positions_path=path,
    )
    assert res.ok
    return lv, sv, res.position_id


def test_fake_transport_rebalance_balanced_is_noop(monkeypatch):
    path = _path("rebal_noop")
    lv, sv, pid = _open_with_fake_transports(path, monkeypatch)
    res = rebalance_pure_futures_pair(
        pid,
        dry_run=False,
        long_venue=lv,
        short_venue=sv,
        positions_path=path,
        long_qty=5.0,
        short_qty=5.0,
    )
    assert res.ok and res.state == "balanced"
    # No extra trades beyond the two opens
    assert [t["type"] for t in lv.trades] == ["open_long"]
    assert [t["type"] for t in sv.trades] == ["open_short"]


def test_fake_transport_rebalance_trims_long_leg(monkeypatch):
    path = _path("rebal_trim_long")
    lv, sv, pid = _open_with_fake_transports(path, monkeypatch)
    res = rebalance_pure_futures_pair(
        pid,
        dry_run=False,
        long_venue=lv,
        short_venue=sv,
        positions_path=path,
        long_qty=5.0,
        short_qty=4.0,
    )
    assert res.ok and res.state == "filled"
    trims = [t for t in lv.trades if t["type"] == "close_long"]
    assert len(trims) == 1
    assert abs(trims[0]["amount_base"] - 1.0) < 1e-9
    pos = load_pure_futures_positions(path)[0]
    assert pos["qty"] == 4.0
    assert pos["long_qty"] == 4.0 and pos["short_qty"] == 4.0
    assert pos["last_rebalance"]["venue"] == "okx"


def test_fake_transport_rebalance_trims_short_leg(monkeypatch):
    path = _path("rebal_trim_short")
    lv, sv, pid = _open_with_fake_transports(path, monkeypatch)
    res = rebalance_pure_futures_pair(
        pid,
        dry_run=False,
        long_venue=lv,
        short_venue=sv,
        positions_path=path,
        long_qty=3.0,
        short_qty=3.5,
    )
    assert res.ok and res.state == "filled"
    trims = [t for t in sv.trades if t["type"] == "close_short"]
    assert len(trims) == 1
    assert abs(trims[0]["amount_base"] - 0.5) < 1e-9
    assert load_pure_futures_positions(path)[0]["qty"] == 3.0


def test_fake_transport_rebalance_trim_failure_aborts(monkeypatch):
    path = _path("rebal_fail")
    lv, sv, pid = _open_with_fake_transports(path, monkeypatch)
    lv.fail_types.add("close_long")
    res = rebalance_pure_futures_pair(
        pid,
        dry_run=False,
        long_venue=lv,
        short_venue=sv,
        positions_path=path,
        long_qty=5.0,
        short_qty=4.0,
    )
    assert not res.ok and res.state == "aborted"
    # Position record untouched on failure
    pos = load_pure_futures_positions(path)[0]
    assert "last_rebalance" not in pos


def test_fake_transport_rebalance_leg_gone_aborts(monkeypatch):
    path = _path("rebal_gone")
    lv, sv, pid = _open_with_fake_transports(path, monkeypatch)
    res = rebalance_pure_futures_pair(
        pid,
        dry_run=False,
        long_venue=lv,
        short_venue=sv,
        positions_path=path,
        long_qty=5.0,
        short_qty=0.0,
    )
    assert not res.ok and res.state == "aborted"


def test_fake_transport_open_aborts_when_margin_insufficient(monkeypatch):
    """Both venues have insufficient balance → abort without placing any orders."""
    path = _path("margin_insufficient")
    lv = FakeFuturesVenue("okx", balances={"spot": 0.0, "futures": 100.0})
    sv = FakeFuturesVenue("bybit", balances={"spot": 0.0, "futures": 100.0})
    _allow_fake_state_machine(monkeypatch, lv, sv)
    res = open_pure_futures_pair(
        "BTC",
        "okx",
        "bybit",
        500,  # needs ≥ 525 (1.05x)
        dry_run=False,
        long_venue=lv,
        short_venue=sv,
        positions_path=path,
    )
    assert not res.ok and res.state == "aborted"
    assert lv.trades == [] and sv.trades == []
    assert any("insufficient pre-funded futures margin" in log for log in res.logs)


def test_fake_transport_open_never_transfers_spot_shortfall(monkeypatch):
    """Spot funds do not permit an automatic transfer into underfunded futures."""
    path = _path("margin_no_transfer")
    lv = FakeFuturesVenue("okx", balances={"spot": 1000.0, "futures": 100.0})
    sv = FakeFuturesVenue("bybit", balances={"spot": 1000.0, "futures": 600.0})
    _allow_fake_state_machine(monkeypatch, lv, sv)
    balances_before = (dict(lv.balances), dict(sv.balances))
    res = open_pure_futures_pair(
        "BTC",
        "okx",
        "bybit",
        500,
        dry_run=False,
        long_venue=lv,
        short_venue=sv,
        positions_path=path,
    )
    assert not res.ok and res.state == "aborted"
    assert lv.trades == [] and sv.trades == []
    assert lv.transfers == [] and sv.transfers == []
    assert (lv.balances, sv.balances) == balances_before
    assert any("insufficient pre-funded futures margin" in log for log in res.logs)


def test_fake_transport_open_margin_includes_capital_buffer(monkeypatch):
    """capital_buffer_pct is included in margin requirement."""
    path = _path("margin_buffer")
    # Balance just meets 1.05x but not 1.05x + 10% buffer
    lv = FakeFuturesVenue("okx", balances={"spot": 0.0, "futures": 530.0})
    sv = FakeFuturesVenue("bybit", balances={"spot": 0.0, "futures": 530.0})
    _allow_fake_state_machine(monkeypatch, lv, sv)
    res = open_pure_futures_pair(
        "BTC",
        "okx",
        "bybit",
        500,  # 1.05x = 525 ≤ 530, but + 10% buffer = 575 > 530
        dry_run=False,
        long_venue=lv,
        short_venue=sv,
        positions_path=path,
        capital_buffer_pct=10.0,
    )
    assert not res.ok and res.state == "aborted"
    # Without buffer it can open
    res2 = open_pure_futures_pair(
        "BTC",
        "okx",
        "bybit",
        500,
        dry_run=False,
        long_venue=lv,
        short_venue=sv,
        positions_path=path,
    )
    assert res2.ok


def test_fake_transport_open_aborts_when_balance_query_fails(monkeypatch):
    """Unknown balance state fails closed and places no fake venue orders."""
    path = _path("margin_api_fail")
    lv = FakeFuturesVenue("okx")
    sv = FakeFuturesVenue("bybit")

    def _boom():
        raise RuntimeError("api down")

    lv.fetch_usdt_account_balances = _boom
    _allow_fake_state_machine(monkeypatch, lv, sv)
    res = open_pure_futures_pair(
        "BTC",
        "okx",
        "bybit",
        500,
        dry_run=False,
        long_venue=lv,
        short_venue=sv,
        positions_path=path,
    )
    assert not res.ok and res.state == "aborted"
    assert lv.trades == [] and sv.trades == []
    assert any("balance query failed; refusing open" in log for log in res.logs)


def test_fake_transport_close_spread_normal_no_warning(monkeypatch):
    """Spread is normal at close (not significantly widened) → no WARN log, normal close."""
    path = _path("close_spread_ok")
    lv = FakeFuturesVenue("okx", price=100.0)
    sv = FakeFuturesVenue("bybit", price=101.0)  # opening spread ~1%
    _allow_fake_state_machine(monkeypatch, lv, sv)
    res = open_pure_futures_pair(
        "BTC",
        "okx",
        "bybit",
        500,
        dry_run=False,
        long_venue=lv,
        short_venue=sv,
        positions_path=path,
    )
    assert res.ok

    # Spread has not widened at close (still ~1%)
    res2 = close_pure_futures_pair(
        res.position_id,
        dry_run=False,
        long_venue=lv,
        short_venue=sv,
        positions_path=path,
        warn_spread_widen_pct=0.5,
    )
    assert res2.ok and res2.state == "filled"
    assert not any("WARN spread widened" in log for log in res2.logs)
    # Verify close_info contains spread data
    pos = load_pure_futures_positions(path)[0]
    assert pos["status"] == "closed"
    assert "open_mark_spread" in pos["close_info"]
    assert "close_mark_spread" in pos["close_info"]


def test_fake_transport_close_spread_widened_warns(monkeypatch):
    """Spread widens beyond threshold → still closes normally, but logs contain WARN."""
    path = _path("close_spread_warn")
    lv = FakeFuturesVenue("okx", price=100.0)
    sv = FakeFuturesVenue("bybit", price=100.5)  # opening spread ~0.5%
    _allow_fake_state_machine(monkeypatch, lv, sv)
    res = open_pure_futures_pair(
        "BTC",
        "okx",
        "bybit",
        500,
        dry_run=False,
        long_venue=lv,
        short_venue=sv,
        positions_path=path,
    )
    assert res.ok

    # Simulate spread widening during hold: bybit price jumps
    sv.price = 103.0  # close spread ~3%

    res2 = close_pure_futures_pair(
        res.position_id,
        dry_run=False,
        long_venue=lv,
        short_venue=sv,
        positions_path=path,
        warn_spread_widen_pct=0.5,
    )
    assert res2.ok and res2.state == "filled"
    assert any("WARN spread widened" in log for log in res2.logs)
    pos = load_pure_futures_positions(path)[0]
    assert pos["close_info"]["open_mark_spread"] > 0
    assert (
        pos["close_info"]["close_mark_spread"] > pos["close_info"]["open_mark_spread"]
    )


def test_fake_transport_close_no_mark_spread_field_graceful(monkeypatch):
    """position record has no mark_spread_pct → no error, graceful degradation."""
    path = _path("close_no_spread")
    lv = FakeFuturesVenue("okx", price=100.0)
    sv = FakeFuturesVenue("bybit", price=100.5)
    _allow_fake_state_machine(monkeypatch, lv, sv)
    res = open_pure_futures_pair(
        "BTC",
        "okx",
        "bybit",
        500,
        dry_run=False,
        long_venue=lv,
        short_venue=sv,
        positions_path=path,
    )
    assert res.ok

    # Manually remove mark_spread_pct to simulate legacy data
    import json

    rows = json.loads(path.read_text())
    del rows[0]["mark_spread_pct"]
    path.write_text(json.dumps(rows))

    # Spread widens further, but should not error
    sv.price = 105.0
    res2 = close_pure_futures_pair(
        res.position_id,
        dry_run=False,
        long_venue=lv,
        short_venue=sv,
        positions_path=path,
        warn_spread_widen_pct=0.5,
    )
    assert res2.ok and res2.state == "filled"
    assert not any("WARN spread widened" in log for log in res2.logs)


def test_rebalance_dry_run_no_record_change():
    path = _path("rebal_dry")
    lv, sv = FakeFuturesVenue("okx"), FakeFuturesVenue("bybit")
    res = open_pure_futures_pair(
        "BTC",
        "okx",
        "bybit",
        500,
        dry_run=True,
        long_venue=lv,
        short_venue=sv,
        positions_path=path,
    )
    res2 = rebalance_pure_futures_pair(
        res.position_id,
        dry_run=True,
        long_venue=lv,
        short_venue=sv,
        positions_path=path,
        long_qty=5.0,
        short_qty=4.0,
    )
    assert res2.ok and res2.state == "simulated"
    pos = load_pure_futures_positions(path)[0]
    assert "last_rebalance" not in pos
