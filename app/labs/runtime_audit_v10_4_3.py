"""ResearchOps V10.4.3 — Runtime Health Audit + Learning/Edge Diagnostic.

Read-only diagnostics that answer two questions honestly:

1. "Is the bot running well right now?"  (runtime health audit)
2. "Is the bot learning well, and what is missing to find real edge?"
   (learning/edge diagnostic — brutally honest, anti-false-hope)

Pure analysis over dicts + safe read-only DB counts. No network calls to
providers, no DB writes, no secrets, no runtime mutation. Verdict ceilings:
nothing here can ever output live/paper readiness.
"""

from __future__ import annotations

import math
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE

# Runtime verdicts.
VERDICT_OK = "OK_RESEARCH_RUNTIME"
VERDICT_WARN = "OK_WITH_WARNINGS"
VERDICT_ATTENTION = "NEEDS_ATTENTION"
VERDICT_UNSAFE = "UNSAFE_STOP"

NEEDS_RUNTIME_CONTEXT = "NEEDS_RUNTIME_CONTEXT"

# Read-only count whitelist — table names are NEVER taken from user input.
DB_AUDIT_TABLES = [
    "trades",
    "signal_observations",
    "signal_labels",
    "signal_path_metrics",
    "latency_metrics",
    "events",
    "virtual_research_trades",
    "strategy_lab_candidates",
    "strategy_lab_walkforward",
    "strategy_lab_recommendations",
]

# Anti-false-hope thresholds (aligned with edge hunter contract V10.4).
MIN_SAMPLES = 150
HIGH_TIME_DEATH = 0.90
EXTREME_TIME_DEATH_CANDIDATE = 0.80  # a "top candidate" above this is not validable
MIN_NET_PF_CANDIDATE = 1.0
SUSPICIOUS_GROSS_PF = 2.0
ARTIFACT_GROSS_PF = 900.0  # gross_PF=999 means "no SL hits in sample" artifact

# Reasons that immediately disqualify a row from being a pending candidate.
DISQUALIFYING_REASONS = (
    "sample_too_small",
    "net_ev_not_positive",
    "high_time_death",
    "candidate_ranking_no_valid_candidates",
    "time_death_or_quality_risk",
)


def count_db_tables(db: Any) -> dict[str, Any]:
    """Read-only COUNT(*) over a fixed whitelist using the repo's real
    Database connection API. A missing table degrades to ``missing`` and an
    unavailable DB degrades to ``db_unavailable`` — never raises."""
    counts: dict[str, Any] = {}
    if db is None or not hasattr(db, "_connect"):
        return {name: "db_unavailable" for name in DB_AUDIT_TABLES}
    for name in DB_AUDIT_TABLES:
        try:
            with db._connect() as conn:
                cur = conn.execute(f"SELECT COUNT(*) AS n FROM {name}")  # noqa: S608 — fixed whitelist
                row = cur.fetchone()
                counts[name] = int(row[0] if not hasattr(row, "keys") or "n" not in row.keys() else row["n"])
        except Exception as exc:
            counts[name] = "missing" if "no such table" in str(exc).lower() else "count_error"
    return counts


def _flag(config: Any, name: str, default: bool) -> bool:
    return bool(getattr(config, name, default))


