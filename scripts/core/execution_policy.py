"""Shared fail-closed policy for REAL exchange writes.

Live orders, internal transfers, withdrawals, borrowing, and repayments are
intentionally unavailable in this build. No environment variable, API setting,
CLI flag, or injected venue can enable them. Paper simulation and public reads
remain supported.
"""
from __future__ import annotations

from typing import NoReturn


class LiveExecutionDisabled(RuntimeError):
    """Raised when a caller attempts a REAL exchange-side mutation."""


LIVE_EXECUTION_ENABLED = False
LIVE_RECOVERY_AVAILABLE = False


def block_real_execution(operation: str = "exchange writes") -> NoReturn:
    """Unconditionally reject a REAL exchange-side mutation.

    ``operation`` is deliberately not included in the exception so untrusted
    identifiers or credential-bearing input can never be reflected into logs.
    """
    del operation
    raise LiveExecutionDisabled(
        "REAL exchange writes are disabled by the shared safety policy; "
        "paper mode and public data remain available"
    )


def require_dry_run(dry_run: bool, operation: str = "orders") -> None:
    """Fail before any work when an execution boundary is asked to run live."""
    if not dry_run:
        block_real_execution(operation)
