#!/usr/bin/env python3
"""Pure futures cross-venue executor for paper simulation and position inspection.

All real orders are rejected by the shared safety boundary. Transfer, borrowing,
rollback, and live recovery are unavailable in this build; historical ambiguous
states remain persisted and block any future live re-enable until verified
reconciliation is implemented.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from core.file_lock import lock_exclusive, unlock  # noqa: E402
from core.credentials import redact_secret_values  # noqa: E402
from core.notify import send_notification  # noqa: E402
from core.execution_policy import block_real_execution, require_dry_run  # noqa: E402
from execution.cross_venue_executor import (  # noqa: E402
    CrossVenueResult,
    _exec_qty,
    _filled,
    _floor_qty,
    _leg_market,
)
from execution.safe_execution import (  # noqa: E402
    SafeExecutionJournal,
    execute_journaled_trade,
    write_json_atomic,
)
from venues import get_venue  # noqa: E402

POSITIONS_PATH = SCRIPTS_DIR / "data" / "pure-futures" / "positions.json"
MAX_MARK_PRICE_SPREAD_PCT = 1.0
MARGIN_BUFFER = 1.05


def load_pure_futures_positions(path: Path = POSITIONS_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise ValueError(f"pure-futures ledger must contain a JSON list of objects: {path}")
    return data


def _save_positions(
    positions: list[dict[str, Any]], path: Path = POSITIONS_PATH
) -> None:
    """Durable atomic write; callers hold the ledger lock."""
    write_json_atomic(path, positions)


def _with_position_lock(path: Path):
    """Acquire exclusive lock on position file for safe concurrent access."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    lock_fd = open(lock_path, "a+b")
    try:
        lock_exclusive(lock_fd)
    except BaseException:
        lock_fd.close()
        raise
    return lock_fd


def _record_position(record: dict[str, Any], path: Path = POSITIONS_PATH) -> None:
    lock_fd = _with_position_lock(path)
    try:
        positions = load_pure_futures_positions(path)
        positions.append(record)
        _save_positions(positions, path)
    finally:
        unlock(lock_fd)
        lock_fd.close()


def _persist_recovery_position(
    position_id: str,
    path: Path,
    updates: dict[str, Any],
) -> None:
    lock_fd = _with_position_lock(path)
    try:
        positions = load_pure_futures_positions(path)
        for row in positions:
            if row.get("id") == position_id:
                row.update(
                    {
                        "status": "recovery_required",
                        "recovery_required": True,
                        "updated_at": int(time.time() * 1000),
                        **updates,
                    }
                )
                _save_positions(positions, path)
                return
        positions.append(
            {
                "id": position_id,
                "status": "recovery_required",
                "dry_run": False,
                "recovery_required": True,
                "updated_at": int(time.time() * 1000),
                **updates,
            }
        )
        _save_positions(positions, path)
    finally:
        unlock(lock_fd)
        lock_fd.close()


def _mark_closed(
    position_id: str, close_info: dict[str, Any], path: Path = POSITIONS_PATH
) -> bool:
    lock_fd = _with_position_lock(path)
    try:
        positions = load_pure_futures_positions(path)
        for p in positions:
            if p.get("id") == position_id and p.get("status") == "open":
                p["status"] = "closed"
                p["closed_at"] = int(time.time() * 1000)
                p["close_info"] = close_info
                _save_positions(positions, path)
                return True
        return False
    finally:
        unlock(lock_fd)
        lock_fd.close()


def _get_open_position(
    position_id: str, path: Path = POSITIONS_PATH
) -> dict[str, Any] | None:
    for p in load_pure_futures_positions(path):
        if p.get("id") == position_id and p.get("status") == "open":
            return p
    return None


def _update_position(
    position_id: str, updates: dict[str, Any], path: Path = POSITIONS_PATH
) -> bool:
    lock_fd = _with_position_lock(path)
    try:
        positions = load_pure_futures_positions(path)
        for p in positions:
            if p.get("id") == position_id and p.get("status") == "open":
                p.update(updates)
                _save_positions(positions, path)
                return True
        return False
    finally:
        unlock(lock_fd)
        lock_fd.close()


def _venue(venue_id: str, injected: Any = None):
    return injected or get_venue({"venue": {"type": venue_id}})


def _check_futures_margin(
    venue: Any, venue_id: str, quote: str, required_usd: float, logs: list[str]
) -> bool:
    """Require pre-funded futures balance; never transfer, redeem, borrow, or fail open."""
    try:
        balances = venue.fetch_usdt_account_balances()
        futures_avail = float(balances.get("futures", 0) or 0)
    except Exception as exc:
        logs.append(f"{venue_id}: futures balance query failed; refusing open ({exc})")
        return False
    if futures_avail < required_usd:
        logs.append(
            f"{venue_id}: insufficient pre-funded futures margin "
            f"available={futures_avail:.2f} need={required_usd:.2f} {quote}"
        )
        return False
    return True


_EARN_PRODUCTS = {"USDT": "964334561256718336"}
_MIN_REDEEM_USDT = 1.0


def _redeem_bitget_earn(coin: str, amount: float, logs: list[str]) -> float:
    """Disabled in safe execution; no automatic earn redemption is permitted."""
    logs.append(f"automatic earn redemption disabled for {coin}")
    return 0.0


def _make_futures_trade(
    base: str, typ: str, qty: float, px: float, qprec: int, reason: str
) -> dict[str, Any]:
    return {
        "symbol": base,
        "type": typ,
        "amount_base": qty,
        "amount_usdt": round(qty * px, 4),
        "quantity_precision": qprec,
        "reason": reason,
    }


