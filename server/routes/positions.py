#!/usr/bin/env python3
"""Position management API routes."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from server.security import live_trading_enabled

router = APIRouter(tags=["positions"])

StrategyKind = Literal["pure_futures", "carry", "unified"]
MAX_ORDER_NOTIONAL_USD = 500.0
EXECUTION_VENUES = frozenset({"binance", "bybit"})
_CARRY_PAPER_MAX_SNAPSHOT_AGE_MS = 60_000
_CARRY_PAPER_MAX_FUTURE_SKEW_MS = 30_000
_CARRY_OPEN_IN_PROGRESS: set[tuple[str, str]] = set()

# ---------------------------------------------------------------------------
# Try importing real position loader / executor
# ---------------------------------------------------------------------------
_load_positions_fn = None
_close_fn = None
_load_cross_positions_fn = None
_open_cross_fn = None
_close_cross_fn = None
try:
    from execution.pure_futures_executor import (  # noqa: E402
        close_pure_futures_pair,
        load_pure_futures_positions,
    )

    _load_positions_fn = load_pure_futures_positions
    _close_fn = close_pure_futures_pair
except Exception:
    pass

try:
    from execution.cross_venue_executor import (  # noqa: E402
        close_cross_venue_position,
        load_positions as load_cross_venue_positions,
        open_cross_venue_position,
    )

    _load_cross_positions_fn = load_cross_venue_positions
    _open_cross_fn = open_cross_venue_position
    _close_cross_fn = close_cross_venue_position
except Exception:
    pass

# ---------------------------------------------------------------------------
# Position file paths
# ---------------------------------------------------------------------------
_POSITIONS_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "scripts"
    / "data"
    / "pure-futures"
    / "positions.json"
)
_PURE_FUTURES_TEMPLATE = (
    Path(__file__).resolve().parent.parent.parent
    / "templates"
    / "config.pure_futures.spread.json"
)


def _read_pure_positions() -> list[dict[str, Any]]:
    """Read pure-futures positions from file, using the real loader if available."""
    if _load_positions_fn is not None:
        try:
            rows = _load_positions_fn()
            for p in rows:
                p.setdefault("strategy", "pure_futures")
            return rows
        except Exception:
            pass
    if not _POSITIONS_PATH.exists():
        return []
    try:
        data = json.loads(_POSITIONS_PATH.read_text(encoding="utf-8"))
        rows = data if isinstance(data, list) else []
        for p in rows:
            p.setdefault("strategy", "pure_futures")
        return rows
    except Exception:
        return []


def _read_cross_positions() -> list[dict[str, Any]]:
    if _load_cross_positions_fn is None:
        return []
    try:
        rows = _load_cross_positions_fn()
        for p in rows:
            p.setdefault("strategy", "carry" if p.get("futures_venue") == p.get("spot_venue") else "unified")
        return rows
    except Exception:
        return []


def _read_positions() -> list[dict[str, Any]]:
    return _read_pure_positions() + _read_cross_positions()


# ---------------------------------------------------------------------------
# Funding income estimate
# ---------------------------------------------------------------------------
# /positions is polled frequently, so cache funding rates per venue to avoid
# hammering upstream APIs. Value: (timestamp, {symbol: rate dict}).
_FUNDING_CACHE: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}
_FUNDING_CACHE_TTL = 60.0


def _get_cached_funding(venue: str, symbol: str) -> dict[str, Any]:
    """Return the cached funding-rate dict for (venue, symbol), refreshing at
    most every _FUNDING_CACHE_TTL seconds. Falls back to a stale value when a
    fresh fetch fails, so PnL enrichment degrades gracefully."""
    now = time.time()
    entry = _FUNDING_CACHE.get(venue)
    if entry is not None:
        ts, rates = entry
        if (now - ts) < _FUNDING_CACHE_TTL and symbol in rates:
            return rates[symbol]
    try:
        from backtest.funding_providers import get_funding_provider  # noqa: E402

        fp = get_funding_provider(venue)
        fresh = fp.fetch_current(symbol) if fp else {}
    except Exception:
        # Fetch failed: serve stale value if we have one, else empty.
        if entry is not None and symbol in entry[1]:
            return entry[1][symbol]
        return {}
    rates = entry[1] if entry is not None else {}
    rates[symbol] = fresh
    _FUNDING_CACHE[venue] = (now, rates)
    return fresh


def _estimate_funding_income(
    pos: dict[str, Any], qty: float, long_mark: float, short_mark: float
) -> tuple[float, float]:
    """Estimate cumulative funding income (USD) for an open pure-futures pair.

    Uses current funding rates as an approximation of realized income; real
    settled income would require querying each venue's funding ledger.

    Returns (estimated_funding_usd, current_spread_annualized_pct).
    """
    base = str(pos.get("base", "")).upper()
    long_id = str(pos.get("long_venue", ""))
    short_id = str(pos.get("short_venue", ""))
    opened_at = int(pos.get("opened_at", 0) or 0)
    if not base or not long_id or not short_id or opened_at <= 0:
        return 0.0, 0.0

    symbol = f"{base}USDT"
    long_data = _get_cached_funding(long_id, symbol)
    short_data = _get_cached_funding(short_id, symbol)
    long_rate_pct = float(long_data.get("rate_pct", 0) or 0)
    short_rate_pct = float(short_data.get("rate_pct", 0) or 0)

    # Funding convention: positive rate => longs pay shorts.
    # forward (long @ long_venue, short @ short_venue):
    #   pay long_rate, receive short_rate  => net = short_rate - long_rate
    # reverse (long @ short_venue, short @ long_venue):
    #   pay short_rate, receive long_rate  => net = long_rate - short_rate
    direction = str(pos.get("direction", "forward")).lower()
    if direction == "reverse":
        net_rate_pct = long_rate_pct - short_rate_pct
    else:
        net_rate_pct = short_rate_pct - long_rate_pct

    # Reference interval: prefer long leg, fall back to short, then default 8h.
    long_interval_ms = int(long_data.get("interval_ms", 0) or 0)
    short_interval_ms = int(short_data.get("interval_ms", 0) or 0)
    interval_ms = long_interval_ms or short_interval_ms or (8 * 60 * 60 * 1000)
    interval_h = interval_ms / (60 * 60 * 1000.0)

    now_ms = int(time.time() * 1000)
    held_hours = max(0.0, (now_ms - opened_at) / 3600000.0)
    periods = held_hours / interval_h if interval_h > 0 else 0.0

    periods_per_year = (365.0 * 24.0) / interval_h if interval_h > 0 else 0.0
    spread_annual = net_rate_pct * periods_per_year

    # Notional ~= avg mark price * qty (use whichever leg has a price).
    if long_mark > 0 and short_mark > 0:
        avg_mark = (long_mark + short_mark) / 2.0
    else:
        avg_mark = long_mark or short_mark or 0.0
    notional_usd = avg_mark * qty

    funding_income_usd = (net_rate_pct / 100.0) * notional_usd * periods
    return round(funding_income_usd, 2), round(spread_annual, 2)


def _enrich_positions_with_pnl(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach unrealized PnL from live mark prices for open pure-futures positions."""
    open_pos = [
        p
        for p in positions
        if p.get("status") == "open"
        and p.get("qty")
        and p.get("strategy", "pure_futures") == "pure_futures"
    ]
    if not open_pos:
        return positions

    get_mark = None
    try:
        from execution.pure_futures_watcher import _get_mark_price  # noqa: E402

        get_mark = _get_mark_price
    except Exception:
        return positions

    venue_ids = {str(p["long_venue"]) for p in open_pos} | {
        str(p["short_venue"]) for p in open_pos
    }
    try:
        from execution.pure_futures_watcher import _prefetch_all_mark_prices  # noqa: E402

        _prefetch_all_mark_prices(venue_ids)
    except Exception:
        pass

    enriched: list[dict[str, Any]] = []
    for pos in positions:
        p = dict(pos)
        if p.get("status") != "open" or p.get("strategy", "pure_futures") != "pure_futures":
            enriched.append(p)
            continue
        qty = float(p.get("qty") or 0)
        long_open = float(p.get("long_price") or 0)
        short_open = float(p.get("short_price") or 0)
        if qty <= 0 or (long_open <= 0 and short_open <= 0):
            enriched.append(p)
            continue
        base = str(p.get("base", ""))
        long_mark = get_mark(str(p["long_venue"]), base) if get_mark else 0.0
        short_mark = get_mark(str(p["short_venue"]), base) if get_mark else 0.0
        long_pnl = (long_mark - long_open) * qty if long_open > 0 else 0.0
        short_pnl = (short_open - short_mark) * qty if short_open > 0 else 0.0
        unrealized = round(long_pnl + short_pnl, 2)
        p["unrealized_pnl_usd"] = unrealized
        p["pnl_usd"] = unrealized
        if long_mark > 0 and short_mark > 0:
            p["mark_spread_pct"] = round(
                abs(long_mark - short_mark) / max(long_mark, short_mark) * 100.0, 4
            )
        funding_income, spread_annual = _estimate_funding_income(p, qty, long_mark, short_mark)
        p["funding_pnl_est_usd"] = funding_income
        p["funding_rate_spread_pct"] = spread_annual
        p["total_pnl_usd"] = round(unrealized + funding_income, 2)
        enriched.append(p)
    return enriched


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class OpenPositionRequest(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    strategy: StrategyKind = Field(
        "pure_futures",
        description="pure_futures | carry | unified",
    )
    base: str = Field(..., description="Base asset of the trading pair, e.g. BTC")
    symbol: str | None = Field(None, description="Exact scanned carry symbol")
    amount_usd: float = Field(
        ...,
        gt=0,
        le=MAX_ORDER_NOTIONAL_USD,
        description=f"Position size (USD; maximum ${MAX_ORDER_NOTIONAL_USD:,.0f})",
    )
    direction: str = Field("forward", description="forward | reverse")
    horizon_hours: float | None = Field(None, gt=0, description="Exact scanned carry horizon")
    scan_snapshot_id: str | None = Field(None, description="Server-cached scanner snapshot identity")
    candidate_snapshot_id: str | None = Field(None, description="Server-cached candidate identity")
    dry_run: bool = Field(True, description="Whether to simulate opening a position")
    long_venue: str | None = Field(None, description="Pure futures long venue")
    short_venue: str | None = Field(None, description="Pure futures short venue")
    futures_venue: str | None = Field(None, description="Carry/unified perp venue")
    spot_venue: str | None = Field(None, description="Carry/unified spot venue")


class ClosePositionRequest(BaseModel):
    reason: str = Field("", description="Reason for closing the position")


def _execution_scope_error(req: OpenPositionRequest) -> str | None:
    """Apply the server-owned execution allowlist and live kill switch."""
    amount = float(req.amount_usd)
    if not math.isfinite(amount) or amount <= 0 or amount > MAX_ORDER_NOTIONAL_USD:
        return f"amount_usd must be finite and between 0 and {MAX_ORDER_NOTIONAL_USD:g}"
    if req.strategy != "carry":
        return "only same-venue cash-and-carry execution is enabled"
    if str(req.direction or "").strip().lower() != "forward":
        return "only forward carry is enabled; reverse carry would borrow spot assets"

    futures_venue = str(req.futures_venue or "").strip().lower()
    spot_venue = str(req.spot_venue or "").strip().lower()
    if not futures_venue or not spot_venue:
        return "futures_venue and spot_venue are required for carry execution"
    if futures_venue != spot_venue:
        return "cross-venue execution is disabled"
    if futures_venue not in EXECUTION_VENUES:
        return "execution is allowlisted only for Binance and Bybit"
    if not req.dry_run and not live_trading_enabled():
        return "live trading is disabled by the process-level kill switch"
    return None


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _timestamp_ms(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0 or not number.is_integer():
        return None
    return int(number)


def _valid_snapshot_reference(value: Any) -> bool:
    return (
        isinstance(value, str) and bool(value)
    ) or (
        isinstance(value, int) and not isinstance(value, bool)
    )


def _carry_paper_context(
    req: OpenPositionRequest, *, now_ms: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve and validate a paper open against server-owned scanner state."""
    if (
        not req.scan_snapshot_id
        or not req.candidate_snapshot_id
        or not req.symbol
        or req.horizon_hours is None
    ):
        raise HTTPException(
            status_code=409,
            detail="paper carry open requires the cached scan and candidate identity; rescan and retry",
        )

    from server.routes import scanner as scanner_routes  # noqa: E402
    from market.carry_scanner_config import (
        MAX_HISTORY_GAP_INTERVALS,
        MAX_HORIZON_HOURS,
        MAX_NOTIONAL_USD,
        MIN_HISTORY_SAMPLES,
    )  # noqa: E402

    cached = scanner_routes._find_cached_carry_candidate(
        req.scan_snapshot_id, req.candidate_snapshot_id
    )
    if cached is None:
        raise HTTPException(
            status_code=409,
            detail="scanner snapshot or candidate is unavailable; run a fresh scan",
        )
    bucket, candidate = cached
    version = bucket.get("schema_version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version < 3
        or bucket.get("error")
    ):
        raise HTTPException(status_code=409, detail="scanner snapshot is not eligible for paper open")

    venue = bucket.get("venue")
    symbol = candidate.get("symbol")
    base = candidate.get("base")
    if (
        not isinstance(venue, str)
        or req.futures_venue != venue
        or req.spot_venue != venue
        or candidate.get("venue") != venue
        or req.direction != "forward"
        or candidate.get("direction") != "forward"
        or candidate.get("snapshot_estimate") is not True
        or not isinstance(base, str)
        or not isinstance(symbol, str)
        or symbol != f"{base}USDT"
        or req.base != base
        or req.symbol != symbol
    ):
        raise HTTPException(
            status_code=409,
            detail="paper open does not match the cached venue, symbol, or forward direction",
        )

    scanned_notional = _positive_number(candidate.get("notional_usd_requested"))
    bucket_notional = _positive_number(bucket.get("notional_usd"))
    requested_notional = _positive_number(req.amount_usd)
    scanned_horizon = _positive_number(candidate.get("horizon_hours"))
    bucket_horizon = _positive_number(bucket.get("horizon_hours"))
    requested_horizon = _positive_number(req.horizon_hours)
    if (
        scanned_notional is None
        or bucket_notional is None
        or requested_notional is None
        or scanned_horizon is None
        or bucket_horizon is None
        or requested_horizon is None
        or scanned_notional > MAX_NOTIONAL_USD
        or scanned_horizon > MAX_HORIZON_HOURS
        or scanned_notional != bucket_notional
        or scanned_notional != requested_notional
        or scanned_horizon != bucket_horizon
        or scanned_horizon != requested_horizon
    ):
        raise HTTPException(
            status_code=409,
            detail="paper open size and horizon must exactly match the scanned candidate; rescan and retry",
        )

    if not all(
        _valid_snapshot_reference(candidate.get(key))
        for key in (
            "spot_book_snapshot_id",
            "perp_book_snapshot_id",
            "spot_instrument_snapshot_id",
            "perp_instrument_snapshot_id",
        )
    ):
        raise HTTPException(status_code=409, detail="candidate market snapshot identity is incomplete")

    assumptions = bucket.get("assumptions")
    if not isinstance(assumptions, dict):
        raise HTTPException(status_code=409, detail="candidate freshness assumptions are unavailable")

    def max_age_ms(key: str, maximum_ms: int) -> int:
        seconds = _positive_number(assumptions.get(key))
        if seconds is None:
            raise HTTPException(status_code=409, detail="candidate freshness assumptions are incomplete")
        return min(int(seconds * 1000), maximum_ms)

    def fresh_timestamp(value: Any, *, allowed_age_ms: int, future_skew_ms: int) -> int:
        stamp = _timestamp_ms(value)
        if stamp is None:
            raise HTTPException(status_code=409, detail="candidate timestamps are incomplete")
        age = now_ms - stamp
        if age > allowed_age_ms or age < -future_skew_ms:
            raise HTTPException(status_code=409, detail="scanner candidate is stale; rescan before opening")
        return stamp

    snapshot_ts = fresh_timestamp(
        candidate.get("snapshot_ts_ms"),
        allowed_age_ms=_CARRY_PAPER_MAX_SNAPSHOT_AGE_MS,
        future_skew_ms=_CARRY_PAPER_MAX_FUTURE_SKEW_MS,
    )
    completed_ts = fresh_timestamp(
        bucket.get("completed_at_ms"),
        allowed_age_ms=_CARRY_PAPER_MAX_SNAPSHOT_AGE_MS,
        future_skew_ms=_CARRY_PAPER_MAX_FUTURE_SKEW_MS,
    )
    fresh_timestamp(
        candidate.get("snapshot_observed_at_ms"),
        allowed_age_ms=_CARRY_PAPER_MAX_SNAPSHOT_AGE_MS,
        future_skew_ms=_CARRY_PAPER_MAX_FUTURE_SKEW_MS,
    )
    funding_age_ms = max_age_ms("maximum_funding_snapshot_age_sec", 300_000)
    book_age_ms = max_age_ms("maximum_order_book_age_sec", 60_000)
    instrument_age_ms = max_age_ms("maximum_instrument_snapshot_age_sec", 3_600_000)
    funding_ts = fresh_timestamp(
        candidate.get("funding_observed_at_ms"),
        allowed_age_ms=funding_age_ms,
        future_skew_ms=max(2_000, funding_age_ms // 2),
    )
    book_source_timestamps: list[int] = []
    for key in ("spot", "perp"):
        fresh_timestamp(
            candidate.get(f"{key}_book_observed_at_ms"),
            allowed_age_ms=book_age_ms,
            future_skew_ms=max(2_000, book_age_ms // 2),
        )
        book_source_timestamps.append(
            fresh_timestamp(
                candidate.get(f"{key}_book_source_ts_ms"),
                allowed_age_ms=book_age_ms,
                future_skew_ms=max(2_000, book_age_ms // 2),
            )
        )
        fresh_timestamp(
            candidate.get(f"{key}_instrument_snapshot_observed_at_ms"),
            allowed_age_ms=instrument_age_ms,
            future_skew_ms=2_000,
        )
    source_skew_max_ms = max_age_ms("maximum_source_timestamp_skew_sec", 60_000)
    source_timestamps = [funding_ts, *book_source_timestamps]
    measured_skew_ms = max(source_timestamps) - min(source_timestamps)
    declared_skew_ms = candidate.get("source_timestamp_skew_ms")
    if (
        isinstance(declared_skew_ms, bool)
        or not isinstance(declared_skew_ms, int)
        or declared_skew_ms < 0
        or declared_skew_ms > source_skew_max_ms
        or abs(measured_skew_ms - declared_skew_ms) > 1_000
    ):
        raise HTTPException(status_code=409, detail="candidate source timestamps are stale or inconsistent")

    interval_hours = _positive_number(candidate.get("interval_h"))
    history_latest = _timestamp_ms(candidate.get("history_latest_ts"))
    history_count = candidate.get("history_samples")
    # Compare the latest settled sample with request time; do not require the
    # next settlement to have occurred before the candidate can be opened.
    if (
        interval_hours is None
        or history_latest is None
        or isinstance(history_count, bool)
        or not isinstance(history_count, int)
        or history_count < MIN_HISTORY_SAMPLES
        or now_ms - history_latest < -2_000
        or now_ms - history_latest
        > interval_hours * MAX_HISTORY_GAP_INTERVALS * 3_600_000
    ):
        raise HTTPException(status_code=409, detail="candidate settlement history is stale; rescan before opening")

    next_funding_ts = _timestamp_ms(candidate.get("next_funding_ts"))
    if next_funding_ts is None or next_funding_ts <= now_ms:
        raise HTTPException(status_code=409, detail="candidate settlement time has passed; rescan before opening")
    if completed_ts + _CARRY_PAPER_MAX_FUTURE_SKEW_MS < snapshot_ts:
        raise HTTPException(status_code=409, detail="scanner candidate does not belong to its scan snapshot")

    provenance = {
        "scan_snapshot_id": req.scan_snapshot_id,
        "candidate_snapshot_id": req.candidate_snapshot_id,
        # Fee repricing creates a new candidate revision, but this immutable
        # market identity keeps the same public snapshot one-use in the ledger.
        "market_snapshot_id": str(
            candidate.get("market_snapshot_id") or candidate.get("snapshot_id")
        ),
        "economics_revision_id": candidate.get("economics_revision_id"),
        "venue": venue,
        "symbol": symbol,
        "direction": "forward",
        "scanned_notional_usd": scanned_notional,
        "horizon_hours": scanned_horizon,
        "snapshot_ts_ms": snapshot_ts,
        "funding_observed_at_ms": funding_ts,
        "history_latest_ts": history_latest,
        "interval_hours": interval_hours,
        "market_snapshot_ids": {
            key: candidate[key]
            for key in (
                "spot_book_snapshot_id",
                "perp_book_snapshot_id",
                "spot_instrument_snapshot_id",
                "perp_instrument_snapshot_id",
            )
        },
        "execution_mode": "paper",
        "fee_assumptions": {
            **dict(bucket.get("fee_assumptions") or {}),
            "spot_taker_fee_pct": candidate.get(
                "spot_fee_pct", assumptions.get("spot_taker_fee_pct")
            ),
            "perp_taker_fee_pct": candidate.get(
                "perp_fee_pct",
                candidate.get("futures_fee_pct", assumptions.get("perp_taker_fee_pct")),
            ),
            "two_leg_taker_fee_pct": candidate.get(
                "fee_pct", bucket.get("two_leg_fee_pct")
            ),
            "fees_are_taker": assumptions.get("fees_are_taker"),
            "fee_application": assumptions.get("fee_application"),
            "source": bucket.get("fee_source")
            or (bucket.get("fee_assumptions") or {}).get("source")
            or assumptions.get("source"),
            "tier": bucket.get("fee_tier")
            or (bucket.get("fee_assumptions") or {}).get("tier")
            or assumptions.get("tier"),
            "private_fee_api_used": assumptions.get("private_fee_api_used"),
        },
        "estimate_assumptions": dict(assumptions),
        # Keep the complete server-cached estimate (inputs and derived values)
        # explicitly classified as an estimate, never as simulated/realized PnL.
        "candidate_estimate": dict(candidate),
    }
    return candidate, provenance


def _carry_candidate_already_used(
    scan_id: str, candidate_id: str, market_snapshot_id: str | None = None
) -> bool:
    """Treat each persisted scanner candidate as one-use, even after close."""
    if _load_cross_positions_fn is None:
        raise HTTPException(status_code=503, detail="paper position ledger is unavailable")
    try:
        rows = _load_cross_positions_fn()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="paper position ledger could not be read") from exc
    if not isinstance(rows, list):
        raise HTTPException(status_code=503, detail="paper position ledger is invalid")
    for row in rows:
        provenance = row.get("scanner_provenance") if isinstance(row, dict) else None
        existing_candidate_ids = (
            {
                value
                for value in (
                    provenance.get("candidate_snapshot_id"),
                    provenance.get("market_snapshot_id"),
                )
                if isinstance(value, str)
            }
            if isinstance(provenance, dict)
            else set()
        )
        requested_candidate_ids = {candidate_id, market_snapshot_id}
        if (
            isinstance(provenance, dict)
            and provenance.get("scan_snapshot_id") == scan_id
            and bool(existing_candidate_ids & requested_candidate_ids)
        ):
            return True
    return False


class _NoTransferVenue:
    """Delegate venue operations while making all account transfers impossible."""

    def __init__(self, venue: Any) -> None:
        self._venue = venue

    def transfer_asset(self, *_args: Any, **_kwargs: Any) -> bool:
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._venue, name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _check_venues_tradeable(venue_ids: list[str], *, live: bool) -> str | None:
    try:
        from server.routes.settings import venue_live_ready, venue_trade_capability  # noqa: E402
    except ImportError:
        return None

    for vid in venue_ids:
        capable, reason = venue_trade_capability(vid)
        if not capable:
            return f"venue {vid!r} is scan-only, cannot trade: {reason}"
        if live:
            ready, live_reason = venue_live_ready(vid)
            if not ready:
                return f"venue {vid!r} not ready for live trading: {live_reason}"
    return None


def _executor_config() -> tuple[dict[str, Any], float]:
    """Build pure-futures executor config from template + Dashboard strategy settings."""
    cfg: dict[str, Any] = {}
    if _PURE_FUTURES_TEMPLATE.exists():
        try:
            cfg = json.loads(_PURE_FUTURES_TEMPLATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    try:
        from core.strategy_config import apply_strategy_to_pure_futures_cfg  # noqa: E402

        cfg = apply_strategy_to_pure_futures_cfg(cfg)
    except Exception:
        pass
    pfa = cfg.get("pureFuturesArbitrage") or {}
    max_mark = float(pfa.get("maxMarkSpreadPct") or 1.0)
    return cfg, max_mark


def _position_kind(position_id: str, positions: list[dict[str, Any]]) -> str:
    for pos in positions:
        if pos.get("id") == position_id:
            return str(pos.get("strategy") or "pure_futures")
    if position_id.startswith("xv-"):
        return "carry"
    return "pure_futures"


def _position_scope_error(position: dict[str, Any]) -> str | None:
    """Only close recorded forward carry positions on the supported CEXs."""
    strategy = str(position.get("strategy") or "").strip().lower()
    direction = str(position.get("direction") or "").strip().lower()
    futures_venue = str(position.get("futures_venue") or "").strip().lower()
    spot_venue = str(position.get("spot_venue") or "").strip().lower()
    if strategy != "carry":
        return "only carry positions are enabled for server-side close"
    if direction != "forward":
        return "only forward carry positions are enabled for close"
    if futures_venue != spot_venue or futures_venue not in EXECUTION_VENUES:
        return "close is allowlisted only for same-venue Binance/Bybit carry positions"
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/positions")
async def list_positions():
    """List all positions (open + closed)."""
    positions = _read_positions()
    live = _load_positions_fn is not None or _load_cross_positions_fn is not None
    if live and positions:
        positions = _enrich_positions_with_pnl(positions)
    return {"success": True, "data": positions, "live": live}


@router.get("/positions/{position_id}")
async def get_position(position_id: str):
    """Get a single position by ID."""
    positions = _read_positions()
    live = _load_positions_fn is not None or _load_cross_positions_fn is not None

    for pos in positions:
        if pos.get("id") == position_id:
            return {"success": True, "data": pos, "live": live}

    raise HTTPException(status_code=404, detail=f"Position {position_id} not found")


@router.post("/positions/open")
async def open_position(req: OpenPositionRequest):
    """Open only forward, same-venue Binance/Bybit cash-and-carry positions."""
    policy_error = _execution_scope_error(req)
    if policy_error:
        raise HTTPException(status_code=403, detail=policy_error)

    strategy = req.strategy
    direction = str(req.direction or "forward").lower()
    if direction not in ("forward", "reverse"):
        return {"success": False, "error": f"invalid direction {req.direction!r}"}

    if strategy == "pure_futures":
        if not req.long_venue or not req.short_venue:
            return {
                "success": False,
                "error": "long_venue and short_venue required for pure_futures",
            }
        venue_err = _check_venues_tradeable(
            [req.long_venue, req.short_venue], live=not req.dry_run
        )
        if venue_err:
            return {"success": False, "error": venue_err}
        if _load_positions_fn is None:
            return {"success": False, "error": "Executor module unavailable", "live": False}
        try:
            import asyncio

            from execution.pure_futures_executor import open_pure_futures_pair  # noqa: E402

            exec_config, max_mark = _executor_config()
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: open_pure_futures_pair(
                    req.base,
                    req.long_venue,
                    req.short_venue,
                    req.amount_usd,
                    direction=direction,
                    dry_run=req.dry_run,
                    max_mark_spread_pct=max_mark,
                    config=exec_config,
                ),
            )
            return _format_open_result(result, live=not req.dry_run)
        except Exception as e:
            return {"success": False, "error": f"Failed to open position: {e}"}

    if strategy == "carry":
        scanner_candidate: dict[str, Any] | None = None
        scanner_provenance: dict[str, Any] | None = None
        reservation: tuple[str, str] | None = None
        if req.dry_run:
            scanner_candidate, scanner_provenance = _carry_paper_context(
                req, now_ms=int(time.time() * 1000)
            )
            scan_id = str(scanner_provenance["scan_snapshot_id"])
            candidate_id = str(scanner_provenance["candidate_snapshot_id"])
            reservation = (scan_id, candidate_id)
            market_snapshot_id = str(
                scanner_candidate.get("market_snapshot_id")
                or scanner_candidate.get("snapshot_id")
            )
            if reservation in _CARRY_OPEN_IN_PROGRESS or _carry_candidate_already_used(
                scan_id, candidate_id, market_snapshot_id
            ):
                raise HTTPException(
                    status_code=409,
                    detail="this scanner candidate was already used for a paper open; run a new scan",
                )
            futures_v = str(scanner_candidate["venue"])
            spot_v = futures_v
            direction = "forward"
            trade_amount = float(scanner_candidate["notional_usd_requested"])
        else:
            futures_v = (req.futures_venue or "").strip().lower()
            spot_v = (req.spot_venue or "").strip().lower()
            direction = str(req.direction or "forward").lower()
            trade_amount = req.amount_usd

        venue_err = _check_venues_tradeable(
            [futures_v, spot_v], live=not req.dry_run
        )
        if venue_err:
            return {"success": False, "error": venue_err}
        if _open_cross_fn is None:
            return {
                "success": False,
                "error": "Cross-venue executor unavailable",
                "live": False,
            }
        if reservation is not None:
            _CARRY_OPEN_IN_PROGRESS.add(reservation)
        try:
            import asyncio

            executor_options: dict[str, Any] = {}
            if not req.dry_run:
                # The existing carry executor may attempt an intra-venue spot →
                # futures transfer. Pass wrappers that categorically deny it.
                from venues import get_venue  # noqa: E402

                executor_options = {
                    "futures_venue": _NoTransferVenue(
                        get_venue({"venue": {"type": futures_v}})
                    ),
                    "spot_venue": _NoTransferVenue(
                        get_venue({"venue": {"type": spot_v}})
                    ),
                }
            elif scanner_provenance is not None:
                executor_options["scanner_provenance"] = scanner_provenance

            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: _open_cross_fn(
                    scanner_candidate["base"] if scanner_candidate is not None else req.base,
                    direction,  # type: ignore[arg-type]
                    futures_v,
                    spot_v,
                    trade_amount,
                    dry_run=req.dry_run,
                    **executor_options,
                ),
            )
            return _format_open_result(result, live=not req.dry_run)
        except Exception as e:
            return {"success": False, "error": f"Failed to open position: {e}"}
        finally:
            if reservation is not None:
                _CARRY_OPEN_IN_PROGRESS.discard(reservation)

    return {"success": False, "error": f"unknown strategy {strategy!r}"}


def _format_open_result(result: Any, *, live: bool = True) -> dict[str, Any]:
    data = result.to_dict() if hasattr(result, "to_dict") else result
    ok = bool(getattr(result, "ok", True))
    resp: dict[str, Any] = {"success": ok, "data": data, "live": live}
    if not ok:
        logs = getattr(result, "logs", None) or []
        resp["error"] = "; ".join(str(x) for x in logs[-3:]) or "open aborted"
    return resp


@router.post("/positions/{position_id}/close")
async def close_position(position_id: str, req: ClosePositionRequest | None = None):
    """Close a position by ID."""
    positions = _read_positions()
    target = None
    for pos in positions:
        if pos.get("id") == position_id:
            target = pos
            break

    if target is None:
        raise HTTPException(status_code=404, detail=f"Position {position_id} not found")

    if target.get("status") == "closed":
        return {"success": False, "error": "Position already closed"}

    scope_error = _position_scope_error(target)
    if scope_error:
        raise HTTPException(status_code=403, detail=scope_error)

    kind = _position_kind(position_id, positions)
    # Never escalate a paper position to a live close; live closes also require
    # the same process-level kill switch as live opens.
    dry_run = bool(target.get("dry_run", True))
    if not dry_run and not live_trading_enabled():
        raise HTTPException(
            status_code=403,
            detail="live trading is disabled by the process-level kill switch",
        )

    try:
        import asyncio

        loop = asyncio.get_running_loop()

        if kind in ("carry", "unified") and _close_cross_fn is not None:
            result = await loop.run_in_executor(
                None,
                lambda: _close_cross_fn(position_id, dry_run=dry_run),
            )
        elif _close_fn is not None:
            result = await loop.run_in_executor(
                None,
                lambda: _close_fn(position_id),
            )
        else:
            return {
                "success": False,
                "error": "Executor module unavailable, cannot close position",
                "live": False,
            }

        data = result.to_dict() if hasattr(result, "to_dict") else result
        ok = bool(getattr(result, "ok", True))
        resp: dict[str, Any] = {"success": ok, "data": data, "live": not dry_run}
        if not ok:
            logs = getattr(result, "logs", None) or []
            resp["error"] = "; ".join(str(x) for x in logs[-3:]) or "close aborted"
        return resp
    except Exception as e:
        return {"success": False, "error": f"Failed to close position: {e}"}
