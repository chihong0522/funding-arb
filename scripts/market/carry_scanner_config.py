"""Shared limits and validation for read-only carry scans."""

from __future__ import annotations

import math
from typing import Any

DEFAULT_NOTIONAL_USD = 100.0
MAX_NOTIONAL_USD = 500.0
DEFAULT_HORIZON_HOURS = 24.0
MAX_HORIZON_HOURS = 168.0
DEFAULT_MAX_BOOK_AGE_SEC = 5.0
DEFAULT_MAX_FUNDING_AGE_SEC = 60.0
DEFAULT_MAX_SOURCE_SKEW_SEC = 5.0
DEFAULT_MAX_INSTRUMENT_AGE_SEC = 300.0
DEFAULT_EXIT_SLIPPAGE_BPS = 10.0
DEFAULT_BASIS_BUFFER_BPS = 10.0
MIN_HISTORY_SAMPLES = 3
HISTORY_LOOKBACK_HOURS = 24.0 * 14.0
HISTORY_FRESH_INTERVALS = 3.0
MAX_HISTORY_GAP_INTERVALS = 1.5
MAX_ORDER_BOOK_LIMIT = 1000


def validate_scan_inputs(notional_usd: Any, horizon_hours: Any) -> tuple[float, float]:
    """Return validated finite scan inputs or raise ValueError."""
    values: list[tuple[str, Any, float, float]] = [
        ("notional_usd", notional_usd, 0.0, MAX_NOTIONAL_USD),
        ("horizon_hours", horizon_hours, 0.0, MAX_HORIZON_HOURS),
    ]
    normalized: list[float] = []
    for name, value, lower, upper in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a finite number in (0, {upper:g}]")
        try:
            number = float(value)
        except (OverflowError, ValueError) as exc:
            raise ValueError(
                f"{name} must be a finite number in (0, {upper:g}]"
            ) from exc
        if not math.isfinite(number) or number <= lower or number > upper:
            raise ValueError(f"{name} must be a finite number in (0, {upper:g}]")
        normalized.append(number)
    return normalized[0], normalized[1]


def validate_order_book_limit(venue: str, limit: Any, default: int) -> int:
    """Validate a caller-supplied public depth limit without silent clamping."""
    if limit is None:
        return default
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("order-book limit must be an integer")
    minimum = 5 if venue == "binance" else 1
    if limit < minimum or limit > MAX_ORDER_BOOK_LIMIT:
        raise ValueError(
            f"order-book limit for {venue} must be in [{minimum}, {MAX_ORDER_BOOK_LIMIT}]"
        )
    return limit
