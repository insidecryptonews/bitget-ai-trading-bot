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
DEFAULT_GENERATION_ROOT = DEFAULT_SAMPLE_ROOT / "klines_v10_45_5"


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


def _is_link_like(path: Path) -> bool:
    """Reject symlinks and Windows junctions before consuming research data."""
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    try:
        return bool(is_junction and is_junction())
    except OSError:
        return True


def _assert_contained_input(path: Path, root: Path) -> Path:
    declared_root = root.absolute()
    declared_path = path.absolute()
    for item in (declared_path, *declared_path.parents):
        if item.exists() and _is_link_like(item):
            raise AtiDataError("ATI_INPUT_LINK_OR_JUNCTION_BLOCKED")
        if item == declared_root:
            break
    resolved_root = declared_root.resolve()
    resolved_path = declared_path.resolve()
    if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
        raise AtiDataError("ATI_INPUT_OUTSIDE_VALIDATED_ROOT")
    return resolved_path


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


def _generation_inputs(symbols: list[str]) -> dict[str, Any] | None:
    """Resolve V10.45.5 CURRENT generations and fully verify their CSV truth."""
    from .. import public_data_backfill_v10_45_1 as backfill

    entries: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        current = backfill.current_generation("bitget", symbol)
        verified = backfill.verify_dataset("bitget", symbol, expected_timeframe="1m")
        if current is None or verified.get("status") != "DATASET_VERIFIED":
            return None
        csv_path = _assert_contained_input(Path(current["csv_path"]), DEFAULT_GENERATION_ROOT)
        manifest_path = _assert_contained_input(
            Path(current["manifest_path"]), DEFAULT_GENERATION_ROOT,
        )
        if csv_path.name != "data.csv" or manifest_path.name != "manifest.json":
            raise AtiDataError("ATI_GENERATION_FILENAME_CONTRACT_FAIL")
        manifest = verified.get("manifest")
        if not isinstance(manifest, dict):
            raise AtiDataError("ATI_GENERATION_MANIFEST_NOT_OBJECT")
        stat = csv_path.stat()
        receipt = {
            **manifest,
            "manifest_path": str(manifest_path),
            "verified_sha256": str(verified["sha256"]),
            "generation_id": str(verified["generation_id"]),
            "verification_status": "DATASET_VERIFIED",
            "source_file_mtime": datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc,
            ).isoformat(),
            "source_file_age_seconds": max(
                0.0, datetime.now(timezone.utc).timestamp() - stat.st_mtime,
            ),
        }
        entries[symbol] = {"csv_path": csv_path, "receipt": receipt}
    return {
        "mode": "v10_45_5_verified_current_generations",
        "series_id": "v10_45_5:bitget:1m:" + ",".join(sorted(symbols)),
        "source_dir": DEFAULT_GENERATION_ROOT.resolve(),
        "entries": entries,
    }


def _legacy_inputs(source_dir: Path, symbols: list[str], *, mode: str) -> dict[str, Any]:
    source_dir = _assert_contained_input(source_dir, source_dir)
    entries: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        csv_path = _assert_contained_input(
            source_dir / f"bitget_{symbol}_1m.csv", source_dir,
        )
        receipt = _validate_manifest(csv_path, symbol)
        stat = csv_path.stat()
        receipt.update({
            "verification_status": "LEGACY_MANIFEST_AND_SHA_VERIFIED",
            "source_file_mtime": datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc,
            ).isoformat(),
            "source_file_age_seconds": max(
                0.0, datetime.now(timezone.utc).timestamp() - stat.st_mtime,
            ),
        })
        entries[symbol] = {"csv_path": csv_path, "receipt": receipt}
    return {
        "mode": mode,
        "series_id": f"legacy_flat:{source_dir.as_posix()}:{','.join(sorted(symbols))}",
        "source_dir": source_dir,
        "entries": entries,
    }


def _resolve_inputs(sample_dir: Path | str | None, symbols: list[str]) -> dict[str, Any] | None:
    if sample_dir is not None:
        return _legacy_inputs(Path(sample_dir), symbols, mode="explicit_legacy_flat_snapshot")
    generated = _generation_inputs(symbols)
    if generated is not None:
        return generated
    legacy = discover_sample_dir(symbols)
    if legacy is None:
        return None
    return _legacy_inputs(legacy, symbols, mode="auto_legacy_flat_fallback")


