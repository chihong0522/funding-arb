#!/usr/bin/env python3
"""Offline regression tests for Binance/Bybit order certainty and safe journaling."""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import venues.binance as binance_module  # noqa: E402
import venues.bybit as bybit_module  # noqa: E402
import execution.cross_venue_executor as cross_venue_executor_module  # noqa: E402
import execution.pure_futures_executor as pure_futures_executor_module  # noqa: E402
import execution.safe_execution as safe_execution_module  # noqa: E402
from execution.cross_venue_executor import (  # noqa: E402
    _exec_qty,
    _filled,
    _record_position,
)
from execution.safe_execution import (  # noqa: E402
    RecoveryUnavailableError,
    SafeExecutionJournal,
    make_client_order_id,
)
from core.execution_policy import LiveExecutionDisabled  # noqa: E402


def _bypass_mock_execution_gates(
    monkeypatch: pytest.MonkeyPatch,
    *gate_refs: tuple[object, str],
) -> None:
    """Permit isolated state-machine tests to exercise only fake venue transports.

    Call only after replacing the venue transport or injecting fake venues. This
    test-scoped override must never be used by the explicit safety-gate tests.
    """

    def allow_fake_execution(*args, **kwargs):
        return None

    for module, gate_name in gate_refs:
        monkeypatch.setattr(module, gate_name, allow_fake_execution)


def test_client_order_id_is_stable_and_exchange_safe():
    first = make_client_order_id("operation-1", "spot", "open", attempt=1)
    second = make_client_order_id("operation-1", "spot", "open", attempt=1)

    assert first == second
    assert len(first) <= 36
    assert first.isascii() and first.replace("-", "").replace("_", "").isalnum()
    assert first != make_client_order_id("operation-1", "spot", "close", attempt=1)


def test_unknown_incident_durably_blocks_new_operations(tmp_path: Path):
    journal = SafeExecutionJournal(tmp_path / "positions.json")
    journal.begin_operation("op-1", "open", position_id="pos-1")
    journal.record_order_intent(
        operation_id="op-1",
        leg="futures",
        venue_id="bybit",
        symbol="BTCUSDT",
        action="open_short",
        requested_qty=0.1,
        reduce_only=False,
        client_order_id="fa-test-client-id",
    )
    journal.record_incident("op-1", "order_status_unknown", {"leg": "futures"})

    reloaded = SafeExecutionJournal(tmp_path / "positions.json")
    assert reloaded.has_unresolved_work()
    assert any("op-1" in reason for reason in reloaded.blocking_reasons())


def test_cross_venue_position_appends_are_locked(tmp_path: Path):
    path = tmp_path / "positions.json"
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda i: _record_position({"id": str(i), "status": "open"}, path),
                range(32),
            )
        )

    import json

    rows = json.loads(path.read_text(encoding="utf-8"))
    assert len(rows) == 32
    assert {row["id"] for row in rows} == {str(i) for i in range(32)}


def test_execution_helpers_never_promote_partial_or_missing_qty_to_filled():
    assert not _filled([{"status": "partial", "exec_qty": 0.4}])
    assert not _filled([{"status": "unknown", "exec_qty": 0.0}])
    assert not _filled(
        [{"status": "filled", "exec_qty": 0.4, "requested_qty": 0.5}]
    )
    assert _exec_qty([{"status": "filled"}], fallback=5.0) == 0.0
    assert _exec_qty([{"status": "partial", "exec_qty": 0.4}], fallback=5.0) == 0.4


def test_order_helper_blocks_live_submission_before_writing_intent(tmp_path: Path):
    journal = SafeExecutionJournal(tmp_path / "positions.json")
    journal.begin_operation("op-before-submit", "open")

    class InspectingVenue:
        called = False

        def execute_trades(self, trades, market, dry_run=False):
            self.called = True
            return [{"status": "filled"}]

    from execution.safe_execution import execute_journaled_trade

    venue = InspectingVenue()
    with pytest.raises(LiveExecutionDisabled):
        execute_journaled_trade(
            venue,
            {"symbol": "BTC", "type": "open_short", "amount_base": 0.1},
            {"BTC": {"pair": "BTCUSDT", "price": 100.0}},
            journal=journal,
            operation_id="op-before-submit",
            leg="futures",
            venue_id="bybit",
        )

    assert not venue.called
    assert not any(
        event.get("event") == "order_intent"
        for event in journal._read(journal.orders_path)
    )


def test_order_helper_does_not_consume_venue_results_in_live_disabled_build(tmp_path: Path):
    from execution.safe_execution import execute_journaled_trade

    journal = SafeExecutionJournal(tmp_path / "positions.json")
    journal.begin_operation("op-underfill", "open")

    class UnderfilledVenue:
        called = False

        def execute_trades(self, trades, market, dry_run=False):
            self.called = True
            return [{"status": "filled", "exec_qty": 0.4}]

    venue = UnderfilledVenue()
    with pytest.raises(LiveExecutionDisabled):
        execute_journaled_trade(
            venue,
            {"symbol": "BTC", "type": "open_short", "amount_base": 0.5},
            {"BTC": {"pair": "BTCUSDT", "price": 100.0}},
            journal=journal,
            operation_id="op-underfill",
            leg="futures",
            venue_id="binance",
        )

    assert not venue.called
    assert journal.has_unresolved_work()
    assert not any(
        event.get("event") == "incident_opened"
        for event in journal._read(journal.incidents_path)
    )


def test_manual_recovery_is_unavailable_even_with_operator_evidence(tmp_path: Path):
    journal = SafeExecutionJournal(tmp_path / "positions.json")
    journal.begin_operation("op-manual", "open")
    journal.record_incident("op-manual", "unknown_order", {"client_id": "fa-1"})

    with pytest.raises(RecoveryUnavailableError):
        journal.record_recovery_resolution(
            "op-manual",
            operator="operator-1",
            resolution="queried order and position; confirmed no exposure",
            evidence={"venue_order_id": "123", "position_qty": 0},
        )

    reloaded = SafeExecutionJournal(tmp_path / "positions.json")
    assert reloaded.has_unresolved_work()
    assert reloaded.recovery_report()["live_recovery_available"] is False
    events = reloaded._read(reloaded.orders_path)
    assert not any(
        event.get("event") == "operation_reconciled"
        and event.get("operator") == "operator-1"
        and event.get("evidence", {}).get("venue_order_id") == "123"
        for event in events
    )


def test_binance_direct_live_order_is_disabled(monkeypatch):
    def unexpected_api_call(*args, **kwargs):
        raise AssertionError("a live order must not be submitted without a journaled ID")

    monkeypatch.setattr(binance_module, "_api_call", unexpected_api_call)
    venue = binance_module.BinanceSpotVenue()

    with pytest.raises(LiveExecutionDisabled):
        venue.place_futures_order("BTCUSDT", "SELL", 0.5)


def test_bybit_direct_live_order_is_disabled(monkeypatch):
    def unexpected_api_call(*args, **kwargs):
        raise AssertionError("a live order must not be submitted without a journaled ID")

    monkeypatch.setattr(bybit_module, "_api_call", unexpected_api_call)
    venue = bybit_module.BybitSpotVenue()
    monkeypatch.setattr(venue, "initialize_futures_symbol", lambda pair: None)

    with pytest.raises(LiveExecutionDisabled):
        venue.place_futures_order("BTCUSDT", "open_short", 0.5)


def test_binance_initializer_verifies_preconfigured_settings_without_mutation(monkeypatch):
    calls: list[tuple[str, str, dict | None, bool]] = []

    def fake_api(method, path, params=None, signed=False):
        calls.append((method, path, params, signed))
        if method == "GET" and path == "/fapi/v1/positionSide/dual":
            return {"dualSidePosition": False}
        if method == "GET" and path == "/fapi/v1/symbolConfig":
            return [{
                "symbol": "BTCUSDT",
                "marginType": "ISOLATED",
                "leverage": 1,
                "isAutoAddMargin": False,
            }]
        raise AssertionError((method, path, params, signed))

    binance_module._initialized_symbols.discard("BTCUSDT")
    monkeypatch.setattr(binance_module, "_api_call", fake_api)
    binance_module.BinanceSpotVenue().initialize_futures_symbol("BTCUSDT")

    assert {method for method, _, _, _ in calls} == {"GET"}
    assert {path for _, path, _, _ in calls} == {
        "/fapi/v1/positionSide/dual", "/fapi/v1/symbolConfig",
    }
    assert all(signed for _, _, _, signed in calls)


def test_binance_initializer_fails_closed_when_symbol_config_is_unavailable(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_api(method, path, params=None, signed=False):
        calls.append((method, path))
        if path == "/fapi/v1/positionSide/dual":
            return {"dualSidePosition": False}
        if path == "/fapi/v1/symbolConfig":
            return []
        raise AssertionError((method, path, params, signed))

    binance_module._initialized_symbols.discard("BTCUSDT")
    monkeypatch.setattr(binance_module, "_api_call", fake_api)
    try:
        binance_module.BinanceSpotVenue().initialize_futures_symbol("BTCUSDT")
    except RuntimeError as exc:
        assert "configuration unavailable" in str(exc)
    else:
        raise AssertionError("missing symbol configuration must fail closed")

    assert calls == [
        ("GET", "/fapi/v1/positionSide/dual"),
        ("GET", "/fapi/v1/symbolConfig"),
    ]
    assert "BTCUSDT" not in binance_module._initialized_symbols


def test_bybit_initializer_verifies_isolated_one_way_settings_without_mutation(monkeypatch):
    calls: list[tuple[str, str, dict | None]] = []

    def fake_api(method, path, params=None, body=None):
        calls.append((method, path, params))
        if method == "GET" and path == "/v5/account/info":
            return {"retCode": 0, "result": {"marginMode": "ISOLATED_MARGIN"}}
        if method == "GET" and path == "/v5/position/list":
            return {
                "retCode": 0,
                "result": {
                    "list": [{
                        "symbol": "BTCUSDT",
                        "positionIdx": 0,
                        "leverage": "1",
                        "autoAddMargin": 0,
                    }]
                },
            }
        raise AssertionError((method, path, params, body))

    bybit_module._initialized_symbols.discard("BTCUSDT")
    monkeypatch.setattr(bybit_module, "_api_call", fake_api)
    bybit_module.BybitSpotVenue().initialize_futures_symbol("BTCUSDT")

    assert calls == [
        ("GET", "/v5/account/info", None),
        ("GET", "/v5/position/list", {"category": "linear", "symbol": "BTCUSDT"}),
    ]
    assert "BTCUSDT" in bybit_module._initialized_symbols


def test_bybit_initializer_fails_closed_when_position_config_is_missing(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_api(method, path, params=None, body=None):
        calls.append((method, path))
        if path == "/v5/account/info":
            return {"retCode": 0, "result": {"marginMode": "ISOLATED_MARGIN"}}
        if path == "/v5/position/list":
            return {"retCode": 0, "result": {"list": []}}
        raise AssertionError((method, path, params, body))

    bybit_module._initialized_symbols.discard("BTCUSDT")
    monkeypatch.setattr(bybit_module, "_api_call", fake_api)
    try:
        bybit_module.BybitSpotVenue().initialize_futures_symbol("BTCUSDT")
    except RuntimeError as exc:
        assert "configuration unavailable" in str(exc)
    else:
        raise AssertionError("missing position configuration must fail closed")

    assert calls == [("GET", "/v5/account/info"), ("GET", "/v5/position/list")]
    assert "BTCUSDT" not in bybit_module._initialized_symbols


def test_bybit_initializer_rejects_cross_margin_without_changing_account(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_api(method, path, params=None, body=None):
        calls.append((method, path))
        if method == "GET" and path == "/v5/account/info":
            return {"retCode": 0, "result": {"marginMode": "REGULAR_MARGIN"}}
        return {"retCode": 0, "result": {}}

    bybit_module._initialized_symbols.discard("BTCUSDT")
    monkeypatch.setattr(bybit_module, "_api_call", fake_api)
    venue = bybit_module.BybitSpotVenue()
    try:
        venue.initialize_futures_symbol("BTCUSDT")
    except RuntimeError as exc:
        assert "ISOLATED_MARGIN" in str(exc)
    else:
        raise AssertionError("cross-margin account must fail closed")

    assert calls == [("GET", "/v5/account/info")]
    assert "BTCUSDT" not in bybit_module._initialized_symbols


def test_bybit_reverse_margin_capability_never_enables_margin_mode(monkeypatch):
    calls = []
    monkeypatch.setattr(bybit_module, "_get_key", lambda: "test-key")
    monkeypatch.setattr(bybit_module, "_get_secret", lambda: "test-secret")
    monkeypatch.setattr(
        bybit_module,
        "_api_call",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {},
    )

    assert bybit_module.BybitSpotVenue().supports_reverse_arbitrage() is False
    assert calls == []


def test_venue_initialization_failure_is_not_marked_successful(monkeypatch):
    pair = "BTCUSDT"
    binance_module._initialized_symbols.discard(pair)
    bybit_module._initialized_symbols.discard(pair)

    def fail_setup(*args, **kwargs):
        raise RuntimeError("configuration endpoint unavailable")

    monkeypatch.setattr(binance_module, "_api_call", fail_setup)
    monkeypatch.setattr(bybit_module, "_api_call", fail_setup)
    venues = (
        (binance_module.BinanceSpotVenue(), binance_module._initialized_symbols),
        (bybit_module.BybitSpotVenue(), bybit_module._initialized_symbols),
    )
    for venue, initialized in venues:
        try:
            venue.initialize_futures_symbol(pair)
        except RuntimeError as exc:
            assert "configuration endpoint unavailable" in str(exc)
        else:
            raise AssertionError("setup failure must prevent live order initialization")
        assert pair not in initialized


def test_binance_futures_close_sends_reduce_only(monkeypatch):
    submitted: list[dict] = []

    def fake_api(method, path, params=None, signed=False):
        if method == "POST" and path == "/fapi/v1/order":
            submitted.append(dict(params or {}))
            return {"orderId": 91}
        if method == "GET" and path == "/fapi/v1/order":
            return {
                "orderId": 91, "status": "FILLED", "origQty": "0.5",
                "executedQty": "0.5", "avgPrice": "100.0", "cummulativeQuoteQty": "50.0",
            }
        raise AssertionError((method, path, params))

    monkeypatch.setattr(binance_module, "_api_call", fake_api)
    _bypass_mock_execution_gates(
        monkeypatch,
        (binance_module, "require_dry_run"),
        (binance_module, "block_real_execution"),
    )
    venue = binance_module.BinanceSpotVenue()
    monkeypatch.setattr(
        venue, "fetch_futures_symbol_rules",
        lambda pair: {"quantity_precision": 3},
    )

    result = venue.execute_trades(
        [{"symbol": "BTC", "type": "close_long", "amount_base": 0.5, "amount_usdt": 50.0,
          "quantity_precision": 3, "client_order_id": "fa-binance-close"}],
        {"BTC": {"pair": "BTCUSDT", "price": 100.0, "quantity_precision": 3}},
        dry_run=False,
    )

    assert result[0]["status"] == "filled"
    assert submitted[0]["side"] == "SELL"
    assert submitted[0]["reduceOnly"] == "true"


def test_binance_timeout_queries_client_id_without_reposting(monkeypatch):
    calls: list[tuple[str, str, dict | None]] = []

    def fake_api(method, path, params=None, signed=False):
        calls.append((method, path, params))
        if method == "POST":
            raise TimeoutError("response lost after submit")
        if method == "GET" and path == "/fapi/v1/order":
            assert params["origClientOrderId"] == "fa-stable-order-id"
            return {
                "orderId": 17,
                "clientOrderId": "fa-stable-order-id",
                "status": "FILLED",
                "origQty": "0.5",
                "executedQty": "0.5",
                "avgPrice": "100.25",
            }
        raise AssertionError((method, path, params))

    monkeypatch.setattr(binance_module, "_api_call", fake_api)
    _bypass_mock_execution_gates(monkeypatch, (binance_module, "block_real_execution"))
    venue = binance_module.BinanceSpotVenue()
    ok, detail = venue.place_futures_order(
        "BTCUSDT",
        "SELL",
        0.5,
        quantity_precision=3,
        client_order_id="fa-stable-order-id",
    )

    assert ok is True
    assert detail["status"] == "filled"
    assert detail["exec_qty"] == 0.5
    assert [call[0] for call in calls].count("POST") == 1
    assert any(call[0] == "GET" for call in calls)


def test_binance_ack_without_confirmed_fill_is_not_fabricated(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_api(method, path, params=None, signed=False):
        calls.append((method, path))
        if method == "POST":
            return {"orderId": 18, "status": "NEW", "executedQty": "0"}
        if method == "GET":
            return {
                "orderId": 18,
                "status": "CANCELED",
                "origQty": "0.5",
                "executedQty": "0",
                "avgPrice": "0",
            }
        if method == "DELETE":
            return {"orderId": 18, "status": "CANCELED"}
        raise AssertionError((method, path, params))

    monkeypatch.setattr(binance_module, "_api_call", fake_api)
    _bypass_mock_execution_gates(monkeypatch, (binance_module, "block_real_execution"))
    venue = binance_module.BinanceSpotVenue()
    ok, detail = venue.place_futures_order(
        "BTCUSDT", "SELL", 0.5, client_order_id="fa-no-fill"
    )

    assert ok is False
    assert detail["status"] == "failed"
    assert detail["exec_qty"] == 0.0
    assert detail["exec_price"] is None
    assert calls.count(("POST", "/fapi/v1/order")) == 1


def test_bybit_timeout_queries_client_id_without_reposting(monkeypatch):
    calls: list[tuple[str, str, dict | None, dict | None]] = []

    def fake_api(method, path, params=None, body=None):
        calls.append((method, path, params, body))
        if method == "POST" and path == "/v5/order/create":
            raise TimeoutError("response lost after submit")
        if method == "GET" and path == "/v5/order/realtime":
            assert (params or {}).get("orderLinkId") == "fa-bybit-stable"
            return {
                "retCode": 0,
                "result": {"list": [{
                    "orderId": "92", "orderLinkId": "fa-bybit-stable", "orderStatus": "Filled",
                    "qty": "0.5", "cumExecQty": "0.5", "cumExecValue": "50.0", "avgPrice": "100.0",
                }]},
            }
        if method == "GET" and path == "/v5/order/history":
            return {"retCode": 0, "result": {"list": []}}
        raise AssertionError((method, path, params, body))

    monkeypatch.setattr(bybit_module, "_api_call", fake_api)
    _bypass_mock_execution_gates(monkeypatch, (bybit_module, "block_real_execution"))
    venue = bybit_module.BybitSpotVenue()
    monkeypatch.setattr(venue, "initialize_futures_symbol", lambda pair: None)

    ok, detail = venue.place_futures_order(
        "BTCUSDT", "open_short", 0.5, client_order_id="fa-bybit-stable",
    )

    assert ok is True and detail["status"] == "filled"
    assert detail["exec_qty"] == 0.5
    assert sum(method == "POST" for method, _, _, _ in calls) == 1


def test_bybit_order_query_failure_is_unknown_not_filled(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_api(method, path, params=None, body=None):
        calls.append((method, path))
        if method == "POST" and path == "/v5/order/create":
            return {"retCode": 0, "result": {"orderId": "77", "orderLinkId": "fa-bybit"}}
        if method == "GET":
            raise TimeoutError("order query unavailable")
        raise AssertionError((method, path, params, body))

    monkeypatch.setattr(bybit_module, "_api_call", fake_api)
    _bypass_mock_execution_gates(monkeypatch, (bybit_module, "block_real_execution"))
    venue = bybit_module.BybitSpotVenue()
    monkeypatch.setattr(venue, "initialize_futures_symbol", lambda pair: None)
    ok, detail = venue.place_futures_order(
        "BTCUSDT", "open_short", 0.5, client_order_id="fa-bybit"
    )

    assert ok is False
    assert detail["status"] == "unknown"
    assert detail["exec_qty"] == 0.0
    assert detail["order_status"] == "UNKNOWN"
    assert calls.count(("POST", "/v5/order/create")) == 1


def test_bybit_futures_close_sends_reduce_only(monkeypatch):
    submitted: list[dict] = []

    def fake_api(method, path, params=None, body=None):
        if method == "POST" and path == "/v5/order/create":
            submitted.append(dict(body or {}))
            return {"retCode": 0, "result": {"orderId": "88", "orderLinkId": "fa-close"}}
        if method == "GET" and path in ("/v5/order/realtime", "/v5/order/history"):
            return {
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "orderId": "88",
                            "orderLinkId": "fa-close",
                            "orderStatus": "Filled",
                            "qty": "0.5",
                            "cumExecQty": "0.5",
                            "avgPrice": "100.0",
                            "cumExecValue": "50.0",
                        }
                    ]
                },
            }
        raise AssertionError((method, path, params, body))

    monkeypatch.setattr(bybit_module, "_api_call", fake_api)
    _bypass_mock_execution_gates(monkeypatch, (bybit_module, "block_real_execution"))
    venue = bybit_module.BybitSpotVenue()
    monkeypatch.setattr(venue, "initialize_futures_symbol", lambda pair: None)
    ok, detail = venue.place_futures_order(
        "BTCUSDT", "close_short", 0.5, client_order_id="fa-close"
    )

    assert ok is True
    assert detail["status"] == "filled"
    assert submitted[0]["side"] == "Buy"
    assert submitted[0]["positionIdx"] == 0
    assert submitted[0]["reduceOnly"] is True


def test_delta_neutral_execution_rejects_margin_orders_without_side_effects():
    from execution.delta_neutral_executor import execute_delta_neutral_trades

    class NoSideEffectVenue:
        def __init__(self):
            self.execute_calls = 0
            self.transfer_calls = 0

        def execute_trades(self, trades, market, dry_run=False):
            self.execute_calls += 1
            return []

        def transfer_asset(self, *args):
            self.transfer_calls += 1
            return True

    venue = NoSideEffectVenue()
    with pytest.raises(LiveExecutionDisabled):
        execute_delta_neutral_trades(
            venue,
            [
                {
                    "symbol": "BTC",
                    "type": "sell",
                    "amount_base": 0.1,
                    "account": "margin",
                    "side_effect": "auto_borrow",
                }
            ],
            {"BTC": {"price": 100.0}},
            dry_run=False,
        )

    assert venue.execute_calls == 0
    assert venue.transfer_calls == 0


def test_legacy_delta_neutral_live_path_fails_closed_without_any_side_effects():
    from execution.delta_neutral_executor import execute_delta_neutral_trades

    class NoSideEffectVenue:
        def __init__(self):
            self.execute_calls = 0
            self.transfer_calls = 0

        def execute_trades(self, trades, market, dry_run=False):
            self.execute_calls += 1
            return []

        def transfer_asset(self, *args):
            self.transfer_calls += 1
            return True

    venue = NoSideEffectVenue()
    with pytest.raises(LiveExecutionDisabled):
        execute_delta_neutral_trades(
            venue,
            [
                {"symbol": "BTC", "type": "buy", "amount_base": 0.1, "amount_usdt": 10.0},
                {"symbol": "BTC", "type": "open_short", "amount_base": 0.1},
            ],
            {"BTC": {"price": 100.0}},
            dry_run=False,
        )

    assert venue.execute_calls == 0
    assert venue.transfer_calls == 0


def test_pure_futures_unknown_order_is_persisted_and_blocks_new_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from execution.pure_futures_executor import open_pure_futures_pair

    class UnknownOrderVenue:
        def __init__(self, venue_id: str):
            self.venue_id = venue_id
            self.execute_calls = 0
            self.transfer_calls = 0

        def fetch_futures_symbol_rules(self, pair):
            return {
                "symbol": pair,
                "quantity_precision": 4,
                "min_trade_usdt": 5.0,
                "min_trade_base": 0.0,
            }

        def get_ticker(self, pair):
            return 100.0

        def fetch_usdt_account_balances(self):
            return {"futures": 10000.0}

        def initialize_futures_symbol(self, pair):
            return None

        def transfer_asset(self, *args):
            self.transfer_calls += 1
            return True

        def execute_trades(self, trades, market, dry_run=False):
            self.execute_calls += 1
            return [
                {
                    "status": "unknown",
                    "order_status": "UNKNOWN",
                    "exec_qty": 0.0,
                    "error": "unresolved exchange state",
                }
            ]

    long_venue = UnknownOrderVenue("binance")
    short_venue = UnknownOrderVenue("bybit")
    positions_path = tmp_path / "positions.json"
    _bypass_mock_execution_gates(
        monkeypatch,
        (pure_futures_executor_module, "require_dry_run"),
        (safe_execution_module, "block_real_execution"),
    )
    result = open_pure_futures_pair(
        "BTC",
        "binance",
        "bybit",
        100.0,
        dry_run=False,
        long_venue=long_venue,
        short_venue=short_venue,
        positions_path=positions_path,
    )

    assert not result.ok and result.state == "recovery_required"
    assert long_venue.transfer_calls == short_venue.transfer_calls == 0
    assert long_venue.execute_calls == short_venue.execute_calls == 1

    next_result = open_pure_futures_pair(
        "BTC",
        "binance",
        "bybit",
        100.0,
        dry_run=False,
        long_venue=long_venue,
        short_venue=short_venue,
        positions_path=positions_path,
    )

    assert not next_result.ok and next_result.state == "recovery_required"
    assert long_venue.execute_calls == short_venue.execute_calls == 1


def test_corrupt_cross_venue_ledger_is_not_treated_as_empty(tmp_path: Path):
    from execution.cross_venue_executor import load_positions

    path = tmp_path / "positions.json"
    path.write_text("{truncated", encoding="utf-8")

    try:
        load_positions(path)
    except (ValueError, OSError):
        pass
    else:
        raise AssertionError("corrupt position ledger must fail closed, not look empty")


def test_corrupt_ledgers_block_live_opens_before_venue_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from execution.cross_venue_executor import open_cross_venue_position
    from execution.pure_futures_executor import open_pure_futures_pair

    class NoOrderVenue:
        def __init__(self):
            self.execute_calls = 0

        def execute_trades(self, trades, market, dry_run=False):
            self.execute_calls += 1
            raise AssertionError("corrupt ledger must be detected before order submission")

    cross_path = tmp_path / "cross-corrupt.json"
    pure_path = tmp_path / "pure-corrupt.json"
    cross_path.write_text("{truncated", encoding="utf-8")
    pure_path.write_text("{truncated", encoding="utf-8")
    cross_venue = NoOrderVenue()
    pure_long = NoOrderVenue()
    pure_short = NoOrderVenue()
    _bypass_mock_execution_gates(
        monkeypatch,
        (cross_venue_executor_module, "require_dry_run"),
        (pure_futures_executor_module, "require_dry_run"),
    )

    cross_result = open_cross_venue_position(
        "BTC", "forward", "binance", "bybit", 100.0, dry_run=False,
        futures_venue=cross_venue, spot_venue=NoOrderVenue(), positions_path=cross_path,
    )
    pure_result = open_pure_futures_pair(
        "BTC", "binance", "bybit", 100.0, dry_run=False,
        long_venue=pure_long, short_venue=pure_short, positions_path=pure_path,
    )

    assert cross_result.state == pure_result.state == "recovery_required"
    assert cross_venue.execute_calls == pure_long.execute_calls == pure_short.execute_calls == 0
    assert SafeExecutionJournal(cross_path).has_unresolved_work()
    assert SafeExecutionJournal(pure_path).has_unresolved_work()


def test_pure_futures_unknown_close_is_journaled_and_not_marked_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import json
    from execution.pure_futures_executor import close_pure_futures_pair

    class UnknownCloseVenue:
        def __init__(self):
            self.orders = []

        def fetch_futures_symbol_rules(self, pair):
            return {"quantity_precision": 4, "min_trade_base": 0, "min_trade_usdt": 0}

        def get_ticker(self, pair):
            return 100.0

        def initialize_futures_symbol(self, pair):
            return None

        def execute_trades(self, trades, market, dry_run=False):
            self.orders.extend(dict(trade) for trade in trades)
            return [{"status": "unknown", "order_status": "UNKNOWN", "exec_qty": 0.0}]

    positions_path = tmp_path / "pure-positions.json"
    positions_path.write_text(
        json.dumps([
            {
                "id": "position-1", "status": "open", "dry_run": False, "base": "BTC",
                "long_venue": "binance", "short_venue": "bybit", "qty": 0.1,
                "long_qty": 0.1, "short_qty": 0.1, "long_price": 100.0, "short_price": 100.0,
            }
        ]),
        encoding="utf-8",
    )
    long_venue = UnknownCloseVenue()
    short_venue = UnknownCloseVenue()
    _bypass_mock_execution_gates(
        monkeypatch,
        (pure_futures_executor_module, "require_dry_run"),
        (safe_execution_module, "block_real_execution"),
    )

    result = close_pure_futures_pair(
        "position-1", dry_run=False, long_venue=long_venue, short_venue=short_venue,
        positions_path=positions_path,
    )

    assert not result.ok and result.state == "recovery_required"
    assert len(short_venue.orders) == 1
    assert long_venue.orders == []
    assert "client_order_id" in short_venue.orders[0]
    assert json.loads(positions_path.read_text(encoding="utf-8"))[0]["status"] == "recovery_required"
    journal = SafeExecutionJournal(positions_path)
    assert journal.has_unresolved_work()


def test_pure_futures_single_leg_unknown_close_is_journaled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import json
    from execution.pure_futures_executor import close_pure_futures_leg

    class UnknownCloseVenue:
        def __init__(self):
            self.orders = []

        def fetch_futures_symbol_rules(self, pair):
            return {"quantity_precision": 4, "min_trade_base": 0, "min_trade_usdt": 0}

        def get_ticker(self, pair):
            return 100.0

        def initialize_futures_symbol(self, pair):
            return None

        def execute_trades(self, trades, market, dry_run=False):
            self.orders.extend(dict(trade) for trade in trades)
            return [{"status": "unknown", "order_status": "UNKNOWN", "exec_qty": 0.0}]

    positions_path = tmp_path / "single-leg-positions.json"
    positions_path.write_text(
        json.dumps([
            {
                "id": "position-2", "status": "open", "dry_run": False, "base": "BTC",
                "long_venue": "binance", "short_venue": "bybit", "qty": 0.1,
                "long_qty": 0.1, "short_qty": 0.1,
            }
        ]),
        encoding="utf-8",
    )
    venue = UnknownCloseVenue()
    _bypass_mock_execution_gates(
        monkeypatch,
        (pure_futures_executor_module, "block_real_execution"),
        (safe_execution_module, "block_real_execution"),
    )

    result = close_pure_futures_leg(
        "position-2", "long", long_venue=venue, positions_path=positions_path,
    )

    assert not result.ok and result.state == "recovery_required"
    assert len(venue.orders) == 1 and "client_order_id" in venue.orders[0]
    assert json.loads(positions_path.read_text(encoding="utf-8"))[0]["status"] == "recovery_required"
    assert SafeExecutionJournal(positions_path).has_unresolved_work()


def test_pure_futures_unknown_rebalance_fill_requires_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import json
    from execution.pure_futures_executor import rebalance_pure_futures_pair

    class UnknownTrimVenue:
        def __init__(self):
            self.orders = []

        def fetch_futures_symbol_rules(self, pair):
            return {"quantity_precision": 4, "min_trade_base": 0, "min_trade_usdt": 0}

        def get_ticker(self, pair):
            return 100.0

        def initialize_futures_symbol(self, pair):
            return None

        def execute_trades(self, trades, market, dry_run=False):
            self.orders.extend(dict(trade) for trade in trades)
            return [{"status": "unknown", "order_status": "UNKNOWN", "exec_qty": 0.0}]

    positions_path = tmp_path / "rebalance-positions.json"
    positions_path.write_text(
        json.dumps([
            {
                "id": "position-3", "status": "open", "dry_run": False, "base": "BTC",
                "long_venue": "binance", "short_venue": "bybit", "qty": 0.1,
                "long_qty": 0.2, "short_qty": 0.1,
            }
        ]),
        encoding="utf-8",
    )
    long_venue = UnknownTrimVenue()
    short_venue = UnknownTrimVenue()
    _bypass_mock_execution_gates(
        monkeypatch,
        (pure_futures_executor_module, "require_dry_run"),
        (safe_execution_module, "block_real_execution"),
    )

    result = rebalance_pure_futures_pair(
        "position-3", dry_run=False, long_venue=long_venue, short_venue=short_venue,
        positions_path=positions_path, long_qty=0.2, short_qty=0.1,
    )

    assert not result.ok and result.state == "recovery_required"
    assert len(long_venue.orders) == 1 and "client_order_id" in long_venue.orders[0]
    assert short_venue.orders == []
    assert json.loads(positions_path.read_text(encoding="utf-8"))[0]["status"] == "recovery_required"
    assert SafeExecutionJournal(positions_path).has_unresolved_work()