def build_runtime_health_audit(
    *,
    config: Any,
    db_counts: dict[str, Any],
    health: dict[str, Any] | None,
    health_source: str,
    git_commit: str = "",
    dashboard_contract: dict[str, Any] | None = None,
    log_audit: str = NEEDS_RUNTIME_CONTEXT,
) -> dict[str, Any]:
    """Compose the runtime health audit and a conservative verdict."""
    h = dict(health or {})
    lock = h.get("worker_lock") if isinstance(h.get("worker_lock"), dict) else {}
    live = _flag(config, "live_trading", False)
    dry = _flag(config, "dry_run", True)
    paper = _flag(config, "paper_trading", True)
    pfilter = _flag(config, "enable_paper_policy_filter", False)
    can_send = live and not dry and not paper
    contract = dict(dashboard_contract or {})

    warnings: list[str] = []
    attention: list[str] = []
    unsafe: list[str] = []

    # V10.4.3.1 (Codex P1-1) — in the V10.4.x phase the paper filter MUST be
    # disabled. Finding it enabled is an unexpected, unsafe configuration.
    if pfilter:
        unsafe.append("paper_filter_enabled_unexpected")
        unsafe.append("paper_filter_must_remain_disabled")
    if live or can_send:
        unsafe.append("live_flags_active")

    if health_source != "ok":
        warnings.append(f"health_endpoint_{health_source}")
    # V10.4.3.2 (Codex P1-2) — strict worker-lock rule: unless the lock is
    # EXPLICITLY disabled (enabled is False), ``acquired`` must be EXPLICITLY
    # True for OK_RESEARCH_RUNTIME. ``lock_status=heartbeat`` alone proves
    # nothing — a missing/None/false ``acquired`` blocks the clean verdict.
    if isinstance(lock, dict) and lock:
        status = str(lock.get("lock_status") or "unknown")
        acquired = lock.get("acquired")
        enabled = lock.get("enabled")
        if status == "blocked_duplicate" or lock.get("warning_if_duplicate_worker"):
            attention.append("duplicate_worker_detected")
            attention.append("worker_lock_blocked_duplicate")
        if enabled is False:
            # Deliberately disabled lock: no duplicate claim, but never a
            # silent OK when the config requires single-worker locking.
            if _flag(config, "require_single_worker_lock", False):
                attention.append("single_worker_lock_disabled_but_required")
            else:
                warnings.append("worker_lock_disabled")
        elif acquired is True:
            pass  # healthy: explicitly acquired
        elif acquired is False:
            attention.append("worker_lock_not_acquired")
        else:  # acquired missing/None/garbage — unknown is not healthy
            attention.append("worker_lock_acquired_missing")
            attention.append("worker_lock_not_acquired")
    elif health_source == "ok":
        warnings.append("worker_lock_unknown")
        warnings.append("worker_lock_not_in_health_payload")
    if h.get("circuit_breaker") is True:
        attention.append("circuit_breaker_active")
    if not h.get("last_scan") and health_source == "ok":
        attention.append("last_scan_missing_or_stale")
    if log_audit == NEEDS_RUNTIME_CONTEXT:
        warnings.append("log_audit_needs_runtime_context")
    missing_tables = [t for t, v in db_counts.items() if v == "missing"]
    unavailable = all(v == "db_unavailable" for v in db_counts.values()) if db_counts else True
    if unavailable:
        warnings.append("db_unavailable_for_counts")
    elif missing_tables:
        warnings.append(f"db_tables_missing:{','.join(missing_tables)}")

    if unsafe:
        verdict = VERDICT_UNSAFE
    elif attention:
        verdict = VERDICT_ATTENTION
    elif warnings:
        verdict = VERDICT_WARN
    else:
        verdict = VERDICT_OK

    return {
        "git_commit": git_commit or "unknown",
        "runtime": {
            "health_source": health_source,
            "mode": h.get("mode", "unknown"),
            "uptime": h.get("uptime", "unknown"),
            "last_scan": h.get("last_scan", "unknown"),
            "open_positions": h.get("open_positions", "unknown"),
            "daily_pnl_paper_only": h.get("daily_pnl", "unknown"),
            "circuit_breaker": h.get("circuit_breaker", "unknown"),
            "worker_lock_status": (lock or {}).get("lock_status", "unknown"),
            "worker_lock_acquired": (lock or {}).get("acquired", "unknown"),
            "duplicate_worker_warning": (lock or {}).get("warning_if_duplicate_worker", "unknown"),
        },
        "safety": {
            "live_trading": live,
            "dry_run": dry,
            "paper_trading": paper,
            "paper_filter_enabled": pfilter,
            "can_send_real_orders": can_send,
        },
        "dashboard": {
            "read_only": contract.get("read_only", "unknown"),
            "heavy_panels_mode": contract.get("heavy_panels_mode", "unknown"),
            "heavy_refresh_mode": contract.get("heavy_refresh_mode", "unknown"),
            "unknown_endpoint_behavior": contract.get("unknown_endpoint_behavior", "unknown"),
            "errors_sanitized": contract.get("errors_sanitized", "unknown"),
            "mutable_endpoints": contract.get("mutable_endpoints", "unknown"),
        },
        "log_audit": log_audit,
        "db_counts": db_counts,
        "warnings": warnings,
        "attention": attention,
        "unsafe_blockers": unsafe,
        "verdict": verdict,
        "research_only": True,
        "paper_ready": False,
        "live_ready": False,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }


# ---------------------------------------------------------------------------
# Learning / Edge diagnostic
# ---------------------------------------------------------------------------

def _to_finite_float(value: Any) -> float | None:
    """V10.4.3.3 (Codex P1) — TOTAL numeric parsing for safety gates.

    Returns a finite float, or None for ANYTHING else: None, NaN, +/-inf,
    booleans, empty/non-numeric strings, huge ints that overflow float,
    containers, and hostile objects whose __float__ raises. Catches
    ``Exception`` (never ``BaseException``) so it truly never raises.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        f = float(value)
    except Exception:  # OverflowError, hostile __float__, TypeError, ValueError…
        return None
    try:
        if not math.isfinite(f):
            return None
    except Exception:  # hostile __float__ returning a non-float exotic
        return None
    return f


# Alias resolution outcomes for _metric_strict.
_METRIC_OK = ""
_METRIC_MISSING = "missing"
_METRIC_INVALID = "invalid"
_METRIC_CONFLICT = "conflict"
_ALIAS_TOLERANCE = 1e-12


def _metric_strict(row: dict[str, Any], *names: str) -> tuple[float | None, str]:
    """V10.4.3.3 — conservative alias resolution. Returns (value, status):

    - no alias present                      -> (None, "missing")
    - ANY present alias invalid/non-finite  -> (None, "invalid")
    - valid aliases that disagree (>1e-12)  -> (None, "conflict")
    - all present aliases valid and equal   -> (value, "")

    An invalid alias can never hide behind a valid one (corrupt-schema guard).
    """
    present = [(n, row.get(n)) for n in names if n in row]
    if not present:
        return None, _METRIC_MISSING
    values: list[float] = []
    for _name, raw in present:
        f = _to_finite_float(raw)
        if f is None:
            return None, _METRIC_INVALID
        values.append(f)
    first = values[0]
    for v in values[1:]:
        if abs(v - first) > _ALIAS_TOLERANCE:
            return None, _METRIC_CONFLICT
    return first, _METRIC_OK


def _num(row: dict[str, Any], *names: str) -> float:
    """Lenient variant for warning generation only (never for validation):
    skips non-finite values instead of letting NaN poison comparisons."""
    for n in names:
        f = _to_finite_float(row.get(n))
        if f is not None:
            return f
    return 0.0


def detect_false_hope(rows: list[dict[str, Any]]) -> list[str]:
    """Anti-overfit watchdog: name every pattern that LOOKS like edge but is
    not evidence of edge."""
    warnings: list[str] = []
    for row in rows or []:
        rid = str(row.get("group_value") or row.get("policy_id") or row.get("group_key") or "?")
        samples = int(_num(row, "samples"))
        gross = _num(row, "gross_PF", "gross_pf")
        net_ev = _num(row, "net_EV", "net_ev")
        net_pf = _num(row, "net_PF", "net_pf")
        time_ratio = _num(row, "time_ratio", "TIME")
        if gross >= ARTIFACT_GROSS_PF:
            warnings.append(
                f"{rid}: gross_PF={gross:.0f} is a no-SL-in-sample artifact, not edge")
        elif gross >= SUSPICIOUS_GROSS_PF and net_ev <= 0:
            warnings.append(
                f"{rid}: gross_PF={gross:.2f} looks great but net_EV={net_ev:.4f} <= 0 "
                f"(costs eat it) — gross PF is NOT edge")
        if time_ratio >= HIGH_TIME_DEATH and samples > 0:
            warnings.append(
                f"{rid}: TIME-death {time_ratio * 100:.0f}% — exits expire instead of hitting TP")
        if 0 < samples < MIN_SAMPLES and (gross > 1.0 or net_pf > 1.0):
            warnings.append(
                f"{rid}: only {samples} samples — any PF at this size is noise")
    # Deduplicate, keep order, cap output.
    seen: set[str] = set()
    out = []
    for w in warnings:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out[:15]


def revalidate_top_candidates(
    top: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """V10.4.3.1 (Codex P1-3) — never trust a 'top candidate' row as edge
    without conservative revalidation. Returns (validated, failure_warnings).

    A row only survives when EVERY metric exists, is finite (no None/NaN/inf/
    non-numeric — V10.4.3.2 Codex P1-1) and passes: net_EV > 0,
    net_PF >= 1.0, samples >= 150 (compared as finite float, so 149.9 is
    insufficient), TIME-death < 0.80 (missing or invalid TIME data fails),
    decision != REJECT and no disqualifying reason. Never raises.

    V10.4.3.3 alias rule: if ANY alias of a metric is present but invalid the
    whole metric fails (invalid_metric:X) even when another alias is valid;
    valid aliases that disagree beyond 1e-12 fail as conflicting_alias:X.
    """
    validated: list[dict[str, Any]] = []
    failures: list[str] = []
    for row in top or []:
        rid = str(row.get("group_value") or row.get("policy_id") or "?")
        fails: list[str] = []

        net_ev, ev_status = _metric_strict(row, "net_EV", "net_ev")
        if ev_status == _METRIC_CONFLICT:
            fails.append("conflicting_alias:net_EV")
        elif ev_status != _METRIC_OK:
            fails.append("invalid_metric:net_EV")
        elif net_ev <= 0:
            fails.append("negative_net_ev")

        net_pf, pf_status = _metric_strict(row, "net_PF", "net_pf")
        if pf_status == _METRIC_CONFLICT:
            fails.append("conflicting_alias:net_PF")
        elif pf_status != _METRIC_OK:
            fails.append("invalid_metric:net_PF")
        elif net_pf < MIN_NET_PF_CANDIDATE:
            fails.append("low_net_pf")

        samples, sm_status = _metric_strict(row, "samples", "sample_count")
        if sm_status == _METRIC_CONFLICT:
            fails.append("conflicting_alias:samples")
        elif sm_status != _METRIC_OK:
            fails.append("invalid_metric:samples")
        elif samples < MIN_SAMPLES:
            fails.append("insufficient_samples")

        time_ratio, tm_status = _metric_strict(row, "time_ratio", "TIME", "time_pct")
        if tm_status == _METRIC_MISSING:
            fails.append("needs_time_death_review")
        elif tm_status == _METRIC_CONFLICT:
            fails.append("conflicting_alias:TIME")
        elif tm_status != _METRIC_OK:
            fails.append("invalid_metric:TIME")
            fails.append("needs_time_death_review")
        elif time_ratio >= EXTREME_TIME_DEATH_CANDIDATE:
            fails.append("high_time_death")

        if str(row.get("decision") or "").upper() == "REJECT":
            fails.append("rejected_decision")
        reason = str(row.get("reason") or "").lower()
        if any(bad in reason for bad in DISQUALIFYING_REASONS):
            fails.append(f"reject_reason:{reason}")

        if fails:
            failures.append(
                f"top_candidate_failed_revalidation: {rid} ({','.join(fails)})")
        else:
            validated.append(row)
    return validated, failures


def build_learning_edge_diagnostic(
    *,
    db_counts: dict[str, Any],
    ranking: dict[str, Any] | None,
    net_edge: dict[str, Any] | None,
    data_readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rank = dict(ranking or {})
    edge = dict(net_edge or {})
    readiness = dict(data_readiness or {})

    def _count(table: str) -> int:
        v = db_counts.get(table)
        return int(v) if isinstance(v, int) else 0

    observations = _count("signal_observations")
    labels = _count("signal_labels")
    path_metrics = _count("signal_path_metrics")
    latency = _count("latency_metrics")

    gaps: list[str] = []
    if observations <= 0:
        gaps.append("no signal observations counted (db unavailable or empty)")
    if labels <= 0:
        gaps.append("no matured labels counted in this view")
    if path_metrics <= 0:
        gaps.append("no path metrics (MFE/MAE) counted in this view")
    if latency <= 0:
        gaps.append("no latency metrics counted in this view")

    learning_status = ("LEARNING_INFRA_ACTIVE" if observations > 0 and path_metrics > 0
                       else "LEARNING_DATA_NOT_VISIBLE")

    top = list(rank.get("top_candidates") or [])
    watch = list(rank.get("watch_list") or [])
    rejects = list(rank.get("reject_list") or []) + list(edge.get("rejects") or [])

    # V10.4.3.1 (Codex P1-3) — conservative revalidation: rows in
    # top_candidates are NOT edge until they survive EV/PF/sample/time gates.
    validated_top, revalidation_failures = revalidate_top_candidates(top)
    edge_status = ("EDGE_CANDIDATE_PRESENT_PENDING_VALIDATION" if validated_top
                   else "NO_EDGE_DEMONSTRATED")

    false_hope = revalidation_failures + detect_false_hope(watch + rejects)

    reject_reasons: dict[str, int] = {}
    for row in watch + rejects:
        reason = str(row.get("reason") or "unspecified")
        reject_reasons[reason] = reject_reasons.get(reason, 0) + 1

    # V10.4.3.1 (Codex P1-4) — blockers are DERIVED from current inputs.
    # Unknown stays UNKNOWN; no invented numbers.
    clean_days: Any = readiness.get("current_clean_days", "UNKNOWN")
    history_status: Any = readiness.get("current_history_status", "UNKNOWN")
    missing_oi_ratio: Any = readiness.get("current_missing_oi_ratio", "UNKNOWN")
    missing_oi_status: Any = readiness.get("missing_oi_status", "UNKNOWN")
    backtester_readiness: Any = readiness.get("backtester_readiness", "UNKNOWN")
    oi_policy: Any = readiness.get("oi_bucket_policy", "UNKNOWN")

    top_blockers: list[str] = []
    if not validated_top:
        top_blockers.append(
            "no candidate passed conservative revalidation "
            "(net_EV>0, net_PF>=1.0, samples>=150, TIME<80%)")
    for reason, count in sorted(reject_reasons.items(), key=lambda kv: -kv[1])[:3]:
        top_blockers.append(f"dominant_reject_reason: {reason} ({count}x)")
    if readiness:
        if isinstance(clean_days, (int, float)) and clean_days < 180:
            top_blockers.append(
                f"clean_days={clean_days} below 180d minimum "
                f"(history_status={history_status})")
        if str(oi_policy) == "BLOCK_OI_BUCKETS" or str(missing_oi_status).startswith("MISSING_OI"):
            top_blockers.append(
                f"OI buckets blocked (missing_oi_status={missing_oi_status}, "
                f"ratio={missing_oi_ratio})")
        if str(backtester_readiness) not in ("", "UNKNOWN", "READY"):
            top_blockers.append(f"backtester_readiness={backtester_readiness}")
    else:
        top_blockers.append("data_readiness_snapshot_unavailable")
        top_blockers.append("history_depth_unknown — requires_180d_365d_verified_history")

    next_steps = [
        "1. manually verify Tardis.dev (pricing, Bitget perp 180/365d sample, OI/funding/liq completeness)",
        "2. acquire 180/365d clean history through the V10.4 acquisition contract (manifest+checksums+human authorization)",
        "3. run bar-by-bar replay backtests on validated data (no lookahead, worst-case same-bar)",
        "4. implement Edge Hunter V10.5 against the frozen contract (min 150 samples, net PF>=1.30, cost x2 pass, OOS)",
        "5. attack TIME-death first: exit-policy calibration on net-EV, not on gross PF",
        "6. only then: regime/symbol-specific candidates -> walk-forward -> shadow",
    ]

    what_not_to_do = [
        "do not treat gross_PF as edge; only net EV after costs counts",
        "do not promote anything with samples < 150",
        "do not enable the paper filter or live from any of this",
        "do not use OI buckets while the OI audit blocks them",
        "do not tune thresholds on the same window you evaluate (overfit)",
        "do not declare profitability without walk-forward + OOS + cost x2",
    ]

    return {
        "learning_status": learning_status,
        "learning_infra": {
            "signal_observations": db_counts.get("signal_observations", "unknown"),
            "signal_labels": db_counts.get("signal_labels", "unknown"),
            "signal_path_metrics": db_counts.get("signal_path_metrics", "unknown"),
            "latency_metrics": db_counts.get("latency_metrics", "unknown"),
            "virtual_research_trades": db_counts.get("virtual_research_trades", "unknown"),
            "strategy_lab_candidates": db_counts.get("strategy_lab_candidates", "unknown"),
        },
        "learning_gaps": gaps,
        "data_readiness_derived": {
            "snapshot_available": bool(readiness),
            "clean_days": clean_days,
            "history_status": history_status,
            "missing_oi_ratio": missing_oi_ratio,
            "missing_oi_status": missing_oi_status,
            "backtester_readiness": backtester_readiness,
            "oi_bucket_policy": oi_policy,
        },
        "edge_status": edge_status,
        "candidate_ranking_status": rank.get("status", "unknown"),
        "top_candidates_count": len(top),
        "validated_top_candidates_count": len(validated_top),
        "watchlist_count": len(watch),
        "reject_count": len(rejects),
        "reject_reasons": reject_reasons,
        "top_blockers": top_blockers,
        "false_hope_warnings": false_hope,
        "highest_value_next_steps": next_steps,
        "what_not_to_do": what_not_to_do,
        "research_only": True,
        "paper_ready": False,
        "live_ready": False,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }


def build_runtime_efficiency(
    *,
    config: Any,
    db_counts: dict[str, Any],
    memory_mb: float | None,
) -> dict[str, Any]:
    """Read-only efficiency report. Recommends; never changes anything."""
    scan_s = int(getattr(config, "scan_interval_seconds", 30) or 30)
    lightweight = _flag(config, "worker_lightweight_mode", True)
    latency_rows = db_counts.get("latency_metrics", "unknown")
    path_rows = db_counts.get("signal_path_metrics", "unknown")

    findings: list[str] = []
    recommendations: list[str] = []
    if memory_mb is None:
        findings.append("memory: needs_vps_snapshot (no portable probe here)")
    else:
        findings.append(f"memory_mb~{memory_mb:.0f} (lightweight target <512)")
    findings.append(f"scan_interval_seconds={scan_s}")
    findings.append(f"worker_lightweight_mode={str(lightweight).lower()}")
    findings.append("cpu: needs_vps_snapshot (no portable probe here; measure on "
                    "the VPS before drawing any conclusion)")
    if isinstance(path_rows, int) and path_rows > 100_000:
        findings.append(f"signal_path_metrics rows={path_rows} — matured MFE/MAE volume is large")
        recommendations.append("consider pruning/archiving matured MFE/MAE rows older than the "
                               "research window (read-only proposal; do NOT apply automatically)")
    recommendations.append("log volume: the per-symbol NO_TRADE table prints every cycle; "
                           "a summarised line would cut log churn (proposal only)")
    recommendations.append("dashboard heavy panels stay CLI-refreshed by design; no change needed")
    recommendations.append("no runtime change is justified by current evidence; re-measure with "
                           "a VPS snapshot before any tuning")

    return {
        "scan_interval_seconds": scan_s,
        "worker_lightweight_mode": lightweight,
        "latency_metrics_rows": latency_rows,
        "signal_path_metrics_rows": path_rows,
        "memory_mb": memory_mb if memory_mb is not None else "needs_vps_snapshot",
        "cpu": "needs_vps_snapshot",
        "findings": findings,
        "recommendations_read_only": recommendations,
        "auto_tuning_applied": False,
        "research_only": True,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }
