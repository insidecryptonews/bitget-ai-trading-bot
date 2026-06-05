"""V8.2.5 — Clean dataset export V2 (research-only).

Generates sanitised CSV/TXT/JSON + ZIP exports under
``training_exports/research_v8_2_5/``. Uses the V8.2.4 dataset deduplicated
with the V8.2.5 audit, plus the SHORT / SCORE / COST auxiliary CSVs.

Hard contract:
- never includes ``.env``, secrets, API keys or DB dumps.
- ZIP only contains ``.csv/.txt/.json`` files.
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
from .counterfactual_cost_stress import stress_costs
from .counterfactual_dedup_audit import _is_evaluable, audit_dedup, dedup_rows
from .counterfactual_training_dataset import (
    DATASET_COLUMNS,
    _sanitise_row,
    build_dataset,
)
from .score_calibration_audit import audit_score_calibration
from .short_sign_barrier_audit import audit_short_sign


EXPORT_SUBDIR_V2 = Path("training_exports") / "research_v8_2_5"

SHORT_AUDIT_COLUMNS: tuple[str, ...] = (
    "signal_id", "timestamp", "symbol", "entry_price",
    "ret_1h_pct", "ret_4h_pct", "ret_24h_pct",
    "mfe_pct", "mae_pct", "first_barrier_hit",
    "baseline_result", "baseline_net_pnl",
    "classification", "notes",
)
SCORE_AUDIT_COLUMNS: tuple[str, ...] = (
    "bucket", "count", "winrate", "net_ev_avg_pct",
)
COST_STRESS_COLUMNS: tuple[str, ...] = (
    "cost_pct", "count", "net_ev_avg_pct", "survives",
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


def export_clean_v2(
    db: Any = None,
    *,
    hours: int = 168,
    limit: int = 50000,
    base_dir: Path | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run all four V8.2.5 audits and persist sanitised CSV/TXT/JSON/ZIP.

    ``rows`` allows test injection. Without it the dataset is rebuilt via
    ``build_dataset``.
    """
    base = Path(base_dir) if base_dir else EXPORT_SUBDIR_V2
    base.mkdir(parents=True, exist_ok=True)
    if rows is None:
        dataset, _summary = build_dataset(db, hours=int(hours), limit=int(limit))
    else:
        dataset = list(rows)
    evaluable = [r for r in dataset if _is_evaluable(r)]
    dedup = dedup_rows(evaluable)
    sanitised_dedup = [_sanitise_row(r) for r in dedup]

    # Audits sharing the same dataset (avoid recomputing).
    dedup_report = audit_dedup(db, hours=hours, limit=limit, rows=dataset)
    short_report = audit_short_sign(db, hours=hours, limit=limit, rows=dataset)
    score_report = audit_score_calibration(db, hours=hours, limit=limit, rows=dataset)
    cost_report = stress_costs(db, hours=hours, limit=limit, rows=dataset)

    # CSVs.
    main_csv = base / "counterfactual_training_dataset_dedup_v2.csv"
    _write_csv(main_csv, sanitised_dedup, DATASET_COLUMNS)
    short_csv = base / "short_sign_audit_v2.csv"
    _write_csv(short_csv, short_report.examples_top_50, SHORT_AUDIT_COLUMNS)
    score_csv = base / "score_calibration_audit_v2.csv"
    _write_csv(score_csv, score_report.score_bucket_table, SCORE_AUDIT_COLUMNS)
    cost_csv = base / "cost_stress_audit_v2.csv"
    _write_csv(cost_csv, cost_report.by_cost_level, COST_STRESS_COLUMNS)

    # Summary TXT.
    summary_txt = base / "counterfactual_dedup_summary_v2.txt"
    with summary_txt.open("w", encoding="utf-8") as f:
        f.write("COUNTERFACTUAL DEDUP SUMMARY V2\n")
        f.write(f"generated_at: {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"hours: {hours} limit: {limit}\n")
        f.write(f"total_rows: {dedup_report.total_rows}\n")
        f.write(f"evaluable_rows: {dedup_report.evaluable_rows}\n")
        f.write(f"duplicate_rows: {dedup_report.duplicate_rows}\n")
        f.write(f"unique_outcomes: {dedup_report.unique_outcomes}\n")
        f.write(f"duplicate_ratio: {dedup_report.duplicate_ratio:.4f}\n")
        for k, v in dedup_report.raw_metrics.items():
            f.write(f"raw_{k}: {v}\n")
        for k, v in dedup_report.dedup_metrics.items():
            f.write(f"dedup_{k}: {v}\n")
        f.write(f"short_verdict: {short_report.verdict}\n")
        f.write(f"short_suspicious_ratio: {short_report.suspicious_ratio:.4f}\n")
        f.write(f"score_monotonicity: {score_report.monotonicity_status}\n")
        f.write(f"cost_samples: {cost_report.samples}\n")
        f.write("research_only: true\n")
        f.write("paper_filter_enabled: false\n")
        f.write("can_send_real_orders: false\n")
        f.write(f"final_recommendation: {FINAL_RECOMMENDATION_NO_LIVE}\n")

    files = [main_csv, short_csv, score_csv, cost_csv, summary_txt]
    manifest: dict[str, Any] = {
        "version": "v8.2.5.v2",
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
    manifest_path = base / "manifest_v2.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    manifest["files"].append({
        "name": manifest_path.name,
        "size_bytes": manifest_path.stat().st_size,
        "sha1": _sha1_file(manifest_path),
    })

    # ZIP — only CSV/TXT/JSON inside.
    zip_path = base / "research_v8_2_5_exports.zip"
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


