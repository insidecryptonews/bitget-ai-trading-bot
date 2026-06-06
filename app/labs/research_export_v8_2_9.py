"""V8.2.9 — Sanitised research export (research-only).

Bundles the rebound LONG extractor, EdgeGuard dedup, score-gate
sandbox, exit monetization audit, strict OOS validation, and
adversarial audit into a single ZIP under
``training_exports/research_v8_2_9/``.

Hard contract:

- research-only;
- ZIP allow-list: CSV / TXT / JSON;
- secrets stripped via ``_sanitise_row``;
- no .env, no DB, no zips, no backups, no vaults.
"""

from __future__ import annotations

import csv
import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from .adversarial_research_audit_v8_2_9 import audit_v829
from .counterfactual_training_dataset import _sanitise_row, build_dataset
from .edgeguard_repeat_dedup_v8_2_9 import dedup_edgeguard_repeats
from .exit_bar_by_bar_replay_v8_2_9_3 import run_bar_by_bar_replay
from .exit_monetization_audit_v8_2_9 import run_exit_monetization_audit
from .outcome_field_canonicalizer_v8_2_9_3 import canonicalize_rows
from .rebound_long_candidate_extractor_v8_2_9 import (
    extract_rebound_long_candidates,
)
from .rebound_long_strict_oos_canonical_v8_2_9_3 import (
    run_strict_oos_canonical,
)
from .rebound_long_strict_oos_v8_2_9 import run_strict_oos_rebound
from .rebound_outcome_reconciliation_v8_2_9 import reconcile_rebound_outcome
from .rebound_outcome_sign_integrity_v8_2_9_3 import audit_sign_integrity
from .research_pack_consistency_check_v8_2_9_2 import (
    run_consistency_check_v8_2_9_2,
)
from .score_gate_sandbox_v8_2_9 import run_score_gate_sandbox
from .signal_path_metrics_bridge_v8_2_9_5 import bridge_candidates
from .canonical_outcome_real_v8_2_9_5 import canonicalize_real
from .strategy_tournament_real_outcomes_v8_2_9_5 import run_tournament_real


EXPORT_SUBDIR_V829 = Path("training_exports") / "research_v8_2_9"

REBOUND_LONG_COLUMNS: tuple[str, ...] = (
    "symbol", "timestamp", "regime_before", "regime_now",
    "entry_price", "drawdown_proxy_prefix", "higher_lows_prefix",
    "trend_recovering_prefix", "bounce_confirmation_prefix",
    "volatility_bucket", "score_bucket_diagnostic",
    "candidate_reason", "detection_mode", "used_future_return_features",
    "net_pnl_est", "outcome_winner_loser",
    "mfe_pct_outcome", "mae_pct_outcome", "barrier_result_outcome",
)

RECONCILIATION_COLUMNS: tuple[str, ...] = (
    "symbol", "timestamp", "regime_before", "regime_now",
    "bounce_confirmation_prefix", "higher_lows_prefix",
    "trend_recovering_prefix", "net_pnl_est", "outcome_winner_loser",
)

CONSISTENCY_COLUMNS: tuple[str, ...] = (
    "finding",
)

# V8.2.9.3 columns.
SIGN_INTEGRITY_COLUMNS: tuple[str, ...] = (
    "symbol", "timestamp", "side",
    "entry_price", "baseline_net_pnl_est", "baseline_gross_pnl",
    "ret_1h_pct_diagnostic", "ret_4h_pct_diagnostic",
    "mfe_pct_diagnostic", "mae_pct_diagnostic",
    "first_barrier_hit_diagnostic", "expected_long_direction",
    "outcome_sign_from_net_pnl",
    "outcome_sign_from_future_return",
    "outcome_sign_from_barrier",
    "mismatch_type", "reason",
)

CANONICAL_OUTCOME_COLUMNS: tuple[str, ...] = (
    "symbol", "timestamp", "side",
    "canonical_outcome_status", "canonical_net_pnl_est",
    "canonical_win", "canonical_source", "reason",
)

BAR_BY_BAR_REPLAY_COLUMNS: tuple[str, ...] = (
    "policy", "slice_label", "samples", "winrate", "avg_net_pct",
    "pf", "max_loss_pct",
    "avg_profit_capture_ratio", "avg_missed_profit_pct",
    "net_ev_cost_normal_pct", "net_ev_cost_realistic_pct",
    "net_ev_cost_stress_pct", "oos_status",
    "used_future_return_features_for_input",
    "same_bar_ambiguity_rule",
)

STRICT_OOS_CANONICAL_COLUMNS: tuple[str, ...] = (
    "rule_id",
    "train_samples", "validation_samples", "test_samples",
    "train_net_ev_pct", "validation_net_ev_pct", "test_net_ev_pct",
    "test_net_ev_after_cost_realistic_pct",
    "test_net_ev_after_cost_stress_pct",
    "test_pf", "test_winrate",
    "test_cluster_ratio", "test_symbol_concentration",
    "duplicate_ratio_after",
    "final_status", "reject_reason",
)

EDGEGUARD_DEDUP_COLUMNS: tuple[str, ...] = (
    "symbol", "side", "regime", "strategy", "timestamp",
    "edgeguard_reason", "entry_price",
    "edgeguard_repeat_seen_again",
)