def open_pure_futures_pair(
    base: str,
    long_venue_id: str,
    short_venue_id: str,
    trade_usd: float,
    *,
    dry_run: bool = True,
    quote: str = "USDT",
    direction: str = "forward",
    max_mark_spread_pct: float = MAX_MARK_PRICE_SPREAD_PCT,
    config: dict[str, Any] | None = None,
    long_venue: Any = None,
    short_venue: Any = None,
    positions_path: Path = POSITIONS_PATH,
    capital_buffer_pct: float = 0.0,
) -> CrossVenueResult:
    """Open a pure futures funding-spread pair: long perp on one venue, short perp on another.

    capital_buffer_pct: additional margin reservation recommended by settle-mismatch planner
    (% of notional), factored into pre-open balance check.
    """
    require_dry_run(dry_run, "pure-futures open")
    logs: list[str] = []
    executed: list[dict[str, Any]] = []
    journal = SafeExecutionJournal(positions_path)
    if not dry_run and journal.has_unresolved_work():
        return CrossVenueResult(
            False,
            "recovery_required",
            logs=journal.blocking_reasons() + ["new opens are blocked until recovery is reconciled"],
        )
    if not dry_run:
        try:
            load_pure_futures_positions(positions_path)
        except (OSError, ValueError) as exc:
            ledger_incident = f"ledger-{uuid.uuid4().hex}"
            journal.record_incident(
                ledger_incident,
                "position_ledger_unreadable",
                {"path": str(positions_path), "error": redact_secret_values(exc)},
            )
            return CrossVenueResult(
                False,
                "recovery_required",
                logs=[f"position ledger unreadable; refusing live open: {exc}"],
            )
    lv = _venue(long_venue_id, long_venue)
    sv = _venue(short_venue_id, short_venue)

    long_mkt = _leg_market(lv, base, quote, futures=True)
    short_mkt = _leg_market(sv, base, quote, futures=True)
    long_px = float(long_mkt.get("price") or 0.0)
    short_px = float(short_mkt.get("price") or 0.0)
    if long_px <= 0 or short_px <= 0:
        return CrossVenueResult(
            False, "aborted", logs=[f"perp price unavailable long={long_px} short={short_px}"]
        )

    mark_spread_pct = abs(long_px - short_px) / max(long_px, short_px) * 100.0
    if mark_spread_pct > max_mark_spread_pct:
        return CrossVenueResult(
            False,
            "aborted",
            logs=[
                f"Inter-venue perp mark spread {mark_spread_pct:.2f}% > {max_mark_spread_pct}%, rejecting open"
            ],
        )

    qty_prec = min(
        int(long_mkt["quantity_precision"]), int(short_mkt["quantity_precision"])
    )
    ref_px = max(long_px, short_px)
    base_amount = _floor_qty(trade_usd / ref_px, qty_prec)
    if base_amount <= 0:
        return CrossVenueResult(
            False, "aborted", logs=["Quantity floored to 0, trade_usd too small"]
        )
    for leg_name, mkt in (("long", long_mkt), ("short", short_mkt)):
        if trade_usd < mkt["min_trade_usdt"] or base_amount < mkt["min_trade_base"]:
            return CrossVenueResult(
                False,
                "aborted",
                logs=[
                    f"{leg_name} leg below minimum: trade_usd={trade_usd} "
                    f"(min {mkt['min_trade_usdt']}), base={base_amount} (min {mkt['min_trade_base']})"
                ],
            )

    position_id = f"pf-{base.upper()}-{long_venue_id}-{short_venue_id}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    reason = (
        f"Pure-futures spread open {base}: long@{long_venue_id} short@{short_venue_id}"
    )
    long_trade = _make_futures_trade(
        base, "open_long", base_amount, long_px, qty_prec, reason
    )
    short_trade = _make_futures_trade(
        base, "open_short", base_amount, short_px, qty_prec, reason
    )
    long_market = {base: long_mkt}
    short_market = {base: short_mkt}

    if dry_run:
        executed.extend(lv.execute_trades([long_trade], long_market, dry_run=True))
        executed.extend(sv.execute_trades([short_trade], short_market, dry_run=True))
        _record_position(
            {
                "id": position_id,
                "status": "open",
                "strategy": "pure_futures_spread",
                "dry_run": True,
                "base": base,
                "direction": direction,
                "quote": quote,
                "long_venue": long_venue_id,
                "short_venue": short_venue_id,
                "qty": base_amount,
                "long_price": long_px,
                "short_price": short_px,
                "trade_usd": trade_usd,
                "mark_spread_pct": round(mark_spread_pct, 6),
                "opened_at": int(time.time() * 1000),
            },
            positions_path,
        )
        logs.append(
            f"[DRY-RUN] open pure-futures {base} qty={base_amount} long@{long_venue_id} short@{short_venue_id}"
        )
        return CrossVenueResult(True, "simulated", position_id, executed, logs)

    # Order book depth pre-check: skip if insufficient depth within deviation window
    # (small-cap slippage can eat multiple periods of spread profit).
    # Only enabled when config contains pureFuturesArbitrage (injected venue tests skip network).
    pfa_cfg = (config or {}).get("pureFuturesArbitrage") or {}
    if pfa_cfg and bool(pfa_cfg.get("depthCheckEnabled", True)):
        from market.futures_depth import check_pair_depth

        depth_ok, depth_detail = check_pair_depth(
            long_venue_id,
            short_venue_id,
            base,
            trade_usd,
            quote=quote,
            max_dev_pct=float(pfa_cfg.get("depthMaxDevPct", 0.3)),
            min_multiple=float(pfa_cfg.get("depthMinMultiple", 3.0)),
            fail_open=bool(pfa_cfg.get("depthCheckFailOpen", True)),
        )
        logs.append(f"depth check: {depth_detail}")
        if not depth_ok:
            return CrossVenueResult(False, "aborted", logs=logs)

    margin_usd = (
        trade_usd * MARGIN_BUFFER + trade_usd * max(capital_buffer_pct, 0.0) / 100.0
    )
    ok_long = _check_futures_margin(lv, long_venue_id, quote, margin_usd, logs)
    ok_short = _check_futures_margin(sv, short_venue_id, quote, margin_usd, logs)
    if not (ok_long and ok_short):
        # Abort before first order to avoid single-leg fill and rollback
        return CrossVenueResult(False, "aborted", "", executed, logs)
    try:
        for venue, mkt in ((lv, long_mkt), (sv, short_mkt)):
            venue.initialize_futures_symbol(mkt["pair"])
    except Exception as exc:
        return CrossVenueResult(
            False, "aborted", "", executed, [f"futures symbol initialization failed: {exc}"]
        )

    journal.begin_operation(
        position_id,
        "pure_futures_open",
        position_id=position_id,
        base=base,
        long_venue=long_venue_id,
        short_venue=short_venue_id,
    )

    parallel_legs = bool((config or {}).get("parallelLegs", True))

    if parallel_legs:
        # Submit both legs in parallel — saves ~400ms vs sequential.
        # Accept tiny qty mismatch (handled by post-fill rebalance if needed).
        target_qty = _floor_qty(base_amount, qty_prec)
        short_trade["amount_base"] = target_qty
        short_trade["amount_usdt"] = round(target_qty * short_px, 4)

        def _submit_long() -> tuple[str, list[dict[str, Any]]]:
            return "long", execute_journaled_trade(
                lv, long_trade, long_market, journal=journal,
                operation_id=position_id, leg="long", venue_id=long_venue_id
            )

        def _submit_short() -> tuple[str, list[dict[str, Any]]]:
            return "short", execute_journaled_trade(
                sv, short_trade, short_market, journal=journal,
                operation_id=position_id, leg="short", venue_id=short_venue_id
            )

        leg_results: dict[str, list[dict[str, Any]]] = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_long = pool.submit(_submit_long)
            f_short = pool.submit(_submit_short)
            for fut in as_completed([f_long, f_short]):
                try:
                    leg, res = fut.result()
                    leg_results[leg] = res
                except Exception as e:
                    logs.append(f"Parallel order exception: {e}")

        res_long = leg_results.get("long", [])
        res_short = leg_results.get("short", [])
        executed.extend(res_long)
        executed.extend(res_short)

        long_ok = _filled(res_long)
        short_ok = _filled(res_short)

        statuses = {
            str(rows[0].get("status", "unknown")) if rows else "unknown"
            for rows in (res_long, res_short)
        }
        if statuses & {"partial", "unknown"}:
            long_qty = _exec_qty(res_long, 0.0)
            short_qty = _exec_qty(res_short, 0.0)
            journal.record_incident(
                position_id,
                "parallel_leg_requires_recovery",
                {"long_qty": long_qty, "short_qty": short_qty, "statuses": sorted(statuses)},
            )
            _persist_recovery_position(
                position_id,
                positions_path,
                {
                    "strategy": "pure_futures_spread",
                    "base": base,
                    "direction": direction,
                    "quote": quote,
                    "long_venue": long_venue_id,
                    "short_venue": short_venue_id,
                    "long_qty": long_qty,
                    "short_qty": short_qty,
                    "qty": min(long_qty, short_qty),
                    "executed": executed,
                    "reason": "parallel order status unresolved/partial",
                },
            )
            return CrossVenueResult(False, "recovery_required", position_id, executed, logs)

        if long_ok and short_ok:
            exec_qty = _floor_qty(_exec_qty(res_long, 0.0), qty_prec)
            short_qty = _exec_qty(res_short, 0.0)
            if abs(exec_qty - short_qty) > max(1e-12, target_qty * 1e-8):
                journal.record_incident(
                    position_id,
                    "paired_fill_quantity_mismatch",
                    {"long_qty": exec_qty, "short_qty": short_qty},
                )
                _persist_recovery_position(
                    position_id,
                    positions_path,
                    {
                        "strategy": "pure_futures_spread",
                        "base": base,
                        "direction": direction,
                        "quote": quote,
                        "long_venue": long_venue_id,
                        "short_venue": short_venue_id,
                        "long_qty": exec_qty,
                        "short_qty": short_qty,
                        "qty": min(exec_qty, short_qty),
                        "executed": executed,
                        "reason": "confirmed leg quantities differ",
                    },
                )
                return CrossVenueResult(False, "recovery_required", position_id, executed, logs)
            logs.append(f"Parallel both legs filled: long={exec_qty} short={short_qty} {base}")
            _record_position(
                {
                    "id": position_id,
                    "status": "open",
                    "strategy": "pure_futures_spread",
                    "dry_run": False,
                    "base": base,
                    "direction": direction,
                    "quote": quote,
                    "long_venue": long_venue_id,
                    "short_venue": short_venue_id,
                    "qty": min(exec_qty, short_qty),
                    "long_qty": exec_qty,
                    "short_qty": short_qty,
                    "long_price": res_long[0].get("exec_price", long_px),
                    "short_price": res_short[0].get("exec_price", short_px),
                    "trade_usd": trade_usd,
                    "mark_spread_pct": round(mark_spread_pct, 6),
                    "opened_at": int(time.time() * 1000),
                    "parallel_legs": True,
                },
                positions_path,
            )
            journal.finish_operation(position_id, "filled")
            return CrossVenueResult(True, "filled", position_id, executed, logs)
        elif long_ok and not short_ok:
            long_qty = _exec_qty(res_long, 0.0)
            logs.append("Parallel mode: long filled but short failed, rolling back long")
            rollback = _make_futures_trade(
                base,
                "close_long",
                long_qty,
                long_px,
                qty_prec,
                "ROLLBACK: parallel-legs short failed",
            )
            res_rb = execute_journaled_trade(
                lv, rollback, long_market, journal=journal,
                operation_id=position_id, leg="long-rollback", venue_id=long_venue_id
            )
            executed.extend(res_rb)
            if _filled(res_rb) and abs(_exec_qty(res_rb, 0.0) - long_qty) <= max(1e-12, long_qty * 1e-8):
                logs.append("Rollback succeeded, no naked position")
                journal.finish_operation(position_id, "rolled_back")
                return CrossVenueResult(False, "rolled_back", "", executed, logs)
            journal.record_incident(
                position_id,
                "parallel_long_rollback_not_confirmed",
                {"long_qty": long_qty, "rollback_result": res_rb[0] if res_rb else None},
            )
            _persist_recovery_position(
                position_id,
                positions_path,
                {
                    "strategy": "pure_futures_spread", "base": base, "direction": direction,
                    "quote": quote, "long_venue": long_venue_id, "short_venue": short_venue_id,
                    "long_qty": long_qty, "short_qty": 0.0, "qty": long_qty,
                    "executed": executed, "reason": "parallel long rollback not confirmed",
                },
            )
            logs.append("Rollback failed! Long leg requires manual recovery")
            send_notification(
                "NAKED PURE FUTURES POSITION",
                f"Pure-futures parallel rollback failed: {long_venue_id} long {long_qty} {base} unhedged",
                config,
            )
            return CrossVenueResult(False, "recovery_required", position_id, executed, logs)
        elif short_ok and not long_ok:
            short_qty = _exec_qty(res_short, 0.0)
            logs.append("Parallel mode: short filled but long failed, rolling back short")
            rollback = _make_futures_trade(
                base,
                "close_short",
                short_qty,
                short_px,
                qty_prec,
                "ROLLBACK: parallel-legs long failed",
            )
            res_rb = execute_journaled_trade(
                sv, rollback, short_market, journal=journal,
                operation_id=position_id, leg="short-rollback", venue_id=short_venue_id
            )
            executed.extend(res_rb)
            if _filled(res_rb) and abs(_exec_qty(res_rb, 0.0) - short_qty) <= max(1e-12, short_qty * 1e-8):
                logs.append("Rollback succeeded, no naked position")
                journal.finish_operation(position_id, "rolled_back")
                return CrossVenueResult(False, "rolled_back", "", executed, logs)
            journal.record_incident(
                position_id,
                "parallel_short_rollback_not_confirmed",
                {"short_qty": short_qty, "rollback_result": res_rb[0] if res_rb else None},
            )
            _persist_recovery_position(
                position_id,
                positions_path,
                {
                    "strategy": "pure_futures_spread", "base": base, "direction": direction,
                    "quote": quote, "long_venue": long_venue_id, "short_venue": short_venue_id,
                    "long_qty": 0.0, "short_qty": short_qty, "qty": short_qty,
                    "executed": executed, "reason": "parallel short rollback not confirmed",
                },
            )
            logs.append("Rollback failed! Short leg requires manual recovery")
            send_notification(
                "NAKED PURE FUTURES POSITION",
                f"Pure-futures parallel rollback failed: {short_venue_id} short {short_qty} {base} unhedged",
                config,
            )
            return CrossVenueResult(False, "recovery_required", position_id, executed, logs)
        else:
            logs.append("Parallel mode: both legs confirmed no-fill")
            journal.finish_operation(position_id, "aborted_no_fill")
            return CrossVenueResult(False, "aborted", "", executed, logs)

    # Sequential mode is journaled identically, but submits one confirmed leg at a time.
    res_long = execute_journaled_trade(
        lv, long_trade, long_market, journal=journal,
        operation_id=position_id, leg="long", venue_id=long_venue_id
    )
    executed.extend(res_long)
    if not _filled(res_long):
        if res_long and res_long[0].get("status") == "failed":
            journal.finish_operation(position_id, "aborted_no_fill")
            return CrossVenueResult(
                False, "aborted", "", executed,
                [f"Long leg failed: {res_long[0].get('error', 'confirmed no fill')}"],
            )
        long_qty = _exec_qty(res_long, 0.0)
        _persist_recovery_position(
            position_id,
            positions_path,
            {
                "strategy": "pure_futures_spread", "base": base, "direction": direction,
                "quote": quote, "long_venue": long_venue_id, "short_venue": short_venue_id,
                "long_qty": long_qty, "short_qty": 0.0, "qty": long_qty,
                "executed": executed, "reason": "sequential long order state unresolved/partial",
            },
        )
        return CrossVenueResult(False, "recovery_required", position_id, executed, logs)
    exec_qty = _exec_qty(res_long, 0.0)
    logs.append(f"Long leg filled {long_venue_id} open_long {exec_qty} {base}")

    short_trade["amount_base"] = _floor_qty(exec_qty, qty_prec)
    short_trade["amount_usdt"] = round(short_trade["amount_base"] * short_px, 4)
    res_short = execute_journaled_trade(
        sv, short_trade, short_market, journal=journal,
        operation_id=position_id, leg="short", venue_id=short_venue_id
    )
    executed.extend(res_short)
    if _filled(res_short):
        short_qty = _exec_qty(res_short, 0.0)
        if abs(exec_qty - short_qty) > max(1e-12, exec_qty * 1e-8):
            journal.record_incident(
                position_id, "paired_fill_quantity_mismatch",
                {"long_qty": exec_qty, "short_qty": short_qty},
            )
            _persist_recovery_position(
                position_id, positions_path,
                {
                    "strategy": "pure_futures_spread", "base": base, "direction": direction,
                    "quote": quote, "long_venue": long_venue_id, "short_venue": short_venue_id,
                    "long_qty": exec_qty, "short_qty": short_qty, "qty": min(exec_qty, short_qty),
                    "executed": executed, "reason": "confirmed leg quantities differ",
                },
            )
            return CrossVenueResult(False, "recovery_required", position_id, executed, logs)
        _record_position(
            {
                "id": position_id, "status": "open", "strategy": "pure_futures_spread",
                "dry_run": False, "base": base, "direction": direction, "quote": quote,
                "long_venue": long_venue_id, "short_venue": short_venue_id,
                "qty": exec_qty, "long_qty": exec_qty, "short_qty": short_qty,
                "long_price": res_long[0].get("exec_price", long_px),
                "short_price": res_short[0].get("exec_price", short_px),
                "trade_usd": trade_usd, "mark_spread_pct": round(mark_spread_pct, 6),
                "opened_at": int(time.time() * 1000),
            },
            positions_path,
        )
        journal.finish_operation(position_id, "filled")
        return CrossVenueResult(True, "filled", position_id, executed, logs)

    short_status = str(res_short[0].get("status", "unknown")) if res_short else "unknown"
    if short_status in {"partial", "unknown"}:
        _persist_recovery_position(
            position_id, positions_path,
            {
                "strategy": "pure_futures_spread", "base": base, "direction": direction,
                "quote": quote, "long_venue": long_venue_id, "short_venue": short_venue_id,
                "long_qty": exec_qty, "short_qty": _exec_qty(res_short, 0.0), "qty": exec_qty,
                "executed": executed, "reason": f"sequential short order state is {short_status}",
            },
        )
        return CrossVenueResult(False, "recovery_required", position_id, executed, logs)

    rollback = _make_futures_trade(
        base, "close_long", exec_qty, long_px, qty_prec,
        "ROLLBACK: pure-futures short leg failed",
    )
    send_notification(
        "Pure Futures Leg Failure",
        f"{short_venue_id} open_short {base} failed; rolling back {long_venue_id} close_long {exec_qty}",
        config,
    )
    res_rb = execute_journaled_trade(
        lv, rollback, long_market, journal=journal,
        operation_id=position_id, leg="long-rollback", venue_id=long_venue_id
    )
    executed.extend(res_rb)
    if _filled(res_rb) and abs(_exec_qty(res_rb, 0.0) - exec_qty) <= max(1e-12, exec_qty * 1e-8):
        journal.finish_operation(position_id, "rolled_back")
        return CrossVenueResult(False, "rolled_back", "", executed, logs)
    journal.record_incident(
        position_id, "sequential_long_rollback_not_confirmed",
        {"long_qty": exec_qty, "rollback_result": res_rb[0] if res_rb else None},
    )
    _persist_recovery_position(
        position_id, positions_path,
        {
            "strategy": "pure_futures_spread", "base": base, "direction": direction,
            "quote": quote, "long_venue": long_venue_id, "short_venue": short_venue_id,
            "long_qty": exec_qty, "short_qty": 0.0, "qty": exec_qty,
            "executed": executed, "reason": "sequential long rollback not confirmed",
        },
    )
    logs.append("Rollback failed! Long leg requires manual recovery")
    send_notification(
        "NAKED PURE FUTURES POSITION",
        f"Pure-futures rollback failed: {long_venue_id} long {exec_qty} {base} unhedged",
        config,
    )
    return CrossVenueResult(False, "recovery_required", position_id, executed, logs)


