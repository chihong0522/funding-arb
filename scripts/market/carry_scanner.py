#!/usr/bin/env python3
"""Forward-only, same-venue executable spot/USDT-perpetual carry estimates.

The scanner is a read-only snapshot estimator: it never signs requests, loads
credentials, or submits orders. Candidates require public spot/perpetual market
intersection, positive current and recent funding, interval/history metadata,
fresh depth on all four execution sides, and supported quantity limits.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, ROUND_DOWN
from statistics import median
from typing import Any

from backtest.funding_providers import get_funding_provider
from core.fee_providers import parse_fee_policy, resolve_venue_fee
from market.carry_scanner_config import (
    DEFAULT_BASIS_BUFFER_BPS,
    DEFAULT_EXIT_SLIPPAGE_BPS,
    DEFAULT_HORIZON_HOURS,
    DEFAULT_MAX_BOOK_AGE_SEC,
    DEFAULT_MAX_FUNDING_AGE_SEC,
    DEFAULT_MAX_INSTRUMENT_AGE_SEC,
    DEFAULT_MAX_SOURCE_SKEW_SEC,
    DEFAULT_NOTIONAL_USD,
    HISTORY_LOOKBACK_HOURS,
    MAX_HISTORY_GAP_INTERVALS,
    MAX_HORIZON_HOURS,
    MAX_NOTIONAL_USD,
    MIN_HISTORY_SAMPLES,
    validate_scan_inputs,
)
from market.carry_scanner_providers import PublicCarryMarketDataProvider

SUPPORTED_VENUES = ("binance", "bybit")

DISCLAIMER = (
    "Snapshot estimate, not guaranteed: funding rates, basis, depth, fees, and "
    "execution prices can change before entry or exit. Capital is illustrative "
    "only, not approval of leverage or live execution. No orders are submitted."
)


class InsufficientDepth(ValueError):
    def __init__(self, requested_qty: float, available_qty: float) -> None:
        self.requested_qty = requested_qty
        self.available_qty = available_qty
        super().__init__(
            f"requested {requested_qty:.12g} base; only {available_qty:.12g} available"
        )


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def book_vwap(book: dict[str, Any], side: str, quantity: float) -> dict[str, float]:
    """Return exact base-quantity VWAP and quote notional for asks/bids.

    The function consumes partial depth levels. It raises ``InsufficientDepth``
    rather than extrapolating beyond the public book.
    """
    side_key = str(side).lower()
    if side_key not in {"bids", "asks"}:
        raise ValueError("side must be 'bids' or 'asks'")
    target = _positive_float(quantity)
    if target is None:
        raise ValueError("quantity must be positive and finite")
    raw_levels = book.get(side_key)
    if not isinstance(raw_levels, (list, tuple)) or not raw_levels:
        raise ValueError("missing_book_side_or_price")
    levels: list[tuple[float, float]] = []
    for level in raw_levels:
        try:
            price, available = float(level[0]), float(level[1])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isfinite(price) and math.isfinite(available) and price > 0 and available > 0:
            levels.append((price, available))
    levels.sort(key=lambda item: item[0], reverse=side_key == "bids")
    remaining = target
    filled = 0.0
    quote = 0.0
    for price, available in levels:
        take = min(remaining, available)
        filled += take
        quote += take * price
        remaining -= take
        if remaining <= max(1e-12, target * 1e-12):
            break
    if remaining > max(1e-12, target * 1e-12):
        raise InsufficientDepth(target, filled)
    return {"filled_qty": target, "quote_usd": quote, "vwap": quote / target}


def _common_step(first: float, second: float) -> Decimal:
    a, b = Decimal(str(first)), Decimal(str(second))
    places = max(0, -a.as_tuple().exponent, -b.as_tuple().exponent)
    scale = Decimal(10) ** places
    ai, bi = int(a * scale), int(b * scale)
    if ai <= 0 or bi <= 0:
        raise ValueError("missing_quantity_limits")
    lcm = abs(ai * bi) // math.gcd(ai, bi)
    return Decimal(lcm) / scale


def _round_down_step(quantity: float, step: Decimal) -> float:
    q = Decimal(str(quantity))
    return float((q / step).to_integral_value(rounding=ROUND_DOWN) * step)


def _best_book_price(book: dict[str, Any], side: str) -> float | None:
    prices: list[float] = []
    for level in book.get(side) or []:
        try:
            price = float(level[0])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isfinite(price) and price > 0:
            prices.append(price)
    if not prices:
        return None
    return max(prices) if side == "bids" else min(prices)


def _quantity_for_quote(book: dict[str, Any], side: str, budget_usd: float) -> float:
    remaining = budget_usd
    quantity = 0.0
    for level in book.get(side) or []:
        try:
            price, available = float(level[0]), float(level[1])
        except (TypeError, ValueError, IndexError):
            continue
        if not (math.isfinite(price) and math.isfinite(available) and price > 0 and available > 0):
            continue
        take_quote = min(remaining, price * available)
        quantity += take_quote / price
        remaining -= take_quote
        if remaining <= max(1e-9, budget_usd * 1e-12):
            break
    if remaining > max(1e-9, budget_usd * 1e-12):
        raise InsufficientDepth(budget_usd, budget_usd - remaining)
    return quantity


def _fee_assumptions(
    venue: str,
    fee_policy: dict[str, Any] | None,
    spot_fee_pct: float | None,
    perp_fee_pct: float | None,
) -> tuple[float, float, dict[str, Any]]:
    # Force static tier resolution: phase 3 uses public market data only, never
    # the private account commission endpoints selected by the normal auto mode.
    policy = parse_fee_policy(fee_policy)
    policy["mode"] = "vip_tier"
    spot = resolve_venue_fee(venue, leg="spot", policy=policy)
    perp = resolve_venue_fee(venue, leg="futures", policy=policy)
    spot_value = float(spot_fee_pct) if spot_fee_pct is not None else float(spot["taker_pct"])
    perp_value = float(perp_fee_pct) if perp_fee_pct is not None else float(perp["taker_pct"])
    if not all(math.isfinite(v) and 0 <= v <= 100 for v in (spot_value, perp_value)):
        raise ValueError("fee assumptions must be finite percentage points in [0, 100]")
    return spot_value, perp_value, {
        "spot_taker_fee_pct": spot_value,
        "perp_taker_fee_pct": perp_value,
        "fees_are_taker": True,
        "fee_application": "entry and exit on each spot/perpetual leg",
        "source": "explicit_override" if spot_fee_pct is not None or perp_fee_pct is not None else "static_vip_tier_assumption",
        "tier": policy.get("venue_tiers", {}).get(venue),
        "private_fee_api_used": False,
    }


def _exclusion(symbol: str, reason: str, detail: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {"symbol": symbol, "reason": reason}
    if detail:
        row["detail"] = detail[:240]
    return row


def _timestamp_ms(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return 0
    if not math.isfinite(number) or number <= 0 or not number.is_integer():
        return 0
    return int(number)


def _book_timestamp(book: dict[str, Any]) -> int:
    """Return the exchange timestamp when present, otherwise local observation time."""
    return _timestamp_ms(book.get("exchange_ts_ms")) or _timestamp_ms(
        book.get("observed_at_ms")
    )


def _validate_book_freshness(
    book: dict[str, Any], *, now_ms: int, max_age_ms: int
) -> tuple[bool, str | None]:
    observed_at = _timestamp_ms(book.get("observed_at_ms"))
    if observed_at <= 0:
        return False, "missing_book_timestamp"
    observed_age = now_ms - observed_at
    if observed_age < -max(2_000, max_age_ms // 2):
        return False, "future_order_book_timestamp"
    if observed_age > max_age_ms:
        return False, "stale_order_book"
    stamp = _book_timestamp(book)
    if stamp <= 0:
        return False, "missing_book_timestamp"
    age = now_ms - stamp
    if age < -max(2_000, max_age_ms // 2):
        return False, "future_order_book_timestamp"
    if age > max_age_ms:
        return False, "stale_order_book"
    request_started = _timestamp_ms(book.get("request_started_at_ms"))
    if request_started and now_ms - request_started > max_age_ms * 2:
        return False, "stale_order_book"
    return True, None


def _history_rate(
    rows: list[dict[str, Any]], *, now_ms: int, interval_h: float
) -> tuple[float | None, int, str | None, int, int, list[dict[str, Any]]]:
    valid_by_ts: dict[int, float] = {}
    for row in rows or []:
        try:
            ts = _timestamp_ms(row.get("ts"))
            rate = float(row.get("rate_pct"))
        except (TypeError, ValueError):
            continue
        if ts > 0 and ts <= now_ms and math.isfinite(rate):
            valid_by_ts[ts] = rate
    valid = sorted(valid_by_ts.items())
    if len(valid) < MIN_HISTORY_SAMPLES:
        first_ts = valid[0][0] if valid else 0
        latest_ts = valid[-1][0] if valid else 0
        return None, len(valid), "missing_funding_history", first_ts, latest_ts, []
    latest_age_ms = now_ms - valid[-1][0]
    max_gap_ms = interval_h * MAX_HISTORY_GAP_INTERVALS * 3_600_000
    if latest_age_ms > max_gap_ms:
        return (
            None,
            len(valid),
            "funding_history_gap",
            valid[0][0],
            valid[-1][0],
            [],
        )
    recent = valid[-8:]
    if any(
        current_ts - previous_ts > max_gap_ms
        for (previous_ts, _), (current_ts, _) in zip(recent, recent[1:])
    ):
        return (
            None,
            len(valid),
            "funding_history_gap",
            valid[0][0],
            valid[-1][0],
            [],
        )
    observations = [
        {"ts_ms": ts, "rate_pct": rate} for ts, rate in recent
    ]
    return (
        float(median(rate for _, rate in recent)),
        len(valid),
        None,
        valid[0][0],
        valid[-1][0],
        observations,
    )


def _content_snapshot_id(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _snapshot_id_value(value: Any) -> str | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value if value else None
    if isinstance(value, int):
        return value
    return None


def _estimate_symbol(
    *,
    venue: str,
    funding_row: dict[str, Any],
    interval_h: float,
    spot_limits: dict[str, Any],
    perp_limits: dict[str, Any],
    instrument_snapshots: dict[str, Any],
    funding_provider: Any,
    market_provider: Any,
    notional_usd: float,
    horizon_hours: float,
    max_book_age_sec: float,
    max_funding_age_sec: float,
    max_source_skew_sec: float,
    max_instrument_age_sec: float,
    exit_slippage_bps: float,
    basis_buffer_bps: float,
    spot_fee_pct: float,
    perp_fee_pct: float,
    fixed_now_ms: int | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    symbol = str(funding_row.get("symbol", "")).upper()
    initial_now_ms = fixed_now_ms if fixed_now_ms is not None else int(time.time() * 1000)
    normalized_instrument_snapshots: dict[str, dict[str, Any]] = {}
    for market_name in ("spot", "perpetual"):
        snapshot = instrument_snapshots.get(market_name)
        if not isinstance(snapshot, dict):
            return None, _exclusion(symbol, "missing_instrument_snapshot_timestamp")
        instrument_ts = _timestamp_ms(snapshot.get("observed_at_ms"))
        if instrument_ts <= 0:
            return None, _exclusion(symbol, "missing_instrument_snapshot_timestamp")
        instrument_age_ms = initial_now_ms - instrument_ts
        if instrument_age_ms < -2_000:
            return None, _exclusion(symbol, "future_instrument_snapshot")
        if instrument_age_ms > max_instrument_age_sec * 1000:
            return None, _exclusion(symbol, "stale_instrument_snapshot")
        instrument_id = _snapshot_id_value(snapshot.get("snapshot_id"))
        if instrument_id is None:
            return None, _exclusion(symbol, "missing_instrument_snapshot_id")
        normalized_instrument_snapshots[market_name] = {
            "observed_at_ms": instrument_ts,
            "snapshot_id": instrument_id,
        }
    try:
        current_rate = float(funding_row.get("rate_pct"))
    except (TypeError, ValueError):
        return None, _exclusion(symbol, "missing_funding_rate")
    if not math.isfinite(current_rate) or current_rate <= 0:
        return None, _exclusion(symbol, "non_positive_funding_rate")

    next_ts = _timestamp_ms(funding_row.get("next_funding_ts"))
    funding_observed = _timestamp_ms(funding_row.get("observed_at_ms"))
    if funding_observed <= 0:
        return None, _exclusion(symbol, "missing_funding_timestamp")
    funding_age_ms = initial_now_ms - funding_observed
    if funding_age_ms < -max(2_000, int(max_funding_age_sec * 500)):
        return None, _exclusion(symbol, "future_funding_timestamp")
    if funding_age_ms > max_funding_age_sec * 1000:
        return None, _exclusion(symbol, "stale_funding_snapshot")

    history_start_ms = initial_now_ms - int(HISTORY_LOOKBACK_HOURS * 3_600_000)
    try:
        history = funding_provider.fetch_since(symbol, history_start_ms, max_pages=10)
    except Exception as exc:
        return None, _exclusion(symbol, "missing_funding_history", str(exc))
    observed_now_ms = fixed_now_ms if fixed_now_ms is not None else int(time.time() * 1000)
    (
        historical_rate,
        history_count,
        history_error,
        history_first_ts,
        history_latest_ts,
        history_observations,
    ) = _history_rate(history, now_ms=observed_now_ms, interval_h=interval_h)
    if history_error:
        return None, _exclusion(symbol, history_error)
    assert historical_rate is not None
    if historical_rate <= 0:
        return None, _exclusion(symbol, "non_positive_historical_funding")
    expected_rate = min(current_rate, historical_rate)
    if expected_rate <= 0:
        return None, _exclusion(symbol, "non_positive_historical_funding")

    mark_price = _positive_float(funding_row.get("mark_price"))
    if mark_price is None:
        return None, _exclusion(symbol, "missing_price", "perpetual mark price is absent or invalid")
    if next_ts <= observed_now_ms:
        return None, _exclusion(symbol, "invalid_next_funding_time")
    if not math.isfinite(interval_h) or interval_h <= 0:
        return None, _exclusion(symbol, "missing_funding_interval")

    try:
        spot_book = market_provider.fetch_order_book(venue, "spot", symbol)
        perp_book = market_provider.fetch_order_book(venue, "perpetual", symbol)
    except Exception as exc:
        return None, _exclusion(symbol, "missing_order_book", str(exc))

    now_for_books = fixed_now_ms if fixed_now_ms is not None else int(time.time() * 1000)
    max_age_ms = int(max_book_age_sec * 1000)
    for book in (spot_book, perp_book):
        ok, reason = _validate_book_freshness(book, now_ms=now_for_books, max_age_ms=max_age_ms)
        if not ok:
            return None, _exclusion(symbol, reason or "stale_order_book")

    spot_source_ts = _book_timestamp(spot_book)
    perp_source_ts = _book_timestamp(perp_book)
    source_skew_ms = max(funding_observed, spot_source_ts, perp_source_ts) - min(
        funding_observed, spot_source_ts, perp_source_ts
    )
    if source_skew_ms > max_source_skew_sec * 1000:
        return None, _exclusion(symbol, "source_timestamp_skew_exceeded")
    spot_book_snapshot_id = _snapshot_id_value(spot_book.get("snapshot_id"))
    perp_book_snapshot_id = _snapshot_id_value(perp_book.get("snapshot_id"))

    for book in (spot_book, perp_book):
        for side in ("bids", "asks"):
            if not book.get(side):
                return None, _exclusion(symbol, "missing_book_side_or_price", f"missing {side}")
    spot_bid = _best_book_price(spot_book, "bids")
    spot_ask = _best_book_price(spot_book, "asks")
    perp_bid = _best_book_price(perp_book, "bids")
    perp_ask = _best_book_price(perp_book, "asks")
    if spot_bid is None or spot_ask is None or perp_bid is None or perp_ask is None:
        return None, _exclusion(symbol, "missing_book_side_or_price")
    if spot_bid > spot_ask or perp_bid > perp_ask:
        return None, _exclusion(symbol, "invalid_order_book", "best bid exceeds best ask")
    if spot_book_snapshot_id is None or perp_book_snapshot_id is None:
        return None, _exclusion(symbol, "missing_order_book_snapshot_id")
    spot_mid = (spot_bid + spot_ask) / 2.0
    if spot_mid <= 0:
        return None, _exclusion(symbol, "missing_price")

    limit_values: list[float] = []
    min_notionals: list[float] = []
    for metadata in (spot_limits, perp_limits):
        step = _positive_float(metadata.get("qty_step"))
        minimum = _positive_float(metadata.get("min_qty"))
        maximum = _positive_float(metadata.get("max_qty"))
        min_notional = _positive_float(metadata.get("min_notional_usd"))
        if step is None or minimum is None or maximum is None or min_notional is None:
            return None, _exclusion(symbol, "missing_quantity_limits")
        limit_values.extend((step, minimum, maximum))
        min_notionals.append(min_notional)
    try:
        common_step = _common_step(limit_values[0], limit_values[3])
        quantity = _round_down_step(_quantity_for_quote(spot_book, "asks", notional_usd), common_step)
    except InsufficientDepth as exc:
        return None, _exclusion(symbol, "insufficient_depth", str(exc))
    except (ValueError, ArithmeticError) as exc:
        return None, _exclusion(symbol, "missing_quantity_limits", str(exc))
    if quantity <= 0 or quantity < max(limit_values[1], limit_values[4]):
        return None, _exclusion(symbol, "quantity_below_minimum")
    if quantity > min(limit_values[2], limit_values[5]):
        return None, _exclusion(symbol, "quantity_limit_exceeded")

    min_notional = max(min_notionals)
    try:
        spot_entry = book_vwap(spot_book, "asks", quantity)
        spot_exit = book_vwap(spot_book, "bids", quantity)
        perp_entry = book_vwap(perp_book, "bids", quantity)
        perp_exit = book_vwap(perp_book, "asks", quantity)
    except InsufficientDepth as exc:
        return None, _exclusion(symbol, "insufficient_depth", str(exc))
    except ValueError as exc:
        reason = "missing_book_side_or_price" if "missing_book_side" in str(exc) else "missing_price"
        return None, _exclusion(symbol, reason, str(exc))
    if min(
        spot_entry["quote_usd"],
        spot_exit["quote_usd"],
        perp_entry["quote_usd"],
        perp_exit["quote_usd"],
    ) < min_notional:
        return None, _exclusion(symbol, "below_minimum_notional")

    payment_count = 0
    interval_ms = int(interval_h * 3_600_000)
    horizon_end_ms = observed_now_ms + int(horizon_hours * 3_600_000)
    settlement_ts = next_ts
    while settlement_ts <= horizon_end_ms and payment_count < 10_000:
        payment_count += 1
        settlement_ts += interval_ms
    if payment_count == 0:
        return None, _exclusion(symbol, "no_funding_settlement_in_horizon")

    spot_entry_notional = spot_entry["quote_usd"]
    spot_exit_notional = spot_exit["quote_usd"]
    perp_entry_notional = perp_entry["quote_usd"]
    perp_exit_notional = perp_exit["quote_usd"]
    entry_fee_usd = (
        spot_entry_notional * spot_fee_pct / 100.0
        + perp_entry_notional * perp_fee_pct / 100.0
    )
    exit_fee_usd = (
        spot_exit_notional * spot_fee_pct / 100.0
        + perp_exit_notional * perp_fee_pct / 100.0
    )
    short_margin_multiplier = 1.0
    short_margin_usd = perp_entry_notional * short_margin_multiplier
    capital_fee_buffer_usd = entry_fee_usd + exit_fee_usd
    capital_required_usd = (
        spot_entry_notional + short_margin_usd + capital_fee_buffer_usd
    )
    # Do not credit favorable mark/basis movement in the snapshot unwind.
    observed_book_cost_usd = max(0.0, spot_entry_notional - spot_exit_notional) + max(
        0.0, perp_exit_notional - perp_entry_notional
    )
    exit_slippage_assumption_usd = (
        2.0 * notional_usd * exit_slippage_bps / 10_000.0
    )  # one allowance for each closing leg
    basis_buffer_usd = notional_usd * basis_buffer_bps / 10_000.0
    gross_funding_usd = (
        payment_count * mark_price * quantity * expected_rate / 100.0
    )
    total_cost_usd = (
        entry_fee_usd
        + exit_fee_usd
        + observed_book_cost_usd
        + exit_slippage_assumption_usd
        + basis_buffer_usd
    )
    net_horizon_usd = gross_funding_usd - total_cost_usd
    per_payment_usd = mark_price * quantity * expected_rate / 100.0
    breakeven_payments = max(1, int(math.ceil(total_cost_usd / per_payment_usd)))
    time_to_first_h = max(0.0, (next_ts - observed_now_ms) / 3_600_000.0)
    breakeven_h = time_to_first_h + (breakeven_payments - 1) * interval_h
    breakeven_rate_pct = total_cost_usd / (payment_count * mark_price * quantity) * 100.0
    snapshot_observed_at_ms = max(
        funding_observed,
        _timestamp_ms(spot_book.get("observed_at_ms")),
        _timestamp_ms(perp_book.get("observed_at_ms")),
        normalized_instrument_snapshots["spot"]["observed_at_ms"],
        normalized_instrument_snapshots["perpetual"]["observed_at_ms"],
    )
    candidate = {
        "venue": venue,
        "base": symbol[:-4] if symbol.endswith("USDT") else symbol,
        "symbol": symbol,
        "direction": "forward",
        "rate_pct": current_rate,
        "historical_median_rate_pct": historical_rate,
        "expected_rate_pct": expected_rate,
        "funding_estimator": "min(current_rate, recent_median_rate)",
        "history_samples": history_count,
        "history_first_ts": history_first_ts,
        "history_latest_ts": history_latest_ts,
        "history_estimator_first_ts": history_observations[0]["ts_ms"],
        "history_estimator_samples": len(history_observations),
        "funding_history_observations": history_observations,
        "funding_observed_at_ms": funding_observed,
        "snapshot_ts_ms": observed_now_ms,
        "snapshot_observed_at_ms": snapshot_observed_at_ms,
        "next_funding_ts": next_ts,
        "interval_h": interval_h,
        "funding_payments_estimated": payment_count,
        "horizon_hours": horizon_hours,
        "notional_usd_requested": notional_usd,
        "quantity_base": quantity,
        "spot_notional_usd": spot_entry_notional,
        "perp_notional_usd": perp_entry_notional,
        "mark_price": mark_price,
        "spot_entry_vwap": spot_entry["vwap"],
        "spot_exit_vwap": spot_exit["vwap"],
        "perp_entry_vwap": perp_entry["vwap"],
        "perp_exit_vwap": perp_exit["vwap"],
        "spot_fee_pct": spot_fee_pct,
        "perp_fee_pct": perp_fee_pct,
        "entry_fee_usd": entry_fee_usd,
        "exit_fee_usd": exit_fee_usd,
        "observed_round_trip_book_cost_usd": observed_book_cost_usd,
        "exit_slippage_bps_per_leg": exit_slippage_bps,
        "exit_slippage_assumption_usd": exit_slippage_assumption_usd,
        "basis_buffer_bps": basis_buffer_bps,
        "basis_buffer_usd": basis_buffer_usd,
        "gross_funding_usd": gross_funding_usd,
        "total_estimated_cost_usd": total_cost_usd,
        "net_horizon_earnings_usd": net_horizon_usd,
        "net_horizon_roi_pct": net_horizon_usd / notional_usd * 100.0,
        "net_horizon_notional_roi_pct": net_horizon_usd / notional_usd * 100.0,
        "short_margin_multiplier": short_margin_multiplier,
        "short_margin_usd": short_margin_usd,
        "borrowing_used": False,
        "capital_fee_buffer_usd": capital_fee_buffer_usd,
        "capital_required_usd": capital_required_usd,
        "net_horizon_capital_roi_pct": net_horizon_usd / capital_required_usd * 100.0,
        "breakeven_rate_pct": breakeven_rate_pct,
        "breakeven_funding_payments": breakeven_payments,
        "breakeven_hours_from_snapshot": breakeven_h,
        "spot_book_observed_at_ms": _timestamp_ms(spot_book.get("observed_at_ms")),
        "perp_book_observed_at_ms": _timestamp_ms(perp_book.get("observed_at_ms")),
        "spot_book_source_ts_ms": spot_source_ts,
        "perp_book_source_ts_ms": perp_source_ts,
        "spot_book_request_started_at_ms": _timestamp_ms(spot_book.get("request_started_at_ms")),
        "perp_book_request_started_at_ms": _timestamp_ms(perp_book.get("request_started_at_ms")),
        "spot_book_snapshot_id": spot_book_snapshot_id,
        "perp_book_snapshot_id": perp_book_snapshot_id,
        "spot_instrument_snapshot_observed_at_ms": normalized_instrument_snapshots["spot"]["observed_at_ms"],
        "perp_instrument_snapshot_observed_at_ms": normalized_instrument_snapshots["perpetual"]["observed_at_ms"],
        "spot_instrument_snapshot_id": normalized_instrument_snapshots["spot"]["snapshot_id"],
        "perp_instrument_snapshot_id": normalized_instrument_snapshots["perpetual"]["snapshot_id"],
        "source_timestamp_skew_ms": int(source_skew_ms),
        "snapshot_estimate": True,
    }
    # Keep the prior carry row fields for existing API clients/UI while the
    # phase-3 USD/horizon estimates remain the authoritative economics.
    candidate.update({
        "next_ts": next_ts,
        "annual_pct": round(current_rate * (24.0 / interval_h) * 365.0, 1),
        "has_spot": True,
        "spot_price": spot_entry["vwap"],
        "futures_fee_pct": perp_fee_pct,
        "fee_pct": round(spot_fee_pct + perp_fee_pct, 4),
        "net_edge_pct": round(
            net_horizon_usd / (payment_count * spot_entry_notional) * 100.0, 6
        ),
    })
    candidate["snapshot_id"] = _content_snapshot_id(
        {
            "venue": venue,
            "symbol": symbol,
            "funding": {
                "observed_at_ms": funding_observed,
                "rate_pct": current_rate,
                "next_funding_ts": next_ts,
                "mark_price": mark_price,
            },
            "history": {
                "first_ts": history_first_ts,
                "latest_ts": history_latest_ts,
                "samples": history_count,
                "median_rate_pct": historical_rate,
                "estimator_observations": history_observations,
            },
            "instruments": normalized_instrument_snapshots,
            "books": {
                "spot": {
                    "observed_at_ms": candidate["spot_book_observed_at_ms"],
                    "source_ts_ms": spot_source_ts,
                    "snapshot_id": spot_book_snapshot_id,
                },
                "perpetual": {
                    "observed_at_ms": candidate["perp_book_observed_at_ms"],
                    "source_ts_ms": perp_source_ts,
                    "snapshot_id": perp_book_snapshot_id,
                },
            },
            "quantity_base": quantity,
            "spot_entry_vwap": spot_entry["vwap"],
            "spot_exit_vwap": spot_exit["vwap"],
            "perp_entry_vwap": perp_entry["vwap"],
            "perp_exit_vwap": perp_exit["vwap"],
        }
    )
    return candidate, None


def scan_carry_venue(
    venue: str,
    *,
    notional_usd: float = DEFAULT_NOTIONAL_USD,
    horizon_hours: float = DEFAULT_HORIZON_HOURS,
    max_book_age_sec: float = DEFAULT_MAX_BOOK_AGE_SEC,
    max_funding_age_sec: float = DEFAULT_MAX_FUNDING_AGE_SEC,
    max_source_skew_sec: float = DEFAULT_MAX_SOURCE_SKEW_SEC,
    max_instrument_age_sec: float = DEFAULT_MAX_INSTRUMENT_AGE_SEC,
    exit_slippage_bps: float = DEFAULT_EXIT_SLIPPAGE_BPS,
    basis_buffer_bps: float = DEFAULT_BASIS_BUFFER_BPS,
    spot_fee_pct: float | None = None,
    perp_fee_pct: float | None = None,
    fee_policy: dict[str, Any] | None = None,
    max_workers: int = 8,
    funding_provider: Any | None = None,
    market_provider: Any | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Scan one same-venue spot/USDT-linear-perpetual pair universe.

    ``notional_usd`` is a maximum spot ask-side budget; the returned common
    quantity is rounded down to both venues' quantity steps and checked against
    all four book sides and both markets' min/max order constraints.
    """
    v = str(venue).strip().lower()
    if v not in SUPPORTED_VENUES:
        raise ValueError(f"Unsupported carry venue={v!r}; supported: {', '.join(SUPPORTED_VENUES)}")
    notional_usd, horizon_hours = validate_scan_inputs(notional_usd, horizon_hours)
    bounded_values = (
        ("max_book_age_sec", max_book_age_sec, 60.0),
        ("max_funding_age_sec", max_funding_age_sec, 300.0),
        ("max_source_skew_sec", max_source_skew_sec, 60.0),
        ("max_instrument_age_sec", max_instrument_age_sec, 3_600.0),
    )
    for name, value, maximum in bounded_values:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0
            or float(value) > maximum
        ):
            raise ValueError(f"{name} must be finite and in (0, {maximum:g}]")
    for name, value in (("exit_slippage_bps", exit_slippage_bps), ("basis_buffer_bps", basis_buffer_bps)):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
            or float(value) > 1_000.0
        ):
            raise ValueError(f"{name} must be finite and in [0, 1000]")
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= 32:
        raise ValueError("max_workers must be an integer in [1, 32]")
    if now_ms is not None and (
        isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms <= 0
    ):
        raise ValueError("now_ms must be a positive integer timestamp in milliseconds")
    policy_spot_fee, policy_perp_fee, fee_assumptions = _fee_assumptions(
        v, fee_policy, spot_fee_pct, perp_fee_pct
    )
    fp = funding_provider or get_funding_provider(v)
    mp = market_provider or PublicCarryMarketDataProvider()
    scan_time_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    result: dict[str, Any] = {
        "schema_version": 3,
        "venue": v,
        "direction": "forward_only",
        "notional_usd": float(notional_usd),
        "max_notional_usd": MAX_NOTIONAL_USD,
        "horizon_hours": float(horizon_hours),
        "max_horizon_hours": MAX_HORIZON_HOURS,
        "disclaimer": DISCLAIMER,
        "assumptions": {
            **fee_assumptions,
            "funding_rate_estimator": "min(current positive rate, median of up to 8 recent signed settlements)",
            "minimum_history_samples": MIN_HISTORY_SAMPLES,
            "exit_slippage_bps_per_leg": float(exit_slippage_bps),
            "basis_buffer_bps_per_position": float(basis_buffer_bps),
            "maximum_order_book_age_sec": float(max_book_age_sec),
            "maximum_funding_snapshot_age_sec": float(max_funding_age_sec),
            "maximum_source_timestamp_skew_sec": float(max_source_skew_sec),
            "maximum_instrument_snapshot_age_sec": float(max_instrument_age_sec),
            "maximum_history_gap_intervals": MAX_HISTORY_GAP_INTERVALS,
            "capital_model": "illustrative_prefunded_spot_plus_1x_short_margin",
            "short_margin_multiplier": 1.0,
            "borrowing_used": False,
            "capital_fee_buffer_method": "estimated_entry_and_exit_taker_fees",
            "leverage_or_live_execution_approved": False,
            "public_market_data_only": True,
        },
        "forward_candidates": [],
        "forward_no_spot": [],
        "reverse_candidates": [],
        "reverse_not_borrowable": [],
        "excluded": [],
        "exclusion_counts": {},
        "total_pairs": 0,
        "intersection_pairs": 0,
        "fee_source": fee_assumptions["source"],
        "spot_fee_pct": policy_spot_fee,
        "futures_fee_pct": policy_perp_fee,
        "two_leg_fee_pct": round(policy_spot_fee + policy_perp_fee, 6),
        "timestamp_ms": scan_time_ms,
        "scan_started_at_ms": scan_time_ms,
    }
    try:
        universe = mp.fetch_market_universe(v)
        spot_markets = universe.get("spot") or {}
        perp_markets = universe.get("perpetual") or {}
        raw_snapshots = universe.get("snapshot_metadata") or {}
        instrument_snapshots = {
            key: value
            for key, value in raw_snapshots.items()
            if key in {"spot", "perpetual"} and isinstance(value, dict)
        } if isinstance(raw_snapshots, dict) else {}
        result["instrument_snapshots"] = instrument_snapshots
        result["instrument_snapshot_observation_times_ms"] = {
            key: _timestamp_ms(value.get("observed_at_ms"))
            for key, value in instrument_snapshots.items()
        }
    except Exception as exc:
        result["excluded"].append(_exclusion("*", "market_universe_unavailable", str(exc)))
        result["market_data_error"] = str(exc)
        result["exclusion_counts"] = {"market_universe_unavailable": 1}
        return result
    try:
        funding_rows = fp.fetch_all("USDT")
    except Exception as exc:
        result["excluded"].append(_exclusion("*", "funding_snapshot_unavailable", str(exc)))
        result["market_data_error"] = str(exc)
        result["exclusion_counts"] = {"funding_snapshot_unavailable": 1}
        return result
    result["total_pairs"] = len(funding_rows)
    result["intersection_pairs"] = len(set(spot_markets) & set(perp_markets))
    funding_observation_times = [
        stamp
        for stamp in (_timestamp_ms(row.get("observed_at_ms")) for row in funding_rows)
        if stamp > 0
    ]
    result["funding_observation_min_ms"] = min(funding_observation_times, default=None)
    result["funding_observation_max_ms"] = max(funding_observation_times, default=None)
    try:
        interval_map = fp.fetch_interval_map("USDT")
        interval_map = {str(k).upper(): value for k, value in interval_map.items()}
        interval_map_error = None
    except Exception as exc:
        interval_map = {}
        interval_map_error = str(exc)

    ready: list[
        tuple[dict[str, Any], float, dict[str, Any], dict[str, Any], dict[str, Any]]
    ] = []
    seen: set[str] = set()
    for row in funding_rows:
        symbol = str(row.get("symbol", "")).upper()
        if not symbol or not symbol.endswith("USDT") or symbol in seen:
            continue
        seen.add(symbol)
        try:
            rate = float(row.get("rate_pct"))
        except (TypeError, ValueError):
            result["excluded"].append(_exclusion(symbol, "missing_funding_rate"))
            continue
        if rate <= 0 or not math.isfinite(rate):
            result["excluded"].append(_exclusion(symbol, "non_positive_funding_rate"))
            continue
        if symbol not in perp_markets:
            result["excluded"].append(_exclusion(symbol, "linear_perpetual_not_listed"))
            continue
        if symbol not in spot_markets:
            result["excluded"].append(_exclusion(symbol, "spot_market_not_listed"))
            continue
        interval_value = interval_map.get(symbol)
        if interval_value is None:
            result["excluded"].append(_exclusion(symbol, "missing_funding_interval", interval_map_error))
            continue
        try:
            interval_h = float(interval_value)
        except (TypeError, ValueError):
            interval_h = 0.0
        if not math.isfinite(interval_h) or interval_h <= 0:
            result["excluded"].append(_exclusion(symbol, "missing_funding_interval"))
            continue
        ready.append((row, interval_h, spot_markets[symbol], perp_markets[symbol], instrument_snapshots))

    candidates: list[dict[str, Any]] = []
    if ready:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(ready))) as pool:
            futures = {
                pool.submit(
                    _estimate_symbol,
                    venue=v,
                    funding_row=row,
                    interval_h=interval_h,
                    spot_limits=spot_limits,
                    perp_limits=perp_limits,
                    instrument_snapshots=instrument_snapshot_set,
                    funding_provider=fp,
                    market_provider=mp,
                    notional_usd=notional_usd,
                    horizon_hours=horizon_hours,
                    max_book_age_sec=float(max_book_age_sec),
                    max_funding_age_sec=float(max_funding_age_sec),
                    max_source_skew_sec=float(max_source_skew_sec),
                    max_instrument_age_sec=float(max_instrument_age_sec),
                    exit_slippage_bps=float(exit_slippage_bps),
                    basis_buffer_bps=float(basis_buffer_bps),
                    spot_fee_pct=policy_spot_fee,
                    perp_fee_pct=policy_perp_fee,
                    fixed_now_ms=now_ms,
                ): str(row.get("symbol", "")).upper()
                for row, interval_h, spot_limits, perp_limits, instrument_snapshot_set in ready
            }
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    candidate, excluded = future.result()
                except Exception as exc:
                    candidate, excluded = None, _exclusion(symbol, "candidate_evaluation_failed", str(exc))
                if candidate is not None:
                    candidates.append(candidate)
                if excluded is not None:
                    result["excluded"].append(excluded)
    candidates.sort(key=lambda row: (-row["net_horizon_earnings_usd"], -row["expected_rate_pct"], row["symbol"]))
    result["forward_candidates"] = candidates
    result["exclusion_counts"] = {}
    for row in result["excluded"]:
        reason = row["reason"]
        result["exclusion_counts"][reason] = result["exclusion_counts"].get(reason, 0) + 1
    result["completed_at_ms"] = int(time.time() * 1000)
    result["snapshot_id"] = _content_snapshot_id(
        {
            "venue": v,
            "scan_started_at_ms": scan_time_ms,
            "instrument_snapshots": result.get("instrument_snapshots", {}),
            "funding_observation_min_ms": result.get("funding_observation_min_ms"),
            "funding_observation_max_ms": result.get("funding_observation_max_ms"),
            "candidate_snapshot_ids": sorted(
                row["snapshot_id"] for row in candidates if row.get("snapshot_id")
            ),
        }
    )
    return result


