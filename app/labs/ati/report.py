"""Validated snapshot ingestion and ATI V2 research report generation."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from . import FEATURE_VERSION, POLICY_VERSION, safety_envelope
from .contracts import REPO_ROOT, contract_receipt, file_sha256
from .features import AtiDataError, build_feature_frame, read_ohlcv_csv
from .metrics import chronological_validation, group_metrics, summarize_trades
from .replay import AtiCostModel, replay_candidates
from .rules import generate_candidates

DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "research" / "ati"
DEFAULT_SAMPLE_ROOT = REPO_ROOT / "external_data" / "staging"


def _safe_number(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _safe_number(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_number(item) for item in value]
    return value


def _json_text(value: Any) -> str:
    return json.dumps(_safe_number(value), indent=2, ensure_ascii=True, allow_nan=False, default=str)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _reject_symlink_ancestors(path: Path, stop: Path) -> None:
    for ancestor in (path, *path.parents):
        if ancestor.exists() and ancestor.is_symlink():
            raise ValueError("ATI_OUTPUT_SYMLINK_BLOCKED")
        if ancestor == stop:
            break


def _safe_output_dir(value: Path | str | None) -> Path:
    declared_root = DEFAULT_OUTPUT_DIR.absolute()
    _reject_symlink_ancestors(declared_root, REPO_ROOT.parent.absolute())
    root = declared_root.resolve()
    declared_target = Path(value).absolute() if value else declared_root
    _reject_symlink_ancestors(declared_target, REPO_ROOT.parent.absolute())
    target = declared_target.resolve()
    if target != root and root not in target.parents:
        raise ValueError("ATI_OUTPUT_OUTSIDE_RESEARCH_ROOT")
    return target


def _manifest_for(csv_path: Path) -> Path:
    return csv_path.with_name(csv_path.stem + "_manifest.json")


def _validate_manifest(csv_path: Path, symbol: str) -> dict[str, Any]:
    manifest_path = _manifest_for(csv_path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AtiDataError("ATI_MANIFEST_MISSING_OR_INVALID") from exc
    if not isinstance(manifest, dict):
        raise AtiDataError("ATI_MANIFEST_NOT_OBJECT")
    blockers = []
    if str(manifest.get("symbol") or "").upper() != symbol:
        blockers.append("symbol_mismatch")
    if str(manifest.get("timeframe") or "").lower() != "1m":
        blockers.append("timeframe_not_1m")
    if manifest.get("quality_pass") is not True or manifest.get("raw_quality_pass") is not True:
        blockers.append("quality_not_pass")
    if manifest.get("download_complete") is not True:
        blockers.append("download_incomplete")
    actual_sha = file_sha256(csv_path)
    if manifest.get("sha256") != actual_sha:
        blockers.append("sha256_mismatch")
    if manifest.get("uses_api_keys") is not False or manifest.get("can_send_real_orders") is not False:
        blockers.append("unsafe_manifest_flags")
    if blockers:
        raise AtiDataError("ATI_MANIFEST_FAIL:" + ",".join(blockers))
    return {**manifest, "manifest_path": str(manifest_path), "verified_sha256": actual_sha}


def discover_sample_dir(symbols: list[str]) -> Path | None:
    candidates: list[tuple[float, Path]] = []
    if not DEFAULT_SAMPLE_ROOT.is_dir():
        return None
    for directory in DEFAULT_SAMPLE_ROOT.iterdir():
        if not directory.is_dir() or directory.is_symlink():
            continue
        if all((directory / f"bitget_{symbol}_1m.csv").is_file() for symbol in symbols):
            mtimes = [(directory / f"bitget_{symbol}_1m.csv").stat().st_mtime for symbol in symbols]
            candidates.append((min(mtimes), directory))
    return max(candidates, default=(0.0, None), key=lambda item: item[0])[1]


def _csv_text(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    from io import StringIO
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value for key, value in row.items()})
    return buffer.getvalue()


def _jsonl_text(rows: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(_safe_number(row), sort_keys=True, ensure_ascii=True, allow_nan=False, default=str) + "\n" for row in rows)


def _composite_hash(receipts: list[dict[str, Any]]) -> str:
    payload = "|".join(sorted(str(item["verified_sha256"]) for item in receipts))
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _history_days(audits: list[dict[str, Any]]) -> float:
    coverage: list[float] = []
    for audit in audits:
        try:
            first = pd.Timestamp(audit["first_timestamp"])
            last = pd.Timestamp(audit["last_timestamp"])
            step_ms = int(audit.get("expected_step_ms") or 0)
            days = ((last - first).total_seconds() + step_ms / 1000.0) / 86_400.0
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(days) and days >= 0:
            coverage.append(days)
    return min(coverage) if coverage else 0.0


def _dataset_available_at(audits: list[dict[str, Any]]) -> str | None:
    values: list[pd.Timestamp] = []
    for audit in audits:
        try:
            opened = pd.Timestamp(audit["last_timestamp"])
            step_ms = int(audit.get("expected_step_ms") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        values.append(opened + pd.Timedelta(milliseconds=step_ms))
    return max(values).isoformat() if values else None


def run_historical_replay(
    *,
    sample_dir: Path | str | None = None,
    symbols: list[str] | None = None,
    output_dir: Path | str | None = None,
    seed: int = 7,
    write: bool = True,
) -> dict[str, Any]:
    symbols = [str(symbol).upper() for symbol in (symbols or ["BTCUSDT", "ETHUSDT"])]
    source_dir = Path(sample_dir).resolve() if sample_dir else discover_sample_dir(symbols)
    base = {
        "schema": "ati_shadow_replay.v2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbols_requested": symbols,
        "policy": contract_receipt(),
        **safety_envelope(),
    }
    if source_dir is None or not source_dir.is_dir() or source_dir.is_symlink():
        return {**base, "status": "NEED_DATA", "blockers": ["validated_sample_dir_missing"]}
    receipts: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    all_candidates: list[dict[str, Any]] = []
    all_trades: list[dict[str, Any]] = []
    for symbol in symbols:
        csv_path = source_dir / f"bitget_{symbol}_1m.csv"
        if csv_path.parent.resolve() != source_dir.resolve() or csv_path.is_symlink():
            return {**base, "status": "NEED_DATA", "blockers": [f"unsafe_input:{symbol}"]}
        try:
            receipt = _validate_manifest(csv_path, symbol)
            raw, audit = read_ohlcv_csv(csv_path, symbol=symbol, timeframe="1m")
            feature_frame = build_feature_frame(raw)
        except (AtiDataError, OSError, ValueError) as exc:
            return {**base, "status": "NEED_DATA", "blockers": [f"{symbol}:{exc}"]}
        receipts.append(receipt)
        audits.append({"symbol": symbol, **audit.to_dict(), "feature_bars_15m": len(feature_frame)})
        candidates = generate_candidates(
            feature_frame, symbol=symbol,
            dataset_source=f"{receipt.get('venue', 'bitget')}:{receipt['verified_sha256']}",
        )
        trades = replay_candidates(feature_frame, candidates, costs=AtiCostModel())
        all_candidates.extend(candidates)
        all_trades.extend(trades)
    complete_trades = [row for row in all_trades if row.get("outcome_complete") is True]
    baseline = [row for row in complete_trades if row["policy"] == "baseline_structural_1_5R"]
    by_setup = group_metrics(baseline, ("setup_id", "setup_variant"), seed=seed)
    by_symbol = group_metrics(baseline, ("symbol",), seed=seed)
    by_regime = group_metrics(baseline, ("regime",), seed=seed)
    trailing = group_metrics(complete_trades, ("policy",), seed=seed)
    validation = {
        key: chronological_validation(
            [row for row in baseline if row["setup_id"] == key], seed=seed + idx
        )
        for idx, key in enumerate(("SHORT_R1", "SHORT_S1", "LONG_R1", "LONG_S1"))
    }
    history_days = _history_days(audits)
    dataset_available_at = _dataset_available_at(audits)
    passing_setups = {
        setup for setup, result in validation.items()
        if result.get("status") == "PASS_SHADOW_RESEARCH_ONLY"
    }
    any_promising = any(
        item["result_status"] == "PROMISING_SHADOW_ONLY"
        and item.get("setup_id") in passing_setups
        for item in by_setup
    )
    blockers = ["paper_forward_30_days_not_met", "human_audit_required"]
    if history_days < 180:
        blockers.append("history_below_180_days")
    blockers.extend(
        f"chronological_validation_not_passed:{setup}"
        for setup, result in validation.items()
        if result.get("status") != "PASS_SHADOW_RESEARCH_ONLY"
    )
    decision_counts: dict[str, int] = {}
    for candidate in all_candidates:
        decision = str(candidate.get("decision") or "UNKNOWN")
        decision_counts[decision] = decision_counts.get(decision, 0) + 1
    summary = {
        **base,
        "status": "PROMISING_SHADOW_ONLY" if any_promising and history_days >= 180 else "INSUFFICIENT_DATA_OR_REJECTED",
        "dataset_source_dir": str(source_dir),
        "dataset_snapshot_sha256": _composite_hash(receipts),
        "dataset_receipts": receipts,
        "data_audits": audits,
        "history_days": history_days,
        "dataset_available_at": dataset_available_at,
        "signals_total": len(all_candidates),
        "decision_counts": dict(sorted(decision_counts.items())),
        "shadow_candidates": sum(row["decision"] == "SHADOW_CANDIDATE" for row in all_candidates),
        "baseline_trades": len(baseline),
        "simulated_policy_rows": len(complete_trades),
        "incomplete_policy_rows": len(all_trades) - len(complete_trades),
        "overall_baseline": summarize_trades(baseline, seed=seed),
        "by_setup": by_setup,
        "by_symbol": by_symbol,
        "by_regime": by_regime,
        "trailing_grid": trailing,
        "chronological_validation": validation,
        "limitations": [
            f"available common history is {history_days:.2f}d; serious validation requires at least 180d",
            "funding is estimated, not joined from a realized funding series",
            "OHLCV cannot identify true intrabar order sequencing",
            "STOP_BEFORE_TP is used for same-bar ambiguity",
            "no setup is automatically promoted",
        ],
        "blockers": sorted(blockers),
    }
    if write:
        target = _safe_output_dir(output_dir)
        _atomic_write(target / "ati_signals.jsonl", _jsonl_text(all_candidates))
        _atomic_write(target / "ati_shadow_trades.jsonl", _jsonl_text(all_trades))
        _atomic_write(target / "ati_summary.json", _json_text(summary))
        _atomic_write(target / "ati_summary.csv", _csv_text([{
            "status": summary["status"], "signals_total": summary["signals_total"],
            "shadow_candidates": summary["shadow_candidates"], "baseline_trades": len(baseline),
            **summary["overall_baseline"], "final_recommendation": "NO LIVE",
        }]))
        _atomic_write(target / "ati_by_setup.csv", _csv_text(by_setup))
        _atomic_write(target / "ati_by_symbol.csv", _csv_text(by_symbol))
        _atomic_write(target / "ati_by_regime.csv", _csv_text(by_regime))
        _atomic_write(target / "ati_trailing_grid.csv", _csv_text(trailing))
        _atomic_write(target / "ati_open_positions.json", _json_text([]))
        health = {
            "status": "HEALTHY" if baseline else "NO_DATA",
            "last_run_at": summary["generated_at"],
            "age_seconds": 0,
            "signals_total": summary["signals_total"],
            "open_positions": 0,
            "closed_shadow_trades": len(baseline),
            "last_error": None,
            "dataset_last_bar_at": max(item["last_timestamp"] for item in audits),
            "dataset_available_at": dataset_available_at,
            "dataset_snapshot_sha256": summary["dataset_snapshot_sha256"],
            "result_status": summary["status"],
            **safety_envelope(),
        }
        if dataset_available_at:
            available = pd.Timestamp(dataset_available_at).to_pydatetime()
            age = max(0.0, (datetime.now(timezone.utc) - available).total_seconds())
            health["dataset_age_seconds"] = age
            health["stale"] = age > 30 * 60
            if health["stale"] and health["status"] == "HEALTHY":
                health["status"] = "DEGRADED"
        _atomic_write(target / "ati_health.json", _json_text(health))
        summary["output_dir"] = str(target)
    return _safe_number(summary)


def render_replay_text(report: dict[str, Any]) -> str:
    overall = report.get("overall_baseline") or {}
    lines = [
        "ATI SHADOW REPLAY V2 START",
        f"status: {report.get('status', 'NEED_DATA')}",
        f"symbols: {','.join(report.get('symbols_requested') or [])}",
        f"dataset_snapshot_sha256: {report.get('dataset_snapshot_sha256', 'N/A')}",
        f"signals_total: {report.get('signals_total', 0)}",
        f"shadow_candidates: {report.get('shadow_candidates', 0)}",
        f"baseline_trades: {report.get('baseline_trades', 0)}",
        f"net_ev: {overall.get('net_ev')}",
        f"profit_factor: {overall.get('profit_factor')}",
        f"win_rate: {overall.get('win_rate')}",
        f"max_drawdown: {overall.get('max_drawdown')}",
        f"blockers: {','.join(report.get('blockers') or []) or 'none'}",
        "research_only: true",
        "shadow_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "paper_ready: false",
        "live_ready: false",
        "final_recommendation: NO LIVE",
        "ATI SHADOW REPLAY V2 END",
    ]
    return "\n".join(lines)