def source_snapshot_status(*, sample_dir: Path | str | None = None,
                           symbols: list[str] | None = None) -> dict[str, Any]:
    """Return a cheap change token; replay still performs full verification.

    The token includes CURRENT bytes plus file metadata. It is only a watcher
    optimization and is never accepted as evidence that a dataset is valid.
    """
    symbols = [str(symbol).upper() for symbol in (symbols or ["BTCUSDT", "ETHUSDT"])]
    parts: list[dict[str, Any]] = []
    blockers: list[str] = []
    mode = "explicit_legacy_flat_snapshot" if sample_dir is not None else "v10_45_5_current_markers"
    try:
        if sample_dir is not None:
            root = _assert_contained_input(Path(sample_dir), Path(sample_dir))
            for symbol in symbols:
                csv_path = _assert_contained_input(root / f"bitget_{symbol}_1m.csv", root)
                manifest_path = _assert_contained_input(_manifest_for(csv_path), root)
                if not csv_path.is_file() or not manifest_path.is_file():
                    blockers.append(f"missing_legacy_source:{symbol}")
                    continue
                csv_stat = csv_path.stat()
                man_stat = manifest_path.stat()
                parts.append({
                    "symbol": symbol,
                    "csv_path": str(csv_path),
                    "csv_size": csv_stat.st_size,
                    "csv_mtime_ns": csv_stat.st_mtime_ns,
                    "manifest_size": man_stat.st_size,
                    "manifest_mtime_ns": man_stat.st_mtime_ns,
                })
        else:
            from .. import public_data_backfill_v10_45_1 as backfill
            for symbol in symbols:
                dataset_dir = backfill._dataset_dir("bitget", symbol)
                marker = _assert_contained_input(
                    dataset_dir / backfill.CURRENT_MARKER, DEFAULT_GENERATION_ROOT,
                )
                if not marker.is_file():
                    blockers.append(f"current_marker_missing:{symbol}")
                    continue
                marker_bytes = marker.read_bytes()
                try:
                    current = json.loads(marker_bytes.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    blockers.append(f"current_marker_invalid:{symbol}")
                    continue
                generation_id = str(current.get("generation_id") or "")
                if not generation_id or not generation_id.isalnum():
                    blockers.append(f"generation_id_invalid:{symbol}")
                    continue
                generation_dir = _assert_contained_input(
                    dataset_dir / f"gen_{generation_id}", DEFAULT_GENERATION_ROOT,
                )
                csv_path = _assert_contained_input(
                    generation_dir / "data.csv", DEFAULT_GENERATION_ROOT,
                )
                manifest_path = _assert_contained_input(
                    generation_dir / "manifest.json", DEFAULT_GENERATION_ROOT,
                )
                if not csv_path.is_file() or not manifest_path.is_file():
                    blockers.append(f"generation_files_missing:{symbol}")
                    continue
                csv_stat = csv_path.stat()
                man_stat = manifest_path.stat()
                parts.append({
                    "symbol": symbol,
                    "generation_id": generation_id,
                    "marker_sha256": hashlib.sha256(marker_bytes).hexdigest(),
                    "csv_path": str(csv_path),
                    "csv_size": csv_stat.st_size,
                    "csv_mtime_ns": csv_stat.st_mtime_ns,
                    "manifest_size": man_stat.st_size,
                    "manifest_mtime_ns": man_stat.st_mtime_ns,
                })
    except (AtiDataError, OSError, ValueError) as exc:
        blockers.append(str(exc))
    policy = contract_receipt()
    token_payload = {
        "mode": mode,
        "symbols": symbols,
        "parts": parts,
        "policy_sha256": policy.get("policy_sha256"),
        "feature_version": policy.get("feature_version"),
    }
    token = hashlib.sha256(
        json.dumps(token_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "status": "SNAPSHOT_AVAILABLE" if len(parts) == len(symbols) and not blockers else "NEED_DATA",
        "snapshot_watch_token": token,
        "source_mode": mode,
        "symbols": symbols,
        "sources": parts,
        "blockers": blockers,
        "verification_scope": "CHANGE_DETECTION_ONLY_FULL_VERIFY_BEFORE_REPLAY",
        **safety_envelope(),
    }


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
    base = {
        "schema": "ati_shadow_replay.v2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbols_requested": symbols,
        "policy": contract_receipt(),
        **safety_envelope(),
    }
    try:
        inputs = _resolve_inputs(sample_dir, symbols)
    except (AtiDataError, OSError, ValueError) as exc:
        return {**base, "status": "NEED_DATA", "blockers": [str(exc)]}
    if inputs is None:
        return {**base, "status": "NEED_DATA", "blockers": ["validated_sample_dir_missing"]}
    source_dir = Path(inputs["source_dir"])
    receipts: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    all_candidates: list[dict[str, Any]] = []
    all_trades: list[dict[str, Any]] = []
    for symbol in symbols:
        entry = inputs["entries"][symbol]
        csv_path = Path(entry["csv_path"])
        try:
            receipt = dict(entry["receipt"])
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
        "dataset_source_mode": inputs["mode"],
        "dataset_source_series_id": inputs["series_id"],
        "dataset_source_paths": {
            symbol: str(inputs["entries"][symbol]["csv_path"]) for symbol in symbols
        },
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
        f"dataset_source_mode: {report.get('dataset_source_mode', 'N/A')}",
        f"history_days: {report.get('history_days')}",
        f"dataset_snapshot_sha256: {report.get('dataset_snapshot_sha256', 'N/A')}",
        f"signals_total: {report.get('signals_total', 0)}",
        f"shadow_candidates: {report.get('shadow_candidates', 0)}",
        f"baseline_trades: {report.get('baseline_trades', 0)}",
        f"net_ev: {overall.get('net_ev')}",
        f"profit_factor: {overall.get('profit_factor')}",
        f"win_rate: {overall.get('win_rate')}",
        f"max_drawdown: {overall.get('max_drawdown')}",
        f"average_mfe: {overall.get('average_mfe')}",
        f"average_mae: {overall.get('average_mae')}",
        f"fees: {overall.get('fees')}",
        f"slippage: {overall.get('slippage')}",
        f"funding: {overall.get('funding')}",
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
