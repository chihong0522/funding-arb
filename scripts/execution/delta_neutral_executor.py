"""Legacy delta-neutral executor with live submission intentionally disabled.

This path can transfer between accounts, supports margin borrow/repay semantics,
and does not use the durable order journal. Keep its paper preview for
compatibility, but fail closed for every live call until it is ported to the
journaled, pre-funded execution contract.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from core.execution_policy import require_dry_run  # noqa: E402


def _margin_rollback_tags(spot_trade: dict[str, Any]) -> dict[str, Any]:
    """Preserve the legacy helper for callers; live execution using it is disabled."""
    if str(spot_trade.get("account", "")).lower() != "margin":
        return {}
    inverse = {"auto_borrow": "auto_repay", "auto_repay": "auto_borrow"}
    tags: dict[str, Any] = {"account": "margin"}
    effect = inverse.get(str(spot_trade.get("side_effect", "")).lower())
    if effect:
        tags["side_effect"] = effect
    return tags


def execute_delta_neutral_trades(
    venue: Any,
    trades: list[dict[str, Any]],
    market: dict[str, dict[str, Any]],
    dry_run: bool,
    config: dict | None = None,
) -> list[dict[str, Any]]:
    """Return a paper preview, but never submit legacy unjournaled live trades."""
    require_dry_run(dry_run, "legacy delta-neutral execution")
    del venue, config
    if dry_run:
        executed = []
        for trade in trades:
            record = dict(trade)
            record["status"] = "simulated"
            record["price"] = market.get(trade.get("symbol", ""), {}).get("price", 0.0)
            executed.append(record)
        return executed
    return []
