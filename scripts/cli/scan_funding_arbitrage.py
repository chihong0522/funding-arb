#!/usr/bin/env python3
"""CLI for the read-only Binance/Bybit forward carry scanner.

Examples:
  python3 scripts/cli/scan_funding_arbitrage.py --venues binance,bybit
  python3 scripts/cli/scan_funding_arbitrage.py --venue bybit --notional-usd 2500
  python3 scripts/cli/scan_funding_arbitrage.py --json --horizon-hours 48
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market.carry_scanner import (  # noqa: E402
    DEFAULT_BASIS_BUFFER_BPS,
    DEFAULT_EXIT_SLIPPAGE_BPS,
    DEFAULT_HORIZON_HOURS,
    DEFAULT_MAX_BOOK_AGE_SEC,
    DEFAULT_NOTIONAL_USD,
    SUPPORTED_VENUES,
    scan_carry_venue,
)

DEFAULT_ENTRY = 0.05  # preserve the former CLI display threshold
DEFAULT_EXIT = 0.01
DEFAULT_UNIVERSE_MIN = 0.03
DEFAULT_BORROW_ANNUAL_PCT = 8.0
DEFAULT_IO_WORKERS = 8


def scan_venue(
    venue: str,
    entry: float = DEFAULT_ENTRY,
    exit_rate: float | None = None,
    universe_min: float | None = None,
    borrow_fallback_annual_pct: float | None = None,
    max_workers: int = 8,
    fee_policy: dict[str, Any] | None = None,
    *,
    notional_usd: float = DEFAULT_NOTIONAL_USD,
    horizon_hours: float = DEFAULT_HORIZON_HOURS,
    max_book_age_sec: float = DEFAULT_MAX_BOOK_AGE_SEC,
    exit_slippage_bps: float = DEFAULT_EXIT_SLIPPAGE_BPS,
    basis_buffer_bps: float = DEFAULT_BASIS_BUFFER_BPS,
    spot_fee_pct: float | None = None,
    perp_fee_pct: float | None = None,
    funding_provider: Any | None = None,
    market_provider: Any | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Backward-compatible entry point; legacy reverse parameters are ignored.

    ``entry`` remains an optional positive-rate display gate for callers using
    the former ticker scanner. The new scanner itself only considers positive
    funding and never creates reverse candidates.
    """
    result = scan_carry_venue(
        venue,
        notional_usd=notional_usd,
        horizon_hours=horizon_hours,
        max_book_age_sec=max_book_age_sec,
        exit_slippage_bps=exit_slippage_bps,
        basis_buffer_bps=basis_buffer_bps,
        spot_fee_pct=spot_fee_pct,
        perp_fee_pct=perp_fee_pct,
        fee_policy=fee_policy,
        max_workers=max_workers,
        funding_provider=funding_provider,
        market_provider=market_provider,
        now_ms=now_ms,
    )
    threshold = max(0.0, float(entry or 0.0))
    all_candidates = result["forward_candidates"]
    result["near_forward"] = [row for row in all_candidates if row["rate_pct"] < threshold]
    result["forward_candidates"] = [row for row in all_candidates if row["rate_pct"] >= threshold]
    result["entry_threshold"] = threshold
    result["exit_threshold"] = exit_rate
    result["universe_min"] = universe_min
    result["forward_no_spot"] = [
        row for row in result["excluded"] if row["reason"] == "spot_market_not_listed"
    ]
    # Preserve the old response shape for consumers while making the forward-
    # only behavior explicit; reverse is intentionally always empty.
    result["reverse_candidates"] = []
    result["reverse_not_borrowable"] = []
    result["negative_all"] = []
    result["positive_all"] = list(all_candidates)
    result["total_pairs"] = result.get("total_pairs", 0)
    return result


