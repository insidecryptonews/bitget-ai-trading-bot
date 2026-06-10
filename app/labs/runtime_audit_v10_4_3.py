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
SUSPICIOUS_GROSS_PF = 2.0
ARTIFACT_GROSS_PF = 900.0  # gross_PF=999 means "no SL hits in sample" artifact


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

    if health_source != "ok":
        warnings.append(f"health_endpoint_{health_source}")
    if isinstance(lock, dict) and lock:
        if str(lock.get("lock_status")) == "blocked_duplicate":
            attention.append("worker_lock_blocked_duplicate")
        if lock.get("warning_if_duplicate_worker"):
            attention.append("duplicate_worker_warning_present")
    elif health_source == "ok":
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

    if live or can_send:
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
        "verdict": verdict,
        "research_only": True,
        "paper_ready": False,
        "live_ready": False,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }


# ---------------------------------------------------------------------------
# Learning / Edge diagnostic
# ---------------------------------------------------------------------------

def _num(row: dict[str, Any], *names: str) -> float:
    for n in names:
        v = row.get(n)
        try:
            if v is not None:
                return float(v)
        except (TypeError, ValueError):
            continue
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


def build_learning_edge_diagnostic(
    *,
    db_counts: dict[str, Any],
    ranking: dict[str, Any] | None,
    net_edge: dict[str, Any] | None,
) -> dict[str, Any]:
    rank = dict(ranking or {})
    edge = dict(net_edge or {})

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
    gaps.append("clean external history < 180d; OI/funding/liquidation buckets blocked")

    learning_status = ("LEARNING_INFRA_ACTIVE" if observations > 0 and path_metrics > 0
                       else "LEARNING_DATA_NOT_VISIBLE")

    top = list(rank.get("top_candidates") or [])
    watch = list(rank.get("watch_list") or [])
    rejects = list(rank.get("reject_list") or []) + list(edge.get("rejects") or [])
    edge_status = ("EDGE_CANDIDATE_PRESENT_PENDING_VALIDATION" if top
                   else "NO_EDGE_DEMONSTRATED")

    false_hope = detect_false_hope(watch + rejects)

    reject_reasons: dict[str, int] = {}
    for row in watch + rejects:
        reason = str(row.get("reason") or "unspecified")
        reject_reasons[reason] = reject_reasons.get(reason, 0) + 1

    top_blockers = [
        "net_EV <= 0 after realistic costs on every observed bucket",
        f"samples below {MIN_SAMPLES} on most buckets (sample_too_small)",
        "TIME-death dominates exits (positions expire, costs accrue, no TP)",
        "clean history ~63d < 180d minimum; no validated long-history dataset",
        "OI audit unavailable/clustered => OI buckets blocked",
    ]

    next_steps = [
        "1. manually verify Tardis.dev (pricing, Bitget perp 180/365d sample, OI/funding/liq completeness)",
        "2. acquire 180/365d clean history through the V10.4 acquisition contract (manifest+checksums+human authorization)",
        "3. run bar-by-bar replay backtests on validated data (no lookahead, worst-case same-bar)",
        "4. implement Edge Hunter V10.5 against the frozen contract (min 150 samples, net PF>=1.30, cost x2 pass, OOS)",
        "5. attack TIME-death first: exit-policy calibration on net-EV, not on gross PF",
        "6. only then: regime/symbol-specific candidates -> walk-forward -> shadow",
    ]

    what_not_to_do = [
        "do not treat gross_PF as edge (every current bucket has net_EV <= 0)",
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
        "edge_status": edge_status,
        "candidate_ranking_status": rank.get("status", "unknown"),
        "top_candidates_count": len(top),
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
    findings.append(f"scan_interval_seconds={scan_s} (full table logged every ~90s)")
    findings.append(f"worker_lightweight_mode={str(lightweight).lower()}")
    findings.append("cpu: needs_vps_snapshot (~31.8% avg seen on VPS = continuous "
                    "10-symbol scan + MFE/MAE tracking; consistent with design)")
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
