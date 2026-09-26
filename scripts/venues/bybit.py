#!/usr/bin/env python3
"""Bybit spot + USDT linear perpetual venue adapter."""

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
from venues.http_util import (
    http_get_json,
    parse_kline_ohlcv,
    rules_for_price,
)

BASE = "https://api.bybit.com"
_symbol_rules_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_futures_rules_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_spot_ticker_loaded_at: float = 0.0
_spot_ticker_prices: dict[str, float] = {}
_futures_ticker_loaded_at: float = 0.0
_futures_ticker_prices: dict[str, float] = {}
_initialized_symbols: set[str] = set()
_env_loaded = False

KLINE_INTERVALS = {
    "1day": "D",
    "1d": "D",
    "4h": "240",
    "1week": "W",
    "1w": "W",
}


def _ensure_env() -> None:
    global _env_loaded
    if _env_loaded:
        return
    ensure_env("BYBIT_")
    _env_loaded = True


def _get_key() -> str:
    _ensure_env()
    return os.environ.get("BYBIT_API_KEY") or ""


def _get_secret() -> str:
    _ensure_env()
    return os.environ.get("BYBIT_SECRET_KEY") or ""


def _sign(payload: str) -> str:
    return hmac.new(
        _get_secret().encode(), payload.encode(), hashlib.sha256
    ).hexdigest()


def _api_call(
    method: str, path: str, params: Optional[dict] = None, body: Optional[dict] = None
) -> dict:
    if method.upper() != "GET":
        block_real_execution("Bybit REST write")
    key, secret = _get_key(), _get_secret()
    if not (key and secret):
        raise RuntimeError(
            "Bybit API credentials missing: please set BYBIT_API_KEY / BYBIT_SECRET_KEY, "
            "or configure them in an approved secure credential store."
        )

    recv_window = "5000"
    ts = str(int(time.time() * 1000))

    if method == "GET" and params:
        query = urllib.parse.urlencode(params)
    else:
        query = ""

    body_str = json.dumps(body) if body else ""
    full_path = path + ("?" + query if query else "")

    prehash = ts + key + recv_window + (query if method == "GET" else body_str)
    sig = _sign(prehash)

    headers = {
        "X-BAPI-API-KEY": key,
        "X-BAPI-SIGN": sig,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv_window,
        "Content-Type": "application/json",
    }

    retries = 3 if method == "GET" else 1
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        url = BASE + full_path
        req = urllib.request.Request(url, headers=headers, method=method)
        if body_str:
            req.data = body_str.encode()
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            if data.get("retCode") != 0:
                raise RuntimeError(
                    f"Bybit API error: {data.get('retMsg', data.get('retCode'))}"
                )
            return data
        except Exception as e:
            last_err = RuntimeError(redact_secret_values(e))
            if method == "GET" and attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise last_err from None
    raise last_err if last_err else RuntimeError("bybit _api_call failed")


class BybitSpotVenue:
    venue_id = "bybit"

    def get_ticker(self, pair: str) -> float:
        url = f"{BASE}/v5/market/tickers?category=spot&symbol={pair}"
        try:
            data = http_get_json(url)
            return float(
                data.get("result", {}).get("list", [{}])[0].get("lastPrice", 0)
            )
        except Exception:
            return 0.0

    def get_futures_ticker(self, pair: str) -> float:
        """USDT perpetual futures last price. pair format e.g. BTCUSDT."""
        url = f"{BASE}/v5/market/tickers?category=linear&symbol={pair}"
        try:
            data = http_get_json(url)
            return float(
                data.get("result", {}).get("list", [{}])[0].get("lastPrice", 0)
            )
        except Exception:
            return 0.0

    def get_all_spot_tickers(self, cache_sec: int = 5) -> dict[str, float]:
        """Bulk spot last prices {PAIR: price}. Cached briefly for screener loops."""
        global _spot_ticker_loaded_at, _spot_ticker_prices
        now = time.time()
        if _spot_ticker_prices and (now - _spot_ticker_loaded_at) < cache_sec:
            return dict(_spot_ticker_prices)
        try:
            data = http_get_json(f"{BASE}/v5/market/tickers?category=spot")
            _spot_ticker_prices = {
                str(r.get("symbol", "")).upper(): float(r.get("lastPrice", 0) or 0)
                for r in data.get("result", {}).get("list", [])
                if r.get("symbol")
            }
            _spot_ticker_loaded_at = now
        except Exception:
            pass
        return dict(_spot_ticker_prices)

    def get_all_futures_tickers(self, cache_sec: int = 5) -> dict[str, float]:
        """Bulk linear perpetual last prices {BTCUSDT: price}. Cached briefly."""
        global _futures_ticker_loaded_at, _futures_ticker_prices
        now = time.time()
        if _futures_ticker_prices and (now - _futures_ticker_loaded_at) < cache_sec:
            return dict(_futures_ticker_prices)
        try:
            data = http_get_json(f"{BASE}/v5/market/tickers?category=linear")
            _futures_ticker_prices = {
                str(r.get("symbol", "")).upper(): float(r.get("lastPrice", 0) or 0)
                for r in data.get("result", {}).get("list", [])
                if r.get("symbol")
            }
            _futures_ticker_loaded_at = now
        except Exception:
            pass
        return dict(_futures_ticker_prices)

    def get_klines(
        self, pair: str, granularity: str = "1day", limit: int = 200
    ) -> list:
        interval = KLINE_INTERVALS.get(granularity, "D")
        limit = min(limit, 1000)
        url = f"{BASE}/v5/market/kline?category=spot&symbol={pair}&interval={interval}&limit={limit}"
        try:
            data = http_get_json(url)
            return list(reversed(data.get("result", {}).get("list", [])))
        except Exception:
            return []

    def fetch_symbol_rules(
        self, pair: str, cache_sec: int = 3600
    ) -> Optional[dict[str, Any]]:
        now = time.time()
        cached = _symbol_rules_cache.get(pair)
        if cached and (now - cached[0]) < cache_sec:
            return dict(cached[1])
        url = f"{BASE}/v5/market/instruments-info?category=spot&symbol={pair}"
        try:
            payload = http_get_json(url)
        except Exception:
            return dict(cached[1]) if cached else None
        rows = payload.get("result", {}).get("list", [])
        if not rows:
            return dict(cached[1]) if cached else None
        info = rows[0]
        lot_filter = {}
        min_notional = {}
        for f in info.get("lotSizeFilter", []):
            lot_filter = f if isinstance(f, dict) else {}
            break
        for f in info.get("minNotionalFilter", []):
            min_notional = f if isinstance(f, dict) else {}
            break
        if not lot_filter:
            lot_filter = info.get("lotSizeFilter") or {}
        if not min_notional:
            min_notional = info.get("minNotionalFilter") or {}

        base_prec = (
            len(
                str(lot_filter.get("basePrecision", "0.000001"))
                .rstrip("0")
                .split(".")[-1]
            )
            if "." in str(lot_filter.get("basePrecision", "0.000001"))
            else 6
        )
        min_base = float(lot_filter.get("minOrderQty", 0))
        min_usdt = float(min_notional.get("minNotionalValue", 0))
        rules = {
            "symbol": pair,
            "min_trade_usdt": min_usdt,
            "min_trade_base": min_base,
            "quantity_precision": base_prec,
            "quote_precision": 2,
            "status": "Trading" if info.get("status") == "Trading" else "",
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
        url = f"{BASE}/v5/market/instruments-info?category=linear&symbol={pair}"
        try:
            payload = http_get_json(url)
        except Exception:
            return dict(cached[1]) if cached else None
        rows = payload.get("result", {}).get("list", [])
        if not rows:
            return dict(cached[1]) if cached else None
        info = rows[0]
        lot_filter = info.get("lotSizeFilter") or {}
        qty_step = str(lot_filter.get("qtyStep", "0.001"))
        qty_prec = len(qty_step.rstrip("0").split(".")[-1]) if "." in qty_step else 3
        min_base = float(lot_filter.get("minOrderQty", 0))
        rules = {
            "symbol": pair,
            "min_trade_usdt": 0,
            "min_trade_base": min_base,
            "quantity_precision": qty_prec,
            "quote_precision": 2,
            "status": "Trading" if info.get("status") == "Trading" else "",
        }
        _futures_rules_cache[pair] = (now, rules)
        return dict(rules)

    def transfer_asset(
        self, asset: str, amount: float, from_account: str, to_account: str
    ) -> bool:
        block_real_execution("internal transfer")
        transfer_idx = None
        if from_account == "spot" and to_account == "futures":
            transfer_idx = "UNIFIED"
        elif from_account == "futures" and to_account == "spot":
            transfer_idx = "UNIFIED"
        else:
            return False
        try:
            _api_call(
                "POST",
                "/v5/asset/transfer/inter-transfer",
                body={
                    "transferAccountType": transfer_idx,
                    "coin": asset.upper(),
                    "amount": f"{amount:.8f}".rstrip("0").rstrip("."),
                },
            )
            return True
        except Exception:
            return False

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
        balances: dict[str, float] = {c: 0.0 for c in coins}
        data = _api_call(
            "GET", "/v5/account/wallet-balance", params={"accountType": "UNIFIED"}
        )
        for acct in data.get("result", {}).get("list", []):
            for coin in acct.get("coin", []):
                c = str(coin.get("coin", "")).upper()
                if c in balances:
                    raw = (
                        coin.get("availableToWithdraw")
                        or coin.get("walletBalance")
                        or "0"
                    )
                    balances[c] = float(raw if raw else 0)
        return balances

    def fetch_live_state(self, assets: list[str]) -> dict[str, Any]:
        balances = self.fetch_balances(assets)
        positions: dict[str, dict[str, Any]] = {}
        try:
            pos_data = _api_call(
                "GET",
                "/v5/position/list",
                params={"category": "linear", "settleCoin": "USDT"},
            )
            for pos in pos_data.get("result", {}).get("list", []):
                amt = float(pos.get("size", 0) or 0)
                if amt < 1e-9:
                    continue
                sym_raw = str(pos.get("symbol", "")).upper()
                base = (
                    sym_raw.replace("USDT", "") if sym_raw.endswith("USDT") else sym_raw
                )
                side_val = str(pos.get("side", "")).lower()
                positions[base] = {
                    "amount": amt,
                    "side": "long" if side_val == "buy" else "short",
                    "entry_price": float(pos.get("avgPrice", 0) or 0),
                    "unrealized_pnl": float(pos.get("unrealisedPnl", 0) or 0),
                    "leverage": float(pos.get("leverage", 1) or 1),
                }
        except Exception:
            pass
        return {"balances": balances, "futures_positions": positions}

    def fetch_futures_positions(self, quote: str = "USDT") -> list[dict[str, Any]]:
        """USDT perpetual positions list (single endpoint; raises on failure)."""
        pos_data = _api_call(
            "GET",
            "/v5/position/list",
            params={"category": "linear", "settleCoin": quote.upper()},
        )
        out: list[dict[str, Any]] = []
        for pos in pos_data.get("result", {}).get("list", []) or []:
            qty = abs(float(pos.get("size", 0) or 0))
            if qty <= 1e-12:
                continue
            out.append(
                {
                    "symbol": str(pos.get("symbol", "")).upper(),
                    "side": "long"
                    if str(pos.get("side", "")).lower() == "buy"
                    else "short",
                    "qty": qty,
                    "entry_price": float(pos.get("avgPrice", 0) or 0),
                    "liq_price": float(pos.get("liqPrice", 0) or 0),
                    "leverage": float(pos.get("leverage", 1) or 1),
                    "unrealized_pnl": float(pos.get("unrealisedPnl", 0) or 0),
                }
            )
        return out

    def initialize_futures_symbol(self, pair: str) -> None:
        """Require safe, preconfigured futures settings without mutating account state."""
        account = _api_call("GET", "/v5/account/info")
        account_info = account.get("result", {}) if isinstance(account, dict) else {}
        if account_info.get("marginMode") != "ISOLATED_MARGIN":
            raise RuntimeError(
                f"Bybit account must be preconfigured for ISOLATED_MARGIN before {pair} execution"
            )

        positions = _api_call(
            "GET",
            "/v5/position/list",
            params={"category": "linear", "symbol": pair},
        )
        result = positions.get("result", {}) if isinstance(positions, dict) else {}
        rows = result.get("list", []) if isinstance(result, dict) else []
        row = next(
            (item for item in rows if isinstance(item, dict) and item.get("symbol") == pair),
            None,
        )
        if row is None:
            raise RuntimeError(f"Bybit futures position configuration unavailable for {pair}")
        raw_position_idx = row.get("positionIdx")
        if not isinstance(raw_position_idx, (str, int)):
            raise RuntimeError(f"Bybit {pair} position mode is unknown")
        position_idx = int(raw_position_idx)
        if position_idx != 0:
            raise RuntimeError(f"Bybit {pair} must be preconfigured for one-way position mode")
        raw_leverage = row.get("leverage")
        if not isinstance(raw_leverage, (str, int)):
            raise RuntimeError(f"Bybit {pair} leverage is unknown")
        leverage = int(raw_leverage)
        if leverage != 1:
            raise RuntimeError(f"Bybit {pair} must be preconfigured at 1x leverage")
        if str(row.get("autoAddMargin", "")).lower() not in {"0", "false"}:
            raise RuntimeError(f"Bybit {pair} auto-add margin must be disabled")

        _initialized_symbols.add(pair)

    def fetch_borrow_rates(self, coins: list[str]) -> dict[str, float]:
        """Fetch annualized borrow rates from Bybit spot margin. Returns {coin: rate_decimal}."""
        rates: dict[str, float] = {c: 0.0 for c in coins}
        for coin in coins:
            try:
                data = _api_call(
                    "GET",
                    "/v5/spot-margin-trade/data",
                    params={"vipLevel": "No VIP", "currency": coin.upper()},
                )
                for vip_group in data.get("result", {}).get("vipCoinList", []):
                    for item in vip_group.get("list", []):
                        if str(item.get("currency", "")).upper() != coin.upper():
                            continue
                        if not item.get("borrowable"):
                            continue
                        hourly = float(item.get("hourlyBorrowRate", 0) or 0)
                        if hourly > 0:
                            rates[coin.upper()] = hourly * 24 * 365
            except Exception:
                pass
        return rates

    # ── UTA spot margin (Reverse C&C: borrow-sell / buy-repay) ────────────────────

    def supports_reverse_arbitrage(self) -> bool:
        """Reverse margin execution is disabled; capability probes must not change account state."""
        return False

    def fetch_margin_debt(self, assets: list[str]) -> dict[str, float]:
        """UTA per-coin liabilities (borrowAmount + accruedInterest), in base coin units."""
        debt: dict[str, float] = {a.upper(): 0.0 for a in assets}
        try:
            data = _api_call(
                "GET", "/v5/account/wallet-balance", params={"accountType": "UNIFIED"}
            )
            for acct in data.get("result", {}).get("list", []):
                for coin in acct.get("coin", []):
                    c = str(coin.get("coin", "")).upper()
                    if c in debt:
                        debt[c] = float(coin.get("borrowAmount", 0) or 0) + float(
                            coin.get("accruedInterest", 0) or 0
                        )
        except Exception as e:
            print(f"bybit fetch_margin_debt failed: {redact_secret_values(e)}", file=sys.stderr)
        return debt

    def margin_borrow(self, asset: str, amount: float) -> bool:
        """UTA borrowing occurs implicitly under isLeverage=1 orders; no standalone borrow interface."""
        block_real_execution("margin borrow")

    def margin_repay(self, asset: str, amount: float) -> bool:
        """UTA manual repayment: prefers official /v5/account/repay, falls back to quick-repayment on failure."""
        block_real_execution("margin repayment")
        coin = asset.upper()
        amt = f"{amount:.8f}".rstrip("0").rstrip(".")
        try:
            _api_call(
                "POST",
                "/v5/account/repay",
                body={"coin": coin, "amount": amt, "repaymentType": "FLEXIBLE"},
            )
            return True
        except Exception as e:
            print(
                f"bybit margin repay {coin} via /account/repay failed: {redact_secret_values(e)}",
                file=sys.stderr,
            )
        try:
            _api_call("POST", "/v5/account/quick-repayment", body={"coin": coin})
            return True
        except Exception as e:
            print(f"bybit margin repay {coin} failed: {redact_secret_values(e)}", file=sys.stderr)
            return False

    @staticmethod
    def _make_client_order_id(value: str | None, prefix: str) -> str:
        del prefix  # client IDs must be supplied by the durable execution journal.
        if not value:
            raise ValueError("client_order_id is required for journaled live execution")
        client_id = value
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,36}", client_id):
            raise ValueError("client_order_id must be 1-36 ASCII letters, digits, '_' or '-'")
        return client_id

    @staticmethod
    def _order_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
        rows = (payload.get("result") or {}).get("list") or []
        return [row for row in rows if isinstance(row, dict)]

    def _query_order(
        self,
        *,
        category: str,
        pair: str,
        client_order_id: str,
        order_id: str | None,
    ) -> dict[str, Any]:
        lookup = {"orderId": order_id} if order_id else {"orderLinkId": client_order_id}
        last_error: Exception | None = None
        for endpoint in ("/v5/order/realtime", "/v5/order/history"):
            try:
                payload = _api_call(
                    "GET",
                    endpoint,
                    params={"category": category, "symbol": pair, **lookup},
                )
                rows = self._order_rows(payload)
                if rows:
                    return rows[0]
            except Exception as exc:
                last_error = exc
        if last_error:
            raise last_error
        raise LookupError("Bybit returned no order for the client order ID")

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
        final_detail: dict[str, Any] = {}
        query_error: Exception | None = None
        canceled = False
        terminal_statuses = {
            "FILLED",
            "CANCELLED",
            "CANCELED",
            "REJECTED",
            "PARTIALLYFILLEDCANCELED",
            "DEACTIVATED",
        }
        for poll in range(3):
            try:
                final_detail = self._query_order(
                    category=category,
                    pair=pair,
                    client_order_id=client_order_id,
                    order_id=order_id,
                )
            except Exception as exc:
                query_error = exc
                break
            status = str(final_detail.get("orderStatus", "")).upper()
            if status in {"NEW", "CREATED", "PARTIALLY_FILLED"}:
                if not canceled:
                    lookup = {"orderId": order_id} if order_id else {"orderLinkId": client_order_id}
                    try:
                        _api_call(
                            "POST",
                            "/v5/order/cancel",
                            body={"category": category, "symbol": pair, **lookup},
                        )
                    except Exception:
                        pass
                    canceled = True
                if poll < 2:
                    time.sleep(0.2)
                    continue
            elif status not in terminal_statuses:
                if poll < 2:
                    time.sleep(0.2)
                    continue
            break

        try:
            executed_qty = float(final_detail.get("cumExecQty", 0) or 0)
            executed_quote = float(final_detail.get("cumExecValue", 0) or 0)
            avg_price = float(final_detail.get("avgPrice", 0) or 0)
        except (TypeError, ValueError):
            executed_qty = executed_quote = avg_price = 0.0
        if avg_price <= 0 and executed_qty > 0 and executed_quote > 0:
            avg_price = executed_quote / executed_qty
        exchange_status = str(final_detail.get("orderStatus", "UNKNOWN")).upper()
        if exchange_status not in terminal_statuses:
            state = "unknown"
        elif executed_qty > 0:
            full_qty = requested_qty is None or executed_qty + max(
                1e-12, requested_qty * 1e-8
            ) >= requested_qty
            state = "filled" if exchange_status == "FILLED" and full_qty else "partial"
        else:
            state = "failed"
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
        submit_ts = time.time()
        submit_error: Exception | None = None
        order_id: str | None = None
        try:
            result = _api_call(
                "POST",
                "/v5/order/create",
                body={
                    "category": "spot",
                    "symbol": pair,
                    "side": "Buy",
                    "orderType": "Market",
                    "marketUnit": "quoteCoin",
                    "qty": f"{amount_usdt:.{quote_precision}f}",
                    "orderLinkId": client_oid,
                },
            )
            order_id = str((result.get("result") or {}).get("orderId") or "") or None
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
        sz = f"{amount_base:.{quantity_precision}f}".rstrip("0").rstrip(".")
        submit_ts = time.time()
        submit_error: Exception | None = None
        order_id: str | None = None
        try:
            result = _api_call(
                "POST",
                "/v5/order/create",
                body={
                    "category": "spot",
                    "symbol": pair,
                    "side": "Sell",
                    "orderType": "Market",
                    "qty": sz,
                    "orderLinkId": client_oid,
                },
            )
            order_id = str((result.get("result") or {}).get("orderId") or "") or None
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
        reduce_only: bool | None = None,
        client_order_id: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        block_real_execution("futures order")
        side_map = {
            "open_long": "Buy",
            "close_short": "Buy",
            "open_short": "Sell",
            "close_long": "Sell",
            "BUY": "Buy",
            "SELL": "Sell",
            "buy": "Buy",
            "sell": "Sell",
        }
        bybit_side = side_map.get(side)
        if bybit_side is None:
            return False, {
                "status": "failed",
                "order_status": "NOT_SUBMITTED",
                "exec_qty": 0.0,
                "error": f"unsupported futures order side/action {side!r}",
            }
        is_close = side.startswith("close_")
        reduce_only = is_close if reduce_only is None else bool(reduce_only or is_close)
        client_oid = self._make_client_order_id(client_order_id, "qfut")
        sz = f"{amount_base:.{quantity_precision}f}".rstrip("0").rstrip(".")
        submit_ts = time.time()

        self.initialize_futures_symbol(pair)

        body = {
            "category": "linear",
            "symbol": pair,
            "side": bybit_side,
            "positionIdx": 0,
            "orderType": "Market",
            "qty": sz,
            "orderLinkId": client_oid,
        }
        if reduce_only:
            body["reduceOnly"] = True
        submit_error: Exception | None = None
        order_id: str | None = None
        try:
            result = _api_call(
                "POST",
                "/v5/order/create",
                body=body,
            )
            order_id = str((result.get("result") or {}).get("orderId") or "") or None
        except Exception as e:
            submit_error = e
        return self._settle_market_order(
            pair=pair,
            category="linear",
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
        require_dry_run(dry_run, "Bybit trade execution")
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
                    trade["type"],
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
            elif trade["type"] in (
                "open_short",
                "close_long",
                "close_short",
                "open_long",
            ):
                ok, detail = self.place_futures_order(
                    pair,
                    trade["type"],
                    trade["amount_base"],
                    int(
                        trade.get("quantity_precision")
                        or mkt.get("quantity_precision", 3)
                    ),
                    ref_price=ref_price,
                    reduce_only=trade["type"].startswith("close_"),
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