# V8.2.9.5 columns.
SIGNAL_PATH_BRIDGE_COLUMNS: tuple[str, ...] = (
    "observation_id", "symbol", "timestamp", "side", "entry_price",
    "path_status", "path_join_method",
    "real_final_return_pct", "real_max_favorable_pct", "real_max_adverse_pct",
    "real_first_barrier_hit", "real_bars_tracked",
    "real_bars_to_mfe", "real_bars_to_mae", "real_outcome_win",
    "real_outcome_source",
    "proxy_net_pnl_est", "proxy_vs_real_delta",
    "proxy_matches_real_sign", "proxy_mismatch_type",
)

CANONICAL_REAL_COLUMNS: tuple[str, ...] = (
    "observation_id", "symbol", "timestamp", "side",
    "canonical_source", "canonical_is_real",
    "canonical_net_pnl_est", "canonical_win",
    "canonical_mfe_pct", "canonical_mae_pct",
    "canonical_first_barrier_hit",
    "canonical_quality", "canonical_warning",
)

TOURNAMENT_REAL_COLUMNS: tuple[str, ...] = (
    "name", "side", "logic", "samples",
    "train_samples", "validation_samples", "test_samples",
    "winrate", "test_winrate", "pf", "test_pf",
    "net_ev_pct", "test_net_ev_pct",
    "test_net_ev_realistic_pct", "test_net_ev_stress_pct",
    "single_symbol_share", "time_cluster_share", "sign_bug_ratio",
    "status", "reason",
)

SCORE_SANDBOX_COLUMNS: tuple[str, ...] = (
    "variant", "samples", "winrate",
    "net_ev_avg_pct", "net_ev_after_cost_pct",
    "pf", "max_loss_pct", "duplicate_ratio",
    "train_samples", "validation_samples", "test_samples",
    "test_net_ev_after_cost_pct", "test_pf", "oos_status",
)

EXIT_AUDIT_COLUMNS: tuple[str, ...] = (
    "side", "entry_time", "entry_price", "exit_time", "exit_price",
    "outcome", "net_pct", "mfe_pct", "mae_pct", "bars",
    "tp_pct", "sl_pct", "closed_by_horizon",
    "profit_capture_ratio", "missed_profit_pct",
    "is_missed_profit_candidate",
    "same_bar_ambiguous", "same_bar_resolution",
)

EXIT_POLICY_COLUMNS: tuple[str, ...] = (
    "policy", "slice_label", "samples", "winrate", "avg_net_pct",
    "pf", "max_loss_pct",
    "avg_profit_capture_ratio", "avg_missed_profit_pct",
    "net_ev_cost_normal_pct", "net_ev_cost_realistic_pct",
    "net_ev_cost_stress_pct", "oos_status",
    "used_future_return_features_for_input",
    "same_bar_ambiguity_rule",
)

STRICT_OOS_COLUMNS: tuple[str, ...] = (
    "rule_id",
    "train_samples", "validation_samples", "test_samples",
    "train_net_ev_pct", "validation_net_ev_pct", "test_net_ev_pct",
    "test_net_ev_after_cost_realistic_pct",
    "test_net_ev_after_cost_stress_pct",
    "test_pf", "test_winrate",
    "test_cluster_ratio", "test_symbol_concentration",
    "duplicate_ratio_after",
    "final_status", "reject_reason",
)

AUDIT_COLUMNS: tuple[str, ...] = (
    "category", "message",
)


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(columns), lineterminator="\n")
        writer.writeheader()
        for r in rows:
            writer.writerow({c: r.get(c, "") for c in columns})


def _sha1_file(path: Path) -> str:
    sha = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha.update(chunk)
    return sha.hexdigest()


def _fetch_path_rows(
    db: Any,
    candidates: list[dict[str, Any]],
    *,
    hours: int,
    limit: int,
) -> list[dict[str, Any]]:
    """V8.2.9.5 read-only fetch of ``signal_path_metrics`` scoped to the
    candidate observation_ids / symbols. Returns [] when ``db`` is None
    or the reader is unavailable — callers treat that as zero real
    coverage (NEED_MORE_DATA), never inventing outcomes."""
    if db is None:
        return []
    obs_ids = []
    symbols = set()
    for c in candidates:
        oid = c.get("observation_id") or c.get("signal_id")
        if oid is not None:
            try:
                obs_ids.append(int(oid))
            except (TypeError, ValueError):
                pass
        sym = str(c.get("symbol") or "").upper()
        if sym:
            symbols.add(sym)
    reader = getattr(db, "fetch_signal_path_metrics", None)
    if not callable(reader):
        return []
    try:
        return reader(
            observation_ids=obs_ids or None,
            symbols=sorted(symbols) or None,
            limit=int(limit or 50000),
        )
    except Exception:
        return []


