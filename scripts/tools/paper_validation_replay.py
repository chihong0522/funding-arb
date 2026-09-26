#!/usr/bin/env python3
"""Offline snapshot replay and deterministic paper lifecycle for Binance/Bybit.

The replay path has no network transport. It feeds normalized snapshots into the
repository's real carry scanner and, for genuine scanner candidates, exercises
the existing paper open/close lifecycle through a transport that only consumes
stored order books and instrument rules. Every fill is explicitly simulated;
this module never calls a venue adapter or a real-write endpoint.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

DEFAULT_FEES = {
    "binance": {"spot": 0.10, "perp": 0.05},
    "bybit": {"spot": 0.10, "perp": 0.055},
}
SUPPORTED_VENUES = ("binance", "bybit")


class SnapshotBindingError(ValueError):
    """Raised when a candidate is not bound to the replayed market snapshot."""


def _book_vwap(book: dict[str, Any], side: str, quantity: float) -> dict[str, float]:
    """Load the production VWAP helper only when replay evaluates a fill."""
    from market.carry_scanner import book_vwap

    return book_vwap(book, side, quantity)


def _scan_carry_venue(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Load the production scanner only when a replay actually scans."""
    from market.carry_scanner import scan_carry_venue

    return scan_carry_venue(*args, **kwargs)


def _copy(value: Any) -> Any:
    return copy.deepcopy(value)