def reprice_candidate(
    row: dict[str, Any], *, spot_fee_pct: float, perp_fee_pct: float
) -> dict[str, Any]:
    """Recalculate snapshot net dollars from stored market/depth quantities."""
    out = dict(row)
    if not out.get("snapshot_estimate"):
        return out
    spot_entry = float(out.get("spot_notional_usd", 0) or 0)
    perp_entry = float(out.get("perp_notional_usd", 0) or 0)
    qty = float(out.get("quantity_base", 0) or 0)
    spot_exit = float(out.get("spot_exit_vwap", 0) or 0) * qty
    perp_exit = float(out.get("perp_exit_vwap", 0) or 0) * qty
    entry_fee = spot_entry * spot_fee_pct / 100.0 + perp_entry * perp_fee_pct / 100.0
    exit_fee = spot_exit * spot_fee_pct / 100.0 + perp_exit * perp_fee_pct / 100.0
    margin_multiplier = float(out.get("short_margin_multiplier", 1.0) or 1.0)
    short_margin = perp_entry * margin_multiplier
    capital_fee_buffer = entry_fee + exit_fee
    capital_required = spot_entry + short_margin + capital_fee_buffer
    gross = float(out.get("gross_funding_usd", 0) or 0)
    costs = (
        entry_fee + exit_fee
        + float(out.get("observed_round_trip_book_cost_usd", 0) or 0)
        + float(out.get("exit_slippage_assumption_usd", 0) or 0)
        + float(out.get("basis_buffer_usd", 0) or 0)
    )
    out.update({
        "spot_fee_pct": spot_fee_pct,
        "perp_fee_pct": perp_fee_pct,
        "futures_fee_pct": perp_fee_pct,
        "fee_pct": round(spot_fee_pct + perp_fee_pct, 4),
        "entry_fee_usd": entry_fee,
        "exit_fee_usd": exit_fee,
        "capital_fee_buffer_usd": capital_fee_buffer,
        "short_margin_usd": short_margin,
        "capital_required_usd": capital_required,
        "total_estimated_cost_usd": costs,
        "net_horizon_earnings_usd": gross - costs,
        "net_horizon_roi_pct": (gross - costs) / float(out.get("notional_usd_requested", 1) or 1) * 100.0,
        "net_horizon_notional_roi_pct": (gross - costs) / float(out.get("notional_usd_requested", 1) or 1) * 100.0,
        "net_horizon_capital_roi_pct": (gross - costs) / capital_required * 100.0 if capital_required > 0 else 0.0,
    })
    payment_count = int(out.get("funding_payments_estimated", 0) or 0)
    if payment_count > 0 and spot_entry > 0:
        out["net_edge_pct"] = round(
            (gross - costs) / (payment_count * spot_entry) * 100.0, 6
        )
    funding_base = float(out.get("funding_payments_estimated", 0) or 0) * float(out.get("mark_price", 0) or 0) * qty
    if funding_base > 0:
        out["breakeven_rate_pct"] = costs / funding_base * 100.0
    per_payment = float(out.get("mark_price", 0) or 0) * qty * float(out.get("expected_rate_pct", 0) or 0) / 100.0
    if per_payment > 0:
        payments = max(1, int(math.ceil(costs / per_payment)))
        out["breakeven_funding_payments"] = payments
        out["breakeven_hours_from_snapshot"] = max(
            0.0,
            (int(out.get("next_funding_ts", 0) or 0) - int(out.get("snapshot_ts_ms", 0) or 0)) / 3_600_000.0,
        ) + (payments - 1) * float(out.get("interval_h", 0) or 0)
    return out
