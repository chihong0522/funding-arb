#!/usr/bin/env python3
"""Public spot/USDT-linear market metadata and order-book sources for carry scans.

Every endpoint used here is public market data. This module intentionally has
no API-key loading, signed requests, account endpoints, or order methods.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any
from urllib.parse import urlencode

from market.carry_scanner_config import validate_order_book_limit
from venues.http_util import http_get_json

BINANCE_SPOT = "https://api.binance.com"
BINANCE_USDM = "https://fapi.binance.com"
BYBIT = "https://api.bybit.com"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _instrument_snapshot_metadata(
    markets: dict[str, dict[str, float]], *, started_at_ms: int, observed_at_ms: int
) -> dict[str, Any]:
    content = json.dumps(
        markets, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return {
        "request_started_at_ms": started_at_ms,
        "observed_at_ms": observed_at_ms,
        "snapshot_id": hashlib.sha256(content).hexdigest(),
    }


def _filters(row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("filterType", "")): item
        for item in row.get("filters", [])
        if isinstance(item, dict)
    }


def _binance_market_limits(row: dict[str, Any]) -> dict[str, float]:
    filters = _filters(row)
    lot = filters.get("LOT_SIZE", {})
    market_lot = filters.get("MARKET_LOT_SIZE", {})
    # MARKET_LOT_SIZE is the applicable market-order filter when populated.
    selected = market_lot if _num(market_lot.get("stepSize")) > 0 else lot
    min_qty = _num(selected.get("minQty"), _num(lot.get("minQty")))
    max_qty = _num(selected.get("maxQty"), _num(lot.get("maxQty")))
    qty_step = _num(selected.get("stepSize"), _num(lot.get("stepSize")))
    notional = filters.get("NOTIONAL", filters.get("MIN_NOTIONAL", {}))
    min_notional = _num(
        notional.get("minNotional"), _num(notional.get("notional"))
    )
    return {
        "min_qty": min_qty,
        "max_qty": max_qty,
        "qty_step": qty_step,
        "min_notional_usd": min_notional,
    }


def _bybit_market_limits(row: dict[str, Any], *, perpetual: bool) -> dict[str, float]:
    lot = row.get("lotSizeFilter") or {}
    max_qty_field = "maxMktOrderQty" if perpetual else "maxMarketOrderQty"
    max_qty = _num(lot.get(max_qty_field), _num(lot.get("maxOrderQty")))
    return {
        "min_qty": _num(lot.get("minOrderQty")),
        "max_qty": max_qty,
        "qty_step": _num(lot.get("qtyStep")),
        "min_notional_usd": _num(
            lot.get("minNotionalValue"), _num(lot.get("minOrderAmt"))
        ),
    }


class PublicCarryMarketDataProvider:
    """Fetch eligible instrument metadata and fresh public L2 snapshots."""

    ORDER_BOOK_LIMIT = 1000
    BYBIT_ORDER_BOOK_LIMIT = 1000
    MAX_INSTRUMENT_PAGES = 20

    def _get_binance_market_universe(self) -> dict[str, Any]:
        spot_started_at_ms = int(time.time() * 1000)
        spot_payload = http_get_json(f"{BINANCE_SPOT}/api/v3/exchangeInfo", timeout=20)
        spot_observed_at_ms = int(time.time() * 1000)
        perpetual_started_at_ms = int(time.time() * 1000)
        perp_payload = http_get_json(f"{BINANCE_USDM}/fapi/v1/exchangeInfo", timeout=20)
        perpetual_observed_at_ms = int(time.time() * 1000)
        spot_rows = spot_payload.get("symbols") if isinstance(spot_payload, dict) else None
        perp_rows = perp_payload.get("symbols") if isinstance(perp_payload, dict) else None
        if not isinstance(spot_rows, list) or not isinstance(perp_rows, list):
            raise RuntimeError("Binance exchange metadata response is malformed")

        spot: dict[str, dict[str, float]] = {}
        for row in spot_rows:
            if not isinstance(row, dict):
                raise RuntimeError("Binance spot instrument metadata row is malformed")
            symbol = str(row.get("symbol", "")).upper()
            if (
                symbol
                and row.get("status") == "TRADING"
                and str(row.get("quoteAsset", "")).upper() == "USDT"
                and row.get("isSpotTradingAllowed", True) is not False
            ):
                spot[symbol] = _binance_market_limits(row)

        perpetual: dict[str, dict[str, float]] = {}
        for row in perp_rows:
            if not isinstance(row, dict):
                raise RuntimeError("Binance perpetual instrument metadata row is malformed")
            symbol = str(row.get("symbol", "")).upper()
            if (
                symbol
                and row.get("status") == "TRADING"
                and row.get("contractType") == "PERPETUAL"
                and str(row.get("quoteAsset", "")).upper() == "USDT"
                and str(row.get("marginAsset", "USDT")).upper() == "USDT"
            ):
                perpetual[symbol] = _binance_market_limits(row)
        return {
            "spot": spot,
            "perpetual": perpetual,
            "snapshot_metadata": {
                "spot": _instrument_snapshot_metadata(
                    spot,
                    started_at_ms=spot_started_at_ms,
                    observed_at_ms=spot_observed_at_ms,
                ),
                "perpetual": _instrument_snapshot_metadata(
                    perpetual,
                    started_at_ms=perpetual_started_at_ms,
                    observed_at_ms=perpetual_observed_at_ms,
                ),
            },
        }

    def _fetch_bybit_instruments(self, category: str) -> list[dict[str, Any]]:
        if category not in {"spot", "linear"}:
            raise ValueError(f"Unsupported Bybit instrument category: {category}")
        rows: list[dict[str, Any]] = []
        cursor = ""
        seen_cursors: set[str] = set()
        for _ in range(self.MAX_INSTRUMENT_PAGES):
            params: dict[str, str | int] = {"category": category}
            # Bybit spot instrument info has no pagination; linear has >500 rows.
            if category == "linear":
                params["limit"] = 1000
                if cursor:
                    params["cursor"] = cursor
            payload = http_get_json(
                f"{BYBIT}/v5/market/instruments-info?{urlencode(params)}", timeout=20
            )
            if not isinstance(payload, dict) or int(payload.get("retCode", 0) or 0) != 0:
                raise RuntimeError(f"Bybit {category} instrument metadata request failed")
            result = payload.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("list"), list):
                raise RuntimeError(f"Bybit {category} instrument list is malformed")
            page_rows = result["list"]
            rows.extend(row for row in page_rows if isinstance(row, dict))
            next_cursor = str(result.get("nextPageCursor") or "")
            if not next_cursor:
                return rows
            if category == "spot" or next_cursor in seen_cursors:
                raise RuntimeError(f"Bybit {category} instrument cursor did not advance")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise RuntimeError(f"Bybit {category} instrument pagination exceeded safety limit")

    def _get_bybit_market_universe(self) -> dict[str, Any]:
        spot_started_at_ms = int(time.time() * 1000)
        spot_rows = self._fetch_bybit_instruments("spot")
        spot_observed_at_ms = int(time.time() * 1000)
        perpetual_started_at_ms = int(time.time() * 1000)
        linear_rows = self._fetch_bybit_instruments("linear")
        perpetual_observed_at_ms = int(time.time() * 1000)
        spot: dict[str, dict[str, float]] = {}
        for row in spot_rows:
            symbol = str(row.get("symbol", "")).upper()
            if (
                symbol
                and row.get("status") == "Trading"
                and str(row.get("quoteCoin", "")).upper() == "USDT"
            ):
                spot[symbol] = _bybit_market_limits(row, perpetual=False)

        perpetual: dict[str, dict[str, float]] = {}
        for row in linear_rows:
            symbol = str(row.get("symbol", "")).upper()
            if (
                symbol
                and row.get("status") == "Trading"
                and row.get("contractType") == "LinearPerpetual"
                and str(row.get("quoteCoin", "")).upper() == "USDT"
                and str(row.get("settleCoin", "")).upper() == "USDT"
            ):
                perpetual[symbol] = _bybit_market_limits(row, perpetual=True)
        return {
            "spot": spot,
            "perpetual": perpetual,
            "snapshot_metadata": {
                "spot": _instrument_snapshot_metadata(
                    spot,
                    started_at_ms=spot_started_at_ms,
                    observed_at_ms=spot_observed_at_ms,
                ),
                "perpetual": _instrument_snapshot_metadata(
                    perpetual,
                    started_at_ms=perpetual_started_at_ms,
                    observed_at_ms=perpetual_observed_at_ms,
                ),
            },
        }

    def fetch_market_universe(self, venue: str) -> dict[str, Any]:
        """Return spot/perpetual limits and timestamped instrument snapshots."""
        v = str(venue).lower()
        if v == "binance":
            return self._get_binance_market_universe()
        if v == "bybit":
            return self._get_bybit_market_universe()
        raise ValueError(f"Unsupported carry venue: {venue!r}")

    @staticmethod
    def _parse_levels(rows: Any) -> list[list[float]]:
        levels: list[list[float]] = []
        for row in rows or []:
            try:
                price, quantity = float(row[0]), float(row[1])
            except (TypeError, ValueError, IndexError):
                continue
            if math.isfinite(price) and math.isfinite(quantity) and price > 0 and quantity > 0:
                levels.append([price, quantity])
        return levels

    def fetch_order_book(
        self,
        venue: str,
        market_type: str,
        symbol: str,
        *,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Fetch both sides of one spot or USDT-linear book with observation times."""
        v = str(venue).lower()
        kind = str(market_type).lower()
        sym = str(symbol).upper()
        if kind not in {"spot", "perpetual", "linear"}:
            raise ValueError(f"Unsupported market type: {market_type!r}")
        if v == "binance":
            count = validate_order_book_limit(v, limit, self.ORDER_BOOK_LIMIT)
            base = BINANCE_SPOT if kind == "spot" else BINANCE_USDM
            path = "/api/v3/depth" if kind == "spot" else "/fapi/v1/depth"
            params = urlencode({"symbol": sym, "limit": count})
            started_at_ms = int(time.time() * 1000)
            payload = http_get_json(f"{base}{path}?{params}", timeout=15)
            observed_at_ms = int(time.time() * 1000)
            if not isinstance(payload, dict):
                raise RuntimeError(f"Malformed Binance order book for {sym}")
            return {
                "bids": self._parse_levels(payload.get("bids")),
                "asks": self._parse_levels(payload.get("asks")),
                "observed_at_ms": observed_at_ms,
                "exchange_ts_ms": int(payload.get("T", payload.get("E", 0)) or 0),
                "request_started_at_ms": started_at_ms,
                "snapshot_id": payload.get("lastUpdateId"),
            }
        if v == "bybit":
            category = "spot" if kind == "spot" else "linear"
            count = validate_order_book_limit(v, limit, self.BYBIT_ORDER_BOOK_LIMIT)
            params = urlencode({"category": category, "symbol": sym, "limit": count})
            started_at_ms = int(time.time() * 1000)
            payload = http_get_json(f"{BYBIT}/v5/market/orderbook?{params}", timeout=15)
            observed_at_ms = int(time.time() * 1000)
            if not isinstance(payload, dict) or int(payload.get("retCode", 0) or 0) != 0:
                raise RuntimeError(f"Bybit order book request failed for {sym}")
            result = payload.get("result") or {}
            if not isinstance(result, dict):
                raise RuntimeError(f"Malformed Bybit order book for {sym}")
            return {
                "bids": self._parse_levels(result.get("b")),
                "asks": self._parse_levels(result.get("a")),
                "observed_at_ms": observed_at_ms,
                "exchange_ts_ms": int(result.get("cts", result.get("ts", payload.get("time", 0))) or 0),
                "request_started_at_ms": started_at_ms,
                "snapshot_id": result.get("u"),
            }
        raise ValueError(f"Unsupported carry venue: {venue!r}")