def find_latest_zip_v2(base_dir: Path | None = None) -> Path | None:
    base = Path(base_dir) if base_dir else EXPORT_SUBDIR_V2
    candidate = base / "research_v8_2_5_exports.zip"
    if candidate.exists() and candidate.is_file():
        return candidate
    return None


# ---- Consolidated pack ----------------------------------------------------

def build_pack(db: Any = None, *, hours: int = 168, limit: int = 50000) -> dict[str, Any]:
    """Single payload combining all V8.2.5 audits — for the research pack CLI."""
    dataset, _ = build_dataset(db, hours=int(hours), limit=int(limit))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pack_version": "counterfactual_quality_v1",
        "hours": int(hours),
        "limit": int(limit),
        "dedup": audit_dedup(db, hours=hours, limit=limit, rows=dataset).as_dict(),
        "short_sign": audit_short_sign(db, hours=hours, limit=limit, rows=dataset).as_dict(),
        "score_calibration": audit_score_calibration(
            db, hours=hours, limit=limit, rows=dataset,
        ).as_dict(),
        "cost_stress": stress_costs(db, hours=hours, limit=limit, rows=dataset).as_dict(),
        "research_only": True,
        "paper_filter_enabled": False,
        "can_send_real_orders": False,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }


def render_pack_text(payload: dict[str, Any]) -> str:
    lines = ["RESEARCH PACK COUNTERFACTUAL QUALITY V1 START"]
    lines.append(f"generated_at: {payload.get('generated_at')}")
    lines.append(f"pack_version: {payload.get('pack_version')}")
    lines.append(f"hours: {payload.get('hours')} limit: {payload.get('limit')}")
    dedup = payload.get("dedup") or {}
    lines.append(
        f"dedup: total={dedup.get('total_rows')} evaluable={dedup.get('evaluable_rows')} "
        f"duplicates={dedup.get('duplicate_rows')} ratio={dedup.get('duplicate_ratio', 0.0):.4f}"
    )
    short = payload.get("short_sign") or {}
    lines.append(
        f"short_sign: verdict={short.get('verdict')} "
        f"suspicious_ratio={short.get('suspicious_ratio', 0.0):.4f}"
    )
    score = payload.get("score_calibration") or {}
    lines.append(
        f"score_calibration: monotonicity={score.get('monotonicity_status')} "
        f"corr_net={score.get('correlation_score_vs_net_pnl', 0.0):.4f}"
    )
    cost = payload.get("cost_stress") or {}
    lines.append(f"cost_stress: samples={cost.get('samples')} "
                 f"dedup_used={cost.get('dedup_used')}")
    lines.extend([
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        f"final_recommendation: {payload.get('final_recommendation')}",
        "RESEARCH PACK COUNTERFACTUAL QUALITY V1 END",
    ])
    return "\n".join(lines)
