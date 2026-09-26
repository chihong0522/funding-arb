#!/usr/bin/env python3
"""Hermetic public market-data provider tests for the carry scanner."""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import backtest.funding_providers as funding_providers
from market.carry_scanner_providers import PublicCarryMarketDataProvider


def test_bybit_interval_map_uses_instrument_funding_interval_minutes(monkeypatch):
    calls = []

    def fake_get(url):
        calls.append(url)
        query = parse_qs(urlparse(url).query)
        cursor = query.get("cursor", [""])[0]
        if cursor == "next-page":
            return {
                "retCode": 0,
                "result": {
                    "list": [
                        {"symbol": "ETHUSDT", "status": "Trading", "quoteCoin": "USDT", "settleCoin": "USDT", "contractType": "LinearPerpetual", "fundingInterval": "480"},
                        {"symbol": "XRPUSDT", "status": "Trading", "quoteCoin": "USDT", "settleCoin": "USDT", "contractType": "LinearPerpetual"},
                    ],
                    "nextPageCursor": "",
                },
            }
        return {
            "retCode": 0,
            "result": {
                "list": [
                    {"symbol": "BTCUSDT", "status": "Trading", "quoteCoin": "USDT", "settleCoin": "USDT", "contractType": "LinearPerpetual", "fundingInterval": "60"},
                ],
                "nextPageCursor": "next-page",
            },
        }

    monkeypatch.setattr(funding_providers, "_http_get_with_retry", fake_get)
    provider = funding_providers.BybitFundingProvider()
    assert provider.fetch_interval_map("USDT") == {"BTCUSDT": 1.0, "ETHUSDT": 8.0}
    assert len(calls) == 2
    first = parse_qs(urlparse(calls[0]).query)
    second = parse_qs(urlparse(calls[1]).query)
    assert first["category"] == ["linear"]
    assert first["limit"] == ["1000"]
    assert second["cursor"] == ["next-page"]
    assert all(urlparse(call).netloc == "api.bybit.com" for call in calls)


def test_bybit_funding_history_pages_backwards_with_end_time(monkeypatch):
    calls = []

    def fake_get(url):
        calls.append(url)
        query = parse_qs(urlparse(url).query)
        assert query["category"] == ["linear"]
        assert query["symbol"] == ["BTCUSDT"]
        assert query["limit"] == ["200"]
        assert "cursor" not in query
        end_time = query.get("endTime", [None])[0]
        if end_time is None:
            rows = [
                {"fundingRateTimestamp": str(ts), "fundingRate": "0.001"}
                for ts in range(2199, 1999, -1)
            ]
        else:
            assert end_time == "1999"
            rows = [
                {"fundingRateTimestamp": "1500", "fundingRate": "0.002"},
                {"fundingRateTimestamp": "1000", "fundingRate": "0.003"},
            ]
        return {"retCode": 0, "result": {"list": rows}}

    monkeypatch.setattr(funding_providers, "_http_get_with_retry", fake_get)
    rows = funding_providers.BybitFundingProvider().fetch_since(
        "BTCUSDT", start_ms=1000, max_pages=5
    )
    assert len(calls) == 2
    assert len(rows) == 201
    assert rows[0] == {"ts": 1500, "rate_pct": 0.2}
    assert rows[-1] == {"ts": 2199, "rate_pct": 0.1}
    assert parse_qs(urlparse(calls[1]).query)["endTime"] == ["1999"]


def test_bybit_missing_interval_metadata_is_not_defaulted_to_eight_hours(monkeypatch):
    monkeypatch.setattr(
        funding_providers,
        "_http_get_with_retry",
        lambda url: {
            "retCode": 0,
            "result": {
                "list": [{"symbol": "BTCUSDT", "status": "Trading", "quoteCoin": "USDT", "settleCoin": "USDT", "contractType": "LinearPerpetual"}],
                "nextPageCursor": "",
            },
        },
    )
    assert funding_providers.BybitFundingProvider().fetch_interval_map("USDT") == {}


