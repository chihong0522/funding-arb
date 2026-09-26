#!/usr/bin/env python3
"""Binance spot venue adapter."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from core.config import resolve_timeframes
from core.credentials import ensure_env, redact_secret_values
from core.execution_policy import block_real_execution, require_dry_run
from venues.base import make_pair
from venues.http_util import parse_kline_ohlcv, rules_for_price

BASE = "https://api.binance.com"
_symbol_rules_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_futures_rules_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_exchange_info_loaded_at: float = 0.0
_exchange_info_symbols: dict[str, dict[str, Any]] = {}
_fapi_exchange_info_loaded_at: float = 0.0
_fapi_exchange_info_rules: dict[str, dict[str, Any]] = {}
_spot_ticker_loaded_at: float = 0.0
_spot_ticker_prices: dict[str, float] = {}
_futures_ticker_loaded_at: float = 0.0
_futures_ticker_prices: dict[str, float] = {}
_initialized_symbols: set[str] = set()
_env_loaded = False

KLINE_INTERVALS = {
    "1day": "1d",
    "4h": "4h",
    "1week": "1w",
}


def _ensure_env() -> None:
    global _env_loaded
    if _env_loaded:
        return
    ensure_env("BINANCE_")
    _env_loaded = True


def _get_key() -> str:
    _ensure_env()
    return (
        os.environ.get("BINANCE_API_KEY")
        or os.environ.get("BINANCE_TRADE_API_KEY")
        or ""
    )


def _get_secret() -> str:
    _ensure_env()
    return (
        os.environ.get("BINANCE_API_SECRET")
        or os.environ.get("BINANCE_SECRET_KEY")
        or os.environ.get("BINANCE_TRADE_SECRET_KEY")
        or ""
    )


def _sign(query: str) -> str:
    return hmac.new(_get_secret().encode(), query.encode(), hashlib.sha256).hexdigest()


def _api_call(
    method: str, path: str, params: Optional[dict] = None, signed: bool = False
) -> Any:
    if method.upper() != "GET":
        block_real_execution("Binance REST write")
    base_params = dict(params or {})
    if signed:
        # Credential validation: fail immediately on missing credentials; never send private requests with empty credentials
        key, secret = _get_key(), _get_secret()
        if not (key and secret):
            raise RuntimeError(
                "Binance API credentials missing: please set BINANCE_API_KEY / BINANCE_API_SECRET, "
                "or configure them in an approved secure credential store."
            )
    # GET can be safely retried; POST (orders) are not retried to avoid duplicate orders
    retries = 3 if method == "GET" else 1
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        if signed:
            params2 = dict(base_params)
            params2["timestamp"] = int(time.time() * 1000)  # Refresh timestamp on retry
            query = urllib.parse.urlencode(params2)
            sig = _sign(query)
            base_url = "https://fapi.binance.com" if path.startswith("/fapi/") else BASE
            url = f"{base_url}{path}?{query}&signature={sig}"
            headers = {"X-MBX-APIKEY": _get_key()}
        else:
            query = urllib.parse.urlencode(base_params) if base_params else ""
            base_url = "https://fapi.binance.com" if path.startswith("/fapi/") else BASE
            url = f"{base_url}{path}" + (f"?{query}" if query else "")
            headers = {}
        req = urllib.request.Request(url, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            last_err = RuntimeError(redact_secret_values(e))
            if method == "GET" and attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise last_err from None
    raise last_err if last_err else RuntimeError("binance _api_call failed")


class BinanceSpotVenue:
    venue_id = "binance"

    def get_ticker(self, pair: str) -> float:
        try:
            data = _api_call("GET", "/api/v3/ticker/price", {"symbol": pair})
            return float(data.get("price", 0))
        except Exception:
            return 0.0

    def get_futures_ticker(self, pair: str) -> float:
        """USDT-M perpetual last price. pair format e.g. BTCUSDT."""
        try:
            data = _api_call("GET", "/fapi/v1/ticker/price", {"symbol": pair})
            return float(data.get("price", 0))
        except Exception:
            return 0.0

    def get_all_spot_tickers(self, cache_sec: int = 5) -> dict[str, float]:
        """Bulk spot last prices {PAIR: price}. Cached briefly for screener loops."""
        global _spot_ticker_loaded_at, _spot_ticker_prices
        now = time.time()
        if _spot_ticker_prices and (now - _spot_ticker_loaded_at) < cache_sec:
            return dict(_spot_ticker_prices)
        data = _api_call("GET", "/api/v3/ticker/price", {})
        if isinstance(data, dict):
            rows = [data]
        else:
            rows = list(data or [])
        _spot_ticker_prices = {
            str(r.get("symbol", "")).upper(): float(r.get("price", 0) or 0)
            for r in rows
            if r.get("symbol")
        }
        _spot_ticker_loaded_at = now
        return dict(_spot_ticker_prices)

    def get_all_futures_tickers(self, cache_sec: int = 5) -> dict[str, float]:
        """Bulk USDT-M perpetual last prices {PAIR: price}. Cached briefly for screener loops."""
        global _futures_ticker_loaded_at, _futures_ticker_prices
        now = time.time()
        if _futures_ticker_prices and (now - _futures_ticker_loaded_at) < cache_sec:
            return dict(_futures_ticker_prices)
        try:
            data = _api_call("GET", "https://fapi.binance.com/fapi/v1/ticker/price")
            if isinstance(data, dict):
                rows = [data]
            else:
                rows = list(data or [])
            _futures_ticker_prices = {
                str(r.get("symbol", "")).upper(): float(r.get("price", 0) or 0)
                for r in rows
                if r.get("symbol")
            }
            _futures_ticker_loaded_at = now
        except Exception:
            pass
        return dict(_futures_ticker_prices)

    def _ensure_spot_exchange_info(self, cache_sec: int = 3600) -> None:
        global _exchange_info_loaded_at, _exchange_info_symbols
        now = time.time()
        if _exchange_info_symbols and (now - _exchange_info_loaded_at) < cache_sec:
            return
        info = _api_call("GET", "/api/v3/exchangeInfo", {})
        _exchange_info_symbols = {
            str(s.get("symbol", "")).upper(): s
            for s in info.get("symbols", [])
            if s.get("symbol")
        }
        _exchange_info_loaded_at = now

    def _ensure_fapi_exchange_info(self, cache_sec: int = 3600) -> None:
        global _fapi_exchange_info_loaded_at, _fapi_exchange_info_rules
        now = time.time()
        if (
            _fapi_exchange_info_rules
            and (now - _fapi_exchange_info_loaded_at) < cache_sec
        ):
            return
        info = _api_call("GET", "/fapi/v1/exchangeInfo")
        rules: dict[str, dict[str, Any]] = {}
        for s in info.get("symbols", []):
            s_pair = str(s.get("symbol", "")).upper()
            if not s_pair:
                continue
            s_min_base = 0.0
            s_min_usdt = 0.0
            s_qty_prec = int(s.get("quantityPrecision", 3))
            s_quote_prec = int(s.get("pricePrecision", 2))
            for f in s.get("filters", []):
                ftype = f.get("filterType")
                if ftype == "LOT_SIZE":
                    s_min_base = float(f.get("minQty", 0))
                elif ftype in ("MIN_NOTIONAL", "NOTIONAL"):
                    s_min_usdt = float(f.get("notional", f.get("minNotional", 0)) or 0)
            if s_min_base <= 0:
                s_min_base = 10 ** (-s_qty_prec)
            rules[s_pair] = {
                "symbol": s_pair,
                "min_trade_usdt": s_min_usdt,
                "min_trade_base": s_min_base,
                "quantity_precision": s_qty_prec,
                "quote_precision": s_quote_prec,
                "status": s.get("status", ""),
            }
        _fapi_exchange_info_rules = rules
        _fapi_exchange_info_loaded_at = now

    def get_klines(
        self, pair: str, granularity: str = "1day", limit: int = 200
    ) -> list:
        interval = KLINE_INTERVALS.get(granularity, granularity)
        try:
            return _api_call(
                "GET",
                "/api/v3/klines",
                {"symbol": pair, "interval": interval, "limit": limit},
            )
        except Exception:
            return []

    def fetch_symbol_rules(
        self, pair: str, cache_sec: int = 3600
    ) -> Optional[dict[str, Any]]:
        now = time.time()
        cached = _symbol_rules_cache.get(pair)
        if cached and (now - cached[0]) < cache_sec:
            return dict(cached[1])
        try:
            self._ensure_spot_exchange_info(cache_sec)
            sym = _exchange_info_symbols.get(pair.upper())
            if not sym:
                return dict(cached[1]) if cached else None
            info = {"symbols": [sym]}
        except Exception:
            return dict(cached[1]) if cached else None
        symbols = info.get("symbols") or []
        if not symbols:
            return dict(cached[1]) if cached else None
        sym = symbols[0]
        min_base = 0.0
        min_usdt = 0.0
        qty_prec = 8
        quote_prec = 8
        for f in sym.get("filters", []):
            ftype = f.get("filterType")
            if ftype == "LOT_SIZE":
                min_base = float(f.get("minQty", 0))
                step = f.get("stepSize", "0.00000001")
                qty_prec = len(step.rstrip("0").split(".")[-1]) if "." in step else 0
            elif ftype in ("NOTIONAL", "MIN_NOTIONAL"):
                min_usdt = float(f.get("minNotional", f.get("notional", 0)) or 0)
            elif ftype == "PRICE_FILTER":
                tick = f.get("tickSize", "0.01")
                quote_prec = len(tick.rstrip("0").split(".")[-1]) if "." in tick else 2
        if min_base <= 0:
            min_base = 10 ** (-qty_prec)
        rules = {
            "symbol": pair,
            "min_trade_usdt": min_usdt,
            "min_trade_base": min_base,
            "quantity_precision": qty_prec,
            "quote_precision": quote_prec,
            "status": sym.get("status", ""),
        }
        _symbol_rules_cache[pair] = (now, rules)
        return dict(rules)

    def fetch_futures_symbol_rules(
        self, pair: str, cache_sec: int = 3600
    ) -> dict[str, Any] | None:
        now = time.time()
        cached = _futures_rules_cache.get(pair)
        if cached and (now - cached[0]) < cache_sec:
            return dict(cached[1])
        try:
            self._ensure_fapi_exchange_info(cache_sec)
        except Exception:
            return dict(cached[1]) if cached else None
        rules = _fapi_exchange_info_rules.get(pair.upper())
        if rules:
            _futures_rules_cache[pair] = (now, dict(rules))
            return dict(rules)
        return dict(cached[1]) if cached else None

    def transfer_asset(
        self, asset: str, amount: float, from_account: str, to_account: str
    ) -> bool:
        """Transfer between main spot and UM futures."""
        block_real_execution("internal transfer")
        # 1: spot -> UM futures, 2: UM futures -> spot
        transfer_type = (
            1
            if from_account == "spot" and to_account == "futures"
            else 2
            if from_account == "futures" and to_account == "spot"
            else None
        )
        if not transfer_type:
            return False

        try:
            res = _api_call(
                "POST",
                "/sapi/v1/futures/transfer",
                {
                    "type": transfer_type,
                    "asset": asset,
                    "amount": f"{amount:.8f}".rstrip("0").rstrip("."),
                },
                signed=True,
            )
            return "tranId" in res
        except Exception:
            return False

    def fetch_usdt_account_balances(self) -> dict[str, float]:
        """Fetch separate Spot and Futures USDT balances. Returns {'spot': val, 'futures': val}."""
        spot_usdt = 0.0
        try:
            data = _api_call("GET", "/api/v3/account", signed=True)
            for asset in data.get("balances", []):
                if str(asset.get("asset", "")).upper() == "USDT":
                    spot_usdt = float(asset.get("free", "0"))
                    break
        except Exception as e:
            print(f"fetch_usdt_account_balances spot error: {redact_secret_values(e)}", file=sys.stderr)
            raise e

        futures_usdt = 0.0
        try:
            f_data = _api_call("GET", "/fapi/v2/account", signed=True)
            for asset in f_data.get("assets", []):
                if str(asset.get("asset", "")).upper() == "USDT":
                    futures_usdt = float(asset.get("availableBalance", "0"))
                    break
        except Exception as e:
            print(f"fetch_usdt_account_balances futures error: {redact_secret_values(e)}", file=sys.stderr)
            raise e

        return {"spot": spot_usdt, "futures": futures_usdt}

    def fetch_asset_market(
        self, asset: str, quote: str = "USDT", cfg: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        pair = make_pair(asset, quote)
        price = self.get_ticker(pair)
        rules = self.fetch_symbol_rules(pair)
        if rules is None:
            return {
                "symbol": asset,
                "price": price,
                "rules_error": True,
                "venue": self.venue_id,
            }
        limits = rules_for_price(rules, price)

        cfg = cfg or {}
        tf = resolve_timeframes(cfg)

        klines_1d = [
            parse_kline_ohlcv(k)
            for k in self.get_klines(pair, tf["slow"]["interval"], tf["slow"]["limit"])
            if k
        ]
        klines_4h = [
            parse_kline_ohlcv(k)
            for k in self.get_klines(pair, tf["mid"]["interval"], tf["mid"]["limit"])
            if k
        ]
        klines_1w = [
            parse_kline_ohlcv(k)
            for k in self.get_klines(
                pair, tf["macro"]["interval"], tf["macro"]["limit"]
            )
            if k
        ]
        return {
            "symbol": asset,
            "pair": pair,
            "price": price,
            "rules_error": False,
            "venue": self.venue_id,
            "symbol_rules": rules,
            **limits,
            "klines_1d": klines_1d,
            "klines_4h": klines_4h,
            "klines_1w": klines_1w,
        }

    def fetch_balances(self, coins: list[str]) -> dict[str, float]:
        # Do not swallow exceptions: balance fetch failure must propagate to abort, never return all zeros
        # (otherwise strategy misjudges account as empty and only uses cash orders)
        balances: dict[str, float] = {c: 0.0 for c in coins}

        data = _api_call("GET", "/api/v3/account", signed=True)
        for asset in data.get("balances", []):
            coin = str(asset.get("asset", "")).upper()
            if coin in balances:
                balances[coin] = float(asset.get("free", "0"))

        # Fetch futures balance if USDT is requested
        if "USDT" in balances:
            try:
                f_data = _api_call("GET", "/fapi/v2/account", signed=True)
                for asset in f_data.get("assets", []):
                    if str(asset.get("asset", "")).upper() == "USDT":
                        balances["USDT"] += float(asset.get("marginBalance", "0"))
            except Exception as e:
                print(f"fetch_futures_balances error: {redact_secret_values(e)}", file=sys.stderr)

        return balances

    def initialize_futures_symbol(self, pair: str) -> None:
        """Require safe, preconfigured futures settings without mutating account state."""
        mode = _api_call("GET", "/fapi/v1/positionSide/dual", signed=True)
        if not isinstance(mode, dict) or mode.get("dualSidePosition") is not False:
            raise RuntimeError(
                f"Binance futures position mode for {pair} is unknown or hedge; "
                "one-way mode must be configured before execution"
            )

        config = _api_call(
            "GET", "/fapi/v1/symbolConfig", {"symbol": pair}, signed=True
        )
        if isinstance(config, list):
            rows = config
        elif isinstance(config, dict):
            rows = [config]
        else:
            rows = []
        row = next(
            (item for item in rows if isinstance(item, dict) and item.get("symbol") == pair),
            None,
        )
        if row is None:
            raise RuntimeError(f"Binance futures configuration unavailable for {pair}")
        if str(row.get("marginType", "")).upper() != "ISOLATED":
            raise RuntimeError(f"Binance {pair} must be preconfigured for ISOLATED margin")
        raw_leverage = row.get("leverage")
        if not isinstance(raw_leverage, (str, int)):
            raise RuntimeError(f"Binance {pair} leverage is unknown")
        leverage = int(raw_leverage)
        if leverage != 1:
            raise RuntimeError(f"Binance {pair} must be preconfigured at 1x leverage")
        if row.get("isAutoAddMargin") not in (False, "false", "0", 0):
            raise RuntimeError(f"Binance {pair} auto-add margin must be disabled")

        _initialized_symbols.add(pair)

    def fetch_live_state(self, assets: list[str]) -> dict[str, Any]:
        """Fetch unified global state: spot balances, margin debt, futures margin, futures positions."""
        # 1. Spot Balances
        spot_balances: dict[str, float] = {c: 0.0 for c in assets}
        try:
            data = _api_call("GET", "/api/v3/account", signed=True)
            for asset in data.get("balances", []):
                coin = str(asset.get("asset", "")).upper()
                if coin in spot_balances:
                    spot_balances[coin] = float(asset.get("free", "0")) + float(
                        asset.get("locked", "0")
                    )
        except Exception as e:
            print(f"fetch_live_state spot error: {redact_secret_values(e)}", file=sys.stderr)
            raise e  # Must propagate if spot fails

        # 2. Cross Margin Balances & Debt (for Reverse Arb)
        margin_balances: dict[str, float] = {c: 0.0 for c in assets}
        try:
            margin_data = _api_call("GET", "/sapi/v1/margin/account", signed=True)
            for asset in margin_data.get("userAssets", []):
                coin = str(asset.get("asset", "")).upper()
                if coin in margin_balances:
                    free = float(asset.get("free", "0")) + float(
                        asset.get("locked", "0")
                    )
                    borrowed = float(asset.get("borrowed", "0"))
                    interest = float(asset.get("interest", "0"))
                    # Net balance = free - borrowed - interest
                    margin_balances[coin] = free - borrowed - interest
        except Exception as e:
            # If cross-margin account doesn't exist, ignore
            pass

        # Combine Spot and Margin balances
        combined_balances = {c: spot_balances[c] + margin_balances[c] for c in assets}

        # 3. Futures Margin (USDT)
        futures_usdt = 0.0
        try:
            f_data = _api_call("GET", "/fapi/v2/account", signed=True)
            for asset in f_data.get("assets", []):
                if str(asset.get("asset", "")).upper() == "USDT":
                    # Use walletBalance instead of marginBalance to avoid double-counting unrealized PnL
                    futures_usdt = float(asset.get("walletBalance", "0"))
        except Exception as e:
            print(f"fetch_live_state futures account error: {redact_secret_values(e)}", file=sys.stderr)
            raise e

        if "USDT" in combined_balances:
            combined_balances["USDT"] += futures_usdt

        # 4. Futures Positions
        positions = {}
        try:
            pos_data = _api_call("GET", "/fapi/v2/positionRisk", signed=True)
            for pos in pos_data:
                amt = float(pos.get("positionAmt", "0"))
                if abs(amt) > 1e-9:
                    sym_raw = pos.get("symbol", "")
                    base_sym = sym_raw
                    if sym_raw.endswith("USDT"):
                        base_sym = sym_raw[:-4]

                    entry = float(pos.get("entryPrice", "0"))
                    unrealized = float(pos.get("unRealizedProfit", "0"))
                    liq_price = float(pos.get("liquidationPrice", "0"))
                    lev = float(pos.get("leverage", "1"))
                    positions[base_sym] = {
                        "amount": abs(amt),
                        "side": "long" if amt > 0 else "short",
                        "entry_price": entry,
                        "unrealized_pnl": unrealized,
                        "liq_price": liq_price,
                        "leverage": lev,
                    }
        except Exception as e:
            print(f"fetch_live_state positionRisk error: {redact_secret_values(e)}", file=sys.stderr)
            raise e

        return {"balances": combined_balances, "futures_positions": positions}

    def fetch_futures_positions(self, quote: str = "USDT") -> list[dict[str, Any]]:
        """USDT perpetual position list (single endpoint, raises on failure)."""
        pos_data = _api_call("GET", "/fapi/v2/positionRisk", signed=True)
        out: list[dict[str, Any]] = []
        for pos in pos_data if isinstance(pos_data, list) else []:
            amt = float(pos.get("positionAmt", "0") or 0)
            if abs(amt) <= 1e-12:
                continue
            out.append(
                {
                    "symbol": str(pos.get("symbol", "")).upper(),
                    "side": "long" if amt > 0 else "short",
                    "qty": abs(amt),
                    "entry_price": float(pos.get("entryPrice", 0) or 0),
                    "liq_price": float(pos.get("liquidationPrice", 0) or 0),
                    "leverage": float(pos.get("leverage", 1) or 1),
                    "unrealized_pnl": float(pos.get("unRealizedProfit", 0) or 0),
                }
            )
        return out

    def fetch_borrow_rates(self, coins: list[str]) -> dict[str, float]:
        """Fetch annualized borrow rates from Binance cross margin. Returns {coin: rate_decimal}."""
        rates: dict[str, float] = {c: 0.0 for c in coins}
        try:
            data = _api_call("GET", "/sapi/v1/margin/crossMarginData", signed=True)
            for item in data:
                coin = str(item.get("coin", "")).upper()
                if coin in rates:
                    rates[coin] = float(
                        item.get(
                            "yearlyInterest", float(item.get("dailyInterest", 0)) * 365
                        )
                    )
        except Exception:
            pass  # Fall back to static config defaults
        return rates

    # ── cross margin (Reverse C&C: borrow-sell / buy-repay) ──────────────────────

    def supports_reverse_arbitrage(self) -> bool:
        """Cross-margin capability probe; it never changes account state or borrows."""
        if not (_get_key() and _get_secret()):
            return True
        try:
            data = _api_call("GET", "/sapi/v1/margin/account", signed=True)
            if isinstance(data, dict) and "userAssets" in data:
                return bool(data.get("tradeEnabled", True))
        except Exception:
            return False
        return False

    def fetch_margin_debt(self, assets: list[str]) -> dict[str, float]:
        """Cross margin per-asset outstanding debt (borrowed + interest), in base coin units."""
        debt: dict[str, float] = {a.upper(): 0.0 for a in assets}
        data = _api_call("GET", "/sapi/v1/margin/account", signed=True)
        for item in data.get("userAssets", []):
            coin = str(item.get("asset", "")).upper()
            if coin in debt:
                debt[coin] = float(item.get("borrowed", "0")) + float(
                    item.get("interest", "0")
                )
        return debt

    def _margin_borrow_repay(self, asset: str, amount: float, op: str) -> bool:
        block_real_execution("margin borrow/repay")
        try:
            res = _api_call(
                "POST",
                "/sapi/v1/margin/borrow-repay",
                {
                    "asset": asset.upper(),
                    "isIsolated": "FALSE",
                    "amount": f"{amount:.8f}".rstrip("0").rstrip("."),
                    "type": op,
                },
                signed=True,
            )
            return "tranId" in res
        except Exception as e:
            print(f"margin {op} {asset} failed: {redact_secret_values(e)}", file=sys.stderr)
            return False

    def margin_borrow(self, asset: str, amount: float) -> bool:
        return self._margin_borrow_repay(asset, amount, "BORROW")

    def margin_repay(self, asset: str, amount: float) -> bool:
        return self._margin_borrow_repay(asset, amount, "REPAY")

    @staticmethod
    def _order_paths(category: str) -> tuple[str, str]:
        if category == "futures":
            return "/fapi/v1/order", "/fapi/v1/order"
        if category == "margin":
            return "/sapi/v1/margin/order", "/sapi/v1/margin/order"
        return "/api/v3/order", "/api/v3/order"

    @staticmethod
    def _make_client_order_id(value: str | None, prefix: str) -> str:
        del prefix  # client IDs must be supplied by the durable execution journal.
        if not value:
            raise ValueError("client_order_id is required for journaled live execution")
        client_id = value
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,36}", client_id):
            raise ValueError("client_order_id must be 1-36 ASCII letters, digits, '_' or '-'")
        return client_id

    def _settle_market_order(
        self,
        *,
        pair: str,
        category: str,
        client_order_id: str,
        order_id: str | None,
        requested_qty: float | None,
        ref_price: float,
        submit_ts: float,
        submit_error: Exception | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        query_path, cancel_path = self._order_paths(category)
        query_key = "orderId" if order_id and order_id != "?" else "origClientOrderId"
        query_value = order_id if query_key == "orderId" else client_order_id
        final_detail: dict[str, Any] = {}
        query_error: Exception | None = None
        canceled = False
        for poll in range(3):
            try:
                final_detail = _api_call(
                    "GET",
                    query_path,
                    {"symbol": pair, query_key: query_value},
                    signed=True,
                )
            except Exception as exc:
                query_error = exc
                break
            status = str(final_detail.get("status", "")).upper()
            if status in {"NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"}:
                if not canceled:
                    cancel_params = {"symbol": pair, query_key: query_value}
                    try:
                        _api_call("DELETE", cancel_path, cancel_params, signed=True)
                    except Exception:
                        pass
                    canceled = True
                if poll < 2:
                    time.sleep(0.2)
                    continue
            elif status not in {
                "FILLED",
                "CANCELED",
                "CANCELLED",
                "REJECTED",
                "EXPIRED",
                "EXPIRED_IN_MATCH",
            }:
                if poll < 2:
                    time.sleep(0.2)
                    continue
            break

        try:
            executed_qty = float(final_detail.get("executedQty", 0) or 0)
            executed_quote = float(final_detail.get("cummulativeQuoteQty", 0) or 0)
            avg_price = float(final_detail.get("avgPrice", 0) or 0)
        except (TypeError, ValueError):
            executed_qty = executed_quote = avg_price = 0.0
        if avg_price <= 0 and executed_qty > 0 and executed_quote > 0:
            avg_price = executed_quote / executed_qty
        exchange_status = str(final_detail.get("status", "UNKNOWN")).upper()
        known_terminal = exchange_status in {
            "FILLED",
            "CANCELED",
            "CANCELLED",
            "REJECTED",
            "EXPIRED",
            "EXPIRED_IN_MATCH",
        }
        if not known_terminal:
            state = "unknown"
        elif executed_qty > 0:
            full_qty = requested_qty is None or executed_qty + max(
                1e-12, requested_qty * 1e-8
            ) >= requested_qty
            state = "filled" if exchange_status == "FILLED" and full_qty else "partial"
        else:
            state = "failed" if known_terminal else "unknown"
        slippage = (
            round((avg_price - ref_price) / ref_price, 6)
            if ref_price > 0 and avg_price > 0
            else None
        )
        detail = {
            "order_id": str(final_detail.get("orderId") or order_id or "?"),
            "client_order_id": client_order_id,
            "exec_price": avg_price if avg_price > 0 else None,
            "exec_qty": executed_qty,
            "exec_quote_usd": executed_quote,
            "requested_qty": requested_qty,
            "ref_price": ref_price,
            "slippage": slippage,
            "submit_ts": round(submit_ts, 3),
            "fill_ts": round(time.time(), 3) if executed_qty > 0 else None,
            "latency_ms": round((time.time() - submit_ts) * 1000),
            "order_status": exchange_status,
            "status": state,
        }
        if state == "unknown":
            detail["error"] = redact_secret_values(
                query_error or submit_error or "order status is not terminal"
            )
        elif state == "partial":
            detail["error"] = "market order did not confirm the requested base quantity"
        elif state == "failed":
            detail["error"] = redact_secret_values(
                submit_error or f"order ended with {exchange_status}"
            )
        return state == "filled", detail

    def place_margin_order(
        self,
        pair: str,
        side: str,
        amount_base: float,
        quantity_precision: int = 6,
        ref_price: float = 0.0,
        side_effect: str = "NO_SIDE_EFFECT",
        client_order_id: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Margin orders are disabled; this executor never auto-borrows or repays."""
        block_real_execution("margin order")
        return False, {
            "status": "failed",
            "order_status": "NOT_SUBMITTED",
            "exec_qty": 0.0,
            "client_order_id": client_order_id,
            "error": "margin orders are disabled; pre-funded spot/perpetual execution only",
        }

    def _fetch_order_detail(
        self,
        pair: str,
        order_id: str | None = None,
        *,
        client_order_id: str | None = None,
        category: str = "spot",
    ) -> dict[str, Any]:
        query_path, _ = self._order_paths(category)
        params: dict[str, Any] = {"symbol": pair}
        if order_id and order_id != "?":
            params["orderId"] = order_id
        elif client_order_id:
            params["origClientOrderId"] = client_order_id
        else:
            raise ValueError("order_id or client_order_id is required")
        return _api_call("GET", query_path, params, signed=True)

    def place_buy(
        self,
        pair: str,
        amount_usdt: float,
        quote_precision: int = 2,
        ref_price: float = 0.0,
        client_order_id: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        block_real_execution("spot order")
        client_oid = self._make_client_order_id(client_order_id, "qbuy")
        quote_qty = f"{amount_usdt:.{quote_precision}f}"
        submit_ts = time.time()
        submit_error: Exception | None = None
        order_id: str | None = None
        try:
            result = _api_call(
                "POST",
                "/api/v3/order",
                {
                    "symbol": pair,
                    "side": "BUY",
                    "type": "MARKET",
                    "quoteOrderQty": quote_qty,
                    "newClientOrderId": client_oid,
                    "newOrderRespType": "RESULT",
                },
                signed=True,
            )
            order_id = str(result.get("orderId") or "") or None
        except Exception as e:
            submit_error = e
        return self._settle_market_order(
            pair=pair,
            category="spot",
            client_order_id=client_oid,
            order_id=order_id,
            requested_qty=None,
            ref_price=ref_price,
            submit_ts=submit_ts,
            submit_error=submit_error,
        )

    def place_sell(
        self,
        pair: str,
        amount_base: float,
        quantity_precision: int = 6,
        ref_price: float = 0.0,
        client_order_id: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        block_real_execution("spot order")
        client_oid = self._make_client_order_id(client_order_id, "qsell")
        qty = f"{amount_base:.{quantity_precision}f}".rstrip("0").rstrip(".")
        if "." not in qty:
            qty = f"{amount_base:.{quantity_precision}f}"
        submit_ts = time.time()
        submit_error: Exception | None = None
        order_id: str | None = None
        try:
            result = _api_call(
                "POST",
                "/api/v3/order",
                {
                    "symbol": pair,
                    "side": "SELL",
                    "type": "MARKET",
                    "quantity": qty,
                    "newClientOrderId": client_oid,
                    "newOrderRespType": "RESULT",
                },
                signed=True,
            )
            order_id = str(result.get("orderId") or "") or None
        except Exception as e:
            submit_error = e
        return self._settle_market_order(
            pair=pair,
            category="spot",
            client_order_id=client_oid,
            order_id=order_id,
            requested_qty=amount_base,
            ref_price=ref_price,
            submit_ts=submit_ts,
            submit_error=submit_error,
        )

    def place_futures_order(
        self,
        pair: str,
        side: str,
        amount_base: float,
        quantity_precision: int = 3,
        ref_price: float = 0.0,
        reduce_only: bool = False,
        client_order_id: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        block_real_execution("futures order")
        client_oid = self._make_client_order_id(client_order_id, "qfut")
        qty = f"{amount_base:.{quantity_precision}f}".rstrip("0").rstrip(".")
        if "." not in qty:
            qty = f"{amount_base:.{quantity_precision}f}"
        submit_ts = time.time()

        # Mode settings are now handled by initialize_futures_symbol at startup

        params = {
            "symbol": pair,
            "side": side,
            "type": "MARKET",
            "quantity": qty,
            "newClientOrderId": client_oid,
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        params["newOrderRespType"] = "RESULT"

        submit_error: Exception | None = None
        order_id: str | None = None
        try:
            result = _api_call(
                "POST",
                "/fapi/v1/order",
                params,
                signed=True,
            )
            order_id = str(result.get("orderId") or "") or None
        except Exception as e:
            submit_error = e
        return self._settle_market_order(
            pair=pair,
            category="futures",
            client_order_id=client_oid,
            order_id=order_id,
            requested_qty=amount_base,
            ref_price=ref_price,
            submit_ts=submit_ts,
            submit_error=submit_error,
        )

    def execute_trades(
        self,
        trades: list[dict[str, Any]],
        market: dict[str, dict[str, Any]],
        dry_run: bool,
    ) -> list[dict[str, Any]]:
        require_dry_run(dry_run, "Binance trade execution")
        results: list[dict[str, Any]] = []
        for trade in trades:
            symbol = trade["symbol"]
            mkt = market.get(symbol, {})
            pair = str(mkt.get("pair") or make_pair(symbol, "USDT"))
            ref_price = float(mkt.get("price", 0))
            record = dict(trade)
            record["dry_run"] = dry_run
            record["ref_price"] = ref_price
            record["venue"] = self.venue_id
            if dry_run:
                record["status"] = "simulated"
                record["order_id"] = None
                record["slippage"] = 0.0
                record["latency_ms"] = 0
                results.append(record)
                continue
            is_margin = str(trade.get("account", "")).lower() == "margin"
            if trade["type"] in ("buy", "sell") and is_margin:
                ok, detail = self.place_margin_order(
                    pair,
                    "BUY" if trade["type"] == "buy" else "SELL",
                    trade["amount_base"],
                    int(mkt.get("quantity_precision", 6)),
                    ref_price=ref_price,
                    client_order_id=trade.get("client_order_id"),
                )
            elif trade["type"] == "buy":
                ok, detail = self.place_buy(
                    pair,
                    trade["amount_usdt"],
                    int(mkt.get("quote_precision", 2)),
                    ref_price=ref_price,
                    client_order_id=trade.get("client_order_id"),
                )
            elif trade["type"] == "sell":
                ok, detail = self.place_sell(
                    pair,
                    trade["amount_base"],
                    int(mkt.get("quantity_precision", 6)),
                    ref_price=ref_price,
                    client_order_id=trade.get("client_order_id"),
                )
            elif trade["type"] in ("open_short", "close_long"):
                # Perp sell (open short or close long)
                f_rules = self.fetch_futures_symbol_rules(pair)
                f_prec = int(f_rules.get("quantity_precision", 3)) if f_rules else 3
                ok, detail = self.place_futures_order(
                    pair,
                    "SELL",
                    trade["amount_base"],
                    int(trade.get("quantity_precision") or f_prec),
                    ref_price=ref_price,
                    reduce_only=(trade["type"] == "close_long"),
                    client_order_id=trade.get("client_order_id"),
                )
            elif trade["type"] in ("close_short", "open_long"):
                # Perp buy (open long or close short)
                f_rules = self.fetch_futures_symbol_rules(pair)
                f_prec = int(f_rules.get("quantity_precision", 3)) if f_rules else 3
                ok, detail = self.place_futures_order(
                    pair,
                    "BUY",
                    trade["amount_base"],
                    int(trade.get("quantity_precision") or f_prec),
                    ref_price=ref_price,
                    reduce_only=(trade["type"] == "close_short"),
                    client_order_id=trade.get("client_order_id"),
                )
            else:
                ok, detail = False, {
                    "status": "failed",
                    "order_status": "NOT_SUBMITTED",
                    "exec_qty": 0.0,
                    "error": f"Unknown trade type {trade['type']}",
                }
            record.update(detail)
            record["status"] = str(
                detail.get("status") or ("filled" if ok else "failed")
            ).lower()
            record.setdefault("order_id", None)
            record.setdefault("error", None if ok else "order did not confirm full fill")
            results.append(record)
        return results