def _format_candidate(row: dict[str, Any]) -> str:
    return (
        f"{row['symbol']:16s} rate={row['rate_pct']:+.5f}% "
        f"expected={row['expected_rate_pct']:.5f}%/payment "
        f"interval={row['interval_h']:g}h payments={row['funding_payments_estimated']} "
        f"net={row['net_horizon_earnings_usd']:+.2f} USDT "
        f"breakeven={row['breakeven_funding_payments']} payments"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only executable forward funding carry scan (Binance/Bybit)."
    )
    parser.add_argument(
        "--venue",
        "--venues",
        dest="venues",
        default=",".join(SUPPORTED_VENUES),
        help="Comma-separated Binance/Bybit venue ids (default: binance,bybit).",
    )
    parser.add_argument("--entry", type=float, default=DEFAULT_ENTRY,
                        help="Optional positive funding-rate display threshold (%%).")
    parser.add_argument("--notional-usd", type=float, default=DEFAULT_NOTIONAL_USD,
                        help=f"Maximum per-leg notional in USDT (default {DEFAULT_NOTIONAL_USD:g}).")
    parser.add_argument("--horizon-hours", type=float, default=DEFAULT_HORIZON_HOURS,
                        help=f"Funding projection horizon in hours (default {DEFAULT_HORIZON_HOURS:g}).")
    parser.add_argument("--max-book-age-sec", type=float, default=DEFAULT_MAX_BOOK_AGE_SEC,
                        help=f"Maximum order-book age (default {DEFAULT_MAX_BOOK_AGE_SEC:g}s).")
    parser.add_argument("--exit-slippage-bps", type=float, default=DEFAULT_EXIT_SLIPPAGE_BPS,
                        help="Additional conservative slippage assumption per closing leg.")
    parser.add_argument("--basis-buffer-bps", type=float, default=DEFAULT_BASIS_BUFFER_BPS,
                        help="Conservative exit basis reserve per paired position.")
    parser.add_argument("--spot-fee-pct", type=float, default=None,
                        help="Override spot taker fee in percentage points (e.g. 0.10).")
    parser.add_argument("--perp-fee-pct", type=float, default=None,
                        help="Override perpetual taker fee in percentage points (e.g. 0.05).")
    parser.add_argument("--fee-tier", default="vip0",
                        help="Static VIP fee tier assumption; never queries private fee APIs.")
    parser.add_argument("--workers", type=int, default=8, help="Parallel public-data fetch workers.")
    parser.add_argument("--json", action="store_true", help="Print structured JSON.")
    args = parser.parse_args(argv)

    venues = [v.strip().lower() for v in args.venues.split(",") if v.strip()]
    unsupported = sorted(set(venues) - set(SUPPORTED_VENUES))
    if unsupported:
        parser.error(f"unsupported carry venues: {', '.join(unsupported)}; allowed: {', '.join(SUPPORTED_VENUES)}")
    if not venues:
        parser.error("at least one supported venue is required")
    if args.fee_tier:
        fee_policy = {"mode": "vip_tier", "venue_tiers": {v: args.fee_tier for v in venues}}
    else:
        fee_policy = {"mode": "vip_tier", "venue_tiers": {}}

    results = [
        scan_venue(
            venue,
            entry=args.entry,
            max_workers=args.workers,
            fee_policy=fee_policy,
            notional_usd=args.notional_usd,
            horizon_hours=args.horizon_hours,
            max_book_age_sec=args.max_book_age_sec,
            exit_slippage_bps=args.exit_slippage_bps,
            basis_buffer_bps=args.basis_buffer_bps,
            spot_fee_pct=args.spot_fee_pct,
            perp_fee_pct=args.perp_fee_pct,
        )
        for venue in venues
    ]
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    print("Funding Carry Scanner — Binance/Bybit, same-venue spot + USDT linear perp")
    print(
        f"Notional <= {args.notional_usd:g} USDT/leg | horizon {args.horizon_hours:g}h | "
        f"fees: taker on entry + exit | exit slip {args.exit_slippage_bps:g} bps/leg | "
        f"basis reserve {args.basis_buffer_bps:g} bps"
    )
    for result in results:
        print(f"\n{result['venue'].upper()}: pairs={result['total_pairs']} intersection={result['intersection_pairs']}")
        print(
            f"  Assumed spot/perp taker fees: {result['spot_fee_pct']:.4f}% / "
            f"{result['futures_fee_pct']:.4f}% ({result['fee_source']})"
        )
        if result["forward_candidates"]:
            for row in result["forward_candidates"]:
                print("  " + _format_candidate(row))
        else:
            print("  No executable positive-funding candidates.")
        print(f"  Excluded: {json.dumps(result['exclusion_counts'], sort_keys=True)}")
    print("\n" + results[0]["disclaimer"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