def _exchange_part(snapshot: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise ValueError("exchange snapshot must be an object")
    required = {"market_universe", "funding", "books"}
    missing = required - set(snapshot)
    if missing:
        raise ValueError(f"exchange snapshot is missing: {', '.join(sorted(missing))}")
    return snapshot


class SnapshotReplayFundingProvider:
    """FundingProvider-compatible view over one captured exchange snapshot."""

    def __init__(self, exchange_snapshot: dict[str, Any]):
        self.snapshot = _exchange_part(exchange_snapshot)

    def fetch_all(self, quote: str = "USDT") -> list[dict[str, Any]]:
        del quote
        rows = self.snapshot["funding"].get("current")
        return _copy(rows if isinstance(rows, list) else [])

    def fetch_interval_map(self, quote: str = "USDT") -> dict[str, float]:
        del quote
        values = self.snapshot["funding"].get("intervals")
        return _copy(values if isinstance(values, dict) else {})

    def fetch_since(self, symbol: str, start_ms: int, max_pages: int = 10) -> list[dict[str, Any]]:
        del max_pages
        history = self.snapshot["funding"].get("history")
        rows = history.get(symbol.upper(), []) if isinstance(history, dict) else []
        return [
            _copy(row)
            for row in rows
            if isinstance(row, dict) and int(row.get("ts", 0) or 0) > int(start_ms)
        ]


class SnapshotReplayMarketProvider:
    """MarketDataProvider-compatible view over captured instruments and books."""

    def __init__(self, exchange_snapshot: dict[str, Any]):
        self.snapshot = _exchange_part(exchange_snapshot)

    def fetch_market_universe(self, venue: str) -> dict[str, Any]:
        del venue
        return _copy(self.snapshot["market_universe"])

    def fetch_order_book(self, venue: str, market_type: str, symbol: str) -> dict[str, Any]:
        del venue
        kind = "perpetual" if market_type == "linear" else market_type
        books = self.snapshot["books"].get(kind, {})
        if not isinstance(books, dict) or symbol.upper() not in books:
            raise LookupError(f"replayed {kind} book is unavailable for {symbol.upper()}")
        return _copy(books[symbol.upper()])


def _precision_from_step(step: Any, default: int = 6) -> int:
    try:
        decimal = Decimal(str(step))
    except Exception:
        return default
    if not decimal.is_finite() or decimal <= 0:
        return default
    return max(0, min(12, -decimal.as_tuple().exponent))


def _market_limits(exchange_snapshot: dict[str, Any], symbol: str, market_type: str) -> dict[str, Any]:
    universe = exchange_snapshot["market_universe"]
    rows = universe.get(market_type, {})
    if not isinstance(rows, dict) or symbol.upper() not in rows:
        raise SnapshotBindingError(f"replayed {market_type} instrument is unavailable for {symbol.upper()}")
    limits = rows[symbol.upper()]
    if not isinstance(limits, dict):
        raise SnapshotBindingError(f"replayed {market_type} instrument limits are malformed")
    step = float(limits.get("qty_step", 0) or 0)
    if not math.isfinite(step) or step <= 0:
        raise SnapshotBindingError(f"replayed {market_type} quantity step is invalid")
    return {
        "quantity_precision": _precision_from_step(step),
        "quote_precision": 2,
        "min_trade_usdt": float(limits.get("min_notional_usd", 0) or 0),
        "min_trade_base": float(limits.get("min_qty", 0) or 0),
        "qty_step": step,
    }


class SnapshotReplayPaperTransport:
    """Deterministic dry-run venue backed only by one scanner candidate snapshot."""

    def __init__(self, exchange_snapshot: dict[str, Any], candidate: dict[str, Any], *, phase: str):
        if phase not in {"open", "close"}:
            raise ValueError("phase must be open or close")
        self.snapshot = _exchange_part(exchange_snapshot)
        self.candidate = candidate
        self.phase = phase
        self.venue_id = "snapshot-replay"
        self.live_write_calls = 0
        self._validate_binding()

    def _validate_binding(self) -> None:
        symbol = str(self.candidate.get("symbol", "")).upper()
        if not symbol or not isinstance(self.candidate.get("snapshot_id"), str):
            raise SnapshotBindingError("candidate snapshot identity is missing")
        books = self.snapshot["books"]
        for market_type, field in (("spot", "spot_book_snapshot_id"), ("perpetual", "perp_book_snapshot_id")):
            book = books.get(market_type, {}).get(symbol)
            if not isinstance(book, dict) or book.get("snapshot_id") != self.candidate.get(field):
                raise SnapshotBindingError(f"{market_type} book identity does not match candidate snapshot")
        metadata = self.snapshot["market_universe"].get("snapshot_metadata", {})
        for market_type, field in (("spot", "spot_instrument_snapshot_id"), ("perpetual", "perp_instrument_snapshot_id")):
            observed = metadata.get(market_type, {})
            if not isinstance(observed, dict) or observed.get("snapshot_id") != self.candidate.get(field):
                raise SnapshotBindingError(f"{market_type} instrument identity does not match candidate snapshot")

    @property
    def symbol(self) -> str:
        return str(self.candidate["symbol"]).upper()

    @property
    def quantity(self) -> float:
        quantity = float(self.candidate.get("quantity_base", 0) or 0)
        if not math.isfinite(quantity) or quantity <= 0:
            raise SnapshotBindingError("candidate quantity is invalid")
        return quantity

    def _book(self, market_type: str) -> dict[str, Any]:
        book = self.snapshot["books"].get(market_type, {}).get(self.symbol)
        if not isinstance(book, dict):
            raise SnapshotBindingError(f"replayed {market_type} book is unavailable")
        return book

    def _price(self, market_type: str, side: str) -> float:
        phase_field = {
            ("spot", "asks", "open"): "spot_entry_vwap",
            ("spot", "bids", "close"): "spot_exit_vwap",
            ("perpetual", "bids", "open"): "perp_entry_vwap",
            ("perpetual", "asks", "close"): "perp_exit_vwap",
        }.get((market_type, side, self.phase))
        if phase_field:
            try:
                stored = float(self.candidate.get(phase_field))
            except (TypeError, ValueError):
                stored = 0.0
            if math.isfinite(stored) and stored > 0:
                # The executor floors a float quote/price division before
                # applying venue precision. Nudge the open spot ticker by one
                # representable float toward zero so a mathematically exact
                # scanned quantity is not lost to binary round-down. Fills
                # still use the captured book VWAP below, not this ticker.
                if market_type == "spot" and side == "asks" and self.phase == "open":
                    return math.nextafter(stored, 0.0)
                return stored
        return float(_book_vwap(self._book(market_type), side, self.quantity)["vwap"])

    def fetch_symbol_rules(self, pair: str) -> dict[str, Any]:
        del pair
        return _market_limits(self.snapshot, self.symbol, "spot")

    def fetch_futures_symbol_rules(self, pair: str) -> dict[str, Any]:
        del pair
        return _market_limits(self.snapshot, self.symbol, "perpetual")

    def get_ticker(self, pair: str) -> float:
        del pair
        return self._price("spot", "asks" if self.phase == "open" else "bids")

    def get_futures_ticker(self, pair: str) -> float:
        del pair
        return self._price("perpetual", "bids" if self.phase == "open" else "asks")

    def execute_trades(self, trades: list[dict[str, Any]], market: dict[str, Any], *, dry_run: bool) -> list[dict[str, Any]]:
        del market
        if not dry_run:
            self.live_write_calls += 1
            raise RuntimeError("snapshot replay transport refuses non-paper execution")
        results: list[dict[str, Any]] = []
        for trade in trades:
            amount = float(trade.get("amount_base", 0) or 0)
            if abs(amount - self.quantity) > max(1e-12, self.quantity * 1e-8):
                raise SnapshotBindingError(
                    f"paper quantity {amount:.12g} is not bound to scanned {self.quantity:.12g}"
                )
            action = str(trade.get("type", ""))
            if action == "buy":
                market_type, side = "spot", "asks"
            elif action == "sell":
                market_type, side = "spot", "bids"
            elif action == "open_short":
                market_type, side = "perpetual", "bids"
            elif action == "close_short":
                market_type, side = "perpetual", "asks"
            else:
                raise SnapshotBindingError(f"unsupported replay action: {action}")
            fill = _book_vwap(self._book(market_type), side, amount)
            results.append(
                {
                    "symbol": trade.get("symbol"),
                    "status": "simulated",
                    "dry_run": True,
                    "order_status": "REPLAYED_SNAPSHOT",
                    "fill_mode": "replayed_simulated",
                    "market_type": market_type,
                    "book_side": side,
                    "fill_quantity_base": fill["filled_qty"],
                    "exec_qty": amount,
                    "requested_qty": amount,
                    "exec_price": fill["vwap"],
                    "exec_quote_usd": fill["quote_usd"],
                    "snapshot_id": self.candidate["snapshot_id"],
                    "real_order_submitted": False,
                }
            )
        return results


def _candidate_provenance(scan: dict[str, Any], candidate: dict[str, Any], venue: str) -> dict[str, Any]:
    return {
        "scan_snapshot_id": scan["snapshot_id"],
        "candidate_snapshot_id": candidate["snapshot_id"],
        "market_snapshot_id": candidate["snapshot_id"],
        "venue": venue,
        "symbol": candidate["symbol"],
        "direction": "forward",
        "scanned_notional_usd": candidate["notional_usd_requested"],
        "horizon_hours": candidate["horizon_hours"],
        "snapshot_ts_ms": candidate["snapshot_ts_ms"],
        "market_snapshot_ids": {
            "spot_book_snapshot_id": candidate["spot_book_snapshot_id"],
            "perp_book_snapshot_id": candidate["perp_book_snapshot_id"],
            "spot_instrument_snapshot_id": candidate["spot_instrument_snapshot_id"],
            "perp_instrument_snapshot_id": candidate["perp_instrument_snapshot_id"],
        },
        "execution_mode": "paper_snapshot_replay",
        "fee_assumptions": {
            "spot_taker_fee_pct": candidate["spot_fee_pct"],
            "perp_taker_fee_pct": candidate["perp_fee_pct"],
            "fees_are_taker": True,
            "private_fee_api_used": False,
        },
        "candidate_estimate": _copy(candidate),
    }


def _strategy_decision(candidate: dict[str, Any]) -> dict[str, Any]:
    """Apply the paper strategy gate without turning estimates into realized PnL."""
    raw_net = candidate.get("net_horizon_earnings_usd")
    try:
        predicted_net = float(raw_net) if raw_net is not None else float("nan")
    except (TypeError, ValueError):
        predicted_net = float("nan")
    positive = math.isfinite(predicted_net) and predicted_net > 0.0
    return {
        "action": "paper_trade" if positive else "no_trade",
        "reason": "positive_net_estimate" if positive else "predicted_net_not_positive",
        "predicted_net_horizon_earnings_usd": predicted_net if math.isfinite(predicted_net) else None,
        "trade_threshold_usd": 0.0,
        "candidate_snapshot_id": candidate.get("snapshot_id"),
        "symbol": candidate.get("symbol"),
    }


def _lifecycle_for_candidate(
    exchange_snapshot: dict[str, Any],
    venue: str,
    scan: dict[str, Any],
    candidate: dict[str, Any],
    ledger_path: Path,
) -> dict[str, Any]:
    # Keep the offline scanner/replay importable without the optional live
    # venue HTTP stack. The executor is needed only for the explicit paper
    # lifecycle path; --help and scanner-only replay must not import it.
    from execution.cross_venue_executor import (
        close_cross_venue_position,
        load_positions,
        open_cross_venue_position,
    )

    provenance = _candidate_provenance(scan, candidate, venue)
    open_transport = SnapshotReplayPaperTransport(exchange_snapshot, candidate, phase="open")
    trade_usd = float(candidate["spot_notional_usd"])
    try:
        opened = open_cross_venue_position(
            candidate["base"],
            "forward",
            venue,
            venue,
            trade_usd,
            dry_run=True,
            futures_venue=open_transport,
            spot_venue=open_transport,
            positions_path=ledger_path,
            scanner_provenance=provenance,
        )
    except Exception as exc:
        return {
            "status": "error",
            "error_type": type(exc).__name__,
            "error": str(exc)[:240],
            "close": {"status": "not_run"},
            "fill_mode": "replayed_simulated",
            "realized_pnl_claimed": False,
            "real_orders_submitted": open_transport.live_write_calls,
        }
    open_dict = opened.to_dict()
    if not opened.ok:
        return {
            "status": open_dict["state"],
            "open": open_dict,
            "close": {"status": "not_run"},
            "fill_mode": "replayed_simulated",
            "realized_pnl_claimed": False,
            "real_orders_submitted": open_transport.live_write_calls,
        }

    close_transport = SnapshotReplayPaperTransport(exchange_snapshot, candidate, phase="close")
    try:
        closed = close_cross_venue_position(
            opened.position_id,
            dry_run=True,
            futures_venue=close_transport,
            spot_venue=close_transport,
            positions_path=ledger_path,
        )
    except Exception as exc:
        return {
            "status": "close_error",
            "open": open_dict,
            "close": {"status": "error", "error_type": type(exc).__name__, "error": str(exc)[:240]},
            "fill_mode": "replayed_simulated",
            "realized_pnl_claimed": False,
            "real_orders_submitted": open_transport.live_write_calls + close_transport.live_write_calls,
        }
    close_dict = closed.to_dict()
    saved = load_positions(ledger_path)
    persisted_status = next(
        (row.get("status") for row in saved if row.get("id") == opened.position_id),
        None,
    )
    return {
        "status": "completed" if closed.ok and persisted_status == "closed" else "close_not_confirmed",
        "open": open_dict,
        "close": close_dict,
        "persisted_status": persisted_status,
        "fill_mode": "replayed_simulated",
        "realized_pnl_claimed": False,
        "real_orders_submitted": open_transport.live_write_calls + close_transport.live_write_calls,
        "candidate_snapshot_id": candidate["snapshot_id"],
        "scanned_notional_usd": candidate["notional_usd_requested"],
        "paper_trade_notional_usd": trade_usd,
    }


def run_replay_validation(
    snapshots: list[dict[str, Any]],
    *,
    notional_usd: float = 100.0,
    horizon_hours: float = 24.0,
    run_paper_lifecycle: bool = True,
    run_negative_plumbing_lifecycle: bool = False,
    max_candidates_per_scan: int = 5,
    fee_assumptions: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """Run the production scanner and strategy-gated paper replay.

    A non-positive predicted net estimate is always a strategy ``no_trade``.
    ``run_negative_plumbing_lifecycle`` is an explicit engine-test escape hatch;
    any such lifecycle is labeled plumbing-only and is never a strategy result.
    """
    if not snapshots:
        raise ValueError("at least one snapshot is required")
    if not 1 <= max_candidates_per_scan <= 5:
        raise ValueError("max_candidates_per_scan must be in [1, 5]")
    fees = fee_assumptions or DEFAULT_FEES
    scans: list[dict[str, Any]] = []
    candidate_count = 0
    positive_estimate_count = 0
    non_positive_estimate_count = 0
    strategy_trade_count = 0
    strategy_no_trade_count = 0
    lifecycle_attempts = 0
    lifecycle_strategy_attempts = 0
    lifecycle_plumbing_only_attempts = 0
    lifecycle_completed = 0
    lifecycle_strategy_completed = 0
    lifecycle_plumbing_only_completed = 0
    real_orders = 0
    with tempfile.TemporaryDirectory(prefix="paper-validation-") as tmp:
        ledger_dir = Path(tmp)
        for snapshot_number, snapshot in enumerate(snapshots):
            captured_at_ms = int(snapshot.get("captured_at_ms", 0) or 0)
            if captured_at_ms <= 0:
                raise ValueError("snapshot captured_at_ms must be positive")
            exchanges = snapshot.get("exchanges")
            if not isinstance(exchanges, dict):
                raise ValueError("snapshot exchanges must be an object")
            for venue in SUPPORTED_VENUES:
                exchange_snapshot = exchanges.get(venue)
                if not isinstance(exchange_snapshot, dict):
                    scans.append(
                        {
                            "snapshot_number": snapshot_number,
                            "venue": venue,
                            "forward_candidates": [],
                            "strategy_decisions": [],
                            "excluded": [{"symbol": "*", "reason": "exchange_snapshot_unavailable"}],
                            "exclusion_counts": {"exchange_snapshot_unavailable": 1},
                            "paper_lifecycle": {"status": "not_run"},
                        }
                    )
                    continue
                venue_fee = fees.get(venue, DEFAULT_FEES[venue])
                scan = _scan_carry_venue(
                    venue,
                    notional_usd=notional_usd,
                    horizon_hours=horizon_hours,
                    spot_fee_pct=float(venue_fee["spot"]),
                    perp_fee_pct=float(venue_fee["perp"]),
                    funding_provider=SnapshotReplayFundingProvider(exchange_snapshot),
                    market_provider=SnapshotReplayMarketProvider(exchange_snapshot),
                    now_ms=captured_at_ms,
                    max_workers=1,
                )
                candidates = scan.get("forward_candidates", [])
                candidate_count += len(candidates)
                decisions = [_strategy_decision(candidate) for candidate in candidates]
                for decision in decisions:
                    if decision["action"] == "paper_trade":
                        positive_estimate_count += 1
                        strategy_trade_count += 1
                    else:
                        non_positive_estimate_count += 1
                        strategy_no_trade_count += 1
                scan_record: dict[str, Any] = {
                    "snapshot_number": snapshot_number,
                    "captured_at_ms": captured_at_ms,
                    "venue": venue,
                    "snapshot_id": scan.get("snapshot_id"),
                    "total_pairs": scan.get("total_pairs", 0),
                    "intersection_pairs": scan.get("intersection_pairs", 0),
                    "forward_candidates": _copy(candidates),
                    "strategy_decisions": decisions,
                    "excluded": _copy(scan.get("excluded", [])),
                    "exclusion_counts": _copy(scan.get("exclusion_counts", {})),
                    "assumptions": _copy(scan.get("assumptions", {})),
                    "paper_lifecycle": {"status": "not_run"},
                }
                if run_paper_lifecycle and candidates:
                    selected_for_lifecycle: list[tuple[int, dict[str, Any], dict[str, Any], str]] = []
                    for index, (candidate, decision) in enumerate(zip(candidates, decisions)):
                        if decision["action"] == "paper_trade":
                            selected_for_lifecycle.append(
                                (index, candidate, decision, "strategy_candidate")
                            )
                        elif run_negative_plumbing_lifecycle:
                            selected_for_lifecycle.append(
                                (index, candidate, decision, "plumbing_only_negative_estimate")
                            )
                        if len(selected_for_lifecycle) >= max_candidates_per_scan:
                            break
                    if selected_for_lifecycle:
                        lifecycle_rows: list[dict[str, Any]] = []
                        for index, candidate, decision, purpose in selected_for_lifecycle:
                            lifecycle_attempts += 1
                            if purpose == "strategy_candidate":
                                lifecycle_strategy_attempts += 1
                            else:
                                lifecycle_plumbing_only_attempts += 1
                            ledger_path = ledger_dir / f"{venue}-{snapshot_number}-{index}.json"
                            lifecycle = _lifecycle_for_candidate(
                                exchange_snapshot, venue, scan, candidate, ledger_path
                            )
                            lifecycle["execution_purpose"] = purpose
                            lifecycle["strategy_decision"] = decision["action"]
                            lifecycle_rows.append(lifecycle)
                            real_orders += int(lifecycle.get("real_orders_submitted", 0) or 0)
                            if lifecycle.get("status") == "completed":
                                lifecycle_completed += 1
                                if purpose == "strategy_candidate":
                                    lifecycle_strategy_completed += 1
                                else:
                                    lifecycle_plumbing_only_completed += 1
                        scan_record["paper_lifecycle"] = {
                            "status": "completed" if lifecycle_rows else "not_run",
                            "candidates": lifecycle_rows,
                        }
                    elif decisions:
                        scan_record["paper_lifecycle"] = {
                            "status": "not_run_strategy_rejected",
                            "candidates": [],
                        }
                scans.append(scan_record)
    return {
        "schema_version": 2,
        "validation_mode": "public_snapshot_replay",
        "strategy_gate": {
            "positive_net_required": True,
            "non_positive_action": "no_trade",
            "negative_plumbing_escape_hatch": run_negative_plumbing_lifecycle,
        },
        "scans": scans,
        "summary": {
            "snapshot_count": len(snapshots),
            "exchange_scans": len(scans),
            "candidate_count": candidate_count,
            "positive_net_estimate_count": positive_estimate_count,
            "non_positive_net_estimate_count": non_positive_estimate_count,
            "strategy_trade_count": strategy_trade_count,
            "strategy_no_trade_count": strategy_no_trade_count,
            "paper_lifecycle_attempts": lifecycle_attempts,
            "paper_lifecycle_strategy_attempts": lifecycle_strategy_attempts,
            "paper_lifecycle_plumbing_only_attempts": lifecycle_plumbing_only_attempts,
            "paper_lifecycle_completed": lifecycle_completed,
            "paper_lifecycle_strategy_completed": lifecycle_strategy_completed,
            "paper_lifecycle_plumbing_only_completed": lifecycle_plumbing_only_completed,
            "real_orders_submitted": real_orders,
        },
        "fee_assumptions": _copy(fees),
        "no_profitability_claim": True,
        "disclaimer": (
            "All market inputs came from captured public responses. Scanner economics are estimates only. "
            "The strategy does not paper-trade candidates with predicted net <= 0. "
            "Any explicitly enabled negative-estimate lifecycle is plumbing-only. "
            "Paper fills are deterministic replayed simulations, not executed orders, obtainable fills, or realized PnL."
        ),
    }


def load_snapshots(input_dir: Path, *, max_snapshots: int = 3) -> list[dict[str, Any]]:
    paths = sorted(input_dir.glob("snapshot-*.json"))
    if not paths or len(paths) > max_snapshots:
        raise ValueError(f"input must contain 1-{max_snapshots} snapshot-*.json files")
    snapshots: list[dict[str, Any]] = []
    for path in paths:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"snapshot is not an object: {path}")
        snapshots.append(value)
    return snapshots


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--notional-usd", type=float, default=100.0)
    parser.add_argument("--horizon-hours", type=float, default=24.0)
    parser.add_argument("--no-paper-lifecycle", action="store_true")
    parser.add_argument(
        "--run-negative-plumbing-lifecycle",
        action="store_true",
        help="engine-test only: replay non-positive candidates with an explicit plumbing-only label",
    )
    args = parser.parse_args(argv)
    snapshots = load_snapshots(args.input_dir)
    result = run_replay_validation(
        snapshots,
        notional_usd=args.notional_usd,
        horizon_hours=args.horizon_hours,
        run_paper_lifecycle=not args.no_paper_lifecycle,
        run_negative_plumbing_lifecycle=args.run_negative_plumbing_lifecycle,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), **result["summary"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
