"""Regression coverage for globally disabled real execution and transfer writes."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core.credentials as credentials_module  # noqa: E402
import venues.aster as aster_module  # noqa: E402
import venues.binance as binance_module  # noqa: E402
import venues.bitget as bitget_module  # noqa: E402
import venues.bybit as bybit_module  # noqa: E402
import venues.dydx as dydx_module  # noqa: E402
import venues.edgex as edgex_module  # noqa: E402
import venues.hyperliquid as hyperliquid_module  # noqa: E402
import venues.lighter as lighter_module  # noqa: E402
import venues.okx as okx_module  # noqa: E402
from cli import orchestrate_funding as orchestrate_module  # noqa: E402
from cli import pure_futures_trade as pure_futures_trade_module  # noqa: E402
from core.execution_policy import (  # noqa: E402
    LiveExecutionDisabled,
    block_real_execution,
)
from execution.cross_venue_executor import open_cross_venue_position  # noqa: E402
from execution.delta_neutral_executor import execute_delta_neutral_trades  # noqa: E402
from execution.pure_futures_executor import open_pure_futures_pair  # noqa: E402
from execution.pure_futures_watcher import watch_cycle  # noqa: E402
from execution.run_cash_and_carry import apply_live_safety  # noqa: E402
from execution.safe_execution import (  # noqa: E402
    RecoveryUnavailableError,
    SafeExecutionJournal,
    execute_journaled_trade,
)
from transfer.cross_venue_router import build_plan, execute_plan  # noqa: E402
from transfer.transfer_providers import (  # noqa: E402
    BitgetTransferProvider,
    BinanceTransferProvider,
    BybitTransferProvider,
    OkxTransferProvider,
)


def test_global_safety_policy_rejects_real_writes_without_consulting_opt_ins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DYDX_ENABLE_LIVE", "1")
    monkeypatch.setenv("FARB_LIVE", "1")
    monkeypatch.setenv("DCA_LIVE", "1")

    with pytest.raises(LiveExecutionDisabled):
        block_real_execution("orders")


def test_global_safety_secret_redaction_covers_adapter_and_wallet_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DYDX_MNEMONIC", "fake-mnemonic-for-redaction")
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "fake-wallet-key-for-redaction")

    text = credentials_module.redact_secret_values(
        "wallet=fake-wallet-key-for-redaction mnemonic=fake-mnemonic-for-redaction"
    )
    assert "fake-wallet-key-for-redaction" not in text
    assert "fake-mnemonic-for-redaction" not in text
    assert text.count("[REDACTED]") == 2


def test_global_safety_injected_venue_cannot_bypass_executor_live_gate(
    tmp_path: Path,
) -> None:
    class InjectedVenue:
        def __init__(self) -> None:
            self.interactions: list[tuple] = []

        def fetch_futures_symbol_rules(self, *args, **kwargs):
            self.interactions.append(("fetch_futures_symbol_rules", args, kwargs))
            return {"quantity_precision": 4, "min_trade_usdt": 0, "min_trade_base": 0}

        def fetch_symbol_rules(self, *args, **kwargs):
            self.interactions.append(("fetch_symbol_rules", args, kwargs))
            return {"quantity_precision": 4, "min_trade_usdt": 0, "min_trade_base": 0}

        def get_ticker(self, *args, **kwargs):
            self.interactions.append(("get_ticker", args, kwargs))
            return 100.0

        def fetch_usdt_account_balances(self, *args, **kwargs):
            self.interactions.append(("fetch_usdt_account_balances", args, kwargs))
            return {"spot": 1000.0, "futures": 1000.0}

        def initialize_futures_symbol(self, *args, **kwargs):
            self.interactions.append(("initialize_futures_symbol", args, kwargs))

        def transfer_asset(self, *args, **kwargs):
            self.interactions.append(("transfer_asset", args, kwargs))
            return True

        def execute_trades(self, trades, market, dry_run=False):
            self.interactions.append(("execute_trades", trades, market, dry_run))
            return [{"status": "filled"}]

    long_venue = InjectedVenue()
    short_venue = InjectedVenue()
    with pytest.raises(LiveExecutionDisabled):
        open_pure_futures_pair(
            "BTC",
            "binance",
            "bybit",
            100,
            dry_run=False,
            long_venue=long_venue,
            short_venue=short_venue,
            positions_path=tmp_path / "pure.json",
        )
    assert long_venue.interactions == short_venue.interactions == []

    futures_venue = InjectedVenue()
    spot_venue = InjectedVenue()
    with pytest.raises(LiveExecutionDisabled):
        open_cross_venue_position(
            "BTC",
            "forward",
            "binance",
            "bybit",
            100,
            dry_run=False,
            futures_venue=futures_venue,
            spot_venue=spot_venue,
            positions_path=tmp_path / "cross.json",
        )
    assert futures_venue.interactions == spot_venue.interactions == []


def test_global_safety_journaled_order_helper_cannot_submit_live_orders(
    tmp_path: Path,
) -> None:
    class InjectedVenue:
        called = False

        def execute_trades(self, trades, market, dry_run=False):
            self.called = True
            return [{"status": "filled"}]

    venue = InjectedVenue()
    journal = SafeExecutionJournal(tmp_path / "positions.json")
    with pytest.raises(LiveExecutionDisabled):
        execute_journaled_trade(
            venue,
            {"symbol": "BTC", "type": "open_short", "amount_base": 0.1},
            {"BTC": {"pair": "BTCUSDT", "price": 100}},
            journal=journal,
            operation_id="global-safety-op",
            leg="futures",
            venue_id="binance",
        )
    assert not venue.called
    assert not journal.orders_path.exists()


def test_global_safety_binance_and_bybit_block_direct_real_entrypoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binance = binance_module.BinanceSpotVenue()
    bybit = bybit_module.BybitSpotVenue()

    calls = [
        lambda: binance_module._api_call("POST", "/api/v3/order"),
        lambda: bybit_module._api_call("POST", "/v5/order/create"),
        lambda: aster_module._api_call("POST", "/fapi/v1/order"),
        lambda: binance.transfer_asset("USDT", 1, "spot", "futures"),
        lambda: binance.margin_borrow("BTC", 1),
        lambda: binance.margin_repay("BTC", 1),
        lambda: binance.place_buy("BTCUSDT", 100),
        lambda: binance.place_sell("BTCUSDT", 1),
        lambda: binance.place_futures_order("BTCUSDT", "SELL", 1),
        lambda: binance.place_margin_order("BTCUSDT", "SELL", 1),
        lambda: binance.execute_trades(
            [{"symbol": "BTC", "type": "open_short", "amount_base": 1}],
            {"BTC": {"pair": "BTCUSDT", "price": 100}},
            dry_run=False,
        ),
        lambda: bybit.transfer_asset("USDT", 1, "spot", "futures"),
        lambda: bybit.margin_borrow("BTC", 1),
        lambda: bybit.margin_repay("BTC", 1),
        lambda: bybit.place_buy("BTCUSDT", 100),
        lambda: bybit.place_sell("BTCUSDT", 1),
        lambda: bybit.place_futures_order("BTCUSDT", "SELL", 1),
        lambda: bybit.place_margin_order("BTCUSDT", "Sell", 1),
        lambda: bybit.execute_trades(
            [{"symbol": "BTC", "type": "open_short", "amount_base": 1}],
            {"BTC": {"pair": "BTCUSDT", "price": 100}},
            dry_run=False,
        ),
    ]
    for write_call in calls:
        with pytest.raises(LiveExecutionDisabled):
            write_call()

    for venue_module in (bitget_module, okx_module):
        with pytest.raises(LiveExecutionDisabled):
            venue_module._api_call("POST", "/private/order")
    for venue in (bitget_module.BitgetSpotVenue(), okx_module.OkxSpotVenue()):
        with pytest.raises(LiveExecutionDisabled):
            venue.transfer_asset("USDT", 1, "spot", "futures")
        with pytest.raises(LiveExecutionDisabled):
            venue.margin_borrow("BTC", 1)
        with pytest.raises(LiveExecutionDisabled):
            venue.margin_repay("BTC", 1)
        with pytest.raises(LiveExecutionDisabled):
            venue.place_margin_order("BTCUSDT", "sell", 1)


def test_global_safety_every_venue_executor_rejects_direct_live_order_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DYDX_ENABLE_LIVE", "1")
    venues = [
        aster_module.AsterVenue(),
        binance_module.BinanceSpotVenue(),
        bitget_module.BitgetSpotVenue(),
        bybit_module.BybitSpotVenue(),
        dydx_module.DydxVenue(),
        edgex_module.EdgexVenue(),
        hyperliquid_module.HyperliquidVenue(),
        lighter_module.LighterVenue(),
        okx_module.OkxSpotVenue(),
    ]
    for venue in venues:
        with pytest.raises(LiveExecutionDisabled):
            venue.execute_trades([], {}, dry_run=False)
        assert venue.execute_trades([], {}, dry_run=True) == []

    with pytest.raises(LiveExecutionDisabled):
        hyperliquid_module.HyperliquidVenue().initialize_futures_symbol("BTC")


def test_global_safety_low_level_dex_order_helpers_reject_live_writes() -> None:
    with pytest.raises(LiveExecutionDisabled):
        dydx_module._submit_market_order(None, None, "BTC", "BTC-USD", "BUY", 1, {}, 0)
    with pytest.raises(LiveExecutionDisabled):
        asyncio.run(edgex_module.EdgexVenue()._submit_limit_order("1", "1", "1", None))
    with pytest.raises(LiveExecutionDisabled):
        asyncio.run(lighter_module.LighterVenue()._submit_market_order(1, 1, 1, True, False))


def test_global_safety_paper_orders_and_public_market_data_remain_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"{}"

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return payload

    monkeypatch.setattr(binance_module.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    assert binance_module._api_call("GET", "/api/v3/ping") == {}

    monkeypatch.setattr(
        bybit_module,
        "http_get_json",
        lambda url: {"result": {"list": [{"lastPrice": "123.5"}]}},
    )
    assert bybit_module.BybitSpotVenue().get_ticker("BTCUSDT") == 123.5

    trades = [{"symbol": "BTC", "type": "open_short", "amount_base": 1}]
    market = {"BTC": {"pair": "BTCUSDT", "price": 100}}
    assert binance_module.BinanceSpotVenue().execute_trades(trades, market, dry_run=True)[0]["status"] == "simulated"
    assert bybit_module.BybitSpotVenue().execute_trades(trades, market, dry_run=True)[0]["status"] == "simulated"


def test_global_safety_transfer_routes_deny_plan_and_provider_write_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "transfer.cross_venue_router.find_routes",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("route lookup must not run for a live plan")),
    )
    with pytest.raises(LiveExecutionDisabled):
        build_plan("binance", "bybit", "USDT", 10, dry_run=False)
    with pytest.raises(LiveExecutionDisabled):
        execute_plan(SimpleNamespace(dry_run=False))

    providers = [
        BitgetTransferProvider(),
        BybitTransferProvider(),
        OkxTransferProvider(),
        BinanceTransferProvider(),
    ]
    for provider in providers:
        with pytest.raises(LiveExecutionDisabled):
            provider.withdraw("USDT", 10, "TEST", "test-address")
        with pytest.raises(LiveExecutionDisabled):
            provider.prepare_for_withdraw("USDT", 10)


def test_global_safety_cli_live_flags_stop_before_starting_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pure_futures_trade.py",
            "open",
            "BTC",
            "--long-venue",
            "binance",
            "--short-venue",
            "bybit",
            "--trade-usd",
            "100",
            "--live",
        ],
    )
    with pytest.raises(SystemExit):
        pure_futures_trade_module.main()

    monkeypatch.setattr(
        sys,
        "argv",
        ["orchestrate_funding.py", "--execute-transfer"],
    )
    with pytest.raises(SystemExit):
        orchestrate_module.main()


def test_global_safety_plaintext_credentials_fallback_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "credentials.json").write_text(
        json.dumps({"env": {"BINANCE_API_KEY": "must-not-load"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(credentials_module, "_AGE_DIRS", [tmp_path])
    monkeypatch.setattr(credentials_module, "_SYSTEMD_CREDS_DIRS", [tmp_path])
    monkeypatch.setattr(credentials_module, "_load_age", lambda: {})
    monkeypatch.setattr(credentials_module, "_load_systemd_creds", lambda: {})
    monkeypatch.setattr(credentials_module, "_load_keyring", lambda: {})
    monkeypatch.setattr(credentials_module, "_cache", None)

    assert credentials_module._load_all() == {}
    assert not hasattr(credentials_module, "_load_json")
    for venue_module in (bitget_module, okx_module):
        source_path = venue_module.__file__
        assert source_path is not None
        source = Path(source_path).read_text(encoding="utf-8")
        assert "credentials_file" not in source
        assert "CONFIG_PATH" not in source
        assert "credentials.json" not in source


def test_global_safety_ambiguous_incidents_remain_blocking_after_reload(
    tmp_path: Path,
) -> None:
    journal = SafeExecutionJournal(tmp_path / "positions.json")
    journal.begin_operation("ambiguous-op", "open")
    journal.record_order_intent(
        operation_id="ambiguous-op",
        leg="futures",
        venue_id="binance",
        symbol="BTCUSDT",
        action="open_short",
        requested_qty=0.1,
        reduce_only=False,
        client_order_id="fa-ambiguous",
    )
    journal.record_incident("ambiguous-op", "order_state_unknown", {"leg": "futures"})

    reloaded = SafeExecutionJournal(tmp_path / "positions.json")
    assert reloaded.has_unresolved_work()
    assert reloaded.recovery_report()["blocked"] is True
    assert reloaded.recovery_report()["live_recovery_available"] is False


def test_global_safety_operator_text_cannot_clear_live_recovery(
    tmp_path: Path,
) -> None:
    journal = SafeExecutionJournal(tmp_path / "positions.json")
    journal.begin_operation("unverified-op", "open")
    journal.record_incident("unverified-op", "unknown_order", {"leg": "spot"})

    with pytest.raises(RecoveryUnavailableError):
        journal.record_recovery_resolution(
            "unverified-op",
            operator="operator-1",
            resolution="I checked; it is clear",
            evidence={"free_text": "no exposure"},
        )
    assert SafeExecutionJournal(tmp_path / "positions.json").has_unresolved_work()


def test_global_safety_legacy_operator_clearance_events_are_not_trusted(
    tmp_path: Path,
) -> None:
    journal = SafeExecutionJournal(tmp_path / "positions.json")
    journal.begin_operation("legacy-op", "open")
    journal._append(
        journal.orders_path,
        {"event": "operation_finished", "operation_id": "legacy-op", "outcome": "operator says clear"},
    )
    journal._append(
        journal.orders_path,
        {"event": "operation_reconciled", "operation_id": "legacy-op", "evidence": {"free_text": "clear"}},
    )
    journal.record_incident("legacy-op", "old_unknown_order", {})
    incident = journal._read(journal.incidents_path)[0]
    journal._append(
        journal.incidents_path,
        {"event": "incident_resolved", "incident_id": incident["incident_id"], "evidence": {"free_text": "clear"}},
    )

    assert SafeExecutionJournal(tmp_path / "positions.json").has_unresolved_work()


def test_global_safety_live_recovery_is_explicitly_unavailable() -> None:
    assert SafeExecutionJournal.LIVE_RECOVERY_AVAILABLE is False


def test_global_safety_finish_operation_cannot_clear_unverified_live_state(
    tmp_path: Path,
) -> None:
    journal = SafeExecutionJournal(tmp_path / "positions.json")
    journal.begin_operation("finish-op", "open")

    with pytest.raises(RecoveryUnavailableError):
        journal.finish_operation("finish-op", "operator says flat")
    assert SafeExecutionJournal(tmp_path / "positions.json").has_unresolved_work()


def test_global_safety_cash_and_carry_live_config_is_rejected_not_downgraded() -> None:
    with pytest.raises(LiveExecutionDisabled):
        apply_live_safety({"dry_run": False, "enableReverseArbitrage": True})


def test_global_safety_legacy_delta_executor_rejects_real_order_requests() -> None:
    with pytest.raises(LiveExecutionDisabled):
        execute_delta_neutral_trades(
            object(),
            [{"symbol": "BTC", "type": "buy", "amount_base": 1}],
            {"BTC": {"price": 100}},
            dry_run=False,
        )


def test_global_safety_watcher_rejects_live_auto_close_before_any_scan() -> None:
    with pytest.raises(LiveExecutionDisabled):
        watch_cycle({"pureFuturesArbitrage": {"venues": []}}, dry_run=False)