def test_binance_interval_map_includes_documented_default_and_funding_info_override(monkeypatch):
    def fake_get(url):
        if "/fapi/v1/fundingInfo" in url:
            return [{"symbol": "ETHUSDT", "fundingIntervalHours": 4}]
        if "/fapi/v1/exchangeInfo" in url:
            return {
                "symbols": [
                    {"symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
                    {"symbol": "ETHUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
                    {"symbol": "BTCUSD_PERP", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USD"},
                ]
            }
        raise AssertionError(url)

    monkeypatch.setattr(funding_providers, "_http_get_with_retry", fake_get)
    assert funding_providers.BinanceFundingProvider().fetch_interval_map("USDT") == {
        "BTCUSDT": 8.0,
        "ETHUSDT": 4.0,
    }


def test_binance_interval_lookup_failure_fails_closed(monkeypatch):
    def fail(url):
        raise RuntimeError("public interval metadata unavailable")

    monkeypatch.setattr(funding_providers, "_http_get_with_retry", fail)
    try:
        funding_providers.BinanceFundingProvider().fetch_interval_map("USDT")
    except RuntimeError:
        pass
    else:
        raise AssertionError("interval metadata failure must be propagated")


def test_bybit_market_universe_intersects_public_spot_and_linear_instruments(monkeypatch):
    requests = []

    def fake_get(url, timeout=8, retries=3):
        requests.append(url)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if parsed.path == "/v5/market/instruments-info" and query.get("category") == ["spot"]:
            return {"retCode": 0, "result": {"list": [
                {"symbol": "BTCUSDT", "status": "Trading", "quoteCoin": "USDT", "lotSizeFilter": {"minOrderQty": "0.001", "maxOrderQty": "100", "qtyStep": "0.001", "minOrderAmt": "5"}},
                {"symbol": "ETHUSDC", "status": "Trading", "quoteCoin": "USDC", "lotSizeFilter": {"minOrderQty": "0.01", "maxOrderQty": "100", "qtyStep": "0.01"}},
            ], "nextPageCursor": ""}}
        if parsed.path == "/v5/market/instruments-info" and query.get("category") == ["linear"]:
            if query.get("cursor") == ["page-2"]:
                return {"retCode": 0, "result": {"list": [
                    {"symbol": "ETHUSDT", "status": "Trading", "contractType": "LinearPerpetual", "quoteCoin": "USDT", "settleCoin": "USDT", "fundingInterval": "480", "lotSizeFilter": {"minOrderQty": "0.01", "maxMktOrderQty": "100", "qtyStep": "0.01", "minNotionalValue": "5"}},
                ], "nextPageCursor": ""}}
            return {"retCode": 0, "result": {"list": [
                {"symbol": "BTCUSDT", "status": "Trading", "contractType": "LinearPerpetual", "quoteCoin": "USDT", "settleCoin": "USDT", "fundingInterval": "60", "lotSizeFilter": {"minOrderQty": "0.001", "maxMktOrderQty": "100", "qtyStep": "0.001", "minNotionalValue": "5"}},
                {"symbol": "DOGEUSDT", "status": "Trading", "contractType": "LinearPerpetual", "quoteCoin": "USDT", "settleCoin": "USDT", "fundingInterval": "60", "lotSizeFilter": {"minOrderQty": "1", "maxMktOrderQty": "100000", "qtyStep": "1", "minNotionalValue": "5"}},
            ], "nextPageCursor": "page-2"}}
        raise AssertionError(url)

    monkeypatch.setattr("market.carry_scanner_providers.http_get_json", fake_get)
    universe = PublicCarryMarketDataProvider().fetch_market_universe("bybit")
    assert set(universe["spot"]) == {"BTCUSDT"}
    assert set(universe["perpetual"]) == {"BTCUSDT", "ETHUSDT", "DOGEUSDT"}
    assert universe["spot"]["BTCUSDT"]["qty_step"] == 0.001
    assert universe["perpetual"]["BTCUSDT"]["max_qty"] == 100.0
    assert len(requests) == 3
    assert set(universe["snapshot_metadata"]) == {"spot", "perpetual"}
    assert all(
        snapshot["observed_at_ms"] > 0 and snapshot["snapshot_id"]
        for snapshot in universe["snapshot_metadata"].values()
    )


def test_binance_market_universe_filters_to_live_usdt_spot_and_linear_perps(monkeypatch):
    def fake_get(url, timeout=8, retries=3):
        if url.startswith("https://api.binance.com/api/v3/exchangeInfo"):
            return {"symbols": [
                {"symbol": "BTCUSDT", "status": "TRADING", "quoteAsset": "USDT", "isSpotTradingAllowed": True,
                 "filters": [{"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "100", "stepSize": "0.001"}]},
                {"symbol": "BTCUSDC", "status": "TRADING", "quoteAsset": "USDC"},
            ]}
        if url.startswith("https://fapi.binance.com/fapi/v1/exchangeInfo"):
            return {"symbols": [
                {"symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT", "marginAsset": "USDT",
                 "filters": [{"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "50", "stepSize": "0.001"}]},
                {"symbol": "ETHUSDT", "status": "TRADING", "contractType": "CURRENT_QUARTER", "quoteAsset": "USDT", "marginAsset": "USDT"},
            ]}
        raise AssertionError(url)

    monkeypatch.setattr("market.carry_scanner_providers.http_get_json", fake_get)
    universe = PublicCarryMarketDataProvider().fetch_market_universe("binance")
    assert set(universe["spot"]) == {"BTCUSDT"}
    assert set(universe["perpetual"]) == {"BTCUSDT"}
    assert universe["spot"]["BTCUSDT"]["qty_step"] == 0.001
    assert universe["perpetual"]["BTCUSDT"]["max_qty"] == 50.0


def test_malformed_binance_market_universe_fails_closed(monkeypatch):
    monkeypatch.setattr("market.carry_scanner_providers.http_get_json", lambda *args, **kwargs: {})
    try:
        PublicCarryMarketDataProvider().fetch_market_universe("binance")
    except RuntimeError:
        pass
    else:
        raise AssertionError("malformed market metadata must not become an empty universe")


def test_bybit_order_book_uses_public_linear_book_with_deep_limit(monkeypatch):
    seen = []

    def fake_get(url, timeout=8, retries=3):
        seen.append(url)
        return {
            "retCode": 0,
            "result": {"b": [["99.9", "2"]], "a": [["100.1", "3"]], "ts": 123456, "cts": 123455, "u": 42},
        }

    monkeypatch.setattr("market.carry_scanner_providers.http_get_json", fake_get)
    book = PublicCarryMarketDataProvider().fetch_order_book("bybit", "perpetual", "BTCUSDT")
    query = parse_qs(urlparse(seen[0]).query)
    assert query["category"] == ["linear"]
    assert query["limit"] == ["1000"]
    assert query["symbol"] == ["BTCUSDT"]
    assert book["bids"] == [[99.9, 2.0]]
    assert book["asks"] == [[100.1, 3.0]]
    assert book["exchange_ts_ms"] == 123455
    assert book["snapshot_id"] == 42
    assert book["observed_at_ms"] > 0


def test_binance_order_book_fetch_is_public_and_preserves_both_sides(monkeypatch):
    seen = []

    def fake_get(url, timeout=8, retries=3):
        seen.append(url)
        return {"lastUpdateId": 123, "bids": [["99.9", "2"]], "asks": [["100.1", "3"]]}

    monkeypatch.setattr("market.carry_scanner_providers.http_get_json", fake_get)
    book = PublicCarryMarketDataProvider().fetch_order_book("binance", "spot", "BTCUSDT")
    assert book["bids"] == [[99.9, 2.0]]
    assert book["asks"] == [[100.1, 3.0]]
    assert book["observed_at_ms"] > 0
    assert book["snapshot_id"] == 123
    assert "/api/v3/depth?" in seen[0]
    assert urlparse(seen[0]).netloc == "api.binance.com"


@pytest.mark.parametrize("venue", ["binance", "bybit"])
@pytest.mark.parametrize("limit", [0, -1, 1001, 1.5, float("nan"), float("inf"), "100", True])
def test_order_book_limit_rejects_non_integer_or_out_of_range_values(venue, limit, monkeypatch):
    monkeypatch.setattr(
        "market.carry_scanner_providers.http_get_json",
        lambda url, timeout=8: (
            {"lastUpdateId": 1, "bids": [["99", "1"]], "asks": [["101", "1"]]}
            if venue == "binance"
            else {"retCode": 0, "result": {"b": [["99", "1"]], "a": [["101", "1"]], "u": 1}}
        ),
    )
    with pytest.raises(ValueError):
        PublicCarryMarketDataProvider().fetch_order_book(
            venue, "spot", "BTCUSDT", limit=limit
        )


@pytest.mark.parametrize(("venue", "limit"), [("binance", 5), ("bybit", 1)])
def test_order_book_limit_accepts_the_minimum_supported_depth(venue, limit, monkeypatch):
    monkeypatch.setattr(
        "market.carry_scanner_providers.http_get_json",
        lambda url, timeout=8: (
            {"lastUpdateId": 1, "bids": [["99", "1"]], "asks": [["101", "1"]]}
            if venue == "binance"
            else {"retCode": 0, "result": {"b": [["99", "1"]], "a": [["101", "1"]], "u": 1}}
        ),
    )
    book = PublicCarryMarketDataProvider().fetch_order_book(
        venue, "spot", "BTCUSDT", limit=limit
    )
    assert book["snapshot_id"]


def test_market_universe_emits_instrument_snapshot_observations_and_ids(monkeypatch):
    monkeypatch.setattr(
        "market.carry_scanner_providers.http_get_json",
        lambda url, timeout=8, retries=3: (
            {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "quoteAsset": "USDT",
                          "filters": [{"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "10", "stepSize": "0.001"}]}]}
            if url.startswith("https://api.binance.com/")
            else {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT", "marginAsset": "USDT",
                                "filters": [{"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "10", "stepSize": "0.001"}]}]}
        ),
    )
    universe = PublicCarryMarketDataProvider().fetch_market_universe("binance")
    snapshots = universe["snapshot_metadata"]
    assert set(snapshots) == {"spot", "perpetual"}
    for snapshot in snapshots.values():
        assert snapshot["observed_at_ms"] > 0
        assert snapshot["snapshot_id"]
