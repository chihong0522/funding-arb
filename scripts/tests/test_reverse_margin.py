#!/usr/bin/env python3
"""Paper-only tests for reverse carry planning and disabled margin mutations."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from accounting.futures.delta_neutral_portfolio import default_futures_state  # noqa: E402
from core.execution_policy import LiveExecutionDisabled  # noqa: E402
from execution.delta_neutral_executor import (  # noqa: E402
    _margin_rollback_tags,
    execute_delta_neutral_trades,
)
from execution.run_cash_and_carry import apply_live_safety, disable_reverse  # noqa: E402
from strategies.futures.cross_asset_arbitrage import (  # noqa: E402
    _close_pair_trades,
    decide_cross_asset_arbitrage,
)
from transfer.transfer_providers import (  # noqa: E402
    BinanceTransferProvider,
    BitgetTransferProvider,
    BybitTransferProvider,
    OkxTransferProvider,
)
from venues.binance import BinanceSpotVenue  # noqa: E402


def approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


def _cfg(**over):
    cc = {
        "maxConcurrentPairs": 1,
        "tradeUsdPerSlot": 1000.0,
        "entryFundingRatePct": 0.05,
        "exitFundingRatePct": 0.01,
        "reverseEntryFundingRatePct": -0.05,
        "reverseExitFundingRatePct": -0.01,
        "minReverseSpreadPct": 0.02,
        "minNetEdgePct": 0.02,
        "preemptionFrictionBufferPct": 1e9,
    }
    cc.update(over)
    return {"cash": "USDT", "crossAssetArbitrage": cc}


class FakeVenue:
    """Side-effect counters for proving disabled paths stop before venue access."""

    venue_id = "fake"

    def __init__(self, margin_debt: float | None = None):
        self.calls: list[dict] = []
        self.transfers: list[tuple] = []
        self.balance_reads = 0
        self.debt_reads = 0
        self._margin_debt = margin_debt

    def execute_trades(self, trades, market, dry_run):
        out = []
        for trade in trades:
            self.calls.append(dict(trade))
            result = dict(trade)
            result["status"] = "filled"
            result["exec_qty"] = trade["amount_base"]
            result["exec_price"] = market.get(trade["symbol"], {}).get("price", 100.0)
            out.append(result)
        return out

    def transfer_asset(self, asset, amount, from_acct, to_acct):
        self.transfers.append((asset, amount, from_acct, to_acct))
        return True

    def fetch_usdt_account_balances(self):
        self.balance_reads += 1
        return {"spot": 0.0, "futures": 10000.0}

    def fetch_margin_debt(self, assets):
        self.debt_reads += 1
        if self._margin_debt is None:
            return {asset: 0.0 for asset in assets}
        return {asset: self._margin_debt for asset in assets}


def test_reverse_open_tags_margin_legs():
    market = {"ETH": {"price": 100.0}}
    trades, _ = decide_cross_asset_arbitrage(
        {"USDT": 10000.0, "ETH": 0.0},
        default_futures_state(),
        {"ETH": 100.0},
        market,
        _cfg(),
        {"ETH": -0.10},
        {"ETH": 0.01},
    )
    by_type = {trade["type"]: trade for trade in trades}
    assert set(by_type) == {"sell", "open_long"}
    sell = by_type["sell"]
    assert sell["account"] == "margin"
    assert sell["side_effect"] == "auto_borrow"
    assert "account" not in by_type["open_long"]
    assert [trade["type"] for trade in trades] == ["sell", "open_long"]
    assert all(trade["amount_base"] == 10.0 for trade in trades)
    assert all(trade["amount_usdt"] == 1000.0 for trade in trades)

    # The legacy executor remains paper-preview-only; retain the planned size,
    # ordering, and reference-price coverage without invoking a transport.
    venue = FakeVenue()
    preview = execute_delta_neutral_trades(
        venue, trades, market, dry_run=True, config={"cash": "USDT"}
    )
    assert [trade["type"] for trade in preview] == ["sell", "open_long"]
    assert all(trade["status"] == "simulated" for trade in preview)
    assert all(trade["price"] == 100.0 for trade in preview)
    assert venue.calls == [] and venue.transfers == []


def test_reverse_close_tags_auto_repay():
    pos = {"amount": 10.0, "side": "long", "entry_price": 100.0}
    trades = _close_pair_trades("ETH", pos, -10.0, 100.0, "test close")
    by_type = {trade["type"]: trade for trade in trades}
    assert set(by_type) == {"buy", "close_long"}
    buy = by_type["buy"]
    assert buy["account"] == "margin"
    assert buy["side_effect"] == "auto_repay"
    assert all(trade["amount_base"] == 10.0 for trade in trades)
    assert all(trade["amount_usdt"] == 1000.0 for trade in trades)
    forward_close = _close_pair_trades(
        "ETH",
        {"amount": 10.0, "side": "short", "entry_price": 100.0},
        10.0,
        100.0,
        "forward close",
    )
    assert all("account" not in trade for trade in forward_close)


def test_margin_rollback_tags_invert():
    assert _margin_rollback_tags(
        {"account": "margin", "side_effect": "auto_borrow"}
    ) == {"account": "margin", "side_effect": "auto_repay"}
    assert _margin_rollback_tags(
        {"account": "margin", "side_effect": "auto_repay"}
    ) == {"account": "margin", "side_effect": "auto_borrow"}
    assert _margin_rollback_tags({"type": "buy"}) == {}


@pytest.mark.parametrize(
    "trades",
    [
        pytest.param(
            [
                {"symbol": "ETH", "type": "sell", "amount_base": 10.0,
                 "account": "margin", "side_effect": "auto_borrow"},
                {"symbol": "ETH", "type": "open_long", "amount_base": 10.0},
            ],
            id="borrow-open",
        ),
        pytest.param(
            [
                {"symbol": "ETH", "type": "buy", "amount_base": 10.0,
                 "account": "margin", "side_effect": "auto_repay"},
                {"symbol": "ETH", "type": "close_long", "amount_base": 10.0},
            ],
            id="repay-close",
        ),
        pytest.param(
            [
                {"symbol": "ETH", "type": "buy", "amount_base": 10.0,
                 "account": "margin", "side_effect": "auto_repay",
                 "reason": "ROLLBACK"},
            ],
            id="rollback-repay",
        ),
    ],
)
def test_legacy_margin_executor_rejects_before_any_venue_side_effects(
    trades, monkeypatch
):
    monkeypatch.setenv("FARB_LIVE", "1")
    monkeypatch.setenv("DYDX_ENABLE_LIVE", "1")
    venue = FakeVenue(margin_debt=10.05)
    with pytest.raises(LiveExecutionDisabled):
        execute_delta_neutral_trades(
            venue, trades, {"ETH": {"price": 100.0}},
            dry_run=False, config={"cash": "USDT"}
        )
    assert venue.calls == []
    assert venue.transfers == []
    assert venue.balance_reads == 0
    assert venue.debt_reads == 0


@pytest.mark.parametrize(
    ("module_name", "venue_class_name"),
    [
        ("venues.binance", "BinanceSpotVenue"),
        ("venues.bitget", "BitgetSpotVenue"),
        ("venues.bybit", "BybitSpotVenue"),
        ("venues.okx", "OkxSpotVenue"),
    ],
)
def test_margin_and_account_mutations_reject_before_http_transport(
    module_name, venue_class_name, monkeypatch
):
    module = importlib.import_module(module_name)
    transport_calls = []

    def forbidden_transport(*args, **kwargs):
        transport_calls.append((args, kwargs))
        raise AssertionError("blocked margin operation reached HTTP transport")

    monkeypatch.setattr(module, "_api_call", forbidden_transport)
    venue = getattr(module, venue_class_name)()
    operations = (
        lambda: venue.margin_borrow("ETH", 1.0),
        lambda: venue.margin_repay("ETH", 1.0),
        lambda: venue.transfer_asset("USDT", 1.0, "spot", "futures"),
        lambda: venue.place_margin_order("ETHUSDT", "sell", 1.0, 4, ref_price=100.0),
    )
    for operation in operations:
        with pytest.raises(LiveExecutionDisabled):
            operation()
    assert transport_calls == []


def test_transfer_providers_reject_preparation_and_withdrawal_without_http(
    monkeypatch,
):
    import venues.binance as binance_module
    import venues.bitget as bitget_module
    import venues.bybit as bybit_module
    import venues.okx as okx_module

    transport_calls = []

    def forbidden_transport(*args, **kwargs):
        transport_calls.append((args, kwargs))
        raise AssertionError("blocked transfer reached HTTP transport")

    for module in (binance_module, bitget_module, bybit_module, okx_module):
        monkeypatch.setattr(module, "_api_call", forbidden_transport)

    providers = (
        BinanceTransferProvider(),
        BitgetTransferProvider(),
        BybitTransferProvider(),
        OkxTransferProvider(),
    )
    for provider in providers:
        with pytest.raises(LiveExecutionDisabled):
            provider.prepare_for_withdraw("USDT", 10.0)
        with pytest.raises(LiveExecutionDisabled):
            provider.withdraw("USDT", 10.0, "TEST", "synthetic-test-address")
    assert transport_calls == []


def test_capability_and_safety_gate():
    assert BinanceSpotVenue().supports_reverse_arbitrage() is True
    assert not getattr(FakeVenue(), "supports_reverse_arbitrage", lambda: False)()

    for cfg in (
        {
            "dry_run": False,
            "crossAssetArbitrage": {"reverseEntryFundingRatePct": -0.05},
        },
        {
            "dry_run": False,
            "enableReverseArbitrage": True,
            "crossAssetArbitrage": {"reverseEntryFundingRatePct": -0.05},
        },
    ):
        with pytest.raises(LiveExecutionDisabled):
            apply_live_safety(cfg)

    cfg2 = {
        "crossAssetArbitrage": {"reverseEntryFundingRatePct": -0.05},
        "cashAndCarry": {"reverseEntryFundingRatePct": -0.05},
    }
    disable_reverse(cfg2)
    assert cfg2["crossAssetArbitrage"]["reverseEntryFundingRatePct"] == -999.0
    assert cfg2["cashAndCarry"]["reverseEntryFundingRatePct"] == -999.0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
