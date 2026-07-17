"""Restart-safe ATI forward-shadow ledger over externally refreshed snapshots."""

from __future__ import annotations

import json
import math
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
    source_snapshot_status,
)


# Replay indexes and source receipts may legitimately move when the same
# canonical dataset is regenerated. They are provenance, not signal identity.
# Keep this allowlist deliberately small: every other field remains collision
# protected.
_VOLATILE_REPLAY_FIELDS = frozenset({
    "dataset_source", "signal_idx", "entry_idx", "exit_idx",
})
_LATE_MATURING_DIAGNOSTIC_PREFIXES = ("gross_return_", "net_return_")
_PAPER_FEED_MAX_DECISION_AGE_SECONDS = 30 * 60
_CANONICAL_FORWARD_OUTCOME_POLICY = "baseline_structural_1_5R"


def _semantic_equal(left: Any, right: Any) -> bool:
    """Strict structural equality with machine-noise tolerance for floats."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        a, b = float(left), float(right)
        return math.isfinite(a) and math.isfinite(b) and math.isclose(
            a, b, rel_tol=1e-12, abs_tol=1e-12,
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _semantic_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _semantic_equal(a, b) for a, b in zip(left, right)
        )
    return type(left) is type(right) and left == right


def _identity_projection(row: dict[str, Any]) -> dict[str, Any]:
    """Remove provenance and post-close diagnostics from durable identity.

    Horizon-suffixed returns can mature after an early TP/SL has already made
    the canonical outcome final. They are deliberately not copied back into
    the append-only ledger; entry, exit, canonical return, costs, and every
    other field remain strict identity inputs.
    """
    return {
        name: value for name, value in row.items()
        if name not in _VOLATILE_REPLAY_FIELDS
        and not any(name.startswith(prefix) for prefix in _LATE_MATURING_DIAGNOSTIC_PREFIXES)
    }


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
        if current is not None:
            current_semantic = _identity_projection(current)
            incoming_semantic = _identity_projection(row)
            if not _semantic_equal(current_semantic, incoming_semantic):
                raise ValueError(f"ATI_FORWARD_ID_COLLISION:{identifier}")
            # Preserve the first durable row and its original provenance. A
            # regenerated replay must not silently rewrite forward history.
            continue
        merged[identifier] = row
    return sorted(merged.values(), key=lambda row: (str(row.get("decision_ts")), str(row.get(key))))


def _paper_feed_metadata(
    row: dict[str, Any], *, now: datetime, outcome_already_known: bool,
) -> dict[str, Any]:
    decision = _parse_utc(row.get("decision_ts"))
    age_seconds = None if decision is None else (now - decision).total_seconds()
    if outcome_already_known:
        eligible = False
        reason = "PREKNOWN_OUTCOME_AT_FIRST_FORWARD_OBSERVATION"
    elif age_seconds is None:
        eligible = False
        reason = "DECISION_TIMESTAMP_INVALID"
    elif age_seconds < -5:
        eligible = False
        reason = "DECISION_TIMESTAMP_IN_FUTURE"
    elif age_seconds > _PAPER_FEED_MAX_DECISION_AGE_SECONDS:
        eligible = False
        reason = "DECISION_STALE_AT_FIRST_FORWARD_OBSERVATION"
    else:
        eligible = True
        reason = None
    return {
        **row,
        "first_forward_observed_at": now.isoformat(),
        "paper_feed_eligible": eligible,
        "paper_feed_status": "ELIGIBLE_FORWARD_ONLY" if eligible else "BLOCKED_RESEARCH_LEDGER_ONLY",
        "paper_feed_block_reason": reason,
        "decision_age_at_first_forward_observation_seconds": age_seconds,
    }


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
    """Return one canonical realized path per signal.

    Alternative trailing rows are counterfactual exit research and cannot
    share the signal-keyed forward outcome ledger.
    """
    return [
        row for row in trades
        if row.get("signal_id") in forward_ids
        and row.get("outcome_complete") is True
        and row.get("policy") == _CANONICAL_FORWARD_OUTCOME_POLICY
    ]


def _parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _source_bounds(audits: list[dict[str, Any]]) -> dict[str, str | None]:
    opened: list[datetime] = []
    available: list[datetime] = []
    for audit in audits:
        parsed = _parse_utc(audit.get("last_timestamp"))
        try:
            step_ms = int(audit.get("expected_step_ms") or 0)
        except (TypeError, ValueError):
            continue
        if parsed is None:
            continue
        opened.append(parsed)
        available.append(parsed + timedelta(milliseconds=step_ms))
    return {
        "oldest_last_bar_at": min(opened).isoformat() if opened else None,
        "newest_last_bar_at": max(opened).isoformat() if opened else None,
        "oldest_available_at": min(available).isoformat() if available else None,
        "newest_available_at": max(available).isoformat() if available else None,
    }


def _age_seconds(value: Any, now: datetime) -> float | None:
    parsed = _parse_utc(value)
    return max(0.0, (now - parsed).total_seconds()) if parsed else None


def _reconciliation(signals: list[dict[str, Any]], outcomes: list[dict[str, Any]],
                    open_rows: list[dict[str, Any]]) -> dict[str, Any]:
    signal_ids = [str(row.get("signal_id") or "") for row in signals]
    outcome_ids = [str(row.get("signal_id") or "") for row in outcomes]
    open_ids = [str(row.get("signal_id") or "") for row in open_rows]
    blockers: list[str] = []
    if any(not item for item in signal_ids + outcome_ids + open_ids):
        blockers.append("missing_signal_id")
    if len(signal_ids) != len(set(signal_ids)):
        blockers.append("duplicate_signal_id")
    if len(outcome_ids) != len(set(outcome_ids)):
        blockers.append("duplicate_outcome_id")
    if len(open_ids) != len(set(open_ids)):
        blockers.append("duplicate_open_position_id")
    if not set(outcome_ids).issubset(set(signal_ids)):
        blockers.append("outcome_without_forward_signal")
    if not set(open_ids).issubset(set(signal_ids)):
        blockers.append("open_without_forward_signal")
    if set(open_ids) & set(outcome_ids):
        blockers.append("signal_both_open_and_closed")
    return {
        "status": "PASS" if not blockers else "FAIL",
        "blockers": blockers,
        "signals": len(signal_ids),
        "outcomes": len(outcome_ids),
        "open_positions": len(open_ids),
    }


def _write_health(target: Path, state: dict[str, Any], *, status: str,
                  last_error: str | None) -> None:
    health = {
        "status": status,
        "last_run_at": state.get("observer_last_cycle_at") or state.get("ran_at"),
        "observer_last_cycle_at": state.get("observer_last_cycle_at") or state.get("ran_at"),
        "observer_cycle_duration_seconds": state.get("observer_cycle_duration_seconds"),
        "metric_ran_at": state.get("heavy_replay_last_run_at"),
        "cache_status": state.get("cache_status"),
        "source_watch_token": state.get("source_watch_token"),
        "source_mode": state.get("dataset_source_mode"),
        "source_paths": state.get("dataset_source_paths") or {},
        "signals_total": int(state.get("signals_total") or 0),
        "open_positions": int(state.get("open_positions") or 0),
        "closed_shadow_trades": int(state.get("closed_outcomes") or 0),
        "last_error": last_error,
        "dataset_last_bar_at": state.get("dataset_last_bar_at"),
        "dataset_newest_bar_at": state.get("dataset_newest_bar_at"),
        "dataset_available_at": state.get("dataset_available_at"),
        "dataset_age_seconds": state.get("dataset_age_seconds"),
        "dataset_snapshot_sha256": state.get("dataset_snapshot_sha256"),
        "stale": bool(state.get("stale", True)),
        "result_status": state.get("status"),
        "observer_status": state.get("observer_status"),
        "boundary_status": state.get("boundary_status"),
        "shadow_phase": state.get("shadow_phase"),
        "reconciliation_status": state.get("reconciliation_status"),
        **safety_envelope(),
    }
    _atomic_write(target / "ati_health.json", _json_text(health))


def run_shadow_once(*, sample_dir: Path | str | None = None,
                    symbols: list[str] | None = None,
                    output_dir: Path | str | None = None,
                    seed: int = 7) -> dict[str, Any]:
    started = time.monotonic()
    target = _safe_output_dir(output_dir)
    source_watch = source_snapshot_status(sample_dir=sample_dir, symbols=symbols)
    replay = run_historical_replay(
        sample_dir=sample_dir, symbols=symbols, output_dir=target,
        seed=seed, write=True,
    )
    now = datetime.now(timezone.utc)
    base = {
        "schema": "ati_forward_shadow.v2",
        "ran_at": now.isoformat(),
        "observer_last_cycle_at": now.isoformat(),
        "source_watch_token": source_watch.get("snapshot_watch_token"),
        "source_watch_status": source_watch.get("status"),
        "observer_status": "OBSERVER_CONNECTED",
        "boundary_status": "FORWARD_BOUNDARY_NOT_FROZEN",
        "reconciliation_status": "NOT_RUN",
        **safety_envelope(),
    }
    if replay.get("status") == "NEED_DATA":
        blocker_text = ",".join(str(item) for item in replay.get("blockers", [])) or "NEED_DATA"
        state = {
            **base,
            "status": "NEED_DATA",
            "shadow_phase": "WAITING_FOR_VALIDATED_DATA",
            "blockers": replay.get("blockers", []),
            "last_error": blocker_text,
            "cache_status": "HEAVY_REPLAY_FAILED_VALIDATION",
            "heavy_replay_executed": True,
            "heavy_replay_last_run_at": replay.get("generated_at"),
            "observer_cycle_duration_seconds": round(time.monotonic() - started, 6),
        }
        _atomic_write(target / "ati_forward_state.json", _json_text(state))
        _write_health(target, state, status="NO_DATA", last_error=blocker_text)
        return state
    signal_rows = _read_jsonl(target / "ati_signals.jsonl")
    trade_rows = [
        row for row in _read_jsonl(target / "ati_shadow_trades.jsonl")
        if row.get("policy") == "baseline_structural_1_5R"
    ]
    boundary_path = target / "ati_forward_boundary.json"
    boundary = _read_json(boundary_path, {})
    audits = replay.get("data_audits") or []
    bounds = _source_bounds(audits)
    latest_source_ts = str(bounds["oldest_last_bar_at"] or "")
    newest_source_ts = str(bounds["newest_last_bar_at"] or "")
    latest_available_at = _latest_available_at(audits)
    source_series_id = str(replay.get("dataset_source_series_id") or "")
    boundary_rebased = not boundary or boundary.get("dataset_source_series_id") != source_series_id
    if boundary_rebased:
        previous = boundary if isinstance(boundary, dict) else {}
        boundary = {
            "forward_boundary": latest_available_at,
            "frozen_at": now.isoformat(),
            "dataset_snapshot_sha256": replay.get("dataset_snapshot_sha256"),
            "dataset_source_series_id": source_series_id,
            "policy_sha256": replay.get("policy", {}).get("policy_sha256"),
            "previous_boundary": previous or None,
            "reason": "INITIAL_FREEZE" if not previous else "SOURCE_SERIES_CHANGED_FAIL_CLOSED",
        }
        _atomic_write(boundary_path, _json_text(boundary))
    boundary_ts = str(boundary.get("forward_boundary") or "")
    raw_forward_candidates = [
        row for row in signal_rows
        if row.get("decision") == "SHADOW_CANDIDATE" and str(row.get("decision_ts") or "") > boundary_ts
    ]
    forward_ids = {row["signal_id"] for row in raw_forward_candidates}
    closed = _closed_forward_trades(trade_rows, forward_ids)
    closed_ids = {row["signal_id"] for row in closed}
    signal_ledger_path = target / "ati_forward_signals.jsonl"
    outcome_ledger_path = target / "ati_forward_outcomes.jsonl"
    existing_signals = [] if boundary_rebased else _read_jsonl(signal_ledger_path)
    existing_outcomes = [] if boundary_rebased else _read_jsonl(outcome_ledger_path)
    existing_by_id = {
        str(row.get("signal_id") or ""): row for row in existing_signals
        if row.get("signal_id")
    }
    forward_candidates: list[dict[str, Any]] = []
    for row in raw_forward_candidates:
        identifier = str(row.get("signal_id") or "")
        previous = existing_by_id.get(identifier)
        if previous is not None:
            # Reuse the first-observation decision verbatim. Eligibility may
            # never improve retrospectively after an outcome becomes known.
            candidate = {
                **row,
                **{
                    field: previous.get(field)
                    for field in (
                        "first_forward_observed_at", "paper_feed_eligible",
                        "paper_feed_status", "paper_feed_block_reason",
                        "decision_age_at_first_forward_observation_seconds",
                    )
                    if field in previous
                },
            }
        else:
            candidate = _paper_feed_metadata(
                row, now=now, outcome_already_known=identifier in closed_ids,
            )
        forward_candidates.append(candidate)
    open_rows = [row for row in forward_candidates if row["signal_id"] not in closed_ids]
    existing_ids = {str(row.get("signal_id")) for row in existing_signals}
    signals_ledger = _merge_unique(existing_signals, forward_candidates, "signal_id")
    outcomes_ledger = _merge_unique(existing_outcomes, closed, "signal_id")
    _atomic_write(signal_ledger_path, _jsonl_text(signals_ledger))
    _atomic_write(outcome_ledger_path, _jsonl_text(outcomes_ledger))
    _atomic_write(target / "ati_open_positions.json", _json_text(open_rows))
    age_seconds = _age_seconds(bounds["oldest_available_at"], now)
    stale = age_seconds is None or age_seconds > 30 * 60
    status = "NOT_ACTIONABLE_HISTORICAL" if stale else "SHADOW_OBSERVATION_ONLY"
    reconciliation = _reconciliation(signals_ledger, outcomes_ledger, open_rows)
    first_new_bar = bool(
        _parse_utc(latest_available_at) and _parse_utc(boundary_ts)
        and _parse_utc(latest_available_at) > _parse_utc(boundary_ts)
    )
    state = {
        **base,
        "status": status,
        "observer_status": "OBSERVER_CONNECTED",
        "boundary_status": "FORWARD_BOUNDARY_FROZEN",
        "shadow_phase": "START_FORWARD_SHADOW_NOW" if first_new_bar else "WAITING_FOR_FIRST_CLOSED_BAR",
        "forward_boundary": boundary_ts,
        "dataset_last_bar_at": latest_source_ts or None,
        "dataset_newest_bar_at": newest_source_ts or None,
        "dataset_available_at": bounds["oldest_available_at"],
        "dataset_newest_available_at": latest_available_at or None,
        "dataset_age_seconds": age_seconds,
        "dataset_snapshot_sha256": replay.get("dataset_snapshot_sha256"),
        "dataset_source_mode": replay.get("dataset_source_mode"),
        "dataset_source_series_id": source_series_id,
        "dataset_source_paths": replay.get("dataset_source_paths") or {},
        "stale": stale,
        "stale_reason": "dataset_last_bar_older_than_30m" if stale else None,
        "signals_total": len(signals_ledger),
        "paper_feed_eligible_signals": sum(
            row.get("paper_feed_eligible") is True for row in signals_ledger
        ),
        "paper_feed_blocked_signals": sum(
            row.get("paper_feed_eligible") is not True for row in signals_ledger
        ),
        "new_forward_candidates_seen": sum(
            str(row.get("signal_id")) not in existing_ids for row in forward_candidates
        ),
        "open_positions": len(open_rows),
        "closed_outcomes": len(outcomes_ledger),
        "canonical_forward_outcome_policy": _CANONICAL_FORWARD_OUTCOME_POLICY,
        "last_error": None,
        "reconciliation_status": reconciliation["status"],
        "reconciliation": reconciliation,
        "cache_status": "HEAVY_REPLAY_REFRESHED",
        "heavy_replay_executed": True,
        "heavy_replay_last_run_at": replay.get("generated_at"),
        "observer_cycle_duration_seconds": round(time.monotonic() - started, 6),
        "edge_validated": False,
        "actionable": False,
        "recommendation": "WAIT_FOR_FRESH_FORWARD_DATA" if stale else "CONTINUE_SHADOW_OBSERVATION",
    }
    _atomic_write(target / "ati_forward_state.json", _json_text(state))
    _write_health(
        target, state,
        status="ERROR" if reconciliation["status"] != "PASS" else ("DEGRADED" if stale else "HEALTHY"),
        last_error=None if reconciliation["status"] == "PASS" else ",".join(reconciliation["blockers"]),
    )
    return state


def render_shadow_text(state: dict[str, Any]) -> str:
    return "\n".join([
        "ATI FORWARD SHADOW V2 START",
        f"status: {state.get('status', 'NEED_DATA')}",
        f"observer_status: {state.get('observer_status', 'NOT_CONNECTED')}",
        f"boundary_status: {state.get('boundary_status', 'NOT_FROZEN')}",
        f"shadow_phase: {state.get('shadow_phase', 'WAITING_FOR_DATA')}",
        f"reconciliation: {state.get('reconciliation_status', 'NOT_RUN')}",
        f"forward_boundary: {state.get('forward_boundary', 'N/A')}",
        f"dataset_last_bar_at: {state.get('dataset_last_bar_at', 'N/A')}",
        f"stale: {str(bool(state.get('stale', True))).lower()}",
        f"signals_total: {state.get('signals_total', 0)}",
        f"open_positions: {state.get('open_positions', 0)}",
        f"closed_outcomes: {state.get('closed_outcomes', 0)}",
        f"recommendation: {state.get('recommendation', 'WAIT_FOR_DATA')}",
        f"cache_status: {state.get('cache_status', 'STALE_UNKNOWN')}",
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


def _refresh_unchanged_heartbeat(target: Path, snapshot: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    now = datetime.now(timezone.utc)
    state = _read_json(target / "ati_forward_state.json", {})
    if not isinstance(state, dict) or not state:
        raise ValueError("ATI_HEARTBEAT_STATE_MISSING")
    dataset_available_at = state.get("dataset_available_at") or state.get("dataset_last_bar_at")
    age = _age_seconds(dataset_available_at, now)
    stale = age is None or age > 30 * 60
    if state.get("status") not in {"NEED_DATA", "ERROR"}:
        state["status"] = "NOT_ACTIONABLE_HISTORICAL" if stale else "SHADOW_OBSERVATION_ONLY"
    state.update({
        "ran_at": now.isoformat(),
        "observer_last_cycle_at": now.isoformat(),
        "observer_status": "OBSERVER_CONNECTED",
        "boundary_status": "FORWARD_BOUNDARY_FROZEN" if state.get("forward_boundary") else "FORWARD_BOUNDARY_NOT_FROZEN",
        "dataset_age_seconds": age,
        "stale": stale,
        "stale_reason": "dataset_last_bar_older_than_30m" if stale else None,
        "source_watch_status": snapshot.get("status"),
        "cache_status": "REUSED_UNCHANGED_SOURCE",
        "heavy_replay_executed": False,
        "observer_cycle_duration_seconds": round(time.monotonic() - started, 6),
    })
    _atomic_write(target / "ati_forward_state.json", _json_text(state))
    health_status = "NO_DATA" if state.get("status") == "NEED_DATA" else (
        "ERROR" if state.get("status") == "ERROR" else ("DEGRADED" if stale else "HEALTHY")
    )
    _write_health(target, state, status=health_status, last_error=state.get("last_error"))
    return state


def run_shadow_loop(*, sample_dir: Path | str | None = None,
                    symbols: list[str] | None = None,
                    output_dir: Path | str | None = None,
                    interval_seconds: float = 60.0,
                    max_cycles: int = 1,
                    seed: int = 7) -> dict[str, Any]:
    cycles = 0
    latest: dict[str, Any] = {}
    target = _safe_output_dir(output_dir)
    try:
        while max_cycles <= 0 or cycles < max_cycles:
            snapshot = source_snapshot_status(sample_dir=sample_dir, symbols=symbols)
            previous = _read_json(target / "ati_forward_state.json", {})
            summary_exists = (target / "ati_summary.json").is_file()
            unchanged = (
                isinstance(previous, dict)
                and previous.get("source_watch_token") == snapshot.get("snapshot_watch_token")
                and summary_exists
            )
            try:
                if unchanged:
                    latest = _refresh_unchanged_heartbeat(target, snapshot)
                else:
                    latest = run_shadow_once(
                        sample_dir=sample_dir, symbols=symbols,
                        output_dir=target, seed=seed,
                    )
            except Exception as exc:
                now = datetime.now(timezone.utc)
                latest = {
                    "schema": "ati_forward_shadow.v2",
                    "ran_at": now.isoformat(),
                    "observer_last_cycle_at": now.isoformat(),
                    "observer_status": "OBSERVER_CONNECTED_WITH_ERROR",
                    "status": "ERROR",
                    "shadow_phase": "WAITING_FOR_VALIDATED_DATA",
                    "last_error": f"{type(exc).__name__}:{str(exc)[:300]}",
                    "source_watch_token": snapshot.get("snapshot_watch_token"),
                    "source_watch_status": snapshot.get("status"),
                    "cache_status": "ERROR_RECORDED",
                    **safety_envelope(),
                }
                _atomic_write(target / "ati_forward_state.json", _json_text(latest))
                _write_health(target, latest, status="ERROR", last_error=latest["last_error"])
            cycles += 1
            if max_cycles > 0 and cycles >= max_cycles:
                break
            time.sleep(max(5.0, float(interval_seconds)))
    except KeyboardInterrupt:
        latest = {**latest, "stopped_by": "CTRL_C"}
    return {"cycles": cycles, "last_state": latest, **safety_envelope()}
