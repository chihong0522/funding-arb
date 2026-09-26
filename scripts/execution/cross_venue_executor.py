#!/usr/bin/env python3
"""Cross-venue delta-neutral planning and paper execution.

All real orders, transfers, borrowing, and repayment writes are blocked by the
shared safety policy. This module does not provide live rollback or recovery.
"""

from __future__ import annotations

import json
import math
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from core.notify import send_notification  # noqa: E402
from core.credentials import redact_secret_values  # noqa: E402
from core.execution_policy import require_dry_run  # noqa: E402
from execution.safe_execution import (  # noqa: E402
    SafeExecutionJournal,
    execute_journaled_trade,
    update_json_list,
)
from venues import get_venue  # noqa: E402
from venues.base import make_pair  # noqa: E402

Direction = Literal["forward", "reverse"]

POSITIONS_PATH = SCRIPTS_DIR / "data" / "cross-venue" / "positions.json"
# Reject opens when cross-venue spot price spread exceeds this threshold (indicates bad data / un-arbitrageable)
MAX_VENUE_PRICE_SPREAD_PCT = 1.0
MARGIN_BUFFER = 1.05


@dataclass
class CrossVenueResult:
    ok: bool
    state: str  # simulated | filled | rolled_back | naked | aborted
    position_id: str = ""
    executed: list[dict[str, Any]] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "state": self.state,
            "position_id": self.position_id,
            "executed": self.executed,
            "logs": self.logs,
        }


# ── Position records ──────────────────────────────────────────────────────────


