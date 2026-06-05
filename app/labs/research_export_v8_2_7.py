"""V8.2.7 — Export with feature columns + final gate (research-only).

Sanitised CSV/TXT/JSON + ZIP under ``training_exports/research_v8_2_7/``.
The candidate rules CSV exposes feature columns explicitly (one column per
ex-ante feature) instead of embedding them in ``rule_id``.
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
from .final_rule_gate_v8_2_7 import NO_PAPER_CANDIDATES_MARKER, run_final_gate
from .score_calibration_audit import MONOTONIC_PASS, audit_score_calibration
from .short_barrier_debug_v8_2_7 import debug_short_barriers_v827
from .strict_oos_rule_selector_v8_2_7 import (
    EX_ANTE_FEATURES,
    select_rules_strict_oos,
)


EXPORT_SUBDIR_V827 = Path("training_exports") / "research_v8_2_7"


# Strict OOS rules CSV — features as separate columns.
STRICT_OOS_COLUMNS: tuple[str, ...] = (
    "symbol", "side", "regime", "strategy", "score_bucket",
    "candidate_selected", "risk_approved",
    "train_samples", "validation_samples", "test_samples",
    "train_net_ev_pct", "validation_net_ev_pct", "test_net_ev_pct",
    "train_pf", "validation_pf", "test_pf",
    "train_winrate", "validation_winrate", "test_winrate",
    "degradation_train_to_test_pct",
    "test_cost_normal_net_ev_pct",
    "test_cost_realistic_net_ev_pct",
    "test_cost_stress_net_ev_pct",
    "train_cluster_ratio", "test_cluster_ratio",
    "test_symbol_concentration_ratio",
    "final_gate", "reject_reason",
)

FINAL_GATE_COLUMNS: tuple[str, ...] = (
    "metric", "value",
)

SHORT_DEBUG_V2_COLUMNS: tuple[str, ...] = (
    "symbol", "timestamp", "side", "entry_price",
    "ret_4h_pct", "mfe_pct", "mae_pct",
    "first_barrier_hit", "classification", "notes",
)


def _flatten_rule_row(rule: dict[str, Any]) -> dict[str, Any]:
    """Lift ``rule['features'][k]`` to top-level ``rule[k]`` so the CSV has
    real feature columns.
    """
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


def export_research_v827(
    db: Any = None,
    *,
    hours: int = 168,
    limit: int = 50000,
    base_dir: Path | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Persist V8.2.7 exports. Returns the manifest dict (with SHA1)."""
    base = Path(base_dir) if base_dir else EXPORT_SUBDIR_V827
    base.mkdir(parents=True, exist_ok=True)
    if rows is None:
        dataset, _ = build_dataset(db, hours=int(hours), limit=int(limit))
    else:
        dataset = list(rows)

    short = debug_short_barriers_v827(db, hours=hours, limit=limit, rows=dataset)
    # V8.2.7.1 fix — compute REAL score calibration status, matching
    # ``final_rule_gate_v8_2_7``. The earlier hardcoded ``False`` could let
    # the export disagree with the gate.
    score_report = audit_score_calibration(db, hours=hours, limit=limit, rows=dataset)
    score_calibration_status = score_report.monotonicity_status
    score_calibration_ok = score_calibration_status == MONOTONIC_PASS
    selector = select_rules_strict_oos(
        db, hours=hours, limit=limit, rows=dataset,
        short_verdict=short.verdict,
        score_calibration_ok=score_calibration_ok,
    )
    final = run_final_gate(db, hours=hours, limit=limit, rows=dataset)

    # CSV: strict OOS rules with feature columns.
    all_rule_groups = (
        selector.paper_sandbox_candidates
        + selector.research_candidates
        + selector.watch_only_rules
        + selector.rejected_rules
        + selector.need_more_data_rules
    )
    flattened = [_sanitise_row(_flatten_rule_row(r)) for r in all_rule_groups]
    strict_csv = base / "strict_oos_rules_v1.csv"
    _write_csv(strict_csv, flattened, STRICT_OOS_COLUMNS)

    # CSV: final gate as a metric table.
    final_rows = [
        {"metric": "short_verdict", "value": final.short_verdict},
        {"metric": "score_monotonicity", "value": final.score_monotonicity},
        {"metric": "score_calibration_status", "value": score_calibration_status},
        {"metric": "score_calibration_ok", "value": score_calibration_ok},
        {"metric": "duplicate_ratio", "value": final.duplicate_ratio},
        {"metric": "duplicate_ratio_gate", "value": getattr(final, "duplicate_ratio_gate", "")},
        {"metric": "duplicate_ratio_gate_status", "value": getattr(final, "duplicate_ratio_gate_status", "")},
        {"metric": "total_rules_mined", "value": final.total_rules_mined},
        {"metric": "rejected", "value": final.rejected},
        {"metric": "watch_only", "value": final.watch_only},
        {"metric": "research_candidates", "value": final.research_candidates},
        {"metric": "paper_sandbox_candidates", "value": final.paper_sandbox_candidates},
        {"metric": "need_more_data", "value": final.need_more_data},
        {"metric": "no_paper_candidates_marker", "value": final.no_paper_candidates_marker},
    ]
    final_csv = base / "final_rule_gate_v1.csv"
    _write_csv(final_csv, final_rows, FINAL_GATE_COLUMNS)

    short_csv = base / "short_barrier_debug_v2.csv"
    _write_csv(
        short_csv,
        [_sanitise_row(r) for r in short.examples_top_100],
        SHORT_DEBUG_V2_COLUMNS,
    )

    summary_txt = base / "research_v8_2_7_summary.txt"
    with summary_txt.open("w", encoding="utf-8") as f:
        f.write("RESEARCH V8.2.7 SUMMARY\n")
        f.write(f"generated_at: {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"hours: {hours} limit: {limit}\n")
        f.write(f"short_verdict: {short.verdict}\n")
        f.write(f"short_suspicious_ratio: {short.suspicious_ratio:.4f}\n")
        f.write(f"short_sign_bug_ratio: {short.sign_bug_ratio:.4f}\n")
        f.write(f"short_barrier_bug_ratio: {short.barrier_bug_ratio:.4f}\n")
        f.write(f"short_same_bar_ratio: {short.same_bar_ratio:.4f}\n")
        # V8.2.7.1 — explicit score calibration + duplicate ratio gate status.
        f.write(f"score_calibration_status: {score_calibration_status}\n")
        f.write(f"score_calibration_ok: {str(score_calibration_ok).lower()}\n")
        f.write(f"duplicate_ratio: {final.duplicate_ratio:.4f}\n")
        f.write(f"duplicate_ratio_gate: {getattr(final, 'duplicate_ratio_gate', '')}\n")
        f.write(f"duplicate_ratio_gate_status: {getattr(final, 'duplicate_ratio_gate_status', '')}\n")
        f.write(f"total_rules_mined: {final.total_rules_mined}\n")
        f.write(f"rejected: {final.rejected}\n")
        f.write(f"watch_only: {final.watch_only}\n")
        f.write(f"research_candidates: {final.research_candidates}\n")
        f.write(f"paper_sandbox_candidates: {final.paper_sandbox_candidates}\n")
        f.write(f"need_more_data: {final.need_more_data}\n")
        f.write(f"no_paper_candidates_marker: {final.no_paper_candidates_marker}\n")
        f.write("research_only: true\n")
        f.write("paper_filter_enabled: false\n")
        f.write("can_send_real_orders: false\n")
        f.write(f"final_recommendation: {FINAL_RECOMMENDATION_NO_LIVE}\n")

    files = [strict_csv, final_csv, short_csv, summary_txt]
    manifest: dict[str, Any] = {
        "version": "v8.2.7.1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_dir": str(base),
        "files": [],
        # V8.2.7.1 — surface calibration + duplicate-ratio gate at the
        # manifest level so a consumer can read the gate without scanning
        # the CSV.
        "score_calibration_status": score_calibration_status,
        "score_calibration_ok": score_calibration_ok,
        "duplicate_ratio": final.duplicate_ratio,
        "duplicate_ratio_gate": getattr(final, "duplicate_ratio_gate", ""),
        "duplicate_ratio_gate_status": getattr(final, "duplicate_ratio_gate_status", ""),
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

    zip_path = base / "research_v8_2_7_exports.zip"
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


def build_pack_v827(db: Any = None, *, hours: int = 168, limit: int = 50000) -> dict[str, Any]:
    dataset, _ = build_dataset(db, hours=int(hours), limit=int(limit))
    short = debug_short_barriers_v827(db, hours=hours, limit=limit, rows=dataset)
    final = run_final_gate(db, hours=hours, limit=limit, rows=dataset)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pack_version": "research_v8_2_7_v1",
        "hours": int(hours),
        "limit": int(limit),
        "short_barrier_debug_v2": short.as_dict(),
        "final_rule_gate": final.as_dict(),
        "research_only": True,
        "paper_filter_enabled": False,
        "can_send_real_orders": False,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }


def render_pack_v827_text(payload: dict[str, Any]) -> str:
    lines = ["RESEARCH PACK V8.2.7 START"]
    lines.append(f"generated_at: {payload.get('generated_at')}")
    lines.append(f"pack_version: {payload.get('pack_version')}")
    lines.append(f"hours: {payload.get('hours')} limit: {payload.get('limit')}")
    short = payload.get("short_barrier_debug_v2") or {}
    lines.append(f"short_verdict: {short.get('verdict')}")
    lines.append(
        f"short_ratios: suspicious={short.get('suspicious_ratio', 0.0):.4f} "
        f"sign_bug={short.get('sign_bug_ratio', 0.0):.4f} "
        f"barrier_bug={short.get('barrier_bug_ratio', 0.0):.4f}"
    )
    final = payload.get("final_rule_gate") or {}
    lines.append(f"total_rules_mined: {final.get('total_rules_mined')}")
    lines.append(f"paper_sandbox_candidates: {final.get('paper_sandbox_candidates')}")
    lines.append(f"research_candidates: {final.get('research_candidates')}")
    lines.append(f"watch_only: {final.get('watch_only')}")
    lines.append(f"rejected: {final.get('rejected')}")
    lines.append(f"need_more_data: {final.get('need_more_data')}")
    if final.get("no_paper_candidates_marker"):
        lines.append(f"NO_PAPER_CANDIDATES_MARKER: {final.get('no_paper_candidates_marker')}")
    lines.extend([
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        f"final_recommendation: {payload.get('final_recommendation')}",
        "RESEARCH PACK V8.2.7 END",
    ])
    return "\n".join(lines)
