"""Restart-safe ATI forward-shadow ledger over externally refreshed snapshots."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import safety_envelope
from .report import (
    _atomic_write,
    _json_text,
    _jsonl_text,
    _safe_output_dir,
    run_historical_replay,
)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    except (OSError, json.JSONDecodeError):
        return []
    return rows


def _merge_unique(existing: list[dict[str, Any]], incoming: list[dict[str, Any]],
                  key: str) -> list[dict[str, Any]]:
    merged = {str(row[key]): row for row in existing if row.get(key)}
    for row in incoming:
        identifier = str(row.get(key) or "")
        if not identifier:
            continue
        current = merged.get(identifier)
        if current is not None and json.dumps(current, sort_keys=True, default=str) != json.dumps(row, sort_keys=True, default=str):
            raise ValueError(f"ATI_FORWARD_ID_COLLISION:{identifier}")
        merged[identifier] = row
    return sorted(merged.values(), key=lambda row: (str(row.get("decision_ts")), str(row.get(key))))


def _latest_available_at(audits: list[dict[str, Any]]) -> str:
    available: list[datetime] = []
    for audit in audits:
        try:
            opened = datetime.fromisoformat(str(audit["last_timestamp"]).replace("Z", "+00:00"))
            step_ms = int(audit.get("expected_step_ms") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        available.append(opened.astimezone(timezone.utc) + timedelta(milliseconds=step_ms))
    return max(available).isoformat() if available else ""


def _closed_forward_trades(trades: list[dict[str, Any]],
                           forward_ids: set[str]) -> list[dict[str, Any]]:
    return [
        row for row in trades
        if row.get("signal_id") in forward_ids and row.get("outcome_complete") is True
    ]


def run_shadow_once(*, sample_dir: Path | str | None = None,
                    symbols: list[str] | None = None,
                    output_dir: Path | str | None = None,
                    seed: int = 7) -> dict[str, Any]:
    target = _safe_output_dir(output_dir)
    replay = run_historical_replay(
        sample_dir=sample_dir, symbols=symbols, output_dir=target,
        seed=seed, write=True,
    )
    now = datetime.now(timezone.utc)
    base = {
        "schema": "ati_forward_shadow.v2",
        "ran_at": now.isoformat(),
        **safety_envelope(),
    }
    if replay.get("status") == "NEED_DATA":
        state = {**base, "status": "NEED_DATA", "blockers": replay.get("blockers", [])}
        _atomic_write(target / "ati_forward_state.json", _json_text(state))
        return state
    signal_rows = _read_jsonl(target / "ati_signals.jsonl")
    trade_rows = [
        row for row in _read_jsonl(target / "ati_shadow_trades.jsonl")
        if row.get("policy") == "baseline_structural_1_5R"
    ]
    boundary_path = target / "ati_forward_boundary.json"
    boundary = _read_json(boundary_path, {})
    audits = replay.get("data_audits") or []
    latest_source_ts = max((str(item.get("last_timestamp") or "") for item in audits), default="")
    latest_available_at = _latest_available_at(audits)
    if not boundary:
        boundary = {
            "forward_boundary": latest_available_at,
            "frozen_at": now.isoformat(),
            "dataset_snapshot_sha256": replay.get("dataset_snapshot_sha256"),
            "policy_sha256": replay.get("policy", {}).get("policy_sha256"),
        }
        _atomic_write(boundary_path, _json_text(boundary))
    boundary_ts = str(boundary.get("forward_boundary") or "")
    forward_candidates = [
        row for row in signal_rows
        if row.get("decision") == "SHADOW_CANDIDATE" and str(row.get("decision_ts") or "") > boundary_ts
    ]
    forward_ids = {row["signal_id"] for row in forward_candidates}
    closed = _closed_forward_trades(trade_rows, forward_ids)
    closed_ids = {row["signal_id"] for row in closed}
    open_rows = [row for row in forward_candidates if row["signal_id"] not in closed_ids]
    signal_ledger_path = target / "ati_forward_signals.jsonl"
    outcome_ledger_path = target / "ati_forward_outcomes.jsonl"
    signals_ledger = _merge_unique(_read_jsonl(signal_ledger_path), forward_candidates, "signal_id")
    outcomes_ledger = _merge_unique(_read_jsonl(outcome_ledger_path), closed, "signal_id")
    _atomic_write(signal_ledger_path, _jsonl_text(signals_ledger))
    _atomic_write(outcome_ledger_path, _jsonl_text(outcomes_ledger))
    _atomic_write(target / "ati_open_positions.json", _json_text(open_rows))
    try:
        latest = datetime.fromisoformat(latest_source_ts.replace("Z", "+00:00"))
        age_seconds = max(0.0, (now - latest.astimezone(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        age_seconds = None
    stale = age_seconds is None or age_seconds > 30 * 60
    status = "NOT_ACTIONABLE_HISTORICAL" if stale else "SHADOW_OBSERVATION_ONLY"
    state = {
        **base,
        "status": status,
        "forward_boundary": boundary_ts,
        "dataset_last_bar_at": latest_source_ts or None,
        "dataset_available_at": latest_available_at or None,
        "dataset_age_seconds": age_seconds,
        "stale": stale,
        "stale_reason": "dataset_last_bar_older_than_30m" if stale else None,
        "signals_total": len(signals_ledger),
        "new_forward_candidates_seen": len(forward_candidates),
        "open_positions": len(open_rows),
        "closed_outcomes": len(outcomes_ledger),
        "last_error": None,
        "edge_validated": False,
        "actionable": False,
        "recommendation": "WAIT_FOR_FRESH_FORWARD_DATA" if stale else "CONTINUE_SHADOW_OBSERVATION",
    }
    _atomic_write(target / "ati_forward_state.json", _json_text(state))
    health = {
        "status": "DEGRADED" if stale else "HEALTHY",
        "last_run_at": state["ran_at"],
        "age_seconds": 0,
        "signals_total": state["signals_total"],
        "open_positions": state["open_positions"],
        "closed_shadow_trades": state["closed_outcomes"],
        "last_error": None,
        "dataset_last_bar_at": latest_source_ts or None,
        "stale": stale,
        "result_status": status,
        **safety_envelope(),
    }
    _atomic_write(target / "ati_health.json", _json_text(health))
    return state


def render_shadow_text(state: dict[str, Any]) -> str:
    return "\n".join([
        "ATI FORWARD SHADOW V2 START",
        f"status: {state.get('status', 'NEED_DATA')}",
        f"forward_boundary: {state.get('forward_boundary', 'N/A')}",
        f"dataset_last_bar_at: {state.get('dataset_last_bar_at', 'N/A')}",
        f"stale: {str(bool(state.get('stale', True))).lower()}",
        f"signals_total: {state.get('signals_total', 0)}",
        f"open_positions: {state.get('open_positions', 0)}",
        f"closed_outcomes: {state.get('closed_outcomes', 0)}",
        f"recommendation: {state.get('recommendation', 'WAIT_FOR_DATA')}",
        "research_only: true",
        "shadow_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "activation: disabled",
        "final_recommendation: NO LIVE",
        "ATI FORWARD SHADOW V2 END",
    ])


def read_shadow_status(output_dir: Path | str | None = None) -> dict[str, Any]:
    target = _safe_output_dir(output_dir)
    status = _read_json(target / "ati_forward_state.json", None)
    if not isinstance(status, dict):
        return {
            "status": "NO_DATA", "signals_total": 0, "open_positions": 0,
            "closed_outcomes": 0, **safety_envelope(),
        }
    return {**status, **safety_envelope()}


def run_shadow_loop(*, sample_dir: Path | str | None = None,
                    symbols: list[str] | None = None,
                    output_dir: Path | str | None = None,
                    interval_seconds: float = 60.0,
                    max_cycles: int = 1,
                    seed: int = 7) -> dict[str, Any]:
    cycles = 0
    latest: dict[str, Any] = {}
    try:
        while max_cycles <= 0 or cycles < max_cycles:
            latest = run_shadow_once(
                sample_dir=sample_dir, symbols=symbols,
                output_dir=output_dir, seed=seed,
            )
            cycles += 1
            if max_cycles > 0 and cycles >= max_cycles:
                break
            time.sleep(max(5.0, float(interval_seconds)))
    except KeyboardInterrupt:
        latest = {**latest, "stopped_by": "CTRL_C"}
    return {"cycles": cycles, "last_state": latest, **safety_envelope()}