def load_positions(path: Path = POSITIONS_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise ValueError(f"cross-venue ledger must contain a JSON list of objects: {path}")
    return data


def _save_positions(
    positions: list[dict[str, Any]], path: Path = POSITIONS_PATH
) -> None:
    def replace_rows(rows: list[dict[str, Any]]) -> None:
        rows[:] = positions

    update_json_list(path, replace_rows)


def _record_position(record: dict[str, Any], path: Path = POSITIONS_PATH) -> None:
    update_json_list(path, lambda positions: positions.append(record))


def _scanner_candidate_key(provenance: Any) -> tuple[str, str]:
    if not isinstance(provenance, dict):
        raise ValueError("scanner provenance must be an object")
    scan_id = provenance.get("scan_snapshot_id")
    candidate_id = provenance.get("candidate_snapshot_id")
    if (
        not isinstance(scan_id, str)
        or not scan_id
        or not isinstance(candidate_id, str)
        or not candidate_id
    ):
        raise ValueError("scanner provenance requires scan and candidate snapshot identities")
    market_snapshot_id = provenance.get("market_snapshot_id")
    if (
        isinstance(market_snapshot_id, bool)
        or not isinstance(market_snapshot_id, (str, int))
        or not str(market_snapshot_id)
    ):
        market_snapshot_id = candidate_id
    return scan_id, str(market_snapshot_id)


def _claim_paper_candidate(record: dict[str, Any], path: Path) -> bool:
    """Atomically consume a scanner candidate and append its durable paper claim.

    ``update_json_list`` uses the same cross-process ledger lock as every other
    position append/update. The claim is written before simulated venue calls,
    so racing workers cannot both simulate and persist the same snapshot.
    """
    provenance = record.get("scanner_provenance")
    if provenance is None:
        raise ValueError("scanner provenance is required for a one-use candidate claim")
    scan_id, candidate_id = _scanner_candidate_key(provenance)

    def claim(rows: list[dict[str, Any]]) -> bool:
        for row in rows:
            existing = row.get("scanner_provenance") if isinstance(row, dict) else None
            existing_candidate_ids = (
                {
                    value
                    for value in (
                        existing.get("candidate_snapshot_id"),
                        existing.get("market_snapshot_id"),
                    )
                    if isinstance(value, str)
                }
                if isinstance(existing, dict)
                else set()
            )
            if (
                isinstance(existing, dict)
                and existing.get("scan_snapshot_id") == scan_id
                and candidate_id in existing_candidate_ids
            ):
                return False
        rows.append(record)
        return True

    return bool(update_json_list(path, claim))


def _update_claimed_paper_position(record: dict[str, Any], path: Path) -> bool:
    """Finalize a previously claimed candidate row under the same ledger lock."""
    position_id = record.get("id")

    def update(rows: list[dict[str, Any]]) -> bool:
        for row in rows:
            if isinstance(row, dict) and row.get("id") == position_id:
                row.update(record)
                return True
        return False

    return bool(update_json_list(path, update))


def _mark_closed(
    position_id: str, close_info: dict[str, Any], path: Path = POSITIONS_PATH
) -> bool:
    def mark(rows: list[dict[str, Any]]) -> bool:
        for row in rows:
            if row.get("id") == position_id and row.get("status") == "open":
                row["status"] = "closed"
                row["closed_at"] = int(time.time() * 1000)
                row["close_info"] = close_info
                return True
        return False

    return bool(update_json_list(path, mark))


# ── Market / rules snapshot ────────────────────────────────────────────────────


def _leg_market(venue: Any, base: str, quote: str, *, futures: bool) -> dict[str, Any]:
    """Single-leg minimal market snapshot: price + precision/min-quantity rules."""
    default_rules = {
        "quantity_precision": 6,
        "quote_precision": 2,
        "min_trade_usdt": 5.0,
        "min_trade_base": 0.0,
    }
    if futures:
        pair = make_pair(base, quote)
        rules = (
            venue.fetch_futures_symbol_rules(pair)
            or venue.fetch_symbol_rules(pair)
            or default_rules
        )
        price = 0.0
        try:
            if hasattr(venue, "get_futures_ticker"):
                price = float(venue.get_futures_ticker(pair) or 0.0)
            else:
                price = float(venue.get_ticker(pair) or 0.0)
        except Exception:
            price = 0.0
        # Fallback: spot ticker if futures ticker returned 0
        if price <= 0:
            try:
                price = float(venue.get_ticker(pair) or 0.0)
            except Exception:
                pass
    else:
        # Spot leg: prefer fetch_asset_market which handles OKX's ETH-USDT format differences;
        # fall back to generic pair query for test fakes / older venue adapters without that method.
        if hasattr(venue, "fetch_asset_market"):
            am = venue.fetch_asset_market(base, quote)
            pair = str(am.get("pair") or make_pair(base, quote))
            rules = (
                am.get("symbol_rules")
                or venue.fetch_symbol_rules(pair)
                or default_rules
            )
            price = float(am.get("price") or 0.0)
        else:
            pair = make_pair(base, quote)
            rules = venue.fetch_symbol_rules(pair) or default_rules
            try:
                price = float(venue.get_ticker(pair) or 0.0)
            except Exception:
                price = 0.0
    return {
        "pair": pair,
        "price": price,
        "quantity_precision": int(rules.get("quantity_precision", 6)),
        "quote_precision": int(rules.get("quote_precision", 2)),
        "min_trade_usdt": float(rules.get("min_trade_usdt", 0) or 0),
        "min_trade_base": float(rules.get("min_trade_base", 0) or 0),
        "rules": {
            "quantity_precision": int(rules.get("quantity_precision", 6)),
            "quote_precision": int(rules.get("quote_precision", 2)),
            "min_trade_usdt": float(rules.get("min_trade_usdt", 0) or 0),
            "min_trade_base": float(rules.get("min_trade_base", 0) or 0),
        },
    }


def _floor_qty(qty: float, precision: int) -> float:
    scale = 10**precision
    return int(qty * scale) / scale


def _filled(results: list[dict[str, Any]]) -> bool:
    if not results:
        return False
    row = results[0]
    if row.get("status") == "simulated":
        return True
    if str(row.get("status", "")).lower() != "filled":
        return False
    try:
        quantity = float(row.get("exec_qty", 0) or 0)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(quantity) or quantity <= 0:
        return False
    raw_target = row.get("requested_qty")
    if raw_target is not None:
        try:
            target = float(raw_target)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(target) or target <= 0:
            return False
        if quantity + max(1e-12, target * 1e-8) < target:
            return False
    return True


def _exec_qty(results: list[dict[str, Any]], fallback: float) -> float:
    if not results:
        return 0.0
    try:
        return max(0.0, float(results[0].get("exec_qty", 0) or 0))
    except (TypeError, ValueError):
        return 0.0


def _persist_recovery_position(
    path: Path,
    *,
    position_id: str,
    base: str,
    direction: str,
    futures_venue: str,
    spot_venue: str,
    fields: dict[str, Any],
) -> None:
    record = {
        "id": position_id,
        "status": "recovery_required",
        "dry_run": False,
        "base": base,
        "direction": direction,
        "futures_venue": futures_venue,
        "spot_venue": spot_venue,
        "recovery_required": True,
        "updated_at": int(time.time() * 1000),
        **fields,
    }

    def upsert(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            if row.get("id") == position_id:
                row.update(record)
                return
        rows.append(record)

    update_json_list(path, upsert)


# ── Open position ──────────────────────────────────────────────────────────────


def open_cross_venue_position(
    base: str,
    direction: Direction,
    futures_venue_id: str,
    spot_venue_id: str,
    trade_usd: float,
    *,
    dry_run: bool = True,
    quote: str = "USDT",
    config: dict[str, Any] | None = None,
    futures_venue: Any = None,
    spot_venue: Any = None,
    positions_path: Path = POSITIONS_PATH,
    scanner_provenance: dict[str, Any] | None = None,
) -> CrossVenueResult:
    """Cross-venue open. forward: spot buy + perp short; reverse: margin borrow-sell + perp long."""
    require_dry_run(dry_run, "cross-venue open")
    logs: list[str] = []
    executed: list[dict[str, Any]] = []
    if direction != "forward":
        return CrossVenueResult(
            False, "aborted", logs=["reverse/borrow execution is disabled in safe mode"]
        )
    if scanner_provenance is not None and not dry_run:
        return CrossVenueResult(
            False,
            "aborted",
            logs=["scanner provenance is accepted only for paper opens"],
        )
    journal = SafeExecutionJournal(positions_path)
    if not dry_run and journal.has_unresolved_work():
        return CrossVenueResult(
            False,
            "recovery_required",
            logs=journal.blocking_reasons() + ["new opens are blocked until recovery is reconciled"],
        )
    if not dry_run:
        try:
            load_positions(positions_path)
        except (OSError, ValueError) as exc:
            journal.record_incident(
                f"ledger-{uuid.uuid4().hex}",
                "position_ledger_unreadable",
                {"path": str(positions_path), "error": redact_secret_values(exc)},
            )
            return CrossVenueResult(
                False,
                "recovery_required",
                logs=[f"position ledger unreadable; refusing live open: {exc}"],
            )
    fv = futures_venue or get_venue({"venue": {"type": futures_venue_id}})
    sv = spot_venue or get_venue({"venue": {"type": spot_venue_id}})

    spot_mkt = _leg_market(sv, base, quote, futures=False)
    fut_mkt = _leg_market(fv, base, quote, futures=True)
    spot_px = spot_mkt["price"]
    fut_px = fut_mkt["price"] or spot_px
    if spot_px <= 0:
        return CrossVenueResult(
            False, "aborted", logs=[f"{spot_venue_id} spot price unavailable"]
        )

    # Spread gate: if cross-venue price deviation is too large, data is likely bad or un-arbitrageable
    if fut_px > 0:
        spread_pct = abs(fut_px - spot_px) / spot_px * 100.0
        if spread_pct > MAX_VENUE_PRICE_SPREAD_PCT:
            return CrossVenueResult(
                False,
                "aborted",
                logs=[
                    f"Cross-venue spread {spread_pct:.2f}% > {MAX_VENUE_PRICE_SPREAD_PCT}%, rejecting open"
                ],
            )


    # Unify quantity across both legs: use the coarser precision of the two to ensure both can place orders
    qty_prec = min(spot_mkt["quantity_precision"], fut_mkt["quantity_precision"])
    base_amount = _floor_qty(trade_usd / spot_px, qty_prec)
    if base_amount <= 0:
        return CrossVenueResult(
            False, "aborted", logs=["Quantity floored to 0, trade_usd too small"]
        )
    for leg_name, mkt in (("spot", spot_mkt), ("futures", fut_mkt)):
        if trade_usd < mkt["min_trade_usdt"] or base_amount < mkt["min_trade_base"]:
            return CrossVenueResult(
                False,
                "aborted",
                logs=[
                    f"{leg_name} leg below minimum: trade_usd={trade_usd} "
                    f"(min {mkt['min_trade_usdt']}), base={base_amount} (min {mkt['min_trade_base']})"
                ],
            )

    spot_trade: dict[str, Any] = {
        "symbol": base,
        "type": "buy" if direction == "forward" else "sell",
        "amount_base": base_amount,
        "amount_usdt": round(base_amount * spot_px, 4),
        "reason": f"Cross-venue {direction} open: fut@{futures_venue_id} spot@{spot_venue_id}",
    }
    fut_trade: dict[str, Any] = {
        "symbol": base,
        "type": "open_short" if direction == "forward" else "open_long",
        "amount_base": base_amount,
        "amount_usdt": round(base_amount * fut_px, 4),
        "quantity_precision": fut_mkt["quantity_precision"],
        "reason": spot_trade["reason"],
    }
    spot_market = {base: spot_mkt}
    fut_market = {base: fut_mkt}
    position_id = f"xv-{int(time.time())}-{uuid.uuid4().hex[:6]}"

    if dry_run:
        position_record = {
            "id": position_id,
            "status": "paper_opening" if scanner_provenance is not None else "open",
            "dry_run": True,
            "base": base,
            "direction": direction,
            "futures_venue": futures_venue_id,
            "spot_venue": spot_venue_id,
            "qty": base_amount,
            "spot_price": spot_px,
            "futures_price": fut_px,
            "trade_usd": trade_usd,
            "attempted_at": int(time.time() * 1000),
            "simulation_execution": {
                "mode": "paper_simulation",
                "status": "pending",
                "requested_trade_usd": trade_usd,
                "qty_base": base_amount,
                "spot": {
                    "venue": spot_venue_id,
                    "pair": spot_mkt["pair"],
                    "ticker_price": spot_px,
                    "rules": dict(spot_mkt["rules"]),
                },
                "perpetual": {
                    "venue": futures_venue_id,
                    "pair": fut_mkt["pair"],
                    "ticker_price": fut_px,
                    "rules": dict(fut_mkt["rules"]),
                },
            },
        }
        if scanner_provenance is not None:
            position_record["scanner_provenance"] = dict(scanner_provenance)
            try:
                claimed = _claim_paper_candidate(position_record, positions_path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                return CrossVenueResult(
                    False,
                    "aborted",
                    logs=[f"paper candidate ledger unavailable; refusing simulation: {exc}"],
                )
            if not claimed:
                return CrossVenueResult(
                    False,
                    "aborted",
                    logs=["scanner candidate was already used for a paper open"],
                )

        try:
            executed.extend(sv.execute_trades([spot_trade], spot_market, dry_run=True))
            executed.extend(fv.execute_trades([fut_trade], fut_market, dry_run=True))
        except Exception as exc:
            if scanner_provenance is not None:
                failed_record = {
                    **position_record,
                    "status": "paper_open_failed",
                    "attempt_completed_at": int(time.time() * 1000),
                    "failure_reason": redact_secret_values(exc),
                }
                failed_record["simulation_execution"] = {
                    **position_record["simulation_execution"],
                    "status": "failed",
                }
                _update_claimed_paper_position(failed_record, positions_path)
            raise
        logs.append(
            f"[DRY-RUN] {direction} {base} qty={base_amount} "
            f"spot@{spot_venue_id}({spot_px:.6g}) fut@{futures_venue_id}({fut_px:.6g})"
        )
        position_record.update(
            {
                "status": "open",
                "opened_at": int(time.time() * 1000),
            }
        )
        position_record["simulation_execution"] = {
            **position_record["simulation_execution"],
            "status": "simulated",
        }
        if scanner_provenance is not None:
            if not _update_claimed_paper_position(position_record, positions_path):
                return CrossVenueResult(
                    False,
                    "aborted",
                    position_id,
                    executed,
                    ["paper candidate claim disappeared before ledger finalization"],
                )
        else:
            _record_position(position_record, positions_path)
        return CrossVenueResult(True, "simulated", position_id, executed, logs)

    # Live requires pre-funded balances; this executor never transfers or borrows.
    margin_usd = base_amount * fut_px * MARGIN_BUFFER
    try:
        fut_balances = fv.fetch_usdt_account_balances()
        spot_balances = sv.fetch_usdt_account_balances()
        if float(fut_balances.get("futures", 0) or 0) < margin_usd:
            return CrossVenueResult(False, "aborted", logs=["pre-funded futures margin is insufficient"])
        if float(spot_balances.get("spot", 0) or 0) < trade_usd:
            return CrossVenueResult(False, "aborted", logs=["pre-funded spot quote balance is insufficient"])
    except Exception as exc:
        return CrossVenueResult(
            False,
            "aborted",
            logs=[f"pre-funded balance verification failed; no order submitted: {exc}"],
        )
    try:
        fv.initialize_futures_symbol(fut_mkt["pair"])
    except Exception as exc:
        return CrossVenueResult(
            False, "aborted", logs=[f"futures symbol initialization failed: {exc}"]
        )

    journal.begin_operation(
        position_id,
        "cross_venue_open",
        position_id=position_id,
        base=base,
        direction=direction,
    )
    res_spot = execute_journaled_trade(
        sv,
        spot_trade,
        spot_market,
        journal=journal,
        operation_id=position_id,
        leg="spot",
        venue_id=spot_venue_id,
        market_type="spot",
    )
    executed.extend(res_spot)
    if not _filled(res_spot):
        logs.append(
            f"Spot leg failed: {res_spot[0].get('error') if res_spot else 'no result'}"
        )
        if res_spot and res_spot[0].get("status") == "failed":
            journal.finish_operation(position_id, "aborted_before_fill")
            return CrossVenueResult(False, "aborted", "", executed, logs)
        _persist_recovery_position(
            positions_path,
            position_id=position_id,
            base=base,
            direction=direction,
            futures_venue=futures_venue_id,
            spot_venue=spot_venue_id,
            fields={
                "spot_qty": _exec_qty(res_spot, 0.0),
                "futures_qty": 0.0,
                "trade_usd": trade_usd,
                "executed": executed,
                "reason": f"spot order state is {res_spot[0].get('status') if res_spot else 'unknown'}",
            },
        )
        return CrossVenueResult(False, "recovery_required", position_id, executed, logs)
    exec_qty = _exec_qty(res_spot, 0.0)
    logs.append(
        f"Spot leg filled {spot_venue_id} {spot_trade['type']} {exec_qty} {base}"
    )

    fut_trade["amount_base"] = _floor_qty(exec_qty, fut_mkt["quantity_precision"])
    if fut_trade["amount_base"] <= 0:
        journal.record_incident(position_id, "spot_fill_below_futures_lot", {"spot_qty": exec_qty})
        _record_position(
            {"id": position_id, "status": "recovery_required", "dry_run": False, "base": base,
             "direction": direction, "futures_venue": futures_venue_id, "spot_venue": spot_venue_id,
             "spot_qty": exec_qty, "futures_qty": 0.0, "reason": "spot fill below futures lot"},
            positions_path,
        )
        return CrossVenueResult(False, "recovery_required", position_id, executed, logs)
    res_fut = execute_journaled_trade(
        fv,
        fut_trade,
        fut_market,
        journal=journal,
        operation_id=position_id,
        leg="futures",
        venue_id=futures_venue_id,
    )
    executed.extend(res_fut)

    if _filled(res_fut):
        futures_qty = _exec_qty(res_fut, 0.0)
        if abs(futures_qty - exec_qty) > max(1e-12, exec_qty * 1e-8):
            journal.record_incident(
                position_id,
                "paired_fill_quantity_mismatch",
                {"spot_qty": exec_qty, "futures_qty": futures_qty},
            )
            _persist_recovery_position(
                positions_path,
                position_id=position_id,
                base=base,
                direction=direction,
                futures_venue=futures_venue_id,
                spot_venue=spot_venue_id,
                fields={
                    "spot_qty": exec_qty,
                    "futures_qty": futures_qty,
                    "trade_usd": trade_usd,
                    "executed": executed,
                    "reason": "confirmed leg fill quantities differ",
                },
            )
            return CrossVenueResult(False, "recovery_required", position_id, executed, logs)
        logs.append(
            f"Futures leg filled {futures_venue_id} {fut_trade['type']} {futures_qty} {base}"
        )
        _record_position(
            {
                "id": position_id,
                "status": "open",
                "dry_run": False,
                "base": base,
                "direction": direction,
                "futures_venue": futures_venue_id,
                "spot_venue": spot_venue_id,
                "qty": futures_qty,
                "spot_qty": exec_qty,
                "futures_qty": futures_qty,
                "spot_price": res_spot[0].get("exec_price", spot_px),
                "futures_price": res_fut[0].get("exec_price", fut_px),
                "trade_usd": trade_usd,
                "opened_at": int(time.time() * 1000),
            },
            positions_path,
        )
        journal.finish_operation(position_id, "filled")
        return CrossVenueResult(True, "filled", position_id, executed, logs)

    # Unknown or partial results need reconciliation before any compensating order.
    result_status = str(res_fut[0].get("status", "unknown")) if res_fut else "unknown"
    if result_status in {"partial", "unknown"}:
        _persist_recovery_position(
            positions_path,
            position_id=position_id,
            base=base,
            direction=direction,
            futures_venue=futures_venue_id,
            spot_venue=spot_venue_id,
            fields={
                "spot_qty": exec_qty,
                "futures_qty": _exec_qty(res_fut, 0.0),
                "trade_usd": trade_usd,
                "executed": executed,
                "reason": f"futures order state is {result_status}",
            },
        )
        return CrossVenueResult(False, "recovery_required", position_id, executed, logs)

    # Confirmed no-fill futures rejection → roll back the confirmed spot fill.
    logs.append(
        f"Futures leg failed: {res_fut[0].get('error') if res_fut else 'no result'}, rolling back spot leg"
    )
    rollback: dict[str, Any] = {
        "symbol": base,
        "type": "sell" if direction == "forward" else "buy",
        "amount_base": exec_qty,
        "amount_usdt": round(exec_qty * spot_px, 4),
        "reason": "ROLLBACK: cross-venue futures leg failed",
    }
    send_notification(
        "Cross-Venue Leg Failure",
        f"{futures_venue_id} {fut_trade['type']} {base} failed; rolling back "
        f"{spot_venue_id} {rollback['type']} {exec_qty} {base}",
        config,
    )
    res_rb = execute_journaled_trade(
        sv,
        rollback,
        spot_market,
        journal=journal,
        operation_id=position_id,
        leg="spot-rollback",
        venue_id=spot_venue_id,
        market_type="spot",
    )
    executed.extend(res_rb)
    if _filled(res_rb):
        logs.append("Rollback succeeded, no naked position")
        journal.finish_operation(position_id, "rolled_back")
        return CrossVenueResult(False, "rolled_back", "", executed, logs)

    journal.record_incident(
        position_id,
        "spot_rollback_not_confirmed",
        {"spot_qty": exec_qty, "rollback_result": res_rb[0] if res_rb else None},
    )
    _persist_recovery_position(
        positions_path,
        position_id=position_id,
        base=base,
        direction=direction,
        futures_venue=futures_venue_id,
        spot_venue=spot_venue_id,
        fields={
            "spot_qty": exec_qty,
            "futures_qty": 0.0,
            "trade_usd": trade_usd,
            "executed": executed,
            "reason": "spot rollback not confirmed",
        },
    )
    logs.append("Rollback failed! Spot leg naked, requires manual handling")
    send_notification(
        "NAKED POSITION",
        f"Cross-venue rollback failed: {spot_venue_id} holds {exec_qty} {base} unhedged",
        config,
    )
    return CrossVenueResult(False, "recovery_required", position_id, executed, logs)


# ── Close position ────────────────────────────────────────────────────────────


def close_cross_venue_position(
    position_id: str,
    *,
    dry_run: bool | None = None,
    quote: str = "USDT",
    config: dict[str, Any] | None = None,
    futures_venue: Any = None,
    spot_venue: Any = None,
    positions_path: Path = POSITIONS_PATH,
) -> CrossVenueResult:
    """Close position per position record. Futures leg closed first (eliminates funding exposure), then spot leg."""
    pos = next(
        (
            p
            for p in load_positions(positions_path)
            if p.get("id") == position_id and p.get("status") == "open"
        ),
        None,
    )
    if pos is None:
        return CrossVenueResult(
            False, "aborted", logs=[f"Open position not found: {position_id}"]
        )

    base = pos["base"]
    direction: Direction = pos["direction"]
    if direction != "forward":
        return CrossVenueResult(
            False,
            "recovery_required",
            position_id,
            logs=["legacy reverse/borrow position requires operator-directed recovery"],
        )
    fv_id, sv_id = pos["futures_venue"], pos["spot_venue"]
    qty = float(pos.get("futures_qty") or pos["qty"])
    spot_qty = float(pos["qty"])
    if dry_run is None:
        dry_run = bool(pos.get("dry_run", True))
    require_dry_run(dry_run, "cross-venue close")

    logs: list[str] = []
    executed: list[dict[str, Any]] = []
    journal = SafeExecutionJournal(positions_path)
    fv = futures_venue or get_venue({"venue": {"type": fv_id}})
    sv = spot_venue or get_venue({"venue": {"type": sv_id}})
    spot_mkt = _leg_market(sv, base, quote, futures=False)
    fut_mkt = _leg_market(fv, base, quote, futures=True)
    spot_px = spot_mkt["price"]

    fut_trade: dict[str, Any] = {
        "symbol": base,
        "type": "close_short" if direction == "forward" else "close_long",
        "amount_base": qty,
        "amount_usdt": round(qty * (fut_mkt["price"] or spot_px), 4),
        "quantity_precision": fut_mkt["quantity_precision"],
        "reason": f"Cross-venue {direction} close {position_id}",
    }
    spot_trade: dict[str, Any] = {
        "symbol": base,
        "type": "sell" if direction == "forward" else "buy",
        "amount_base": spot_qty,
        "amount_usdt": round(spot_qty * spot_px, 4),
        "reason": fut_trade["reason"],
    }
    fut_market = {base: fut_mkt}
    spot_market = {base: spot_mkt}

    if dry_run:
        executed.extend(fv.execute_trades([fut_trade], fut_market, dry_run=True))
        executed.extend(sv.execute_trades([spot_trade], spot_market, dry_run=True))
        _mark_closed(position_id, {"dry_run": True}, positions_path)
        logs.append(f"[DRY-RUN] close {direction} {base} qty={qty}")
        return CrossVenueResult(True, "simulated", position_id, executed, logs)

    if journal.has_unresolved_work():
        return CrossVenueResult(
            False,
            "recovery_required",
            position_id,
            logs=journal.blocking_reasons() + ["close blocked until existing recovery is reconciled"],
        )
    operation_id = f"{position_id}-close-{uuid.uuid4().hex}"
    journal.begin_operation(operation_id, "cross_venue_close", position_id=position_id)
    res_fut = execute_journaled_trade(
        fv,
        fut_trade,
        fut_market,
        journal=journal,
        operation_id=operation_id,
        leg="futures-close",
        venue_id=fv_id,
    )
    executed.extend(res_fut)
    if not _filled(res_fut):
        logs.append(
            f"Futures close failed: {res_fut[0].get('error') if res_fut else 'no result'}"
        )
        if res_fut and res_fut[0].get("status") == "failed":
            journal.finish_operation(operation_id, "close_rejected")
            return CrossVenueResult(False, "aborted", position_id, executed, logs)
        _persist_recovery_position(
            positions_path,
            position_id=position_id,
            base=base,
            direction=direction,
            futures_venue=fv_id,
            spot_venue=sv_id,
            fields={"qty": qty, "close_result": res_fut[0] if res_fut else None},
        )
        return CrossVenueResult(False, "recovery_required", position_id, executed, logs)
    closed_qty = _exec_qty(res_fut, 0.0)
    if abs(closed_qty - qty) > max(1e-12, qty * 1e-8):
        journal.record_incident(
            operation_id,
            "partial_futures_close",
            {"requested_qty": qty, "closed_qty": closed_qty},
        )
        _persist_recovery_position(
            positions_path,
            position_id=position_id,
            base=base,
            direction=direction,
            futures_venue=fv_id,
            spot_venue=sv_id,
            fields={"qty": qty, "futures_closed_qty": closed_qty},
        )
        return CrossVenueResult(False, "recovery_required", position_id, executed, logs)
    logs.append(f"Futures leg closed {fv_id} {fut_trade['type']} {closed_qty} {base}")

    res_spot = execute_journaled_trade(
        sv,
        spot_trade,
        spot_market,
        journal=journal,
        operation_id=operation_id,
        leg="spot-close",
        venue_id=sv_id,
        market_type="spot",
    )
    executed.extend(res_spot)
    if _filled(res_spot):
        spot_closed_qty = _exec_qty(res_spot, 0.0)
        if abs(spot_closed_qty - closed_qty) > max(1e-12, closed_qty * 1e-8):
            journal.record_incident(
                operation_id,
                "paired_close_quantity_mismatch",
                {"futures_closed_qty": closed_qty, "spot_closed_qty": spot_closed_qty},
            )
            _persist_recovery_position(
                positions_path,
                position_id=position_id,
                base=base,
                direction=direction,
                futures_venue=fv_id,
                spot_venue=sv_id,
                fields={
                    "qty": qty,
                    "futures_closed_qty": closed_qty,
                    "spot_closed_qty": spot_closed_qty,
                    "reason": "confirmed close quantities differ",
                },
            )
            return CrossVenueResult(False, "recovery_required", position_id, executed, logs)
        logs.append(
            f"Spot leg closed {sv_id} {spot_trade['type']} {spot_closed_qty} {base}"
        )
        marked = _mark_closed(
            position_id,
            {
                "futures_price": res_fut[0].get("exec_price"),
                "spot_price": res_spot[0].get("exec_price"),
            },
            positions_path,
        )
        if not marked:
            journal.record_incident(
                operation_id, "closed_pair_ledger_update_failed", {"position_id": position_id}
            )
            return CrossVenueResult(False, "recovery_required", position_id, executed, logs)
        journal.finish_operation(operation_id, "closed")
        return CrossVenueResult(True, "filled", position_id, executed, logs)

    spot_status = str(res_spot[0].get("status", "unknown")) if res_spot else "unknown"
    if spot_status in {"partial", "unknown"}:
        _persist_recovery_position(
            positions_path,
            position_id=position_id,
            base=base,
            direction=direction,
            futures_venue=fv_id,
            spot_venue=sv_id,
            fields={
                "qty": qty,
                "futures_closed_qty": closed_qty,
                "spot_closed_qty": _exec_qty(res_spot, 0.0),
                "reason": f"spot close order state is {spot_status}",
            },
        )
        return CrossVenueResult(False, "recovery_required", position_id, executed, logs)

    # Confirmed no-fill spot rejection → reopen the reduced-only futures close.
    logs.append("Spot leg failed, reopening futures leg to hedge")
    reopen = dict(fut_trade)
    reopen["type"] = "open_short"
    reopen["amount_base"] = closed_qty
    reopen["reason"] = "ROLLBACK: cross-venue spot close failed"
    send_notification(
        "Cross-Venue Close Rollback",
        f"{sv_id} {spot_trade['type']} {base} failed; re-opening {fv_id} {reopen['type']} {closed_qty}",
        config,
    )
    res_rb = execute_journaled_trade(
        fv,
        reopen,
        fut_market,
        journal=journal,
        operation_id=operation_id,
        leg="futures-reopen-rollback",
        venue_id=fv_id,
    )
    executed.extend(res_rb)
    if _filled(res_rb):
        logs.append("Re-hedged successfully, position remains open")
        journal.finish_operation(operation_id, "rolled_back")
        return CrossVenueResult(False, "rolled_back", position_id, executed, logs)

    journal.record_incident(
        operation_id,
        "futures_reopen_not_confirmed",
        {"closed_qty": closed_qty, "rollback_result": res_rb[0] if res_rb else None},
    )
    _persist_recovery_position(
        positions_path,
        position_id=position_id,
        base=base,
        direction=direction,
        futures_venue=fv_id,
        spot_venue=sv_id,
        fields={
            "qty": qty,
            "futures_closed_qty": closed_qty,
            "spot_closed_qty": 0.0,
            "reason": "futures reopen not confirmed after spot close rejection",
        },
    )
    logs.append("Re-hedge failed! Spot leg naked, requires manual handling")
    send_notification(
        "NAKED POSITION",
        f"Cross-venue close rollback failed: {sv_id} {base} exposure unhedged",
        config,
    )
    return CrossVenueResult(False, "recovery_required", position_id, executed, logs)
