"""Durable order journal and fail-closed incident tracking for execution flows.

This module records ambiguous prior operation state and never treats operator
text as exchange reconciliation. REAL submission and live recovery are disabled
by the shared execution policy; existing unresolved records remain blocking.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from core.credentials import redact_secret_values
from core.execution_policy import LIVE_RECOVERY_AVAILABLE, block_real_execution
from core.file_lock import lock_exclusive, unlock


def make_client_order_id(
    operation_id: str, leg: str, action: str, *, attempt: int = 1
) -> str:
    """Return a deterministic, exchange-safe client ID (32 ASCII characters)."""
    source = f"{operation_id}|{leg}|{action}|{int(attempt)}".encode("utf-8")
    return "fa" + hashlib.sha256(source).hexdigest()[:30]


@contextmanager
def _locked_path(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "a+b") as lock_fd:
        lock_exclusive(lock_fd)
        try:
            yield
        finally:
            unlock(lock_fd)


def _fsync_directory(directory: Path) -> None:
    """Best-effort directory fsync after rename, where supported."""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_json_atomic(path: Path, value: Any) -> None:
    """Durably replace a JSON file. Caller must hold the path lock for read-modify-write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _redact_record_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secret_values(value)
    if isinstance(value, list):
        return [_redact_record_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_record_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_record_value(item) for key, item in value.items()}
    return value


def update_json_list(path: Path, update) -> Any:
    """Lock, load, mutate, and atomically persist a JSON-list ledger."""
    with _locked_path(path):
        if path.exists():
            with open(path, encoding="utf-8") as stream:
                rows = json.load(stream)
            if not isinstance(rows, list):
                raise ValueError(f"ledger must contain a JSON list: {path}")
        else:
            rows = []
        result = update(rows)
        write_json_atomic(path, rows)
        return result


class RecoveryUnavailableError(RuntimeError):
    """Raised because this build cannot verify or clear live recovery state."""