def close_pure_futures_pair(
    position_id: str,
    *,
    dry_run: bool | None = None,
    quote: str = "USDT",
    config: dict[str, Any] | None = None,
    long_venue: Any = None,
    short_venue: Any = None,
    positions_path: Path = POSITIONS_PATH,
    warn_spread_widen_pct: float = 0.5,
) -> CrossVenueResult:
    """Close an open pure futures pair. Short leg first; rollback by reopening short if long close fails."""
    pos = _get_open_position(position_id, positions_path)
    if pos is None:
        return CrossVenueResult(
            False, "aborted", logs=[f"open position not found {position_id}"]
        )
    base = str(pos["base"])
    long_id = str(pos["long_venue"])
    short_id = str(pos["short_venue"])
    if dry_run is None:
        dry_run = bool(pos.get("dry_run", True))
    require_dry_run(dry_run, "pure-futures close")

    lv = _venue(long_id, long_venue)
    sv = _venue(short_id, short_venue)
    long_mkt = _leg_market(lv, base, quote, futures=True)
    short_mkt = _leg_market(sv, base, quote, futures=True)
    long_px = float(long_mkt.get("price") or pos.get("long_price") or 0.0)
    short_px = float(short_mkt.get("price") or pos.get("short_price") or 0.0)
    qty = float(
        pos.get("qty")
        or min(float(pos.get("long_qty", 0)), float(pos.get("short_qty", 0)))
    )
    qty_prec = min(
        int(long_mkt["quantity_precision"]), int(short_mkt["quantity_precision"])
    )
    qty = _floor_qty(qty, qty_prec)
    if qty <= 0:
        return CrossVenueResult(False, "aborted", position_id, logs=["invalid position quantity"])

    # Pre-close spread check (warning only; does not block close)
    open_mark_spread = float(pos.get("mark_spread_pct", 0.0))
    close_mark_spread = round(
        abs(long_px - short_px) / max(long_px, short_px) * 100.0, 6
    )
    logs: list[str] = []
    if open_mark_spread > 0 and warn_spread_widen_pct > 0:
        spread_widen = round(close_mark_spread - open_mark_spread, 4)
        if spread_widen > warn_spread_widen_pct:
            logs.append(
                f"WARN spread widened: entry {open_mark_spread:.2f}% → close {close_mark_spread:.2f}% "
                f"(widened {spread_widen:.2f}%, threshold {warn_spread_widen_pct}%)",
            )
    # ---------- end spread check ----------

    reason = f"Pure-futures spread close {position_id}"
    short_close = _make_futures_trade(
        base, "close_short", qty, short_px, qty_prec, reason
    )
    long_close = _make_futures_trade(base, "close_long", qty, long_px, qty_prec, reason)
    short_market = {base: short_mkt}
    long_market = {base: long_mkt}
    executed: list[dict[str, Any]] = []

    if dry_run:
        executed.extend(sv.execute_trades([short_close], short_market, dry_run=True))
        executed.extend(lv.execute_trades([long_close], long_market, dry_run=True))
        _mark_closed(
            position_id,
            {
                "dry_run": True,
                "open_mark_spread": open_mark_spread,
                "close_mark_spread": close_mark_spread,
            },
            positions_path,
        )
        logs.append(f"[DRY-RUN] close pure-futures {base} qty={qty}")
        return CrossVenueResult(True, "simulated", position_id, executed, logs)

    journal = SafeExecutionJournal(positions_path)
    if journal.has_unresolved_work():
        return CrossVenueResult(
            False, "recovery_required", position_id,
            logs=journal.blocking_reasons() + ["close blocked until existing recovery is reconciled"],
        )
    try:
        for venue, market_snapshot in ((sv, short_mkt), (lv, long_mkt)):
            venue.initialize_futures_symbol(market_snapshot["pair"])
    except Exception as exc:
        return CrossVenueResult(
            False, "aborted", position_id, logs=[f"futures symbol initialization failed: {exc}"]
        )

    operation_id = f"{position_id}-close-{uuid.uuid4().hex}"
    journal.begin_operation(operation_id, "pure_futures_close", position_id=position_id)
    res_short = execute_journaled_trade(
        sv, short_close, short_market, journal=journal,
        operation_id=operation_id, leg="short-close", venue_id=short_id,
    )
    executed.extend(res_short)
    if not _filled(res_short):
        status = str(res_short[0].get("status", "unknown")) if res_short else "unknown"
        logs.append(f"Short close state {status}: {res_short[0].get('error') if res_short else 'no result'}")
        if status == "failed":
            journal.finish_operation(operation_id, "close_rejected")
            return CrossVenueResult(False, "aborted", position_id, executed, logs)
        _persist_recovery_position(
            position_id, positions_path,
            {
                "strategy": "pure_futures_spread", "base": base, "direction": pos.get("direction"),
                "quote": quote, "long_venue": long_id, "short_venue": short_id,
                "long_qty": float(pos.get("long_qty", qty)), "short_qty": float(pos.get("short_qty", qty)),
                "qty": qty, "executed": executed, "reason": f"short close state is {status}",
            },
        )
        return CrossVenueResult(False, "recovery_required", position_id, executed, logs)

    closed_short_qty = _exec_qty(res_short, 0.0)
    if abs(closed_short_qty - qty) > max(1e-12, qty * 1e-8):
        journal.record_incident(
            operation_id, "pure_futures_partial_short_close",
            {"requested_qty": qty, "closed_qty": closed_short_qty},
        )
        _persist_recovery_position(
            position_id, positions_path,
            {
                "strategy": "pure_futures_spread", "base": base, "direction": pos.get("direction"),
                "quote": quote, "long_venue": long_id, "short_venue": short_id,
                "long_qty": float(pos.get("long_qty", qty)), "short_qty": max(0.0, qty-closed_short_qty),
                "qty": qty, "executed": executed, "reason": "short close quantity differs from requested",
            },
        )
        return CrossVenueResult(False, "recovery_required", position_id, executed, logs)
    logs.append(f"Short leg closed {short_id} close_short {closed_short_qty} {base}")

    long_close["amount_base"] = _floor_qty(closed_short_qty, qty_prec)
    res_long = execute_journaled_trade(
        lv, long_close, long_market, journal=journal,
        operation_id=operation_id, leg="long-close", venue_id=long_id,
    )
    executed.extend(res_long)
    if _filled(res_long):
        closed_long_qty = _exec_qty(res_long, 0.0)
        if abs(closed_long_qty - closed_short_qty) > max(1e-12, closed_short_qty * 1e-8):
            journal.record_incident(
                operation_id, "paired_close_quantity_mismatch",
                {"short_closed_qty": closed_short_qty, "long_closed_qty": closed_long_qty},
            )
            _persist_recovery_position(
                position_id, positions_path,
                {
                    "strategy": "pure_futures_spread", "base": base, "direction": pos.get("direction"),
                    "quote": quote, "long_venue": long_id, "short_venue": short_id,
                    "long_qty": max(0.0, qty-closed_long_qty), "short_qty": max(0.0, qty-closed_short_qty),
                    "qty": qty, "executed": executed, "reason": "confirmed close quantities differ",
                },
            )
            return CrossVenueResult(False, "recovery_required", position_id, executed, logs)
        marked = _mark_closed(
            position_id,
            {
                "short_price": res_short[0].get("exec_price"),
                "long_price": res_long[0].get("exec_price"),
                "open_mark_spread": open_mark_spread,
                "close_mark_spread": close_mark_spread,
            },
            positions_path,
        )
        if not marked:
            journal.record_incident(
                operation_id, "closed_pair_ledger_update_failed", {"position_id": position_id}
            )
            _persist_recovery_position(
                position_id, positions_path,
                {
                    "strategy": "pure_futures_spread", "base": base, "direction": pos.get("direction"),
                    "quote": quote, "long_venue": long_id, "short_venue": short_id,
                    "long_qty": 0.0, "short_qty": 0.0, "qty": qty,
                    "executed": executed, "reason": "both closes filled but ledger update failed",
                },
            )
            return CrossVenueResult(False, "recovery_required", position_id, executed, logs)
        journal.finish_operation(operation_id, "closed")
        logs.append(f"Both legs closed {base} qty={closed_long_qty}")
        return CrossVenueResult(True, "filled", position_id, executed, logs)

    long_status = str(res_long[0].get("status", "unknown")) if res_long else "unknown"
    if long_status in {"partial", "unknown"}:
        _persist_recovery_position(
            position_id, positions_path,
            {
                "strategy": "pure_futures_spread", "base": base, "direction": pos.get("direction"),
                "quote": quote, "long_venue": long_id, "short_venue": short_id,
                "long_qty": max(0.0, qty-_exec_qty(res_long, 0.0)),
                "short_qty": max(0.0, qty-closed_short_qty), "qty": qty,
                "executed": executed, "reason": f"long close state is {long_status}",
            },
        )
        return CrossVenueResult(False, "recovery_required", position_id, executed, logs)

    # Long close was confirmed no-fill; reopen the short quantity already closed.
    reopen = _make_futures_trade(
        base, "open_short", closed_short_qty, short_px, qty_prec,
        "ROLLBACK: pure-futures long close failed",
    )
    send_notification(
        "Pure Futures Close Rollback",
        f"{long_id} close_long {base} failed; re-opening {short_id} open_short {closed_short_qty}",
        config,
    )
    res_rb = execute_journaled_trade(
        sv, reopen, short_market, journal=journal,
        operation_id=operation_id, leg="short-reopen-rollback", venue_id=short_id,
    )
    executed.extend(res_rb)
    if _filled(res_rb) and abs(_exec_qty(res_rb, 0.0) - closed_short_qty) <= max(1e-12, closed_short_qty * 1e-8):
        journal.finish_operation(operation_id, "rolled_back")
        logs.append("Re-hedged; position remains open")
        return CrossVenueResult(False, "rolled_back", position_id, executed, logs)

    journal.record_incident(
        operation_id, "short_reopen_not_confirmed",
        {"short_qty": closed_short_qty, "rollback_result": res_rb[0] if res_rb else None},
    )
    _persist_recovery_position(
        position_id, positions_path,
        {
            "strategy": "pure_futures_spread", "base": base, "direction": pos.get("direction"),
            "quote": quote, "long_venue": long_id, "short_venue": short_id,
            "long_qty": qty, "short_qty": max(0.0, qty-closed_short_qty), "qty": qty,
            "executed": executed, "reason": "short reopen not confirmed after long close rejection",
        },
    )
    logs.append("Re-hedge failed! Position requires manual recovery")
    send_notification(
        "NAKED PURE FUTURES POSITION",
        f"Pure-futures close rollback failed: {long_id} long {base} exposure requires reconciliation",
        config,
    )
    return CrossVenueResult(False, "recovery_required", position_id, executed, logs)