def export_research_v829(
    db: Any = None,
    *,
    hours: int = 168,
    limit: int = 50000,
    base_dir: Path | None = None,
    rows: list[dict[str, Any]] | None = None,
    score_anti_calibrated: bool = True,
) -> dict[str, Any]:
    """Build the V8.2.9 sanitised export under ``base_dir``."""
    base = Path(base_dir) if base_dir else EXPORT_SUBDIR_V829
    base.mkdir(parents=True, exist_ok=True)
    if rows is None:
        dataset, _ = build_dataset(db, hours=int(hours), limit=int(limit))
    else:
        dataset = list(rows)

    extractor = extract_rebound_long_candidates(
        db, hours=hours, limit=limit, rows=dataset,
    )
    dedup_rows, dedup_report = dedup_edgeguard_repeats(dataset, hours=hours)
    score_sandbox = run_score_gate_sandbox(
        extractor.candidates, hours=hours,
        score_anti_calibrated=bool(score_anti_calibrated),
    )
    exit_audit = run_exit_monetization_audit(
        db, hours=hours, limit=limit, rows=dataset,
    )
    # V8.2.9.2 — wire the deduplicated candidate set + after-dedup ratio
    # explicitly into strict OOS. ``input_is_deduped=True`` so the OOS
    # report exposes that the input has already been deduped.
    deduped_candidates, deduped_candidate_report = dedup_edgeguard_repeats(
        extractor.candidates, hours=hours,
    )
    strict_oos = run_strict_oos_rebound(
        deduped_candidates, hours=hours,
        score_anti_calibrated=bool(score_anti_calibrated),
        duplicate_ratio_after=deduped_candidate_report.duplicate_ratio_after,
        input_is_deduped=True,
        exit_monetization_diagnostic={
            "best_policy": exit_audit.best_policy,
            "best_policy_test_status": exit_audit.best_policy_test_status,
            "horizon_close_problem_detected": exit_audit.horizon_close_problem_detected,
            "avg_profit_capture_ratio": exit_audit.avg_profit_capture_ratio,
            "avg_missed_profit_pct": exit_audit.avg_missed_profit_pct,
            "exit_policy_replay_mode": exit_audit.exit_policy_replay_mode,
            "exit_policy_productive_ready": exit_audit.exit_policy_productive_ready,
            "exit_policy_candidate_status": exit_audit.exit_policy_candidate_status,
        },
    )

    symbol_conc = 0.0
    cluster_conc = 0.0
    test_net_stress = 0.0
    for rule in strict_oos.paper_sandbox_candidates:
        symbol_conc = max(symbol_conc, rule.get("test_symbol_concentration", 0.0))
        cluster_conc = max(cluster_conc, rule.get("test_cluster_ratio", 0.0))
        test_net_stress = max(
            test_net_stress,
            rule.get("test_net_ev_after_cost_stress_pct", 0.0),
        )

    # V8.2.9.2 — reconcile the V8.2.8 vs V8.2.9 rebound outcome gap.
    reconciliation = reconcile_rebound_outcome(
        db, hours=hours, limit=limit, rows=dataset,
    )

    # V8.2.9.5 — bridge candidates to REAL path outcomes + canonical real
    # + tournament on real outcomes. Read-only: pulls signal_path_metrics
    # from the db when available; otherwise path coverage is 0 and the
    # tournament correctly returns NEED_MORE_DATA.
    path_rows = _fetch_path_rows(db, deduped_candidates, hours=hours, limit=limit)
    bridge = bridge_candidates(deduped_candidates, path_rows, hours=hours)
    canonical_real = canonicalize_real(deduped_candidates, path_rows, hours=hours)
    tournament_real = run_tournament_real(
        deduped_candidates, path_rows, hours=hours,
    )

    # V8.2.9.2 — consistency check across the pipeline. Runs BEFORE the
    # adversarial audit so the audit can include the consistency status
    # as a hard blocker (``FAIL_CONSISTENCY``).
    strict_top = strict_oos.final_status_top_level
    consistency = run_consistency_check_v8_2_9_2(
        hours=hours,
        duplicate_ratio_before=dedup_report.duplicate_ratio_before,
        duplicate_ratio_after=deduped_candidate_report.duplicate_ratio_after,
        strict_oos_duplicate_ratio_used=strict_oos.duplicate_ratio_after,
        strict_oos_input_is_deduped=strict_oos.strict_oos_input_is_deduped,
        strict_oos_status=strict_top,
        paper_sandbox_candidates=len(strict_oos.paper_sandbox_candidates),
        adversarial_duplicate_source="after",
        adversarial_duplicate_ratio_used=deduped_candidate_report.duplicate_ratio_after,
        exit_oos_status=exit_audit.best_policy_test_status,
        exit_policy_replay_mode=exit_audit.exit_policy_replay_mode,
        exit_policy_productive_ready=exit_audit.exit_policy_productive_ready,
        final_recommendation=FINAL_RECOMMENDATION_NO_LIVE,
    )

    audit = audit_v829(
        hours=hours,
        score_anti_calibrated=bool(score_anti_calibrated),
        score_used_as_gate=False,
        duplicate_ratio_raw=dedup_report.duplicate_ratio_before,
        duplicate_ratio_after_dedup=deduped_candidate_report.duplicate_ratio_after,
        strict_oos_input_is_deduped=strict_oos.strict_oos_input_is_deduped,
        single_symbol_concentration=symbol_conc,
        single_cluster_concentration=cluster_conc,
        paper_filter_enabled=False,
        can_send_real_orders=False,
        live_trading=False,
        paper_sandbox_candidates_count=len(strict_oos.paper_sandbox_candidates),
        test_net_ev_after_stress_pct=test_net_stress,
        exit_policy_used_future_returns=False,
        exit_policy_selected_on_test=False,
        same_bar_resolution_conservative=True,
        consistency_check_failed=(
            consistency.consistency_check_status != "PASS"
        ),
        consistency_findings=consistency.consistency_findings,
    )

    rebound_csv = base / "rebound_long_candidates_v1.csv"
    _write_csv(
        rebound_csv,
        [_sanitise_row(r) for r in extractor.candidates],
        REBOUND_LONG_COLUMNS,
    )
    dedup_csv = base / "edgeguard_repeat_dedup_v1.csv"
    _write_csv(
        dedup_csv,
        [_sanitise_row(r) for r in dedup_rows[:5000]],
        EDGEGUARD_DEDUP_COLUMNS,
    )
    score_csv = base / "score_gate_sandbox_v1.csv"
    _write_csv(score_csv, score_sandbox.variants, SCORE_SANDBOX_COLUMNS)
    exit_audit_csv = base / "exit_monetization_audit_v1.csv"
    _write_csv(
        exit_audit_csv,
        [_sanitise_row(r) for r in exit_audit.rows],
        EXIT_AUDIT_COLUMNS,
    )
    exit_policy_csv = base / "exit_policy_simulation_v1.csv"
    _write_csv(exit_policy_csv, exit_audit.policies, EXIT_POLICY_COLUMNS)
    rerun_csv = base / "rebound_long_strict_oos_v1.csv"
    all_rule_rows = (
        strict_oos.paper_sandbox_candidates
        + strict_oos.research_candidates
        + strict_oos.watch_only
        + strict_oos.rejected
        + strict_oos.need_more_data
    )
    _write_csv(rerun_csv, all_rule_rows, STRICT_OOS_COLUMNS)
    audit_csv = base / "adversarial_research_audit_v1.csv"
    _write_csv(audit_csv, audit.findings, AUDIT_COLUMNS)

    # V8.2.9.3 — sign integrity, canonical outcome, bar-by-bar replay,
    # strict OOS canonical.
    sign_integrity = audit_sign_integrity(
        deduped_candidates, dataset_rows=dataset, hours=hours,
    )
    canonical = canonicalize_rows(deduped_candidates, hours=hours)
    bar_by_bar = run_bar_by_bar_replay(deduped_candidates, hours=hours)
    strict_oos_canonical = run_strict_oos_canonical(
        deduped_candidates, hours=hours,
        score_anti_calibrated=bool(score_anti_calibrated),
        duplicate_ratio_after=deduped_candidate_report.duplicate_ratio_after,
        input_is_deduped=True,
    )

    sign_integrity_csv = base / "rebound_outcome_sign_integrity_v1.csv"
    _write_csv(
        sign_integrity_csv,
        [_sanitise_row(r) for r in sign_integrity.rows],
        SIGN_INTEGRITY_COLUMNS,
    )
    canonical_csv = base / "canonical_outcome_v1.csv"
    _write_csv(
        canonical_csv,
        [_sanitise_row(r) for r in canonical.rows],
        CANONICAL_OUTCOME_COLUMNS,
    )
    bar_by_bar_csv = base / "exit_bar_by_bar_replay_v1.csv"
    _write_csv(bar_by_bar_csv, bar_by_bar.by_policy, BAR_BY_BAR_REPLAY_COLUMNS)
    strict_canonical_csv = base / "rebound_long_strict_oos_canonical_v1.csv"
    canonical_rules = (
        strict_oos_canonical.paper_sandbox_candidates
        + strict_oos_canonical.research_candidates
        + strict_oos_canonical.watch_only
        + strict_oos_canonical.rejected
        + strict_oos_canonical.need_more_data
    )
    _write_csv(
        strict_canonical_csv, canonical_rules, STRICT_OOS_CANONICAL_COLUMNS,
    )

    # V8.2.9.2 — new CSVs.
    reconciliation_csv = base / "rebound_outcome_reconciliation_v1.csv"
    _write_csv(
        reconciliation_csv,
        [_sanitise_row(r) for r in reconciliation.examples_top_100],
        RECONCILIATION_COLUMNS,
    )
    consistency_csv = base / "consistency_check_v1.csv"
    _write_csv(
        consistency_csv,
        [{"finding": f} for f in consistency.consistency_findings],
        CONSISTENCY_COLUMNS,
    )

    # V8.2.9.5 — signal path bridge + canonical real + tournament real.
    bridge_csv = base / "signal_path_metrics_bridge_v1.csv"
    _write_csv(
        bridge_csv,
        [_sanitise_row(r) for r in bridge.rows],
        SIGNAL_PATH_BRIDGE_COLUMNS,
    )
    canonical_real_csv = base / "canonical_outcome_real_v1.csv"
    _write_csv(
        canonical_real_csv,
        [_sanitise_row(r) for r in canonical_real.rows],
        CANONICAL_REAL_COLUMNS,
    )
    tournament_real_csv = base / "strategy_tournament_real_outcomes_v1.csv"
    _write_csv(
        tournament_real_csv,
        [_sanitise_row(r) for r in tournament_real.results],
        TOURNAMENT_REAL_COLUMNS,
    )

    summary_txt = base / "research_v8_2_9_summary.txt"
    with summary_txt.open("w", encoding="utf-8") as f:
        f.write("RESEARCH V8.2.9 SUMMARY\n")
        f.write(f"generated_at: {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"hours: {hours} limit: {limit}\n")
        f.write(f"raw_rebound_candidates: {extractor.candidates_count}\n")
        f.write(f"dedup_rebound_candidates: {len(deduped_candidates)}\n")
        f.write(f"duplicate_ratio_before: {dedup_report.duplicate_ratio_before:.4f}\n")
        f.write(f"duplicate_ratio_after: {dedup_report.duplicate_ratio_after:.4f}\n")
        f.write(
            f"edgeguard_repeat_blocks_removed: "
            f"{dedup_report.edgeguard_repeat_blocks_removed}\n"
        )
        f.write(f"score_gate_best_variant: {score_sandbox.best_variant or 'NONE'}\n")
        f.write(
            f"score_used_as_gate: "
            f"{str(score_sandbox.score_used_as_positive_gate).lower()}\n"
        )
        f.write(f"strict_oos_status: {strict_oos.final_status_top_level}\n")
        f.write(
            f"paper_sandbox_candidates: "
            f"{len(strict_oos.paper_sandbox_candidates)}\n"
        )
        f.write(f"best_exit_policy: {exit_audit.best_policy}\n")
        f.write(f"exit_oos_status: {exit_audit.best_policy_test_status}\n")
        f.write(
            f"horizon_close_problem_detected: "
            f"{str(exit_audit.horizon_close_problem_detected).lower()}\n"
        )
        f.write(
            f"avg_profit_capture_ratio: "
            f"{exit_audit.avg_profit_capture_ratio:.4f}\n"
        )
        f.write(
            f"avg_missed_profit_pct: "
            f"{exit_audit.avg_missed_profit_pct:.4f}\n"
        )
        f.write(f"adversarial_audit_status: {audit.audit_status}\n")
        f.write(
            f"blockers: {','.join(audit.blockers) if audit.blockers else 'NONE'}\n"
        )
        # V8.2.9.2 wiring transparency.
        f.write(
            f"strict_oos_input_is_deduped: "
            f"{str(strict_oos.strict_oos_input_is_deduped).lower()}\n"
        )
        f.write(
            f"strict_oos_duplicate_ratio_used: "
            f"{strict_oos.duplicate_ratio_after:.4f}\n"
        )
        f.write(
            f"adversarial_duplicate_source: "
            f"{audit.adversarial_duplicate_source}\n"
        )
        f.write(
            f"adversarial_duplicate_ratio_used: "
            f"{audit.adversarial_duplicate_ratio_used:.4f}\n"
        )
        # V8.2.9.2 exit hardening transparency.
        f.write(
            f"exit_policy_replay_mode: "
            f"{exit_audit.exit_policy_replay_mode}\n"
        )
        f.write(
            f"exit_policy_productive_ready: "
            f"{str(exit_audit.exit_policy_productive_ready).lower()}\n"
        )
        f.write(
            f"requires_bar_by_bar_replay: "
            f"{str(exit_audit.requires_bar_by_bar_replay).lower()}\n"
        )
        f.write(
            f"exit_policy_candidate_status: "
            f"{exit_audit.exit_policy_candidate_status}\n"
        )
        # V8.2.9.3 sign integrity + canonical + bar-by-bar replay +
        # strict OOS canonical.
        sign_status = (
            "PASS" if sign_integrity.sign_bug_ratio <= 0.05 else "FAIL"
        )
        f.write(f"sign_integrity_status: {sign_status}\n")
        f.write(f"sign_bug_ratio: {sign_integrity.sign_bug_ratio:.4f}\n")
        outcome_field_mismatch_ratio = (
            sign_integrity.outcome_field_mismatch_count
            / max(sign_integrity.total_candidates, 1)
        )
        f.write(
            f"outcome_field_mismatch_ratio: {outcome_field_mismatch_ratio:.4f}\n"
        )
        f.write(
            f"canonical_outcome_ok_ratio: {canonical.canonical_outcome_ok_ratio:.4f}\n"
        )
        f.write(
            f"canonical_outcome_source_top: "
            f"{canonical.canonical_outcome_source_top or 'NONE'}\n"
        )
        f.write(
            f"bar_by_bar_replay_available: "
            f"{str(bar_by_bar.bar_by_bar_replay_available).lower()}\n"
        )
        f.write(
            f"best_policy_bar_by_bar: {bar_by_bar.best_policy_bar_by_bar or 'NONE'}\n"
        )
        f.write(
            f"best_policy_bar_by_bar_status: {bar_by_bar.best_policy_bar_by_bar_status}\n"
        )
        f.write(
            f"strict_oos_canonical_status: "
            f"{strict_oos_canonical.final_status_top_level}\n"
        )
        f.write(
            f"paper_sandbox_candidates_canonical: "
            f"{len(strict_oos_canonical.paper_sandbox_candidates)}\n"
        )
        # V8.2.9.4 trazability flags.
        f.write(
            f"sign_integrity_join_method_top: "
            f"{sign_integrity.join_method_top or 'NONE'}\n"
        )
        f.write(
            f"sign_integrity_ambiguous_join_count: "
            f"{sign_integrity.ambiguous_join_count}\n"
        )
        f.write(
            f"sign_integrity_join_symbol_mismatch_count: "
            f"{sign_integrity.join_symbol_mismatch_count}\n"
        )
        f.write("bar_replay_intrabar_rule: STOP_BEFORE_TP\n")
        f.write("bar_replay_trailing_uses_previous_bar_only: true\n")
        f.write("canonical_supports_short_ohlcv_replay: true\n")
        # V8.2.9.2 reconciliation + consistency.
        f.write(
            f"rebound_reconciliation_reason: {reconciliation.reason_for_gap}\n"
        )
        f.write(
            f"v828_like_winrate: {reconciliation.winrate_v828_like:.4f}\n"
        )
        f.write(
            f"v829_raw_winrate: {reconciliation.winrate_v829_raw:.4f}\n"
        )
        f.write(
            f"v829_dedup_winrate: {reconciliation.winrate_v829_dedup:.4f}\n"
        )
        f.write(
            f"consistency_check_status: {consistency.consistency_check_status}\n"
        )
        if consistency.consistency_findings:
            f.write(
                "consistency_findings: "
                + "; ".join(consistency.consistency_findings) + "\n"
            )
        else:
            f.write("consistency_findings: NONE\n")
        # V8.2.9.5 — signal path bridge + canonical real + tournament real.
        f.write(
            f"signal_path_metrics_coverage_ratio: {bridge.path_coverage_ratio:.4f}\n"
        )
        f.write(f"path_found_count: {bridge.path_found_count}\n")
        f.write(f"path_missing_count: {bridge.path_missing_count}\n")
        f.write(f"path_ambiguous_count: {bridge.path_ambiguous_count}\n")
        f.write(
            f"proxy_sign_mismatch_ratio: {bridge.proxy_sign_mismatch_ratio:.4f}\n"
        )
        f.write(f"proxy_net_ev_avg: {bridge.proxy_net_ev_avg:.4f}\n")
        f.write(f"real_net_ev_avg: {bridge.real_net_ev_avg:.4f}\n")
        f.write(f"real_winrate: {bridge.real_winrate:.4f}\n")
        f.write(
            f"canonical_real_ok_ratio: {canonical_real.canonical_real_ok_ratio:.4f}\n"
        )
        f.write(
            f"canonical_source_top: {canonical_real.canonical_source_top or 'NONE'}\n"
        )
        f.write(
            f"tournament_real_status: {tournament_real.tournament_real_status}\n"
        )
        f.write(
            f"tournament_real_best_strategy: "
            f"{tournament_real.tournament_real_best_strategy or 'NONE'}\n"
        )
        f.write(
            f"tournament_real_best_status: "
            f"{tournament_real.tournament_real_best_status}\n"
        )
        f.write(
            f"paper_sandbox_candidates_real: "
            f"{tournament_real.paper_sandbox_candidates_real}\n"
        )
        f.write("research_only: true\n")
        f.write("paper_filter_enabled: false\n")
        f.write("can_send_real_orders: false\n")
        f.write(f"final_recommendation: {FINAL_RECOMMENDATION_NO_LIVE}\n")

    files = [
        rebound_csv, dedup_csv, score_csv,
        exit_audit_csv, exit_policy_csv, rerun_csv, audit_csv,
        reconciliation_csv, consistency_csv,
        sign_integrity_csv, canonical_csv, bar_by_bar_csv,
        strict_canonical_csv,
        bridge_csv, canonical_real_csv, tournament_real_csv,
        summary_txt,
    ]
    manifest: dict[str, Any] = {
        "version": "v8.2.9.v5",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_dir": str(base),
        "files": [],
        "raw_rebound_candidates": extractor.candidates_count,
        "dedup_rebound_candidates": len(deduped_candidates),
        "duplicate_ratio_before": dedup_report.duplicate_ratio_before,
        "duplicate_ratio_after": deduped_candidate_report.duplicate_ratio_after,
        "duplicate_ratio_raw_dataset_before": dedup_report.duplicate_ratio_before,
        "duplicate_ratio_raw_dataset_after": dedup_report.duplicate_ratio_after,
        "strict_oos_input_is_deduped": strict_oos.strict_oos_input_is_deduped,
        "strict_oos_duplicate_ratio_used": strict_oos.duplicate_ratio_after,
        "adversarial_duplicate_source": audit.adversarial_duplicate_source,
        "adversarial_duplicate_ratio_used": audit.adversarial_duplicate_ratio_used,
        "score_gate_best_variant": score_sandbox.best_variant or "NONE",
        "strict_oos_status": strict_oos.final_status_top_level,
        "paper_sandbox_candidates": len(strict_oos.paper_sandbox_candidates),
        "best_exit_policy": exit_audit.best_policy,
        "exit_oos_status": exit_audit.best_policy_test_status,
        "horizon_close_problem_detected": exit_audit.horizon_close_problem_detected,
        "avg_profit_capture_ratio": exit_audit.avg_profit_capture_ratio,
        "avg_missed_profit_pct": exit_audit.avg_missed_profit_pct,
        "exit_policy_replay_mode": exit_audit.exit_policy_replay_mode,
        "exit_policy_productive_ready": exit_audit.exit_policy_productive_ready,
        "requires_bar_by_bar_replay": exit_audit.requires_bar_by_bar_replay,
        "exit_policy_candidate_status": exit_audit.exit_policy_candidate_status,
        "rebound_reconciliation_reason": reconciliation.reason_for_gap,
        "v828_like_winrate": reconciliation.winrate_v828_like,
        "v829_raw_winrate": reconciliation.winrate_v829_raw,
        "v829_dedup_winrate": reconciliation.winrate_v829_dedup,
        "consistency_check_status": consistency.consistency_check_status,
        "consistency_findings": consistency.consistency_findings,
        # V8.2.9.3 keys.
        "sign_integrity_status": (
            "PASS" if sign_integrity.sign_bug_ratio <= 0.05 else "FAIL"
        ),
        "sign_bug_ratio": sign_integrity.sign_bug_ratio,
        "outcome_field_mismatch_ratio": (
            sign_integrity.outcome_field_mismatch_count
            / max(sign_integrity.total_candidates, 1)
        ),
        "canonical_outcome_ok_ratio": canonical.canonical_outcome_ok_ratio,
        "canonical_outcome_source_top": canonical.canonical_outcome_source_top,
        "bar_by_bar_replay_available": bar_by_bar.bar_by_bar_replay_available,
        "best_policy_bar_by_bar": bar_by_bar.best_policy_bar_by_bar,
        "best_policy_bar_by_bar_status": bar_by_bar.best_policy_bar_by_bar_status,
        "strict_oos_canonical_status": strict_oos_canonical.final_status_top_level,
        "paper_sandbox_candidates_canonical": len(
            strict_oos_canonical.paper_sandbox_candidates
        ),
        # V8.2.9.4 trazability.
        "sign_integrity_join_method_top": sign_integrity.join_method_top,
        "sign_integrity_ambiguous_join_count": sign_integrity.ambiguous_join_count,
        "sign_integrity_join_symbol_mismatch_count": (
            sign_integrity.join_symbol_mismatch_count
        ),
        "bar_replay_intrabar_rule": "STOP_BEFORE_TP",
        "bar_replay_trailing_uses_previous_bar_only": True,
        "canonical_supports_short_ohlcv_replay": True,
        # V8.2.9.5 keys — real outcome bridge.
        "signal_path_metrics_coverage_ratio": bridge.path_coverage_ratio,
        "path_found_count": bridge.path_found_count,
        "path_missing_count": bridge.path_missing_count,
        "path_ambiguous_count": bridge.path_ambiguous_count,
        "proxy_sign_mismatch_ratio": bridge.proxy_sign_mismatch_ratio,
        "proxy_net_ev_avg": bridge.proxy_net_ev_avg,
        "real_net_ev_avg": bridge.real_net_ev_avg,
        "real_winrate": bridge.real_winrate,
        "canonical_real_ok_ratio": canonical_real.canonical_real_ok_ratio,
        "canonical_real_source_top": canonical_real.canonical_source_top,
        "tournament_real_status": tournament_real.tournament_real_status,
        "tournament_real_best_strategy": tournament_real.tournament_real_best_strategy,
        "tournament_real_best_status": tournament_real.tournament_real_best_status,
        "paper_sandbox_candidates_real": tournament_real.paper_sandbox_candidates_real,
        "adversarial_audit_status": audit.audit_status,
        "blockers": audit.blockers,
        "research_only": True,
        "paper_filter_enabled": False,
        "can_send_real_orders": False,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }
    for path in files:
        if path.exists():
            manifest["files"].append({
                "name": path.name,
                "size_bytes": path.stat().st_size,
                "sha1": _sha1_file(path),
            })
    manifest_path = base / "manifest_v1.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    manifest["files"].append({
        "name": manifest_path.name,
        "size_bytes": manifest_path.stat().st_size,
        "sha1": _sha1_file(manifest_path),
    })

    zip_path = base / "research_v8_2_9_exports.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in files + [manifest_path]:
            if not path.exists():
                continue
            if path.suffix not in {".csv", ".txt", ".json"}:
                continue
            zf.write(path, arcname=path.name)
    manifest["zip"] = {
        "name": zip_path.name,
        "size_bytes": zip_path.stat().st_size,
        "sha1": _sha1_file(zip_path),
    }
    return manifest


