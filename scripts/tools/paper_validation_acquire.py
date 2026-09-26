#!/usr/bin/env python3
"""Bounded, allowlisted public Binance/Bybit paper-validation acquisition.

This file is intentionally standalone and standard-library-only. It is the only
networking path used by the validation workflow. It performs HTTPS GETs only to
explicit public market-data endpoints, rejects redirects outside the same
allowlist, disables environment proxies, never reads credentials, and writes
raw responses plus normalized snapshot inputs for an offline replay.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


# Exact public endpoint paths. Private/account/order paths are deliberately absent.
_ALLOWED_QUERY_KEYS: dict[tuple[str, str], frozenset[str]] = {
    ("api.binance.com", "/api/v3/exchangeInfo"): frozenset({"symbol", "symbols"}),
    ("api.binance.com", "/api/v3/depth"): frozenset({"symbol", "limit"}),
    ("fapi.binance.com", "/fapi/v1/exchangeInfo"): frozenset({"symbol", "symbols"}),
    ("fapi.binance.com", "/fapi/v1/premiumIndex"): frozenset({"symbol"}),
    ("fapi.binance.com", "/fapi/v1/fundingInfo"): frozenset(),
    ("fapi.binance.com", "/fapi/v1/fundingRate"): frozenset(
        {"symbol", "startTime", "endTime", "limit"}
    ),
    ("fapi.binance.com", "/fapi/v1/depth"): frozenset({"symbol", "limit"}),
    ("api.bybit.com", "/v5/market/instruments-info"): frozenset(
        {"category", "limit", "cursor", "symbol"}
    ),
    ("api.bybit.com", "/v5/market/tickers"): frozenset({"category", "symbol"}),
    ("api.bybit.com", "/v5/market/funding/history"): frozenset(
        {"category", "symbol", "limit", "endTime"}
    ),
    ("api.bybit.com", "/v5/market/orderbook"): frozenset(
        {"category", "symbol", "limit"}
    ),
}

ALLOWED_ENDPOINTS = tuple(sorted(_ALLOWED_QUERY_KEYS))
# Binance's all-market spot exchangeInfo is currently larger than 8 MiB. Keep
# the bound finite while allowing the complete public discovery response.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_TIMEOUT_SEC = 30.0
DEFAULT_EXCLUDED_SYMBOLS = frozenset({"BTCUSDT", "ETHUSDT", "SOLUSDT"})
DEFAULT_DISCOVERY_POOL_SIZE = 12
MAX_DISCOVERY_POOL_SIZE = 25


class AllowlistViolation(ValueError):
    """Raised before any request that is not an approved public endpoint."""


class PublicFetchError(RuntimeError):
    """A bounded public request failed; carries safe HTTP evidence."""

    def __init__(self, message: str, evidence: dict[str, Any]):
        super().__init__(message)
        self.evidence = evidence


def validate_public_url(url: str) -> urllib.parse.SplitResult:
    """Validate scheme, host, exact path, and query keys before opening a URL."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise AllowlistViolation("only https public endpoints are allowed")
    if parsed.username or parsed.password or parsed.fragment:
        raise AllowlistViolation("userinfo and fragments are not allowed")
    if parsed.port not in (None, 443):
        raise AllowlistViolation("non-default HTTPS ports are not allowed")
    host = (parsed.hostname or "").lower()
    endpoint = (host, parsed.path)
    allowed_keys = _ALLOWED_QUERY_KEYS.get(endpoint)
    if allowed_keys is None:
        raise AllowlistViolation(f"endpoint is not on the public allowlist: {host}{parsed.path}")
    keys = [key for key, _value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)]
    unexpected = sorted(set(keys) - set(allowed_keys))
    if unexpected:
        raise AllowlistViolation(
            f"query keys are not allowlisted for {host}{parsed.path}: {','.join(unexpected)}"
        )
    return parsed


class _AllowlistedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Do not honor HTTP(S)_PROXY/ALL_PROXY from the host. The caller controls the
# container's network boundary; this code controls the destination allowlist.
_PUBLIC_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _AllowlistedRedirectHandler()
)


def _bounded_read(response: Any, max_bytes: int) -> bytes:
    raw_length = response.headers.get("Content-Length")
    if raw_length:
        try:
            if int(raw_length) > max_bytes:
                raise PublicFetchError(
                    "public response exceeds bounded size",
                    {"kind": "response_too_large", "content_length": int(raw_length)},
                )
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(64 * 1024, max_bytes - total + 1))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise PublicFetchError(
                "public response exceeds bounded size",
                {"kind": "response_too_large", "bytes_read": total},
            )
    return b"".join(chunks)


def fetch_public_json(
    url: str, *, timeout: float = 15.0, max_bytes: int = MAX_RESPONSE_BYTES
) -> tuple[Any, bytes, dict[str, Any]]:
    """Fetch one allowlisted JSON response and return parsed/raw/evidence."""
    validate_public_url(url)
    if not (0 < float(timeout) <= MAX_TIMEOUT_SEC):
        raise ValueError(f"timeout must be in (0, {MAX_TIMEOUT_SEC:g}]")
    if (
        not isinstance(max_bytes, int)
        or isinstance(max_bytes, bool)
        or not 0 < max_bytes <= MAX_RESPONSE_BYTES
    ):
        raise ValueError(f"max_bytes must be an integer in (0, {MAX_RESPONSE_BYTES}]")
    started_at_ms = int(time.time() * 1000)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "funding-arb-paper-validation/1.0",
        },
        method="GET",
    )
    try:
        with _PUBLIC_OPENER.open(request, timeout=float(timeout)) as response:
            final_url = response.geturl()
            validate_public_url(final_url)
            raw = _bounded_read(response, max_bytes)
            observed_at_ms = int(time.time() * 1000)
            status = int(response.getcode() or 0)
            evidence = {
                "url": url,
                "final_url": final_url,
                "status": status,
                "request_started_at_ms": started_at_ms,
                "observed_at_ms": observed_at_ms,
                "response_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
    except PublicFetchError:
        raise
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read(MAX_RESPONSE_BYTES)
        except Exception:
            pass
        evidence = {
            "url": url,
            "status": int(exc.code),
            "request_started_at_ms": started_at_ms,
            "observed_at_ms": int(time.time() * 1000),
            "response_bytes": len(body),
            "body_sha256": hashlib.sha256(body).hexdigest() if body else None,
            "reason": str(exc.reason),
        }
        raise PublicFetchError(f"HTTP {exc.code} from allowlisted public endpoint", evidence) from None
    except (urllib.error.URLError, TimeoutError, OSError, AllowlistViolation) as exc:
        evidence = {
            "url": url,
            "request_started_at_ms": started_at_ms,
            "observed_at_ms": int(time.time() * 1000),
            "error_type": type(exc).__name__,
            "reason": str(exc),
        }
        raise PublicFetchError("allowlisted public request failed", evidence) from None

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicFetchError(
            "allowlisted public response was not valid UTF-8 JSON",
            {**evidence, "error_type": type(exc).__name__, "reason": str(exc)},
        ) from None
    return payload, raw, evidence


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _sha_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def select_smallcoin_symbols(
    *,
    spot_markets: dict[str, Any],
    perpetual_markets: dict[str, Any],
    funding_rows: list[dict[str, Any]],
    excluded_symbols: set[str] | frozenset[str],
    pool_size: int,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Rank non-major positive-funding symbols in one venue's spot/perp intersection.

    This is discovery only: it does not relax the scanner's history, freshness,
    depth, or economics gates. The replay scanner remains the authority for
    final candidate eligibility.
    """
    if not 1 <= int(pool_size) <= MAX_DISCOVERY_POOL_SIZE:
        raise ValueError(f"pool_size must be in [1, {MAX_DISCOVERY_POOL_SIZE}]")
    intersection = set(spot_markets) & set(perpetual_markets)
    excluded = {str(symbol).upper() for symbol in excluded_symbols}
    best_by_symbol: dict[str, dict[str, Any]] = {}
    for row in funding_rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol", "")).upper()
        if not symbol or symbol in excluded or symbol not in intersection:
            continue
        try:
            rate_pct = float(row.get("rate_pct"))
        except (TypeError, ValueError):
            continue
        if not (rate_pct > 0 and math.isfinite(rate_pct)):
            continue
        candidate = {
            "symbol": symbol,
            "rate_pct": rate_pct,
            "funding_observed_at_ms": int(row.get("observed_at_ms", 0) or 0),
            "spot_listed": True,
            "perpetual_listed": True,
        }
        previous = best_by_symbol.get(symbol)
        if previous is None or rate_pct > float(previous["rate_pct"]):
            best_by_symbol[symbol] = candidate
    ranking = sorted(
        best_by_symbol.values(),
        key=lambda row: (-float(row["rate_pct"]), str(row["symbol"])),
    )
    return [str(row["symbol"]) for row in ranking[: int(pool_size)]], ranking


def _filters(row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("filterType", "")): item
        for item in row.get("filters", [])
        if isinstance(item, dict)
    }


def _binance_limits(row: dict[str, Any]) -> dict[str, float]:
    filters = _filters(row)
    lot = filters.get("LOT_SIZE", {})
    market_lot = filters.get("MARKET_LOT_SIZE", {})
    selected = market_lot if _num(market_lot.get("stepSize")) > 0 else lot
    notional = filters.get("NOTIONAL", filters.get("MIN_NOTIONAL", {}))
    return {
        "min_qty": _num(selected.get("minQty"), _num(lot.get("minQty"))),
        "max_qty": _num(selected.get("maxQty"), _num(lot.get("maxQty"))),
        "qty_step": _num(selected.get("stepSize"), _num(lot.get("stepSize"))),
        "min_notional_usd": _num(
            notional.get("minNotional"), _num(notional.get("notional"))
        ),
    }


def _bybit_limits(row: dict[str, Any], *, perpetual: bool) -> dict[str, float]:
    lot = row.get("lotSizeFilter") or {}
    max_field = "maxMktOrderQty" if perpetual else "maxMarketOrderQty"
    # Bybit spot publishes basePrecision instead of qtyStep; both are actual
    # quantity granularity metadata and are retained in the normalized snapshot.
    step = lot.get("qtyStep", lot.get("basePrecision"))
    return {
        "min_qty": _num(lot.get("minOrderQty")),
        "max_qty": _num(lot.get(max_field), _num(lot.get("maxOrderQty"))),
        "qty_step": _num(step),
        "min_notional_usd": _num(
            lot.get("minNotionalValue"), _num(lot.get("minOrderAmt"))
        ),
    }


def _instrument_metadata(markets: dict[str, Any], started: int, observed: int) -> dict[str, Any]:
    return {
        "request_started_at_ms": started,
        "observed_at_ms": observed,
        "snapshot_id": _sha_json(markets),
    }


def _parse_levels(rows: Any) -> list[list[float]]:
    out: list[list[float]] = []
    for row in rows or []:
        try:
            price, quantity = float(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if price > 0 and quantity > 0:
            out.append([price, quantity])
    return out


class _Collector:
    def __init__(self, output_dir: Path, snapshot_index: int, *, timeout: float):
        self.output_dir = output_dir
        self.snapshot_index = snapshot_index
        self.timeout = timeout
        self.errors: list[dict[str, Any]] = []
        self.evidence: list[dict[str, Any]] = []
        self.raw_files: list[str] = []

    def fetch(self, exchange: str, label: str, url: str) -> Any | None:
        try:
            payload, raw, meta = fetch_public_json(url, timeout=self.timeout)
        except PublicFetchError as exc:
            self.errors.append({"exchange": exchange, "label": label, **exc.evidence})
            return None
        safe_exchange = re.sub(r"[^A-Za-z0-9_.-]+", "_", exchange)
        # URL-quote labels instead of collapsing non-ASCII exchange symbols to
        # the same underscore name; raw response provenance must not collide.
        safe_label = urllib.parse.quote(label, safe="-_.")
        rel = Path("raw") / f"snapshot-{self.snapshot_index:03d}" / safe_exchange / f"{safe_label}.json"
        path = self.output_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        self.raw_files.append(str(rel))
        self.evidence.append({"exchange": exchange, "label": label, **meta, "raw_file": str(rel)})
        return payload


def _binance_instruments(
    collector: _Collector, symbols: list[str] | None = None
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    symbol_query = ""
    if symbols:
        symbol_query = urllib.parse.urlencode(
            {"symbols": json.dumps(symbols, separators=(",", ":"))}
        )
    spot_started = int(time.time() * 1000)
    spot_payload = collector.fetch(
        "binance",
        "spot-exchange-info",
        "https://api.binance.com/api/v3/exchangeInfo"
        + (f"?{symbol_query}" if symbol_query else ""),
    )
    spot_observed = int(time.time() * 1000)
    perp_started = int(time.time() * 1000)
    perp_payload = collector.fetch(
        "binance",
        "perp-exchange-info",
        "https://fapi.binance.com/fapi/v1/exchangeInfo"
        + (f"?{symbol_query}" if symbol_query else ""),
    )
    perp_observed = int(time.time() * 1000)
    spot: dict[str, Any] = {}
    perp: dict[str, Any] = {}
    for row in (spot_payload or {}).get("symbols", []) if isinstance(spot_payload, dict) else []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol", "")).upper()
        if (
            symbol
            and row.get("status") == "TRADING"
            and str(row.get("quoteAsset", "")).upper() == "USDT"
            and row.get("isSpotTradingAllowed", True) is not False
        ):
            spot[symbol] = _binance_limits(row)
    for row in (perp_payload or {}).get("symbols", []) if isinstance(perp_payload, dict) else []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol", "")).upper()
        if (
            symbol
            and row.get("status") == "TRADING"
            and row.get("contractType") == "PERPETUAL"
            and str(row.get("quoteAsset", "")).upper() == "USDT"
            and str(row.get("marginAsset", "USDT")).upper() == "USDT"
        ):
            perp[symbol] = _binance_limits(row)
    metadata = {
        "spot": _instrument_metadata(spot, spot_started, spot_observed),
        "perpetual": _instrument_metadata(perp, perp_started, perp_observed),
    }
    return spot, perp, metadata


def _bybit_instruments(collector: _Collector, category: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor = ""
    for page in range(20):
        params: dict[str, str | int] = {"category": category, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        query = urllib.parse.urlencode(params)
        payload = collector.fetch(
            "bybit",
            f"{category}-instruments-page-{page + 1:02d}",
            f"https://api.bybit.com/v5/market/instruments-info?{query}",
        )
        if not isinstance(payload, dict) or int(payload.get("retCode", 0) or 0) != 0:
            break
        result = payload.get("result")
        page_rows = result.get("list") if isinstance(result, dict) else None
        if not isinstance(page_rows, list):
            break
        rows.extend(row for row in page_rows if isinstance(row, dict))
        next_cursor = str((result or {}).get("nextPageCursor") or "") if isinstance(result, dict) else ""
        if not next_cursor:
            break
        if next_cursor == cursor:
            collector.errors.append(
                {"exchange": "bybit", "label": f"{category}-instruments", "reason": "cursor did not advance"}
            )
            break
        cursor = next_cursor
    return rows


def _bybit_market_universe(collector: _Collector) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    spot_started = int(time.time() * 1000)
    spot_rows = _bybit_instruments(collector, "spot")
    spot_observed = int(time.time() * 1000)
    perp_started = int(time.time() * 1000)
    perp_rows = _bybit_instruments(collector, "linear")
    perp_observed = int(time.time() * 1000)
    spot: dict[str, Any] = {}
    perp: dict[str, Any] = {}
    for row in spot_rows:
        symbol = str(row.get("symbol", "")).upper()
        if (
            symbol
            and row.get("status") == "Trading"
            and str(row.get("quoteCoin", "")).upper() == "USDT"
        ):
            spot[symbol] = _bybit_limits(row, perpetual=False)
    for row in perp_rows:
        symbol = str(row.get("symbol", "")).upper()
        if (
            symbol
            and row.get("status") == "Trading"
            and row.get("contractType") == "LinearPerpetual"
            and str(row.get("quoteCoin", "")).upper() == "USDT"
            and str(row.get("settleCoin", "")).upper() == "USDT"
        ):
            perp[symbol] = _bybit_limits(row, perpetual=True)
    metadata = {
        "spot": _instrument_metadata(spot, spot_started, spot_observed),
        "perpetual": _instrument_metadata(perp, perp_started, perp_observed),
    }
    return spot, perp, metadata


def _selected_rows(rows: Any, symbols: set[str], key: str = "symbol") -> list[dict[str, Any]]:
    return [
        row
        for row in rows or []
        if isinstance(row, dict) and str(row.get(key, "")).upper() in symbols
    ]


def _binance_data(
    collector: _Collector,
    symbols: list[str] | None,
    *,
    history_start_ms: int,
    excluded_symbols: set[str] | frozenset[str],
    candidate_pool_size: int,
) -> dict[str, Any]:
    discovery_mode = symbols is None
    spot, perp, metadata = _binance_instruments(collector, None if discovery_mode else symbols)
    funding_payload = collector.fetch(
        "binance", "funding-current", "https://fapi.binance.com/fapi/v1/premiumIndex"
    )
    funding_info = collector.fetch(
        "binance", "funding-info", "https://fapi.binance.com/fapi/v1/fundingInfo"
    )
    intervals = {symbol: 8.0 for symbol in perp}
    if isinstance(funding_info, list):
        for row in funding_info:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol", "")).upper()
            if symbol not in intervals:
                continue
            try:
                value = float(row.get("fundingIntervalHours"))
            except (TypeError, ValueError):
                continue
            if value > 0:
                intervals[symbol] = value

    all_funding_rows: list[dict[str, Any]] = []
    for row in funding_payload if isinstance(funding_payload, list) else []:
        if not isinstance(row, dict):
            continue
        observed = int(row.get("time", 0) or 0) or int(time.time() * 1000)
        all_funding_rows.append(
            {
                "symbol": str(row.get("symbol", "")).upper(),
                "rate_pct": _num(row.get("lastFundingRate")) * 100.0,
                "next_funding_ts": int(row.get("nextFundingTime", 0) or 0),
                "mark_price": _num(row.get("markPrice")),
                "index_price": _num(row.get("indexPrice")),
                "observed_at_ms": observed,
            }
        )
    if discovery_mode:
        selected_symbols, ranking = select_smallcoin_symbols(
            spot_markets=spot,
            perpetual_markets=perp,
            funding_rows=all_funding_rows,
            excluded_symbols=excluded_symbols,
            pool_size=candidate_pool_size,
        )
    else:
        selected_symbols = list(dict.fromkeys(str(symbol).upper() for symbol in (symbols or [])))
        ranking = []
    selected = set(selected_symbols)
    funding_rows = [row for row in all_funding_rows if row["symbol"] in selected]

    histories: dict[str, list[dict[str, Any]]] = {}
    for symbol in selected_symbols:
        payload = collector.fetch(
            "binance",
            f"funding-history-{symbol}",
            "https://fapi.binance.com/fapi/v1/fundingRate?"
            + urllib.parse.urlencode(
                {"symbol": symbol, "startTime": history_start_ms, "limit": 1000}
            ),
        )
        rows: list[dict[str, Any]] = []
        for row in payload if isinstance(payload, list) else []:
            try:
                rows.append(
                    {
                        "ts": int(row["fundingTime"]),
                        "rate_pct": float(row["fundingRate"]) * 100.0,
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
        histories[symbol] = rows

    books: dict[str, dict[str, Any]] = {"spot": {}, "perpetual": {}}
    for symbol in selected_symbols:
        for market_type, base, path in (
            ("spot", "https://api.binance.com", "/api/v3/depth"),
            ("perpetual", "https://fapi.binance.com", "/fapi/v1/depth"),
        ):
            payload = collector.fetch(
                "binance",
                f"book-{market_type}-{symbol}",
                f"{base}{path}?" + urllib.parse.urlencode({"symbol": symbol, "limit": 1000}),
            )
            evidence = next(
                (
                    row
                    for row in reversed(collector.evidence)
                    if row.get("exchange") == "binance"
                    and row.get("label") == f"book-{market_type}-{symbol}"
                ),
                {},
            )
            if isinstance(payload, dict):
                books[market_type][symbol] = {
                    "bids": _parse_levels(payload.get("bids")),
                    "asks": _parse_levels(payload.get("asks")),
                    "observed_at_ms": int(evidence.get("observed_at_ms", 0) or 0),
                    "exchange_ts_ms": int(payload.get("T", payload.get("E", 0)) or 0),
                    "request_started_at_ms": int(evidence.get("request_started_at_ms", 0) or 0),
                    "snapshot_id": payload.get("lastUpdateId"),
                }
    discovery = {
        "mode": "all_market_positive_funding_smallcoin" if discovery_mode else "explicit_symbols",
        "smallcoin_definition": "USDT symbols outside the configured BTC/ETH/SOL exclusion set",
        "excluded_symbols": sorted(str(symbol).upper() for symbol in excluded_symbols),
        "candidate_pool_size": candidate_pool_size if discovery_mode else len(selected_symbols),
        "selected_symbols": selected_symbols,
        "ranked_positive_funding_intersection": ranking[:candidate_pool_size],
        "spot_market_count": len(spot),
        "perpetual_market_count": len(perp),
        "intersection_market_count": len(set(spot) & set(perp)),
    }
    return {
        "market_universe": {"spot": spot, "perpetual": perp, "snapshot_metadata": metadata},
        "funding": {
            "current": funding_rows,
            "discovery_current": all_funding_rows,
            "intervals": intervals,
            "history": histories,
        },
        "books": books,
        "discovery": discovery,
    }


def _bybit_data(
    collector: _Collector,
    symbols: list[str] | None,
    *,
    history_start_ms: int,
    excluded_symbols: set[str] | frozenset[str],
    candidate_pool_size: int,
) -> dict[str, Any]:
    discovery_mode = symbols is None
    spot_rows = _bybit_instruments(collector, "spot")
    linear_rows = _bybit_instruments(collector, "linear")
    spot_started = next(
        (int(row.get("request_started_at_ms", 0)) for row in collector.evidence if row.get("label") == "spot-instruments-page-01"),
        int(time.time() * 1000),
    )
    perp_started = next(
        (int(row.get("request_started_at_ms", 0)) for row in collector.evidence if row.get("label") == "linear-instruments-page-01"),
        int(time.time() * 1000),
    )
    spot_observed = max(
        (int(row.get("observed_at_ms", 0)) for row in collector.evidence if row.get("label", "").startswith("spot-instruments-page-")),
        default=int(time.time() * 1000),
    )
    perp_observed = max(
        (int(row.get("observed_at_ms", 0)) for row in collector.evidence if row.get("label", "").startswith("linear-instruments-page-")),
        default=int(time.time() * 1000),
    )
    spot: dict[str, Any] = {}
    perp: dict[str, Any] = {}
    intervals: dict[str, float] = {}
    for row in spot_rows:
        symbol = str(row.get("symbol", "")).upper()
        if symbol and row.get("status") == "Trading" and str(row.get("quoteCoin", "")).upper() == "USDT":
            spot[symbol] = _bybit_limits(row, perpetual=False)
    for row in linear_rows:
        symbol = str(row.get("symbol", "")).upper()
        if (
            symbol
            and row.get("status") == "Trading"
            and row.get("contractType") == "LinearPerpetual"
            and str(row.get("quoteCoin", "")).upper() == "USDT"
            and str(row.get("settleCoin", "")).upper() == "USDT"
        ):
            perp[symbol] = _bybit_limits(row, perpetual=True)
            try:
                interval_minutes = float(row.get("fundingInterval"))
            except (TypeError, ValueError):
                interval_minutes = 0.0
            if interval_minutes > 0:
                intervals[symbol] = interval_minutes / 60.0
    metadata = {
        "spot": _instrument_metadata(spot, spot_started, spot_observed),
        "perpetual": _instrument_metadata(perp, perp_started, perp_observed),
    }
    ticker = collector.fetch("bybit", "funding-current", "https://api.bybit.com/v5/market/tickers?category=linear")
    ticker_result = ticker.get("result", {}) if isinstance(ticker, dict) else {}
    ticker_rows = ticker_result.get("list", []) if isinstance(ticker_result, dict) else []
    payload_observed = int(ticker.get("time", 0) or 0) if isinstance(ticker, dict) else 0
    payload_observed = payload_observed or int(time.time() * 1000)
    all_funding_rows: list[dict[str, Any]] = []
    for row in ticker_rows if isinstance(ticker_rows, list) else []:
        if not isinstance(row, dict):
            continue
        all_funding_rows.append(
            {
                "symbol": str(row.get("symbol", "")).upper(),
                "rate_pct": _num(row.get("fundingRate")) * 100.0,
                "next_funding_ts": int(row.get("nextFundingTime", 0) or 0),
                "mark_price": _num(row.get("markPrice"), _num(row.get("lastPrice"))),
                "index_price": _num(row.get("indexPrice")),
                "observed_at_ms": payload_observed,
            }
        )
    if discovery_mode:
        selected_symbols, ranking = select_smallcoin_symbols(
            spot_markets=spot,
            perpetual_markets=perp,
            funding_rows=all_funding_rows,
            excluded_symbols=excluded_symbols,
            pool_size=candidate_pool_size,
        )
    else:
        selected_symbols = list(dict.fromkeys(str(symbol).upper() for symbol in (symbols or [])))
        ranking = []
    selected = set(selected_symbols)
    funding_rows = [row for row in all_funding_rows if row["symbol"] in selected]

    histories: dict[str, list[dict[str, Any]]] = {}
    for symbol in selected_symbols:
        out_by_ts: dict[int, dict[str, Any]] = {}
        end_time: int | None = None
        for page in range(10):
            params: dict[str, str | int] = {"category": "linear", "symbol": symbol, "limit": 200}
            if end_time is not None:
                params["endTime"] = end_time
            payload = collector.fetch(
                "bybit",
                f"funding-history-{symbol}-page-{page + 1:02d}",
                "https://api.bybit.com/v5/market/funding/history?" + urllib.parse.urlencode(params),
            )
            result = payload.get("result", {}) if isinstance(payload, dict) else {}
            rows = result.get("list", []) if isinstance(result, dict) else []
            if not isinstance(rows, list) or not rows:
                break
            timestamps: list[int] = []
            for row in rows:
                try:
                    ts = int(row.get("fundingRateTimestamp", 0) or 0)
                    rate = float(row.get("fundingRate"))
                except (AttributeError, TypeError, ValueError):
                    continue
                if ts <= 0:
                    continue
                timestamps.append(ts)
                if ts > history_start_ms and (end_time is None or ts <= end_time):
                    out_by_ts[ts] = {"ts": ts, "rate_pct": rate * 100.0}
            if not timestamps or min(timestamps) <= history_start_ms or len(rows) < 200:
                break
            next_end = min(timestamps) - 1
            if next_end <= 0 or next_end == end_time:
                break
            end_time = next_end
        histories[symbol] = [out_by_ts[ts] for ts in sorted(out_by_ts)]

    books: dict[str, dict[str, Any]] = {"spot": {}, "perpetual": {}}
    for symbol in selected_symbols:
        for market_type, category in (("spot", "spot"), ("perpetual", "linear")):
            payload = collector.fetch(
                "bybit",
                f"book-{market_type}-{symbol}",
                "https://api.bybit.com/v5/market/orderbook?"
                + urllib.parse.urlencode({"category": category, "symbol": symbol, "limit": 1000}),
            )
            evidence = next(
                (
                    row
                    for row in reversed(collector.evidence)
                    if row.get("exchange") == "bybit"
                    and row.get("label") == f"book-{market_type}-{symbol}"
                ),
                {},
            )
            result = payload.get("result", {}) if isinstance(payload, dict) else {}
            if isinstance(result, dict):
                books[market_type][symbol] = {
                    "bids": _parse_levels(result.get("b")),
                    "asks": _parse_levels(result.get("a")),
                    "observed_at_ms": int(evidence.get("observed_at_ms", 0) or 0),
                    "exchange_ts_ms": int(result.get("cts", result.get("ts", payload.get("time", 0))) or 0) if isinstance(payload, dict) else 0,
                    "request_started_at_ms": int(evidence.get("request_started_at_ms", 0) or 0),
                    "snapshot_id": result.get("u"),
                }
    discovery = {
        "mode": "all_market_positive_funding_smallcoin" if discovery_mode else "explicit_symbols",
        "smallcoin_definition": "USDT symbols outside the configured BTC/ETH/SOL exclusion set",
        "excluded_symbols": sorted(str(symbol).upper() for symbol in excluded_symbols),
        "candidate_pool_size": candidate_pool_size if discovery_mode else len(selected_symbols),
        "selected_symbols": selected_symbols,
        "ranked_positive_funding_intersection": ranking[:candidate_pool_size],
        "spot_market_count": len(spot),
        "perpetual_market_count": len(perp),
        "intersection_market_count": len(set(spot) & set(perp)),
    }
    return {
        "market_universe": {"spot": spot, "perpetual": perp, "snapshot_metadata": metadata},
        "funding": {
            "current": funding_rows,
            "discovery_current": all_funding_rows,
            "intervals": intervals,
            "history": histories,
        },
        "books": books,
        "discovery": discovery,
    }


def acquire_snapshot(
    output_dir: Path,
    snapshot_index: int,
    symbols: list[str] | None,
    *,
    timeout: float,
    history_days: float,
    excluded_symbols: set[str] | frozenset[str],
    candidate_pool_size: int,
) -> dict[str, Any]:
    started_at_ms = int(time.time() * 1000)
    history_start_ms = started_at_ms - int(history_days * 24 * 3_600_000)
    collector = _Collector(output_dir, snapshot_index, timeout=timeout)
    exchanges = {
        "binance": _binance_data(
            collector,
            symbols,
            history_start_ms=history_start_ms,
            excluded_symbols=excluded_symbols,
            candidate_pool_size=candidate_pool_size,
        ),
        "bybit": _bybit_data(
            collector,
            symbols,
            history_start_ms=history_start_ms,
            excluded_symbols=excluded_symbols,
            candidate_pool_size=candidate_pool_size,
        ),
    }
    captured_at_ms = int(time.time() * 1000)
    return {
        "schema_version": 2,
        "snapshot_index": snapshot_index,
        "snapshot_started_at_ms": started_at_ms,
        "captured_at_ms": captured_at_ms,
        "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(captured_at_ms / 1000)),
        "symbols_requested": symbols,
        "selection_mode": "all_market_positive_funding_smallcoin" if symbols is None else "explicit_symbols",
        "excluded_symbols": sorted(str(symbol).upper() for symbol in excluded_symbols),
        "candidate_pool_size": candidate_pool_size,
        "history_start_ms": history_start_ms,
        "allowlist": [
            {"host": host, "path": path, "query_keys": sorted(_ALLOWED_QUERY_KEYS[(host, path)])}
            for host, path in ALLOWED_ENDPOINTS
        ],
        "exchanges": exchanges,
        "raw_files": collector.raw_files,
        "request_evidence": collector.evidence,
        "errors": collector.errors,
        "complete": not collector.errors,
    }


def _parse_symbol_list(raw: str, *, name: str, maximum: int) -> list[str]:
    symbols = [item.strip().upper() for item in raw.split(",") if item.strip()]
    if len(symbols) > maximum or any(
        not re.fullmatch(r"[A-Z0-9]{5,20}", item) for item in symbols
    ):
        raise ValueError(f"{name} must contain at most {maximum} simple exchange symbols")
    return list(dict.fromkeys(symbols))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--snapshots", type=int, default=3)
    parser.add_argument("--sleep-sec", type=float, default=3.0)
    parser.add_argument(
        "--symbols",
        default="",
        help="optional explicit symbols; omit to discover all-market positive-funding smallcoins",
    )
    parser.add_argument(
        "--exclude-symbols",
        default=",".join(sorted(DEFAULT_EXCLUDED_SYMBOLS)),
        help="comma-separated major symbols excluded from discovery",
    )
    parser.add_argument("--candidate-pool-size", type=int, default=DEFAULT_DISCOVERY_POOL_SIZE)
    parser.add_argument("--timeout-sec", type=float, default=15.0)
    parser.add_argument("--history-days", type=float, default=14.0)
    args = parser.parse_args(argv)
    if not 1 <= args.snapshots <= 3:
        parser.error("--snapshots must be in [1, 3]")
    if not 0 <= args.sleep_sec <= 30:
        parser.error("--sleep-sec must be in [0, 30]")
    if not 0 < args.timeout_sec <= MAX_TIMEOUT_SEC:
        parser.error(f"--timeout-sec must be in (0, {MAX_TIMEOUT_SEC:g}]")
    if not 0 < args.history_days <= 14:
        parser.error("--history-days must be in (0, 14]")
    if not 1 <= args.candidate_pool_size <= MAX_DISCOVERY_POOL_SIZE:
        parser.error(f"--candidate-pool-size must be in [1, {MAX_DISCOVERY_POOL_SIZE}]")
    try:
        explicit_symbols = _parse_symbol_list(args.symbols, name="--symbols", maximum=25)
        excluded_symbols = _parse_symbol_list(
            args.exclude_symbols,
            name="--exclude-symbols",
            maximum=25,
        )
    except ValueError as exc:
        parser.error(str(exc))
    args.symbols = explicit_symbols or None
    args.excluded_symbols = frozenset(excluded_symbols)
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    snapshots: list[str] = []
    all_errors: list[dict[str, Any]] = []
    for index in range(args.snapshots):
        snapshot = acquire_snapshot(
            args.output_dir,
            index,
            args.symbols,
            timeout=args.timeout_sec,
            history_days=args.history_days,
            excluded_symbols=args.excluded_symbols,
            candidate_pool_size=args.candidate_pool_size,
        )
        path = args.output_dir / f"snapshot-{index:03d}.json"
        path.write_text(json.dumps(snapshot, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        snapshots.append(path.name)
        all_errors.extend(snapshot["errors"])
        if index + 1 < args.snapshots and args.sleep_sec:
            time.sleep(args.sleep_sec)
    manifest = {
        "schema_version": 2,
        "created_at_ms": int(time.time() * 1000),
        "snapshots": snapshots,
        "symbols": args.symbols,
        "selection_mode": "all_market_positive_funding_smallcoin" if args.symbols is None else "explicit_symbols",
        "excluded_symbols": sorted(args.excluded_symbols),
        "candidate_pool_size": args.candidate_pool_size,
        "snapshots_requested": args.snapshots,
        "allowlist_endpoints": [f"https://{host}{path}" for host, path in ALLOWED_ENDPOINTS],
        "errors": all_errors,
        "complete": not all_errors and len(snapshots) == args.snapshots,
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"manifest": str(args.output_dir / "manifest.json"), **manifest}, sort_keys=True))
    return 0 if manifest["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