class SafeExecutionJournal:
    """Append-only durable journal and incident log associated with a position ledger."""

    LIVE_RECOVERY_AVAILABLE = LIVE_RECOVERY_AVAILABLE

    def __init__(self, ledger_path: Path):
        ledger_path = Path(ledger_path)
        self.ledger_path = ledger_path
        self.orders_path = ledger_path.with_name(f"{ledger_path.stem}.orders.jsonl")
        self.incidents_path = ledger_path.with_name(
            f"{ledger_path.stem}.incidents.jsonl"
        )

    @staticmethod
    def _append(path: Path, record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        safe_record = _redact_record_value(record)
        line = json.dumps(safe_record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        with _locked_path(path):
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(line + "\n")
                stream.flush()
                os.fsync(stream.fileno())

    @staticmethod
    def _read(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        with open(path, encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"corrupt journal {path}:{line_number}") from exc
                if not isinstance(row, dict):
                    raise ValueError(f"invalid journal record {path}:{line_number}")
                records.append(row)
        return records

    def begin_operation(
        self, operation_id: str, kind: str, **details: Any
    ) -> None:
        self._append(
            self.orders_path,
            {
                "event": "operation_started",
                "operation_id": operation_id,
                "kind": kind,
                "at_ms": int(time.time() * 1000),
                **details,
            },
        )

    def finish_operation(self, operation_id: str, outcome: str) -> None:
        """Do not clear live work without an implemented exchange verifier."""
        del operation_id, outcome
        raise RecoveryUnavailableError(
            "live operation clearance is unavailable; verified exchange evidence is required"
        )

    def record_recovery_resolution(
        self,
        operation_id: str,
        *,
        operator: str,
        resolution: str,
        evidence: dict[str, Any],
    ) -> None:
        """Reject text-only clearance; verified live recovery is not implemented."""
        del operation_id, operator, resolution, evidence
        raise RecoveryUnavailableError(
            "live recovery is unavailable; operator-provided text is not exchange evidence"
        )

    def record_order_intent(
        self,
        *,
        operation_id: str,
        leg: str,
        venue_id: str,
        symbol: str,
        action: str,
        requested_qty: float,
        reduce_only: bool,
        client_order_id: str,
        market_type: str = "linear",
        attempt: int = 1,
    ) -> None:
        self._append(
            self.orders_path,
            {
                "event": "order_intent",
                "operation_id": operation_id,
                "leg": leg,
                "venue_id": venue_id,
                "symbol": symbol,
                "action": action,
                "market_type": market_type,
                "requested_qty": requested_qty,
                "reduce_only": bool(reduce_only),
                "client_order_id": client_order_id,
                "attempt": int(attempt),
                "at_ms": int(time.time() * 1000),
            },
        )

    def record_order_result(
        self,
        *,
        operation_id: str,
        leg: str,
        client_order_id: str,
        result: dict[str, Any],
    ) -> None:
        self._append(
            self.orders_path,
            {
                "event": "order_result",
                "operation_id": operation_id,
                "leg": leg,
                "client_order_id": client_order_id,
                "status": str(result.get("status", "unknown")),
                "order_status": str(result.get("order_status", "UNKNOWN")),
                "order_id": result.get("order_id"),
                "requested_qty": result.get("requested_qty"),
                "exec_qty": result.get("exec_qty", 0.0),
                "exec_price": result.get("exec_price"),
                "error": result.get("error"),
                "at_ms": int(time.time() * 1000),
            },
        )

    def record_incident(
        self, operation_id: str, reason: str, details: dict[str, Any]
    ) -> str:
        incident_id = uuid.uuid4().hex
        self._append(
            self.incidents_path,
            {
                "event": "incident_opened",
                "incident_id": incident_id,
                "operation_id": operation_id,
                "status": "recovery_required",
                "reason": reason,
                "details": details,
                "at_ms": int(time.time() * 1000),
            },
        )
        return incident_id

    def blocking_reasons(self) -> list[str]:
        try:
            order_events = self._read(self.orders_path)
            incident_events = self._read(self.incidents_path)
        except (OSError, ValueError) as exc:
            return [f"journal unreadable; recovery required: {redact_secret_values(exc)}"]

        started: dict[str, dict[str, Any]] = {}
        for event in order_events:
            operation_id = str(event.get("operation_id", ""))
            if event.get("event") == "operation_started" and operation_id:
                started[operation_id] = event

        reasons = [
            f"operation {operation_id} ({event.get('kind', 'unknown')}) requires verified recovery"
            for operation_id, event in started.items()
        ]
        open_incidents: dict[str, dict[str, Any]] = {}
        for event in incident_events:
            incident_id = str(event.get("incident_id", ""))
            if not incident_id:
                continue
            if event.get("event") == "incident_opened":
                open_incidents[incident_id] = event

        reasons.extend(
            f"incident {row.get('incident_id')} for {row.get('operation_id')}: {redact_secret_values(row.get('reason', ''))}"
            for row in open_incidents.values()
        )
        return reasons

    def has_unresolved_work(self) -> bool:
        return bool(self.blocking_reasons())

    def recovery_report(self) -> dict[str, Any]:
        """Return persisted unresolved work for operator review; never auto-replays orders."""
        try:
            order_events = self._read(self.orders_path)
            incident_events = self._read(self.incidents_path)
        except (OSError, ValueError) as exc:
            return {
                "blocked": True,
                "live_recovery_available": False,
                "error": redact_secret_values(exc),
                "operations": [],
                "incidents": [],
            }
        operations = [
            _redact_record_value(row)
            for row in order_events
            if row.get("event") == "operation_started"
        ]
        incidents = [
            _redact_record_value(row)
            for row in incident_events
            if row.get("event") == "incident_opened"
        ]
        return {
            "blocked": bool(operations or incidents),
            "live_recovery_available": False,
            "operations": operations,
            "incidents": incidents,
        }


def execute_journaled_trade(
    venue: Any,
    trade: dict[str, Any],
    market: dict[str, dict[str, Any]],
    *,
    journal: SafeExecutionJournal,
    operation_id: str,
    leg: str,
    venue_id: str,
    market_type: str = "linear",
    attempt: int = 1,
) -> list[dict[str, Any]]:
    """Write intent durably, submit once, persist the returned state, and fail closed."""
    block_real_execution("order submission")
    action = str(trade.get("type", "unknown"))
    client_id = make_client_order_id(operation_id, leg, action, attempt=attempt)
    trade["client_order_id"] = client_id
    reduce_only = action.startswith("close_")
    symbol = str(trade.get("pair") or trade.get("symbol", "")).upper()
    requested_qty = float(trade.get("amount_base", 0.0) or 0.0)
    journal.record_order_intent(
        operation_id=operation_id,
        leg=leg,
        venue_id=venue_id,
        symbol=symbol,
        action=action,
        requested_qty=requested_qty,
        reduce_only=reduce_only,
        client_order_id=client_id,
        market_type=market_type,
        attempt=attempt,
    )
    try:
        results = venue.execute_trades([trade], market, dry_run=False)
        result = dict(results[0]) if results else {
            "status": "unknown",
            "order_status": "UNKNOWN",
            "exec_qty": 0.0,
            "error": "venue returned no order result",
        }
    except Exception as exc:
        result = {
            "status": "unknown",
            "order_status": "UNKNOWN",
            "exec_qty": 0.0,
            "error": redact_secret_values(exc),
        }
    result.setdefault("client_order_id", client_id)
    result.setdefault("requested_qty", requested_qty)
    status = str(result.get("status", "unknown")).lower()
    try:
        executed_qty = float(result.get("exec_qty", 0.0) or 0.0)
    except (TypeError, ValueError):
        executed_qty = 0.0
        status = "unknown"
    if not math.isfinite(executed_qty) or executed_qty < 0:
        executed_qty = 0.0
        status = "unknown"
    order_status = str(result.get("order_status", "UNKNOWN")).upper()
    try:
        raw_requested_qty = result.get("requested_qty")
        requested_qty = (
            float(raw_requested_qty) if raw_requested_qty is not None else None
        )
    except (TypeError, ValueError):
        requested_qty = None
        status = "unknown"

    confirmed_no_fill_statuses = {
        "CANCELED",
        "CANCELLED",
        "REJECTED",
        "EXPIRED",
        "EXPIRED_IN_MATCH",
        "DEACTIVATED",
        "NOT_SUBMITTED",
    }
    if status == "filled":
        qty_matches = requested_qty is None or (
            math.isfinite(requested_qty)
            and requested_qty > 0
            and executed_qty + max(1e-12, requested_qty * 1e-8) >= requested_qty
        )
        if order_status != "FILLED" or executed_qty <= 0 or not qty_matches:
            status = "partial" if executed_qty > 0 else "unknown"
            result["error"] = "filled result lacks a matching terminal exchange fill quantity"
    elif status == "failed":
        if executed_qty > 0:
            status = "partial"
            result["error"] = "failed result reports a nonzero executed quantity"
        elif order_status not in confirmed_no_fill_statuses:
            status = "unknown"
            result["error"] = "failed result lacks a confirmed no-fill exchange state"
    elif status not in {"partial", "unknown"}:
        status = "unknown"

    result["status"] = status
    if status in {"partial", "unknown"}:
        journal.record_incident(
            operation_id,
            "order_state_requires_recovery",
            {"leg": leg, "venue_id": venue_id, "symbol": symbol, "result": result},
        )
    journal.record_order_result(
        operation_id=operation_id,
        leg=leg,
        client_order_id=client_id,
        result=result,
    )
    return [result]
