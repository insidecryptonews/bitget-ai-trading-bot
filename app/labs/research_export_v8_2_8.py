"""V8.2.8 — Sanitised research export (research-only).

Bundles the dual-side barrier audit, duplicate root cause, side-aware
score calibration, rebound readiness lab, and a re-run of strict OOS
into a single ZIP under ``training_exports/research_v8_2_8/``.

Hard contract: research-only. ZIP allow-list (CSV/TXT/JSON). No secrets.
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
from .counterfactual_training_dataset import _sanitise_row, build_dataset
from .dual_side_barrier_truth_audit_v8_2_8 import audit_dual_side_barriers
from .duplicate_source_root_cause_v8_2_8 import audit_duplicate_root_cause
from .final_rule_gate_v8_2_7 import run_final_gate
from .rebound_regime_turn_lab_v8_2_8 import detect_rebound_setups
from .side_aware_score_calibration_v8_2_8 import calibrate_score_by_side
from .strict_oos_rule_selector_v8_2_7 import EX_ANTE_FEATURES


EXPORT_SUBDIR_V828 = Path("training_exports") / "research_v8_2_8"


DUAL_SIDE_COLUMNS: tuple[str, ...] = (
    "side", "signal_id", "timestamp", "symbol",
    "entry_price", "tp_price", "sl_price",
    "ret_1h_pct", "ret_4h_pct",
    "mfe_pct", "mae_pct", "first_barrier_hit",
    "classification", "notes",
)

DUPLICATE_ROOT_COLUMNS: tuple[str, ...] = (
    "fingerprint", "count", "symbol", "side", "regime",
    "timestamp_bucket", "probable_root_cause", "sample_reason",
)

SCORE_SIDE_COLUMNS: tuple[str, ...] = (
    "side", "bucket", "count", "winrate", "net_ev_avg_pct",
)

REBOUND_COLUMNS: tuple[str, ...] = (
    "signal_id", "timestamp", "symbol", "side",
    "regime_before", "regime_now", "score",
    "drawdown_from_recent_high_pct", "bounce_confirmation",
    "trend_alignment_recovering", "volatility_bucket",
    "rebound_label", "net_pnl",
    "detection_mode", "detection_reason", "used_future_return_features",
    "note",
)

STRICT_OOS_RERUN_COLUMNS: tuple[str, ...] = (
    "symbol", "side", "regime", "strategy", "score_bucket",
    "candidate_selected", "risk_approved",
    "train_samples", "validation_samples", "test_samples",
    "train_net_ev_pct", "validation_net_ev_pct", "test_net_ev_pct",
    "train_pf", "validation_pf", "test_pf",
    "final_gate", "reject_reason",
)


def _flatten_rule_row(rule: dict[str, Any]) -> dict[str, Any]:
    flat = dict(rule)
    for f in EX_ANTE_FEATURES:
        flat[f] = (rule.get("features") or {}).get(f, "")
    return flat


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


def _build_side_bucket_rows(side_block: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    side_name = getattr(side_block, "side", "")
    for bucket in (getattr(side_block, "bucket_table", []) or []):
        out.append({"side": side_name, **bucket})
    return out


def export_research_v828(
    db: Any = None,
    *,
    hours: int = 168,
    limit: int = 50000,
    base_dir: Path | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    base = Path(base_dir) if base_dir else EXPORT_SUBDIR_V828
    base.mkdir(parents=True, exist_ok=True)
    if rows is None:
        dataset, _ = build_dataset(db, hours=int(hours), limit=int(limit))
    else:
        dataset = list(rows)

    barrier = audit_dual_side_barriers(db, hours=hours, limit=limit, rows=dataset)
    dup_root = audit_duplicate_root_cause(db, hours=hours, limit=limit, rows=dataset)
    score_side = calibrate_score_by_side(db, hours=hours, limit=limit, rows=dataset)
    rebound = detect_rebound_setups(db, hours=hours, limit=limit, rows=dataset)
    rerun = run_final_gate(db, hours=hours, limit=limit, rows=dataset)

    dual_csv = base / "dual_side_barrier_audit_v1.csv"
    dual_rows = []
    for case in barrier.long_metrics.examples_top_100:
        dual_rows.append({"side": "LONG", **case})
    for case in barrier.short_metrics.examples_top_100:
        dual_rows.append({"side": "SHORT", **case})
    _write_csv(dual_csv, [_sanitise_row(r) for r in dual_rows], DUAL_SIDE_COLUMNS)

    dup_csv = base / "duplicate_root_cause_v1.csv"
    _write_csv(
        dup_csv,
        [_sanitise_row(r) for r in dup_root.top_duplicate_fingerprints],
        DUPLICATE_ROOT_COLUMNS,
    )

    score_csv = base / "side_aware_score_calibration_v1.csv"
    score_rows = (
        _build_side_bucket_rows(score_side.long_block)
        + _build_side_bucket_rows(score_side.short_block)
    )
    _write_csv(score_csv, score_rows, SCORE_SIDE_COLUMNS)

    rebound_csv = base / "rebound_regime_turn_v1.csv"
    _write_csv(
        rebound_csv,
        [_sanitise_row(r) for r in rebound.examples_top_100],
        REBOUND_COLUMNS,
    )

    rerun_csv = base / "strict_oos_rerun_after_quality_v1.csv"
    rerun_rules = (
        rerun.paper_sandbox_rules
        + rerun.research_candidate_rules
    )
    _write_csv(
        rerun_csv,
        [_sanitise_row(_flatten_rule_row(r)) for r in rerun_rules],
        STRICT_OOS_RERUN_COLUMNS,
    )

    summary_txt = base / "research_v8_2_8_summary.txt"
    with summary_txt.open("w", encoding="utf-8") as f:
        f.write("RESEARCH V8.2.8 SUMMARY\n")
        f.write(f"generated_at: {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"hours: {hours} limit: {limit}\n")
        f.write(f"long_verdict: {barrier.long_verdict}\n")
        f.write(f"short_verdict: {barrier.short_verdict}\n")
        f.write(f"duplicate_ratio: {dup_root.duplicate_ratio:.4f}\n")
        causes = ", ".join(
            f"{k}={v}" for k, v in (dup_root.by_root_cause or {}).items()
        )
        f.write(f"duplicate_root_cause: {causes}\n")
        f.write(f"score_status_long: {score_side.long_block.usefulness}\n")
        f.write(f"score_status_short: {score_side.short_block.usefulness}\n")
        f.write(f"rebound_status: {rebound.readiness}\n")
        f.write(f"rebound_detection_mode: {rebound.report_detection_mode}\n")
        f.write(
            "rebound_used_future_return_features: "
            f"{str(bool(rebound.used_future_return_features)).lower()}\n"
        )
        f.write(
            f"rebound_prefix_only_count: {rebound.prefix_only_count}\n"
        )
        f.write(
            f"rebound_need_data_count: {rebound.need_data_count}\n"
        )
        f.write(f"paper_sandbox_candidates_after_quality: {rerun.paper_sandbox_candidates}\n")
        f.write(f"duplicate_ratio_gate_status: {rerun.duplicate_ratio_gate_status}\n")
        f.write("research_only: true\n")
        f.write("paper_filter_enabled: false\n")
        f.write("can_send_real_orders: false\n")
        f.write(f"final_recommendation: {FINAL_RECOMMENDATION_NO_LIVE}\n")

    files = [dual_csv, dup_csv, score_csv, rebound_csv, rerun_csv, summary_txt]
    manifest: dict[str, Any] = {
        "version": "v8.2.8.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_dir": str(base),
        "files": [],
        "long_verdict": barrier.long_verdict,
        "short_verdict": barrier.short_verdict,
        "duplicate_ratio": dup_root.duplicate_ratio,
        "rebound_readiness": rebound.readiness,
        "rebound_detection_mode": rebound.report_detection_mode,
        "rebound_used_future_return_features": bool(rebound.used_future_return_features),
        "rebound_prefix_only_count": int(rebound.prefix_only_count),
        "rebound_need_data_count": int(rebound.need_data_count),
        "paper_sandbox_candidates_after_quality": rerun.paper_sandbox_candidates,
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

    zip_path = base / "research_v8_2_8_exports.zip"
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


def build_pack_v828(db: Any = None, *, hours: int = 168, limit: int = 50000) -> dict[str, Any]:
    dataset, _ = build_dataset(db, hours=int(hours), limit=int(limit))
    barrier = audit_dual_side_barriers(db, hours=hours, limit=limit, rows=dataset)
    dup = audit_duplicate_root_cause(db, hours=hours, limit=limit, rows=dataset)
    side_score = calibrate_score_by_side(db, hours=hours, limit=limit, rows=dataset)
    rebound = detect_rebound_setups(db, hours=hours, limit=limit, rows=dataset)
    rerun = run_final_gate(db, hours=hours, limit=limit, rows=dataset)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pack_version": "research_v8_2_8_v1",
        "hours": int(hours),
        "limit": int(limit),
        "dual_side_barrier": barrier.as_dict(),
        "duplicate_root_cause": dup.as_dict(),
        "side_aware_score": side_score.as_dict(),
        "rebound_regime_turn": rebound.as_dict(),
        "strict_oos_rerun": rerun.as_dict(),
        "research_only": True,
        "paper_filter_enabled": False,
        "can_send_real_orders": False,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }


def render_pack_v828_text(payload: dict[str, Any]) -> str:
    lines = ["RESEARCH PACK V8.2.8 START"]
    lines.append(f"generated_at: {payload.get('generated_at')}")
    lines.append(f"pack_version: {payload.get('pack_version')}")
    lines.append(f"hours: {payload.get('hours')} limit: {payload.get('limit')}")
    barrier = payload.get("dual_side_barrier") or {}
    lines.append(
        f"long_verdict: {barrier.get('long_verdict')} short_verdict: {barrier.get('short_verdict')}"
    )
    dup = payload.get("duplicate_root_cause") or {}
    lines.append(
        f"duplicate_ratio: {dup.get('duplicate_ratio', 0.0):.4f} "
        f"causes: {','.join(f'{k}={v}' for k, v in (dup.get('by_root_cause') or {}).items())}"
    )
    score = payload.get("side_aware_score") or {}
    long_block = (score.get("long_block") or {}).get("usefulness")
    short_block = (score.get("short_block") or {}).get("usefulness")
    lines.append(f"score_status_long: {long_block} score_status_short: {short_block}")
    rebound = payload.get("rebound_regime_turn") or {}
    lines.append(
        f"rebound_status: {rebound.get('readiness')} "
        f"candidates={rebound.get('rebound_candidates_count')}"
    )
    rerun = payload.get("strict_oos_rerun") or {}
    lines.append(f"paper_sandbox_candidates_after_quality: {rerun.get('paper_sandbox_candidates')}")
    lines.extend([
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        f"final_recommendation: {payload.get('final_recommendation')}",
        "RESEARCH PACK V8.2.8 END",
    ])
    return "\n".join(lines)