def close_pure_futures_leg(
    position_id: str,
    leg: str,
    *,
    quote: str = "USDT",
    config: dict[str, Any] | None = None,
    long_venue: Any = None,
    short_venue: Any = None,
    positions_path: Path = POSITIONS_PATH,
    close_reason: str = "single_leg_close",
) -> CrossVenueResult:
    """Close only the specified leg (emergency handling when the other leg has been liquidated/disappeared).

    Placing a close order on a disappeared leg opens a new opposite position, so when both-leg
    state is abnormal, only submit orders for the leg that is still alive. leg ∈ {"long", "short"}.
    """
    block_real_execution("single-leg order close")
    if leg not in ("long", "short"):
        return CrossVenueResult(False, "aborted", logs=[f"invalid leg={leg!r}"])
    pos = _get_open_position(position_id, positions_path)
    if pos is None:
        return CrossVenueResult(
            False, "aborted", logs=[f"open position not found {position_id}"]
        )
    base = str(pos["base"])
    venue_id = str(pos[f"{leg}_venue"])
    v = _venue(venue_id, long_venue if leg == "long" else short_venue)
    mkt = _leg_market(v, base, quote, futures=True)
    px = float(mkt.get("price") or pos.get(f"{leg}_price") or 0.0)
    qty = float(pos.get(f"{leg}_qty", 0) or pos.get("qty", 0))
    qprec = int(mkt["quantity_precision"])
    qty = _floor_qty(qty, qprec)
    if qty <= 0:
        return CrossVenueResult(False, "aborted", position_id, logs=["invalid leg quantity"])

    trade = _make_futures_trade(
        base, f"close_{leg}", qty, px, qprec, f"{close_reason} {position_id}"
    )
    journal = SafeExecutionJournal(positions_path)
    if journal.has_unresolved_work():
        return CrossVenueResult(
            False, "recovery_required", position_id,
            logs=journal.blocking_reasons() + ["single-leg close blocked until recovery is reconciled"],
        )
    try:
        v.initialize_futures_symbol(mkt["pair"])
    except Exception as exc:
        return CrossVenueResult(
            False, "aborted", position_id,
            logs=[f"futures symbol initialization failed: {exc}"],
        )

    operation_id = f"{position_id}-single-close-{leg}-{uuid.uuid4().hex}"
    journal.begin_operation(operation_id, "pure_futures_single_leg_close", position_id=position_id, leg=leg)
    res = execute_journaled_trade(
        v, trade, {base: mkt}, journal=journal, operation_id=operation_id,
        leg=f"{leg}-close", venue_id=venue_id,
    )
    logs: list[str] = []
    if not _filled(res):
        status = str(res[0].get("status", "unknown")) if res else "unknown"
        logs.append(f"{venue_id} close_{leg} state {status}: {res[0].get('error') if res else 'no result'}")
        if status == "failed":
            journal.finish_operation(operation_id, "close_rejected")
            return CrossVenueResult(False, "aborted", position_id, res, logs)
        _persist_recovery_position(
            position_id, positions_path,
            {
                "strategy": "pure_futures_spread", "base": base,
                "long_venue": pos.get("long_venue"), "short_venue": pos.get("short_venue"),
                "qty": float(pos.get("qty", qty)), "executed": res,
                "reason": f"single-leg {leg} close state is {status}",
            },
        )
        send_notification(
            "Single Leg Close Requires Recovery",
            f"Position {position_id} {base}: {venue_id} close_{leg} has unresolved state",
            config,
        )
        return CrossVenueResult(False, "recovery_required", position_id, res, logs)

    closed_qty = _exec_qty(res, 0.0)
    if abs(closed_qty - qty) > max(1e-12, qty * 1e-8):
        journal.record_incident(
            operation_id, "single_leg_close_quantity_mismatch",
            {"requested_qty": qty, "closed_qty": closed_qty},
        )
        _persist_recovery_position(
            position_id, positions_path,
            {
                "strategy": "pure_futures_spread", "base": base,
                "long_venue": pos.get("long_venue"), "short_venue": pos.get("short_venue"),
                "qty": float(pos.get("qty", qty)), "executed": res,
                "reason": "single-leg close quantity differs from requested",
            },
        )
        return CrossVenueResult(False, "recovery_required", position_id, res, logs)

    logs.append(f"{venue_id} close_{leg} {closed_qty} {base} closed (other leg disappeared)")
    marked = _mark_closed(
        position_id,
        {
            "single_leg": leg,
            "reason": close_reason,
            f"{leg}_price": res[0].get("exec_price"),
        },
        positions_path,
    )
    if not marked:
        journal.record_incident(
            operation_id, "single_leg_close_ledger_update_failed", {"position_id": position_id}
        )
        _persist_recovery_position(
            position_id, positions_path,
            {
                "strategy": "pure_futures_spread", "base": base,
                "long_venue": pos.get("long_venue"), "short_venue": pos.get("short_venue"),
                "qty": float(pos.get("qty", qty)), "executed": res,
                "reason": "single-leg close filled but ledger update failed",
            },
        )
        return CrossVenueResult(False, "recovery_required", position_id, res, logs)
    journal.finish_operation(operation_id, "closed_single_leg")
    return CrossVenueResult(True, "filled", position_id, res, logs)


def _leg_qty_from_venue(
    venue: Any, base: str, side: str, quote: str = "USDT"
) -> float | None:
    """Read the actual position quantity for a leg from the exchange API; return None on failure or when not found."""
    try:
        positions = venue.fetch_futures_positions(quote)
    except Exception:
        return None
    base_u = base.upper()
    for p in positions:
        sym = str(p.get("symbol", "")).upper()
        p_side = str(p.get("side", "")).lower()
        if sym.startswith(base_u) and p_side == side:
            return abs(float(p.get("qty", 0) or p.get("amount", 0)))
    return None


def rebalance_pure_futures_pair(
    position_id: str,
    *,
    dry_run: bool | None = None,
    quote: str = "USDT",
    config: dict[str, Any] | None = None,
    long_venue: Any = None,
    short_venue: Any = None,
    positions_path: Path = POSITIONS_PATH,
    long_qty: float | None = None,
    short_qty: float | None = None,
) -> CrossVenueResult:
    """When the two legs have mismatched quantities (partial liquidation/ADL), reduce the larger leg to restore delta neutrality.

    Only trim the oversized leg: partially close the larger leg until it matches the smaller one.
    Do not add to the smaller leg（avoiding extra margin requirements and amplifying slippage/exposure risk）。

    long_qty / short_qty can be injected explicitly (for tests or when an upper layer has already queried);
    otherwise live mode reads from the exchange API, while dry-run uses the position record.
    """
    pos = _get_open_position(position_id, positions_path)
    if pos is None:
        return CrossVenueResult(
            False, "aborted", logs=[f"open position not found {position_id}"]
        )
    base = str(pos["base"])
    long_id = str(pos["long_venue"])
    short_id = str(pos["short_venue"])
    if dry_run is None:
        dry_run = bool(pos.get("dry_run", True))
    require_dry_run(dry_run, "pure-futures rebalance")

    lv = _venue(long_id, long_venue)
    sv = _venue(short_id, short_venue)
    long_mkt = _leg_market(lv, base, quote, futures=True)
    short_mkt = _leg_market(sv, base, quote, futures=True)
    qty_prec = min(
        int(long_mkt["quantity_precision"]), int(short_mkt["quantity_precision"])
    )

    rec_lq = float(pos.get("long_qty", 0) or pos.get("qty", 0))
    rec_sq = float(pos.get("short_qty", 0) or pos.get("qty", 0))
    lq = long_qty if long_qty is not None else rec_lq
    sq = short_qty if short_qty is not None else rec_sq
    if not dry_run:
        if long_qty is None:
            api_lq = _leg_qty_from_venue(lv, base, "long", quote)
            if api_lq is not None:
                lq = api_lq
        if short_qty is None:
            api_sq = _leg_qty_from_venue(sv, base, "short", quote)
            if api_sq is not None:
                sq = api_sq

    logs: list[str] = [f"leg qty: long={lq} short={sq}"]
    executed: list[dict[str, Any]] = []

    if lq <= 0 or sq <= 0:
        # One leg has completely disappeared; rebalance is not meaningful, use emergency close instead
        return CrossVenueResult(
            False,
            "aborted",
            position_id,
            executed,
            logs + ["One leg quantity is 0; handle with close/emergency close"],
        )

    trim_qty = _floor_qty(abs(lq - sq), qty_prec)
    if trim_qty <= 0:
        return CrossVenueResult(
            True, "balanced", position_id, executed, logs + ["Both leg quantities match; no rebalance needed"]
        )

    if lq > sq:
        trim_venue, trim_id, trim_mkt = lv, long_id, long_mkt
        trade_type = "close_long"
    else:
        trim_venue, trim_id, trim_mkt = sv, short_id, short_mkt
        trade_type = "close_short"
    px = float(trim_mkt.get("price") or 0.0)
    trade = _make_futures_trade(
        base,
        trade_type,
        trim_qty,
        px,
        qty_prec,
        f"REBALANCE: trim oversized leg {position_id}",
    )
    market = {base: trim_mkt}

    if dry_run:
        executed.extend(trim_venue.execute_trades([trade], market, dry_run=True))
        logs.append(f"[DRY-RUN] rebalance {trim_id} {trade_type} {trim_qty} {base}")
        return CrossVenueResult(True, "simulated", position_id, executed, logs)

    journal = SafeExecutionJournal(positions_path)
    if journal.has_unresolved_work():
        return CrossVenueResult(
            False, "recovery_required", position_id, executed,
            journal.blocking_reasons() + ["rebalance blocked until recovery is reconciled"],
        )
    try:
        trim_venue.initialize_futures_symbol(trim_mkt["pair"])
    except Exception as exc:
        return CrossVenueResult(
            False, "aborted", position_id, executed,
            logs + [f"futures symbol initialization failed: {exc}"],
        )
    operation_id = f"{position_id}-rebalance-{uuid.uuid4().hex}"
    journal.begin_operation(operation_id, "pure_futures_rebalance", position_id=position_id)
    res = execute_journaled_trade(
        trim_venue, trade, market, journal=journal, operation_id=operation_id,
        leg=f"{trade_type}-trim", venue_id=trim_id,
    )
    executed.extend(res)
    if not _filled(res):
        status = str(res[0].get("status", "unknown")) if res else "unknown"
        logs.append(f"Rebalance state {status}: {res[0].get('error') if res else 'no result'}")
        send_notification(
            "Pure Futures Rebalance Requires Recovery",
            f"Position {position_id} {base}: {trim_id} {trade_type} {trim_qty} has state {status}; "
            f"legs remain skewed (long={lq} short={sq})",
            config,
        )
        if status == "failed":
            journal.finish_operation(operation_id, "aborted_no_fill")
            return CrossVenueResult(False, "aborted", position_id, executed, logs)
        _persist_recovery_position(
            position_id, positions_path,
            {
                "strategy": "pure_futures_spread", "base": base,
                "long_venue": long_id, "short_venue": short_id,
                "long_qty": lq, "short_qty": sq, "qty": min(lq, sq),
                "executed": executed, "reason": f"rebalance order state is {status}",
            },
        )
        return CrossVenueResult(False, "recovery_required", position_id, executed, logs)

    trimmed = _exec_qty(res, 0.0)
    if abs(trimmed - trim_qty) > max(1e-12, trim_qty * 1e-8):
        journal.record_incident(
            operation_id, "rebalance_quantity_mismatch",
            {"requested_qty": trim_qty, "trimmed_qty": trimmed},
        )
        _persist_recovery_position(
            position_id, positions_path,
            {
                "strategy": "pure_futures_spread", "base": base,
                "long_venue": long_id, "short_venue": short_id,
                "long_qty": lq, "short_qty": sq, "qty": min(lq, sq),
                "executed": executed, "reason": "confirmed rebalance quantity differs",
            },
        )
        return CrossVenueResult(False, "recovery_required", position_id, executed, logs)

    new_qty = _floor_qty(min(lq, sq), qty_prec)
    logs.append(f"Rebalance filled {trim_id} {trade_type} {trimmed} {base} → qty={new_qty}")
    updated = _update_position(
        position_id,
        {
            "qty": new_qty,
            "long_qty": new_qty,
            "short_qty": new_qty,
            "last_rebalance": {
                "ts": int(time.time() * 1000),
                "venue": trim_id,
                "type": trade_type,
                "trim_qty": trimmed,
                "before": {"long_qty": lq, "short_qty": sq},
            },
        },
        positions_path,
    )
    if not updated:
        journal.record_incident(
            operation_id, "rebalance_ledger_update_failed", {"position_id": position_id}
        )
        _persist_recovery_position(
            position_id, positions_path,
            {
                "strategy": "pure_futures_spread", "base": base,
                "long_venue": long_id, "short_venue": short_id,
                "long_qty": lq, "short_qty": sq, "qty": new_qty,
                "executed": executed, "reason": "rebalance filled but ledger update failed",
            },
        )
        return CrossVenueResult(False, "recovery_required", position_id, executed, logs)
    journal.finish_operation(operation_id, "rebalanced")
    return CrossVenueResult(True, "filled", position_id, executed, logs)
