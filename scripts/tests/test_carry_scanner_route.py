#!/usr/bin/env python3
"""Hermetic API contract tests for phase-3 carry scanner routing."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
for path in (str(ROOT), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

from server.routes import scanner
from server.security import request_requires_auth


def test_explicit_unsupported_carry_venues_are_rejected_without_scanning(monkeypatch):
    calls = []
    monkeypatch.setattr(scanner, "_scan_carry_fn", lambda **kwargs: calls.append(kwargs))

    async def fake_broadcast(event, data):
        return None

    monkeypatch.setattr(scanner, "_broadcast", fake_broadcast)
    response = asyncio.run(scanner.scanner_trigger(strategy="carry", venues="okx"))
    assert response is not None
    assert response["success"] is False
    assert "binance, bybit" in response["error"]
    assert calls == []


def test_carry_route_is_binance_bybit_only_and_preserves_exclusions(monkeypatch):
    assert scanner.CARRY_VENUES == ["binance", "bybit"]
    calls = []

    def fake_scan(**kwargs):
        calls.append(kwargs)
        return {
            "schema_version": 3,
            "venue": kwargs["venue"],
            "total_pairs": 9,
            "intersection_pairs": 4,
            "forward_candidates": [{"symbol": "BTCUSDT", "net_horizon_earnings_usd": 3.2}],
            "near_forward": [],
            "reverse_candidates": [{"symbol": "NOPE"}],
            "excluded": [{"symbol": "BADUSDT", "reason": "missing_funding_history"}],
            "exclusion_counts": {"missing_funding_history": 1},
            "assumptions": {"fees_are_taker": True, "private_fee_api_used": False},
            "notional_usd": kwargs["notional_usd"],
            "horizon_hours": kwargs["horizon_hours"],
            "disclaimer": "Snapshot estimate, not guaranteed.",
            "timestamp_ms": 1_700_000_000_000,
            "spot_fee_pct": 0.1,
            "futures_fee_pct": 0.05,
            "two_leg_fee_pct": 0.15,
            "fee_source": "static_vip_tier_assumption",
        }

    async def fake_broadcast(event, data):
        return None

    monkeypatch.setattr(scanner, "_scan_carry_fn", fake_scan)
    monkeypatch.setattr(scanner, "_broadcast", fake_broadcast)
    monkeypatch.setattr(scanner, "_carry_results", [])
    monkeypatch.setattr(scanner, "_carry_ts", 0.0)

    response = asyncio.run(scanner.scanner_trigger(
        strategy="carry",
        venues="binance,bybit,bitget,okx",
        notional_usd=500.0,
        horizon_hours=48.0,
    ))
    assert response is not None
    assert response["success"] is True
    assert {call["venue"] for call in calls} == {"binance", "bybit"}
    assert all(call["notional_usd"] == 500.0 for call in calls)
    assert all(call["horizon_hours"] == 48.0 for call in calls)
    assert len(response["data"]) == 2
    for block in response["data"]:
        assert block["forward"] == [{"symbol": "BTCUSDT", "net_horizon_earnings_usd": 3.2}]
        assert block["reverse"] == []
        assert block["excluded"][0]["reason"] == "missing_funding_history"
        assert block["assumptions"]["private_fee_api_used"] is False
        assert block["direction"] == "forward_only"


def test_scan_notional_and_horizon_are_shared_bounded_inputs(monkeypatch):
    calls = []
    monkeypatch.setattr(scanner, "_scan_carry_fn", lambda **kwargs: calls.append(kwargs) or {})

    async def fake_broadcast(event, data):
        return None

    monkeypatch.setattr(scanner, "_broadcast", fake_broadcast)
    monkeypatch.setattr(scanner, "_carry_results", [])
    monkeypatch.setattr(scanner, "_carry_ts", 0.0)

    response = asyncio.run(scanner.scanner_trigger(strategy="carry", venues="binance"))
    assert response["success"] is True
    assert calls[-1]["notional_usd"] == 100.0
    assert calls[-1]["horizon_hours"] == 24.0

    for overrides in (
        {"notional_usd": 501.0},
        {"notional_usd": float("nan")},
        {"notional_usd": float("inf")},
        {"horizon_hours": 169.0},
        {"horizon_hours": float("nan")},
        {"horizon_hours": float("inf")},
    ):
        try:
            asyncio.run(scanner.scanner_trigger(strategy="carry", venues="binance", **overrides))
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 422
        else:
            raise AssertionError(f"invalid scanner inputs must be rejected: {overrides}")
    assert len(calls) == 1


def test_scanner_reads_remain_public_but_scan_trigger_requires_authentication():
    assert not request_requires_auth("/api/scanner/status", "GET")
    assert not request_requires_auth("/api/scanner/opportunities", "GET")
    assert request_requires_auth("/api/scanner/trigger", "POST")
