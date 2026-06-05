"""V8.2.6 — Sanitised export + consolidated pack (research-only).

Persists 4 CSVs + 1 TXT + manifest + ZIP under
``training_exports/research_v8_2_6/``.

Hard contract:
- ZIP only contains ``.csv/.txt/.json``.
- No ``.env``, no DB, no secrets.
- ``training_exports/`` is gitignored.
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
from .candidate_rule_miner_v8_2_6 import mine_candidate_rules
from .candidate_rule_walkforward_v8_2_6 import run_walkforward
from .counterfactual_training_dataset import _sanitise_row, build_dataset
from .score_recalibration_sandbox_v8_2_6 import sandbox_recalibration
from .short_barrier_debug_v8_2_6 import debug_short_barriers


EXPORT_SUBDIR_V826 = Path("training_exports") / "research_v8_2_6"


CANDIDATE_RULE_COLUMNS: tuple[str, ...] = (
    "rule_id", "samples", "winrate", "net_ev_avg_pct", "pf",
    "max_loss_pct", "drawdown_proxy_pct",
    "cost_normal_net_ev_pct", "cost_realistic_net_ev_pct",
    "cost_stress_net_ev_pct",
    "timestamp_cluster_max_ratio", "rule_status", "rule_reason",
)

WALKFORWARD_COLUMNS: tuple[str, ...] = (
    "rule_id", "total_samples", "train_samples", "test_samples",
    "train_net_ev_pct", "test_net_ev_pct", "train_pf", "test_pf",
    "degradation_net_ev_pct", "folds", "decision", "reason",
)

SHORT_DEBUG_COLUMNS: tuple[str, ...] = (
    "signal_id", "timestamp", "symbol", "entry_price",
    "stop_loss", "take_profit_1", "take_profit_2",
    "ret_1h_pct", "ret_4h_pct", "ret_24h_pct",
    "mfe_pct", "mae_pct", "first_barrier_hit",
    "classification", "barrier_inverted",
    "mfe_mae_orientation_ok", "same_bar_suspected", "notes",
)

SCORE_RECAL_COLUMNS: tuple[str, ...] = (
    "bucket", "count", "winrate", "net_ev_avg_pct",
)


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(columns), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})


def _sha1_file(path: Path) -> str:
    sha = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha.update(chunk)
    return sha.hexdigest()


def export_research_v826(
    db: Any = None,
    *,
    hours: int = 168,
    limit: int = 50000,
    base_dir: Path | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Generate the V8.2.6 sanitised export bundle."""
    base = Path(base_dir) if base_dir else EXPORT_SUBDIR_V826
    base.mkdir(parents=True, exist_ok=True)
    if rows is None:
        dataset, _ = build_dataset(db, hours=int(hours), limit=int(limit))
    else:
        dataset = list(rows)

    # Run all 4 audits sharing the dataset.
    short_report = debug_short_barriers(db, hours=hours, limit=limit, rows=dataset)
    recal_report = sandbox_recalibration(db, hours=hours, limit=limit, rows=dataset)
    score_ok = recal_report.old_monotonicity == "PASS"
    miner_report = mine_candidate_rules(
        db, hours=hours, limit=limit, rows=dataset,
        short_verdict=short_report.verdict,
        score_calibration_ok=score_ok,
    )
    candidate_rule_dicts = (
        miner_report.candidate_rules + miner_report.watch_only_rules
    )
    wf_report = run_walkforward(
        db, hours=hours, limit=limit, rows=dataset,
        rules=candidate_rule_dicts,
    )

    # CSVs.
    candidate_csv = base / "candidate_rules_v1.csv"
    _write_csv(
        candidate_csv,
        [_sanitise_row(r) for r in (
            miner_report.candidate_rules
            + miner_report.watch_only_rules
            + miner_report.rejected_rules
        )],
        CANDIDATE_RULE_COLUMNS,
    )
    wf_csv = base / "walkforward_results_v1.csv"
    _write_csv(wf_csv, [_sanitise_row(r) for r in wf_report.results], WALKFORWARD_COLUMNS)
    short_csv = base / "short_barrier_debug_v1.csv"
    _write_csv(
        short_csv,
        [_sanitise_row(r) for r in short_report.examples_top_100],
        SHORT_DEBUG_COLUMNS,
    )
    score_csv = base / "score_recalibration_sandbox_v1.csv"
    _write_csv(
        score_csv,
        recal_report.bucket_table_recalibrated,
        SCORE_RECAL_COLUMNS,
    )

    # Summary TXT.
    summary_txt = base / "research_v8_2_6_summary.txt"
    with summary_txt.open("w", encoding="utf-8") as f:
        f.write("RESEARCH V8.2.6 SUMMARY\n")
        f.write(f"generated_at: {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"hours: {hours} limit: {limit}\n")
        f.write(f"short_verdict: {short_report.verdict}\n")
        f.write(f"short_trusted: {short_report.trusted_count}\n")
        f.write(f"short_legitimate: {short_report.legitimate_stop_before_drop}\n")
        f.write(f"short_sign_bug: {short_report.possible_sign_bug}\n")
        f.write(f"short_barrier_bug: {short_report.possible_barrier_bug}\n")
        f.write(f"score_old_monotonicity: {recal_report.old_monotonicity}\n")
        f.write(f"score_recalibrated_monotonicity: {recal_report.recalibrated_monotonicity}\n")
        f.write(f"score_recommendation: {recal_report.recommendation}\n")
        f.write(f"total_rules_evaluated: {miner_report.total_rules}\n")
        for status, count in miner_report.by_status.items():
            f.write(f"rules_by_status {status}: {count}\n")
        for decision, count in wf_report.by_decision.items():
            f.write(f"walkforward_by_decision {decision}: {count}\n")
        f.write("research_only: true\n")
        f.write("paper_filter_enabled: false\n")
        f.write("can_send_real_orders: false\n")
        f.write(f"final_recommendation: {FINAL_RECOMMENDATION_NO_LIVE}\n")

    files = [candidate_csv, wf_csv, short_csv, score_csv, summary_txt]
    manifest: dict[str, Any] = {
        "version": "v8.2.6.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_dir": str(base),
        "files": [],
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

    zip_path = base / "research_v8_2_6_exports.zip"
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


def build_pack_v826(db: Any = None, *, hours: int = 168, limit: int = 50000) -> dict[str, Any]:
    dataset, _ = build_dataset(db, hours=int(hours), limit=int(limit))
    short_report = debug_short_barriers(db, hours=hours, limit=limit, rows=dataset)
    recal_report = sandbox_recalibration(db, hours=hours, limit=limit, rows=dataset)
    score_ok = recal_report.old_monotonicity == "PASS"
    miner_report = mine_candidate_rules(
        db, hours=hours, limit=limit, rows=dataset,
        short_verdict=short_report.verdict,
        score_calibration_ok=score_ok,
    )
    candidate_rule_dicts = (
        miner_report.candidate_rules + miner_report.watch_only_rules
    )
    wf_report = run_walkforward(
        db, hours=hours, limit=limit, rows=dataset,
        rules=candidate_rule_dicts,
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pack_version": "research_v8_2_6_v1",
        "hours": int(hours),
        "limit": int(limit),
        "short_barrier_debug": short_report.as_dict(),
        "score_recalibration_sandbox": recal_report.as_dict(),
        "candidate_rules": miner_report.as_dict(),
        "walkforward": wf_report.as_dict(),
        "research_only": True,
        "paper_filter_enabled": False,
        "can_send_real_orders": False,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }


def render_pack_v826_text(payload: dict[str, Any]) -> str:
    lines = ["RESEARCH PACK V8.2.6 START"]
    lines.append(f"generated_at: {payload.get('generated_at')}")
    lines.append(f"pack_version: {payload.get('pack_version')}")
    lines.append(f"hours: {payload.get('hours')} limit: {payload.get('limit')}")
    short = payload.get("short_barrier_debug") or {}
    lines.append(f"short_verdict: {short.get('verdict')}")
    lines.append(
        f"short_counts: trusted={short.get('trusted_count')} "
        f"legit={short.get('legitimate_stop_before_drop')} "
        f"sign_bug={short.get('possible_sign_bug')} "
        f"barrier_bug={short.get('possible_barrier_bug')}"
    )
    recal = payload.get("score_recalibration_sandbox") or {}
    lines.append(
        f"score_recalibration: old_mono={recal.get('old_monotonicity')} "
        f"new_mono={recal.get('recalibrated_monotonicity')} "
        f"rec={recal.get('recommendation')}"
    )
    miner = payload.get("candidate_rules") or {}
    lines.append(f"miner_total_rules: {miner.get('total_rules')}")
    for status, count in (miner.get("by_status") or {}).items():
        lines.append(f"by_status {status}: {count}")
    wf = payload.get("walkforward") or {}
    lines.append(f"walkforward_rules_evaluated: {wf.get('rules_evaluated')}")
    for dec, count in (wf.get("by_decision") or {}).items():
        lines.append(f"walkforward {dec}: {count}")
    lines.extend([
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        f"final_recommendation: {payload.get('final_recommendation')}",
        "RESEARCH PACK V8.2.6 END",
    ])
    return "\n".join(lines)
