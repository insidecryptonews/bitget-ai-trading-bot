"""Research Cockpit — compact JSON status payload for Dashboard V4.

Aggregates the bot's research-relevant state into a single small JSON
document. Designed for a clean cockpit-style dashboard card without the
"200 lines of raw text" problem.

NO RUNTIME HOOK. Read-only. No exchange calls.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .utils import iso_utc


FINAL_RECOMMENDATION = "NO LIVE"


@dataclass
class CockpitState:
    generated_at: str
    mode: str
    git_commit_short: str
    git_commit_full: str
    health: str
    open_positions: int
    last_backup: str
    ohlcv_status: str
    ohlcv_symbols_with_data: int
    ohlcv_total_rows: int
    latest_backtest_decision: str
    latest_breakdown_decision: str
    latest_policy_decision: str
    policy_ready_for_paper: bool
    safety_flags: dict[str, Any]
    final_recommendation: str = FINAL_RECOMMENDATION
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _git_commit() -> tuple[str, str]:
    try:
        full = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True, timeout=2,
        ).strip()
        short = full[:7] if full else "unknown"
        return short, full
    except Exception:
        return "unknown", "unknown"


def _ohlcv_status_snapshot(db: Any, config: Any) -> tuple[str, int, int]:
    symbols = list(getattr(config, "symbols", []) or [])
    timeframe = str(getattr(config, "main_timeframe", "5m") or "5m").lower()
    if not db or not symbols:
        return "UNKNOWN", 0, 0
    try:
        if not db.table_exists("ohlcv_candles"):
            return "NEED_DATA", 0, 0
    except Exception:
        return "ERROR", 0, 0
    symbols_with_data = 0
    total_rows = 0
    sql = (
        "SELECT COUNT(*) AS cnt FROM ohlcv_candles "
        "WHERE symbol = ? AND timeframe = ?"
    )
    if bool(getattr(db, "_use_postgres", False)):
        sql = sql.replace("?", "%s")
    for symbol in symbols:
        try:
            with db._connect() as conn:
                row = conn.execute(sql, (str(symbol).upper(), timeframe)).fetchone()
            cnt = int(db._row_value(row, "cnt", 0, 0) or 0) if row else 0
        except Exception:
            cnt = 0
        if cnt > 0:
            symbols_with_data += 1
            total_rows += cnt
    status = "OK" if symbols_with_data == len(symbols) else (
        "PARTIAL" if symbols_with_data > 0 else "NEED_DATA"
    )
    return status, symbols_with_data, total_rows


def _safety_flags(config: Any) -> dict[str, Any]:
    return {
        "LIVE_TRADING": bool(getattr(config, "live_trading", False)),
        "DRY_RUN": bool(getattr(config, "dry_run", True)),
        "PAPER_TRADING": bool(getattr(config, "paper_trading", True)),
        "ENABLE_PAPER_POLICY_FILTER": bool(getattr(config, "enable_paper_policy_filter", False)),
        "ENABLE_CANDIDATE_SHADOW_MONITOR": bool(getattr(config, "enable_candidate_shadow_monitor", False)),
        "can_send_real_orders": bool(getattr(config, "can_send_real_orders", False)),
    }


def _open_positions(db: Any) -> int:
    if not db:
        return 0
    try:
        rows = db.get_open_paper_positions_summary(limit=100)
        return len(rows) if rows else 0
    except Exception:
        return 0


def _last_backup(db: Any) -> str:
    """Best-effort lookup of last vault backup. Falls back to empty string."""
    if not db:
        return ""
    try:
        # Optional method; if not present, return empty.
        if hasattr(db, "get_state"):
            value = db.get_state("data_vault_status", {})
            if isinstance(value, dict):
                return str(value.get("latest_remote_backup") or value.get("latest_local_backup") or "")
    except Exception:
        return ""
    return ""


def build_cockpit_state(
    config: Any,
    db: Any,
    *,
    mode: str = "paper",
    latest_backtest_decision: str = "UNKNOWN",
    latest_breakdown_decision: str = "UNKNOWN",
    latest_policy_decision: str = "UNKNOWN",
) -> CockpitState:
    short, full = _git_commit()
    ohlcv_status, syms_with_data, total_rows = _ohlcv_status_snapshot(db, config)
    flags = _safety_flags(config)
    policy_ready = latest_policy_decision == "POLICY_READY_FOR_PAPER"
    notes = []
    if policy_ready:
        notes.append("policy_ready_but_paper_filter_must_be_activated_by_human")
    if not policy_ready and latest_policy_decision in {"NO_EDGE_FOUND", "NEED_MORE_DATA"}:
        notes.append("no_actionable_candidate_currently")
    return CockpitState(
        generated_at=iso_utc(),
        mode=mode,
        git_commit_short=short,
        git_commit_full=full,
        health="OK",
        open_positions=_open_positions(db),
        last_backup=_last_backup(db),
        ohlcv_status=ohlcv_status,
        ohlcv_symbols_with_data=syms_with_data,
        ohlcv_total_rows=total_rows,
        latest_backtest_decision=latest_backtest_decision,
        latest_breakdown_decision=latest_breakdown_decision,
        latest_policy_decision=latest_policy_decision,
        policy_ready_for_paper=policy_ready,
        safety_flags=flags,
        notes=notes,
    )


def render_cockpit_text(state: CockpitState) -> str:
    lines = ["RESEARCH COCKPIT START"]
    lines.append(f"generated_at: {state.generated_at}")
    lines.append(f"mode: {state.mode}")
    lines.append(f"git_commit_short: {state.git_commit_short}")
    lines.append(f"health: {state.health}")
    lines.append(f"open_positions: {state.open_positions}")
    lines.append(f"last_backup: {state.last_backup or 'unknown'}")
    lines.append(f"ohlcv_status: {state.ohlcv_status}")
    lines.append(f"ohlcv_symbols_with_data: {state.ohlcv_symbols_with_data}")
    lines.append(f"ohlcv_total_rows: {state.ohlcv_total_rows}")
    lines.append(f"latest_backtest_decision: {state.latest_backtest_decision}")
    lines.append(f"latest_breakdown_decision: {state.latest_breakdown_decision}")
    lines.append(f"latest_policy_decision: {state.latest_policy_decision}")
    lines.append(f"policy_ready_for_paper: {str(state.policy_ready_for_paper).lower()}")
    lines.append("safety_flags:")
    for key, value in state.safety_flags.items():
        lines.append(f"- {key}={value}")
    if state.notes:
        lines.append("notes:")
        for note in state.notes:
            lines.append(f"- {note}")
    lines.append("paper_filter_enabled: false")
    lines.append("can_send_real_orders: false")
    lines.append("auto_activation: never")
    lines.append("research_only: true")
    lines.append(f"final_recommendation: {state.final_recommendation}")
    lines.append("RESEARCH COCKPIT END")
    return "\n".join(lines)


def export_cockpit_json(state: CockpitState) -> str:
    return json.dumps(state.as_dict(), indent=2, default=str)