def build_pack_v829(
    db: Any = None,
    *,
    hours: int = 168,
    limit: int = 50000,
    score_anti_calibrated: bool = True,
) -> dict[str, Any]:
    dataset, _ = build_dataset(db, hours=int(hours), limit=int(limit))
    extractor = extract_rebound_long_candidates(
        db, hours=hours, limit=limit, rows=dataset,
    )
    _, dedup_report = dedup_edgeguard_repeats(dataset, hours=hours)
    score_sandbox = run_score_gate_sandbox(
        extractor.candidates, hours=hours,
        score_anti_calibrated=bool(score_anti_calibrated),
    )
    exit_audit = run_exit_monetization_audit(
        db, hours=hours, limit=limit, rows=dataset,
    )
    deduped_candidates, deduped_candidate_report = dedup_edgeguard_repeats(
        extractor.candidates, hours=hours,
    )
    strict_oos = run_strict_oos_rebound(
        deduped_candidates, hours=hours,
        score_anti_calibrated=bool(score_anti_calibrated),
        duplicate_ratio_after=deduped_candidate_report.duplicate_ratio_after,
        input_is_deduped=True,
    )
    reconciliation = reconcile_rebound_outcome(
        db, hours=hours, limit=limit, rows=dataset,
    )
    consistency = run_consistency_check_v8_2_9_2(
        hours=hours,
        duplicate_ratio_before=dedup_report.duplicate_ratio_before,
        duplicate_ratio_after=deduped_candidate_report.duplicate_ratio_after,
        strict_oos_duplicate_ratio_used=strict_oos.duplicate_ratio_after,
        strict_oos_input_is_deduped=strict_oos.strict_oos_input_is_deduped,
        strict_oos_status=strict_oos.final_status_top_level,
        paper_sandbox_candidates=len(strict_oos.paper_sandbox_candidates),
        adversarial_duplicate_source="after",
        adversarial_duplicate_ratio_used=deduped_candidate_report.duplicate_ratio_after,
        exit_oos_status=exit_audit.best_policy_test_status,
        exit_policy_replay_mode=exit_audit.exit_policy_replay_mode,
        exit_policy_productive_ready=exit_audit.exit_policy_productive_ready,
        final_recommendation=FINAL_RECOMMENDATION_NO_LIVE,
    )
    audit = audit_v829(
        hours=hours,
        score_anti_calibrated=bool(score_anti_calibrated),
        score_used_as_gate=False,
        duplicate_ratio_raw=dedup_report.duplicate_ratio_before,
        duplicate_ratio_after_dedup=deduped_candidate_report.duplicate_ratio_after,
        strict_oos_input_is_deduped=strict_oos.strict_oos_input_is_deduped,
        paper_filter_enabled=False,
        can_send_real_orders=False,
        live_trading=False,
        paper_sandbox_candidates_count=len(strict_oos.paper_sandbox_candidates),
        consistency_check_failed=(
            consistency.consistency_check_status != "PASS"
        ),
        consistency_findings=consistency.consistency_findings,
    )
    sign_integrity = audit_sign_integrity(
        deduped_candidates, dataset_rows=dataset, hours=hours,
    )
    canonical = canonicalize_rows(deduped_candidates, hours=hours)
    bar_by_bar = run_bar_by_bar_replay(deduped_candidates, hours=hours)
    strict_oos_canonical = run_strict_oos_canonical(
        deduped_candidates, hours=hours,
        score_anti_calibrated=bool(score_anti_calibrated),
        duplicate_ratio_after=deduped_candidate_report.duplicate_ratio_after,
        input_is_deduped=True,
    )
    # V8.2.9.5 — real outcome bridge + canonical real + tournament real.
    path_rows = _fetch_path_rows(db, deduped_candidates, hours=hours, limit=limit)
    bridge = bridge_candidates(deduped_candidates, path_rows, hours=hours)
    canonical_real = canonicalize_real(deduped_candidates, path_rows, hours=hours)
    tournament_real = run_tournament_real(
        deduped_candidates, path_rows, hours=hours,
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pack_version": "research_v8_2_9_v5",
        "hours": int(hours),
        "limit": int(limit),
        "rebound_extractor": extractor.as_dict(),
        "edgeguard_repeat_dedup": dedup_report.as_dict(),
        "score_gate_sandbox": score_sandbox.as_dict(),
        "exit_monetization": exit_audit.as_dict(),
        "strict_oos_rebound": strict_oos.as_dict(),
        "rebound_outcome_reconciliation": reconciliation.as_dict(),
        "consistency_check": consistency.as_dict(),
        "adversarial_audit": audit.as_dict(),
        "sign_integrity": sign_integrity.as_dict(),
        "canonical_outcome": canonical.as_dict(),
        "bar_by_bar_replay": bar_by_bar.as_dict(),
        "strict_oos_canonical": strict_oos_canonical.as_dict(),
        "signal_path_metrics_bridge": bridge.as_dict(),
        "canonical_outcome_real": canonical_real.as_dict(),
        "strategy_tournament_real": tournament_real.as_dict(),
        "research_only": True,
        "paper_filter_enabled": False,
        "can_send_real_orders": False,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }


def render_pack_v829_text(payload: dict[str, Any]) -> str:
    lines = ["RESEARCH PACK V8.2.9 START"]
    lines.append(f"generated_at: {payload.get('generated_at')}")
    lines.append(f"pack_version: {payload.get('pack_version')}")
    extractor = payload.get("rebound_extractor") or {}
    lines.append(
        f"rebound_candidates_count: {extractor.get('candidates_count', 0)}"
    )
    dedup = payload.get("edgeguard_repeat_dedup") or {}
    lines.append(
        f"duplicate_ratio_before: {dedup.get('duplicate_ratio_before', 0.0):.4f} "
        f"after: {dedup.get('duplicate_ratio_after', 0.0):.4f}"
    )
    sandbox = payload.get("score_gate_sandbox") or {}
    lines.append(f"score_gate_best_variant: {sandbox.get('best_variant') or 'NONE'}")
    exit_payload = payload.get("exit_monetization") or {}
    lines.append(
        f"best_exit_policy: {exit_payload.get('best_policy')} "
        f"exit_oos_status: {exit_payload.get('best_policy_test_status')}"
    )
    lines.append(
        f"horizon_close_problem_detected: "
        f"{str(exit_payload.get('horizon_close_problem_detected')).lower()}"
    )
    strict = payload.get("strict_oos_rebound") or {}
    lines.append(f"strict_oos_status: {strict.get('final_status_top_level')}")
    lines.append(
        f"paper_sandbox_candidates: "
        f"{len(strict.get('paper_sandbox_candidates') or [])}"
    )
    audit = payload.get("adversarial_audit") or {}
    lines.append(f"adversarial_audit_status: {audit.get('audit_status')}")
    sign = payload.get("sign_integrity") or {}
    if sign:
        lines.append(
            f"sign_bug_ratio: {sign.get('sign_bug_ratio', 0.0):.4f}"
        )
    canonical = payload.get("canonical_outcome") or {}
    if canonical:
        lines.append(
            f"canonical_outcome_ok_ratio: "
            f"{canonical.get('canonical_outcome_ok_ratio', 0.0):.4f}"
        )
        lines.append(
            f"canonical_outcome_source_top: "
            f"{canonical.get('canonical_outcome_source_top') or 'NONE'}"
        )
    bbr = payload.get("bar_by_bar_replay") or {}
    if bbr:
        lines.append(
            f"bar_by_bar_replay_available: "
            f"{str(bbr.get('bar_by_bar_replay_available')).lower()}"
        )
        lines.append(
            f"best_policy_bar_by_bar: "
            f"{bbr.get('best_policy_bar_by_bar') or 'NONE'}"
        )
        lines.append(
            f"best_policy_bar_by_bar_status: "
            f"{bbr.get('best_policy_bar_by_bar_status')}"
        )
    soc = payload.get("strict_oos_canonical") or {}
    if soc:
        lines.append(
            f"strict_oos_canonical_status: "
            f"{soc.get('final_status_top_level')}"
        )
    lines.extend([
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        f"final_recommendation: {payload.get('final_recommendation')}",
        "RESEARCH PACK V8.2.9 END",
    ])
    return "\n".join(lines)
