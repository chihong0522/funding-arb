"""Regression tests for the FastAPI security boundaries.

Run only in the reviewed, isolated project test environment; importing route
modules may load project helpers and is not suitable for the host environment.
"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from server.security import (
    API_TOKEN_ENV,
    LIVE_TRADING_ENV,
    is_api_authorized,
    is_origin_allowed,
    is_websocket_authorized,
    live_trading_enabled,
    request_requires_auth,
)
from server.routes.backtest import _resolve_jsonl_path
from server.routes.positions import (
    MAX_ORDER_NOTIONAL_USD,
    OpenPositionRequest,
    _NoTransferVenue,
    _execution_scope_error,
    _position_scope_error,
)


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/positions", "GET"),
        ("/api/positions/open", "POST"),
        ("/api/positions/example/close", "POST"),
        ("/api/settings/wallet/connect", "POST"),
        ("/api/settings/strategy", "GET"),
        ("/api/backtest/run", "POST"),
        ("/api/backtest/history", "GET"),
        ("/api/scanner/trigger", "POST"),
        ("/api/scanner/recalc-fees", "POST"),
        ("/api/scanner/scan-all", "POST"),
    ],
)
def test_control_plane_http_routes_require_auth(path: str, method: str) -> None:
    assert request_requires_auth(path, method)


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/scanner/status", "GET"),
        ("/api/scanner/opportunities", "GET"),
    ],
)
def test_public_scanner_routes_remain_available(path: str, method: str) -> None:
    assert not request_requires_auth(path, method)


def test_unclassified_api_routes_fail_closed() -> None:
    assert request_requires_auth("/api/unrecognized-control", "GET")
    assert request_requires_auth("/api/scanner/opportunities", "POST")


def test_api_auth_requires_a_configured_long_bearer_token() -> None:
    token = "a" * 40
    assert is_api_authorized(f"Bearer {token}", environ={API_TOKEN_ENV: token})
    assert not is_api_authorized(None, environ={API_TOKEN_ENV: token})
    assert not is_api_authorized("Bearer wrong", environ={API_TOKEN_ENV: token})
    assert not is_api_authorized(f"Bearer {token}", environ={})
    assert not is_api_authorized("Bearer short", environ={API_TOKEN_ENV: "short"})


def test_browser_origin_allowlist_is_exact_and_never_wildcard() -> None:
    assert is_origin_allowed("http://localhost:8787", environ={})
    assert not is_origin_allowed("null", environ={})
    assert not is_origin_allowed("https://attacker.example", environ={})
    assert is_origin_allowed(
        "https://dashboard.example",
        environ={"FUNDING_ARB_ALLOWED_ORIGINS": "https://dashboard.example, *"},
    )
    assert not is_origin_allowed(
        "https://attacker.example",
        environ={"FUNDING_ARB_ALLOWED_ORIGINS": "*"},
    )


def test_websocket_requires_authentication() -> None:
    token = "b" * 40
    env = {API_TOKEN_ENV: token}
    assert is_websocket_authorized(f"Bearer {token}", None, environ=env)
    assert is_websocket_authorized(
        None,
        f"funding-arb-token.{token}, funding-arb-events.v1",
        environ=env,
    )
    assert not is_websocket_authorized(None, None, environ=env)
    assert not is_websocket_authorized(
        None, "funding-arb-token.wrong, funding-arb-events.v1", environ=env
    )
    assert not is_websocket_authorized(
        f"Bearer {token}", None, environ={}
    )


def test_live_trading_is_disabled_without_explicit_process_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(LIVE_TRADING_ENV, raising=False)
    assert not live_trading_enabled()
    monkeypatch.setenv(LIVE_TRADING_ENV, "true")
    assert not live_trading_enabled()
    monkeypatch.setenv(LIVE_TRADING_ENV, "1")
    assert live_trading_enabled()


def test_execution_allows_only_forward_same_venue_binance_or_bybit_carry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(LIVE_TRADING_ENV, raising=False)
    for venue in ("binance", "bybit"):
        request = OpenPositionRequest(
            strategy="carry",
            base="BTC",
            amount_usd=100,
            direction="forward",
            futures_venue=venue,
            spot_venue=venue,
            dry_run=True,
        )
        assert _execution_scope_error(request) is None


def test_execution_rejects_cross_venue_reverse_and_non_carry_requests() -> None:
    cases = [
        {"strategy": "carry", "futures_venue": "binance", "spot_venue": "bybit"},
        {"strategy": "carry", "futures_venue": "binance", "spot_venue": "binance", "direction": "reverse"},
        {"strategy": "unified", "futures_venue": "binance", "spot_venue": "binance"},
        {"strategy": "pure_futures", "long_venue": "binance", "short_venue": "bybit"},
        {"strategy": "carry", "futures_venue": "hyperliquid", "spot_venue": "hyperliquid"},
        {"strategy": "carry", "futures_venue": "okx", "spot_venue": "okx"},
    ]
    for overrides in cases:
        values = {
            "strategy": "carry",
            "base": "BTC",
            "amount_usd": 100,
            "direction": "forward",
            "futures_venue": "binance",
            "spot_venue": "binance",
            "dry_run": True,
        }
        values.update(overrides)
        assert _execution_scope_error(OpenPositionRequest(**values))


def test_close_allowlist_rejects_non_target_positions() -> None:
    allowed = {
        "strategy": "carry",
        "direction": "forward",
        "futures_venue": "bybit",
        "spot_venue": "bybit",
    }
    assert _position_scope_error(allowed) is None
    for overrides in (
        {"strategy": "unified"},
        {"direction": "reverse"},
        {"spot_venue": "binance"},
        {"futures_venue": "hyperliquid", "spot_venue": "hyperliquid"},
    ):
        position = dict(allowed)
        position.update(overrides)
        assert _position_scope_error(position)


def test_live_open_requires_process_level_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = OpenPositionRequest(
        strategy="carry",
        base="BTC",
        amount_usd=100,
        direction="forward",
        futures_venue="binance",
        spot_venue="binance",
        dry_run=False,
    )
    monkeypatch.delenv(LIVE_TRADING_ENV, raising=False)
    assert _execution_scope_error(request)
    monkeypatch.setenv(LIVE_TRADING_ENV, "1")
    assert _execution_scope_error(request) is None


def test_order_amount_is_finite_and_has_a_hard_notional_ceiling() -> None:
    assert MAX_ORDER_NOTIONAL_USD == 500.0
    at_cap = OpenPositionRequest(
        strategy="carry",
        base="BTC",
        amount_usd=MAX_ORDER_NOTIONAL_USD,
        direction="forward",
        futures_venue="binance",
        spot_venue="binance",
        dry_run=True,
    )
    assert _execution_scope_error(at_cap) is None

    for amount in (math.nan, math.inf, -math.inf, MAX_ORDER_NOTIONAL_USD + 0.01):
        with pytest.raises(ValidationError):
            OpenPositionRequest(
                strategy="carry",
                base="BTC",
                amount_usd=amount,
                futures_venue="binance",
                spot_venue="binance",
            )


def test_transfer_disabled_venue_never_delegates_transfer() -> None:
    class Venue:
        transfers: list[tuple[object, ...]]

        def __init__(self) -> None:
            self.transfers = []
            self.name = "test venue"

        def transfer_asset(self, *args: object) -> bool:
            self.transfers.append(args)
            return True

        def get_ticker(self, symbol: str) -> float:
            return 1.0

    venue = Venue()
    safe_venue = _NoTransferVenue(venue)
    assert safe_venue.transfer_asset("USDT", 10, "spot", "futures") is False
    assert venue.transfers == []
    assert safe_venue.get_ticker("BTCUSDT") == 1.0
    assert safe_venue.name == "test venue"


def test_backtest_path_is_confined_to_data_directory(tmp_path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    snapshot = data_root / "safe.jsonl"
    snapshot.write_text("{}\n", encoding="utf-8")

    assert _resolve_jsonl_path("safe.jsonl", data_root=data_root) == snapshot.resolve()
    assert (
        _resolve_jsonl_path("data/safe.jsonl", data_root=data_root)
        == snapshot.resolve()
    )
    with pytest.raises(ValueError):
        _resolve_jsonl_path("../outside.jsonl", data_root=data_root)
    with pytest.raises(ValueError):
        _resolve_jsonl_path(str(snapshot), data_root=data_root)
    with pytest.raises(ValueError):
        _resolve_jsonl_path("safe.json", data_root=data_root)


def test_backtest_path_rejects_symlink_escape(tmp_path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    link = data_root / "escape.jsonl"
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks unavailable in this test environment")

    with pytest.raises(ValueError):
        _resolve_jsonl_path("escape.jsonl", data_root=data_root)
