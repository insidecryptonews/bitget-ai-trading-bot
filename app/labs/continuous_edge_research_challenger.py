"""Bounded, causal challenger over verified Storage Efficiency V2 features.

The challenger is intentionally unable to edit an active policy, paper account,
or execution path.  It searches a small preregistered family set, evaluates only
train and validation data, and leaves the chronological holdout sealed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .cross_venue import REPO_ROOT, STAGING_ROOT, safety_envelope
from .storage_efficiency_v2 import (
    FEATURE_MANIFEST_PATH,
    load_storage_config,
)

TOOL_VERSION = "CONTINUOUS_EDGE_RESEARCH_CHALLENGER_V2"
REPORT_ROOT = REPO_ROOT / "reports" / "research" / "continuous_edge_challenger"
STATUS_PATH = REPO_ROOT / "data" / "runtime" / "storage_efficiency_v2" / "challenger_status.json"
PROHIBITED_FEATURE_TOKENS = frozenset({
    "future", "outcome", "label", "mfe", "mae", "ret_", "pnl", "barrier",
    "exit_price", "first_barrier_hit", "training_label",
})
COST_SCENARIOS_BPS = (14.5, 15.5, 18.0)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def challenger_safety() -> dict[str, Any]:
    return {
        **safety_envelope(),
        "activation": "disabled",
        "auto_promotion": False,
        "active_policy_modified": False,
        "holdout_auto_unlock": False,
        "maximum_automatic_state": "WATCH_ONLY",
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("CHALLENGER_SYMLINK_BLOCKED")
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dataset_contract(
    feature_manifest_path: Path = FEATURE_MANIFEST_PATH,
) -> tuple[list[Path], str, list[str]]:
    manifest = _read_json(feature_manifest_path, {})
    segments = manifest.get("segments") if isinstance(manifest, dict) else None
    paths: list[Path] = []
    hashes: list[str] = []
    source_ids: list[str] = []
    for source_id, record in sorted((segments or {}).items()):
        if not isinstance(record, dict) or record.get("status") != "VERIFIED_FEATURES":
            continue
        for output in record.get("outputs") or []:
            relative = str(output.get("path") or "")
            sha = str(output.get("sha256") or "")
            if not relative or not sha:
                continue
            path = STAGING_ROOT / relative
            resolved = path.resolve(strict=False)
            root = STAGING_ROOT.resolve(strict=False)
            if root not in resolved.parents or path.is_symlink() or not path.is_file():
                continue
            actual_sha = _file_sha256(path)
            if actual_sha != sha:
                raise RuntimeError("CHALLENGER_FEATURE_SHA_MISMATCH")
            paths.append(path)
            hashes.append(actual_sha)
            source_ids.append(str(source_id))
    payload = "|".join(sorted(hashes)).encode("ascii")
    return paths, hashlib.sha256(payload).hexdigest(), sorted(set(source_ids))


def load_feature_rows(
    feature_manifest_path: Path = FEATURE_MANIFEST_PATH,
    *, symbols: Iterable[str] | None = None, max_rows: int = 500_000,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    paths, dataset_hash, source_ids = _dataset_contract(feature_manifest_path)
    wanted = {str(symbol).upper() for symbol in symbols or () if str(symbol).strip()}
    if not paths:
        return [], {
            "status": "NEED_MORE_DATA", "dataset_hash": dataset_hash,
            "verified_feature_files": 0, "source_partition_ids": source_ids,
        }
    try:
        import pyarrow.parquet as pq  # type: ignore
    except ImportError:
        return [], {
            "status": "NEED_DEPENDENCY", "reason": "PYARROW_NOT_INSTALLED",
            "dataset_hash": dataset_hash, "verified_feature_files": len(paths),
            "source_partition_ids": source_ids,
        }
    rows: list[dict[str, Any]] = []
    required = {
        "venue", "canonical_symbol", "bucket_start_ms", "causal_cutoff_ms",
        "first_event_timestamp_ms", "last_event_timestamp_ms", "first_midpoint",
        "last_midpoint", "spread_bps", "book_imbalance", "aggressive_buy_volume",
        "aggressive_sell_volume", "net_aggressor_volume", "trade_intensity_per_second",
        "price_return_bps", "gap_flag", "source_partition_id", "dataset_hash",
        "feature_version",
    }
    path_rows: list[tuple[Path, int]] = []
    for path in sorted(paths):
        parquet = pq.ParquetFile(path)
        try:
            path_rows.append((path, int(parquet.metadata.num_rows)))
        finally:
            parquet.close()
    total_available_rows = sum(count for _, count in path_rows)
    budget = max(1, int(max_rows))
    quotas: dict[Path, int] = {}
    if total_available_rows <= budget:
        quotas = {path: count for path, count in path_rows}
    else:
        active = [(path, count) for path, count in path_rows if count > 0]
        reserved = min(len(active), budget)
        quotas = {path: (1 if index < reserved else 0) for index, (path, _) in enumerate(active)}
        remaining = budget - sum(quotas.values())
        if remaining > 0 and total_available_rows > 0:
            shares = [
                (remaining * count / total_available_rows, path)
                for path, count in active
            ]
            for raw, path in shares:
                quotas[path] += int(raw)
            leftover = budget - sum(quotas.values())
            for _, path in sorted(
                ((raw - int(raw), path) for raw, path in shares),
                key=lambda item: (-item[0], str(item[1])),
            )[:leftover]:
                quotas[path] += 1
        for path, count in active:
            quotas[path] = min(count, quotas.get(path, 0))
    for path, file_row_count in path_rows:
        quota = quotas.get(path, 0)
        if quota <= 0 or file_row_count <= 0:
            continue
        selected_indices = (
            None if quota >= file_row_count else {
                min(file_row_count - 1, int((index + 0.5) * file_row_count / quota))
                for index in range(quota)
            }
        )
        parquet = pq.ParquetFile(path)
        horizon_ms = 0
        for part in path.parts:
            if part.startswith("horizon_ms="):
                try:
                    horizon_ms = int(part.split("=", 1)[1])
                except ValueError:
                    horizon_ms = 0
                break
        if horizon_ms <= 0:
            continue
        available = set(parquet.schema_arrow.names)
        if not required.issubset(available):
            continue
        local_index = 0
        for batch in parquet.iter_batches(batch_size=50_000, columns=sorted(required)):
            for row in batch.to_pylist():
                take = selected_indices is None or local_index in selected_indices
                local_index += 1
                if not take:
                    continue
                symbol = str(row.get("canonical_symbol") or "").upper()
                if wanted and symbol not in wanted:
                    continue
                timestamp = _finite(row.get("bucket_start_ms"))
                cutoff = _finite(row.get("causal_cutoff_ms"))
                first_ts = _finite(row.get("first_event_timestamp_ms"))
                last_ts = _finite(row.get("last_event_timestamp_ms"))
                first_mid = _finite(row.get("first_midpoint"))
                last_mid = _finite(row.get("last_midpoint"))
                if None in (timestamp, cutoff, first_ts, last_ts, first_mid, last_mid):
                    continue
                if first_mid <= 0 or last_mid <= 0 or first_ts > last_ts or last_ts > cutoff:
                    continue
                clean = dict(row)
                clean.update({
                    "bucket_start_ms": int(timestamp),
                    "causal_cutoff_ms": int(cutoff),
                    "first_event_timestamp_ms": int(first_ts),
                    "last_event_timestamp_ms": int(last_ts),
                    "first_midpoint": first_mid,
                    "last_midpoint": last_mid,
                    "canonical_symbol": symbol,
                    "venue": str(row.get("venue") or "").lower(),
                    "horizon_ms": horizon_ms,
                })
                rows.append(clean)
    rows.sort(key=lambda row: (
        int(row["bucket_start_ms"]), str(row["canonical_symbol"]), str(row["venue"]),
    ))
    return rows, {
        "status": (
            "OK_DOWNSAMPLED_RESOURCE_BUDGET"
            if total_available_rows > budget and rows else
            "OK" if rows else "NEED_MORE_DATA"
        ),
        "dataset_hash": dataset_hash,
        "verified_feature_files": len(paths),
        "source_partition_ids": source_ids,
        "rows": len(rows),
        "total_available_rows": total_available_rows,
        "maximum_feature_rows": budget,
        "downsampled": total_available_rows > budget,
        "sampling_method": (
            "DETERMINISTIC_EVEN_WITHIN_VERIFIED_FILE"
            if total_available_rows > budget else "ALL_VERIFIED_ROWS"
        ),
        "sampling_is_not_full_dataset": total_available_rows > budget,
    }


def _augment_prefix_features(rows: list[dict[str, Any]]) -> None:
    by_bucket: dict[tuple[str, int, int], list[dict[str, Any]]] = defaultdict(list)
    by_stream: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_bucket[(
            str(row["canonical_symbol"]), int(row["horizon_ms"]),
            int(row["bucket_start_ms"]),
        )].append(row)
        by_stream[(
            str(row["canonical_symbol"]), str(row["venue"]), int(row["horizon_ms"]),
        )].append(row)
    for bucket_rows in by_bucket.values():
        directions = []
        for row in bucket_rows:
            value = _finite(row.get("price_return_bps"))
            if value is not None and value != 0:
                directions.append(1 if value > 0 else -1)
        consensus = sum(directions) / len(directions) if directions else 0.0
        available_at = max(int(row["causal_cutoff_ms"]) for row in bucket_rows)
        for row in bucket_rows:
            row["consensus_strength"] = consensus
            row["consensus_venues"] = len(directions)
            row["consensus_available_at_ms"] = available_at
    for stream_rows in by_stream.values():
        stream_rows.sort(key=lambda row: int(row["bucket_start_ms"]))
        history: list[int] = []
        for row in stream_rows:
            value = _finite(row.get("price_return_bps")) or 0.0
            direction = 1 if value > 0 else -1 if value < 0 else 0
            history.append(direction)
            prefix = history[-5:]
            row["direction_persistence_5"] = sum(prefix) / max(1, len(prefix))


def compile_trial_specs(max_families: int = 5, max_trials: int = 80) -> list[dict[str, Any]]:
    families = [
        ("extreme_flow", ("flow_threshold",), (0.45, 0.60, 0.75)),
        ("temporal_consensus", ("consensus_threshold",), (0.50, 0.75, 1.00)),
        ("order_flow_precursor", ("imbalance_threshold",), (0.25, 0.45, 0.65)),
        ("leader_stability", ("persistence_threshold",), (0.40, 0.60, 0.80)),
        ("short_trend_down", ("persistence_threshold",), (0.40, 0.60, 0.80)),
    ][: max(0, int(max_families))]
    specs: list[dict[str, Any]] = []
    for family, parameter_names, values in families:
        for threshold in values:
            for holding_buckets in (1, 2, 5, 10):
                spec = {
                    "family": family,
                    parameter_names[0]: threshold,
                    "holding_buckets": holding_buckets,
                    "required_features": {
                        "extreme_flow": ["aggressive_buy_volume", "aggressive_sell_volume"],
                        "temporal_consensus": ["consensus_strength", "consensus_venues"],
                        "order_flow_precursor": ["book_imbalance", "net_aggressor_volume"],
                        "leader_stability": ["direction_persistence_5", "price_return_bps"],
                        "short_trend_down": ["direction_persistence_5", "price_return_bps"],
                    }[family],
                }
                specs.append(spec)
                if len(specs) >= max(0, int(max_trials)):
                    break
            if len(specs) >= max(0, int(max_trials)):
                break
        if len(specs) >= max(0, int(max_trials)):
            break
    for spec in specs:
        for feature in spec["required_features"]:
            lowered = feature.lower()
            if any(token in lowered for token in PROHIBITED_FEATURE_TOKENS):
                raise ValueError(f"CHALLENGER_FEATURE_LEAKAGE:{feature}")
        spec["trial_id"] = hashlib.sha256(
            json.dumps(spec, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
    return specs


def _signal(row: dict[str, Any], spec: dict[str, Any]) -> int:
    family = spec["family"]
    if int(row.get("gap_flag") or 0) != 0:
        return 0
    if family == "extreme_flow":
        buy = max(0.0, _finite(row.get("aggressive_buy_volume")) or 0.0)
        sell = max(0.0, _finite(row.get("aggressive_sell_volume")) or 0.0)
        ratio = (buy - sell) / max(buy + sell, 1e-12)
        threshold = float(spec["flow_threshold"])
        return 1 if ratio >= threshold else -1 if ratio <= -threshold else 0
    if family == "temporal_consensus":
        if int(row.get("consensus_venues") or 0) < 2:
            return 0
        value = _finite(row.get("consensus_strength")) or 0.0
        threshold = float(spec["consensus_threshold"])
        return 1 if value >= threshold else -1 if value <= -threshold else 0
    if family == "order_flow_precursor":
        imbalance = _finite(row.get("book_imbalance")) or 0.0
        flow = _finite(row.get("net_aggressor_volume")) or 0.0
        threshold = float(spec["imbalance_threshold"])
        return 1 if imbalance >= threshold and flow > 0 else -1 if imbalance <= -threshold and flow < 0 else 0
    persistence = _finite(row.get("direction_persistence_5")) or 0.0
    current = _finite(row.get("price_return_bps")) or 0.0
    threshold = float(spec["persistence_threshold"])
    if family == "leader_stability":
        return 1 if persistence >= threshold and current > 0 else -1 if persistence <= -threshold and current < 0 else 0
    if family == "short_trend_down":
        return -1 if persistence <= -threshold and current < 0 else 0
    return 0


def _opportunities(
    rows: list[dict[str, Any]], spec: dict[str, Any],
    *, start_ms: int, end_ms: int,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(
            str(row["canonical_symbol"]), str(row["venue"]),
            str(row.get("source_partition_id") or ""), int(row["horizon_ms"]),
        )].append(row)
    outcomes: list[dict[str, Any]] = []
    hold = int(spec["holding_buckets"])
    for stream in grouped.values():
        stream.sort(key=lambda row: int(row["bucket_start_ms"]))
        if len(stream) < hold + 2:
            continue
        diffs = [
            int(stream[index + 1]["bucket_start_ms"]) - int(stream[index]["bucket_start_ms"])
            for index in range(len(stream) - 1)
            if int(stream[index + 1]["bucket_start_ms"]) > int(stream[index]["bucket_start_ms"])
        ]
        expected_step = int(statistics.median(diffs)) if diffs else 0
        if expected_step <= 0:
            continue
        for index in range(0, len(stream) - hold - 1):
            row = stream[index]
            signal_ts = max(
                int(row["causal_cutoff_ms"]),
                int(row.get("consensus_available_at_ms") or 0),
            )
            if signal_ts < start_ms or signal_ts >= end_ms:
                continue
            side = _signal(row, spec)
            if side == 0:
                continue
            entry = stream[index + 1]
            exit_row = stream[index + 1 + hold]
            entry_ts = int(entry["first_event_timestamp_ms"])
            exit_ts = int(exit_row["last_event_timestamp_ms"])
            if entry_ts <= signal_ts or exit_ts >= end_ms:
                continue
            path = stream[index:index + hold + 2]
            if any(
                int(path[j + 1]["bucket_start_ms"]) - int(path[j]["bucket_start_ms"]) > expected_step * 2
                for j in range(len(path) - 1)
            ):
                continue
            entry_price = float(entry["first_midpoint"])
            exit_price = float(exit_row["last_midpoint"])
            gross_bps = side * (exit_price / entry_price - 1.0) * 10_000
            if not math.isfinite(gross_bps):
                continue
            outcomes.append({
                "signal_timestamp_ms": signal_ts,
                "entry_timestamp_ms": entry_ts,
                "exit_timestamp_ms": exit_ts,
                "symbol": row["canonical_symbol"],
                "venue": row["venue"],
                "side": "LONG" if side > 0 else "SHORT",
                "gross_bps": gross_bps,
            })
    outcomes.sort(key=lambda row: int(row["entry_timestamp_ms"]))
    return outcomes


def _effective_sample_size(outcomes: list[dict[str, Any]], net: list[float]) -> float:
    if not net:
        return 0.0
    non_overlapping = 0
    last_exit = -1
    for row in outcomes:
        if int(row["entry_timestamp_ms"]) >= last_exit:
            non_overlapping += 1
            last_exit = int(row["exit_timestamp_ms"])
    if len(net) < 3 or statistics.pvariance(net) <= 1e-18:
        autocorrelation_n = float(len(net) if len(set(net)) > 1 else 1)
    else:
        mean = statistics.fmean(net)
        denominator = sum((value - mean) ** 2 for value in net)
        rho = sum(
            (net[index] - mean) * (net[index - 1] - mean)
            for index in range(1, len(net))
        ) / max(denominator, 1e-18)
        rho = max(-0.95, min(0.95, rho))
        autocorrelation_n = len(net) * (1.0 - rho) / (1.0 + rho)
    return max(1.0, min(float(len(net)), float(non_overlapping), autocorrelation_n))


def _moving_block_lower_bound(
    net: list[float], *, alpha: float, seed: int, iterations: int = 400,
) -> float | None:
    if len(net) < 2:
        return None
    rng = random.Random(seed)
    block = max(2, min(len(net), int(math.sqrt(len(net)))))
    means = []
    for _ in range(iterations):
        sample: list[float] = []
        while len(sample) < len(net):
            start = rng.randrange(0, max(1, len(net) - block + 1))
            sample.extend(net[start:start + block])
        means.append(statistics.fmean(sample[:len(net)]))
    means.sort()
    index = max(0, min(len(means) - 1, int(alpha * len(means))))
    return means[index]


def _metrics(
    outcomes: list[dict[str, Any]], *, cost_bps: float, trials_total: int,
    seed: int,
) -> dict[str, Any]:
    gross = [float(row["gross_bps"]) for row in outcomes]
    net = [value - cost_bps for value in gross]
    if not net:
        return {
            "trades": 0, "n_eff": 0.0, "gross_ev_bps": None,
            "net_ev_bps": None, "net_ev_lower_bound_bps": None,
            "profit_factor": None, "win_rate": None, "max_drawdown_bps": None,
        }
    n_eff = _effective_sample_size(outcomes, net)
    mean = statistics.fmean(net)
    stdev = statistics.stdev(net) if len(net) > 1 else 0.0
    alpha = 0.05 / max(1, int(trials_total))
    z = statistics.NormalDist().inv_cdf(max(0.500001, 1.0 - alpha))
    normal_lower = mean - z * stdev / math.sqrt(max(n_eff, 1.0))
    bootstrap_lower = _moving_block_lower_bound(net, alpha=alpha, seed=seed)
    lower = min(normal_lower, bootstrap_lower) if bootstrap_lower is not None else normal_lower
    wins = sum(value for value in net if value > 0)
    losses = -sum(value for value in net if value < 0)
    equity = peak = drawdown = 0.0
    for value in net:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    by_day: dict[str, float] = defaultdict(float)
    by_symbol: dict[str, float] = defaultdict(float)
    for row, value in zip(outcomes, net):
        day = datetime.fromtimestamp(int(row["entry_timestamp_ms"]) / 1000, timezone.utc).strftime("%Y-%m-%d")
        by_day[day] += value
        by_symbol[str(row["symbol"])] += value
    total_positive = sum(value for value in by_symbol.values() if value > 0)
    symbol_concentration = max(by_symbol.values(), default=0.0) / total_positive if total_positive > 0 else 1.0
    return {
        "trades": len(net),
        "n_eff": round(n_eff, 6),
        "gross_ev_bps": statistics.fmean(gross),
        "net_ev_bps": mean,
        "net_ev_lower_bound_bps": lower,
        "profit_factor": wins / losses if losses > 0 else None,
        "win_rate": sum(value > 0 for value in net) / len(net),
        "max_drawdown_bps": drawdown,
        "profitable_days": sum(value > 0 for value in by_day.values()),
        "days": len(by_day),
        "single_symbol_profit_concentration": symbol_concentration,
        "multiple_testing_alpha": alpha,
        "bootstrap": "MOVING_BLOCK_400",
    }


def _exposure_matched_baselines(
    outcomes: list[dict[str, Any]], *, cost_bps: float, trials_total: int,
    seed: int,
) -> dict[str, Any]:
    opposite = [{**row, "gross_bps": -float(row["gross_bps"])} for row in outcomes]
    rng = random.Random(seed)
    random_sign = [
        {**row, "gross_bps": float(row["gross_bps"]) * rng.choice((-1.0, 1.0))}
        for row in outcomes
    ]
    return {
        "contract": "SAME_TIMESTAMPS_HOLDS_EXPOSURE_AND_COSTS",
        "no_trade": {
            "trades": 0, "net_ev_bps": 0.0, "net_ev_lower_bound_bps": 0.0,
        },
        "opposite_direction": _metrics(
            opposite, cost_bps=cost_bps, trials_total=trials_total, seed=seed + 11,
        ),
        "deterministic_random_sign": _metrics(
            random_sign, cost_bps=cost_bps, trials_total=trials_total, seed=seed + 29,
        ),
    }


def _evaluate(
    rows: list[dict[str, Any]], spec: dict[str, Any], *, start_ms: int,
    end_ms: int, trials_total: int, seed: int,
) -> dict[str, Any]:
    outcomes = _opportunities(rows, spec, start_ms=start_ms, end_ms=end_ms)
    costs = {
        str(cost): _metrics(
            outcomes, cost_bps=cost, trials_total=trials_total, seed=seed + int(cost * 10),
        )
        for cost in COST_SCENARIOS_BPS
    }
    baselines = {
        str(cost): _exposure_matched_baselines(
            outcomes, cost_bps=cost, trials_total=trials_total,
            seed=seed + int(cost * 100),
        )
        for cost in COST_SCENARIOS_BPS
    }
    return {"outcomes": outcomes, "cost_scenarios": costs, "baselines": baselines}


def _walk_forward_stability(
    rows: list[dict[str, Any]], spec: dict[str, Any], *, end_ms: int,
    trials_total: int, seed: int,
) -> dict[str, Any]:
    timestamps = sorted({
        int(row["bucket_start_ms"]) for row in rows
        if int(row["bucket_start_ms"]) < end_ms
    })
    if len(timestamps) < 20:
        return {"status": "NEED_MORE_DATA", "folds": []}
    folds = []
    for index, (left_fraction, right_fraction) in enumerate(
        ((0.20, 0.40), (0.35, 0.55), (0.50, 0.70), (0.60, 0.80))
    ):
        left = timestamps[int((len(timestamps) - 1) * left_fraction)]
        right = timestamps[int((len(timestamps) - 1) * right_fraction)]
        evaluation = _evaluate(
            rows, spec, start_ms=left, end_ms=right,
            trials_total=trials_total, seed=seed + index * 1000,
        )
        metric = evaluation["cost_scenarios"]["15.5"]
        folds.append({
            "fold": index + 1, "test_start_ms": left, "test_end_ms": right,
            "metrics": metric,
            "positive": bool(metric.get("net_ev_bps") is not None and metric["net_ev_bps"] > 0),
        })
    sufficient = [fold for fold in folds if fold["metrics"]["trades"] >= 15]
    positive = sum(fold["positive"] for fold in sufficient)
    status = "PASS" if len(sufficient) == 4 and positive >= 3 else "FAIL" if sufficient else "NEED_MORE_DATA"
    return {
        "status": status, "method": "FIXED_SPEC_ROLLING_STABILITY_NO_RETUNING",
        "folds": folds,
    }


def _state_for(
    train: dict[str, Any], validation: dict[str, Any], walk_forward: dict[str, Any],
) -> tuple[str, list[str]]:
    reasons = []
    train_base = train["cost_scenarios"]["15.5"]
    validation_base = validation["cost_scenarios"]["15.5"]
    validation_stress = validation["cost_scenarios"]["18.0"]
    if train_base["trades"] < 50 or train_base["n_eff"] < 30:
        reasons.append("TRAIN_SAMPLE_INSUFFICIENT")
    if validation_base["trades"] < 30 or validation_base["n_eff"] < 20:
        reasons.append("VALIDATION_SAMPLE_INSUFFICIENT")
    for prefix, metric in (("TRAIN", train_base), ("VALIDATION", validation_base), ("COST_STRESS", validation_stress)):
        if metric["net_ev_bps"] is None or metric["net_ev_bps"] <= 0:
            reasons.append(f"{prefix}_NET_EV_NOT_POSITIVE")
        if metric["net_ev_lower_bound_bps"] is None or metric["net_ev_lower_bound_bps"] <= 0:
            reasons.append(f"{prefix}_LOWER_BOUND_NOT_POSITIVE")
        if metric["profit_factor"] is None or metric["profit_factor"] <= 1.0:
            reasons.append(f"{prefix}_PF_NOT_ABOVE_ONE")
    if validation_base.get("single_symbol_profit_concentration", 1.0) > 0.80:
        reasons.append("SINGLE_SYMBOL_PROFIT_CONCENTRATION")
    baseline = validation["baselines"]["15.5"]
    baseline_ev = max(
        float(baseline["no_trade"].get("net_ev_bps") or 0.0),
        float(baseline["opposite_direction"].get("net_ev_bps") or -math.inf),
        float(baseline["deterministic_random_sign"].get("net_ev_bps") or -math.inf),
    )
    if validation_base.get("net_ev_bps") is None or validation_base["net_ev_bps"] <= baseline_ev:
        reasons.append("DOES_NOT_BEAT_EXPOSURE_MATCHED_BASELINE")
    if walk_forward.get("status") != "PASS":
        reasons.append(f"WALK_FORWARD_{walk_forward.get('status', 'UNKNOWN')}")
    if reasons:
        return ("NEED_MORE_DATA" if any("SAMPLE" in reason for reason in reasons) else "REJECTED"), reasons
    return "WATCH_ONLY", ["HOLDOUT_SEALED_NO_PROMOTION"]


def run_challenger(
    *, symbols: Iterable[str] | None = None,
    feature_manifest_path: Path = FEATURE_MANIFEST_PATH,
    max_families: int | None = None, max_trials: int | None = None,
    max_runtime_minutes: int | None = None, seed: int = 104402,
    report_root: Path = REPORT_ROOT,
) -> dict[str, Any]:
    config = load_storage_config()
    families = min(int(max_families or config["challenger_max_families"]), 5)
    trials_budget = min(int(max_trials or config["challenger_max_trials"]), 80)
    runtime_minutes = min(int(max_runtime_minutes or config["challenger_max_runtime_minutes"]), 30)
    started = time.monotonic()
    rows, dataset = load_feature_rows(
        feature_manifest_path, symbols=symbols,
        max_rows=int(config.get("challenger_max_feature_rows", 500_000)),
    )
    base = {
        "schema": "continuous_edge_research_challenger.v1",
        "generated_at": utc_now(),
        "tool_version": TOOL_VERSION,
        "dataset": dataset,
        "dataset_hash": dataset.get("dataset_hash"),
        "budget": {
            "max_families": families, "max_trials": trials_budget,
            "max_runtime_minutes": runtime_minutes, "seed": seed,
        },
        "cost_scenarios_bps": list(COST_SCENARIOS_BPS),
        "holdout_access_count": 0,
        "holdout_status": "SEALED_NOT_EVALUATED",
        **challenger_safety(),
    }
    if len(rows) < 100:
        result = {
            **base, "status": "NEED_MORE_DATA", "state": "NEED_MORE_DATA",
            "reason": (
                dataset.get("reason")
                if dataset.get("status") == "RESOURCE_BUDGET_EXCEEDED"
                else "VERIFIED_CAUSAL_FEATURE_SAMPLE_INSUFFICIENT"
            ),
            "families_tested": 0, "trials": 0, "candidates": [],
            "rejected": 0, "need_more_data": 1,
        }
        _write_report(result, report_root)
        return result
    _augment_prefix_features(rows)
    timestamps = sorted({int(row["bucket_start_ms"]) for row in rows})
    if len(timestamps) < 20:
        result = {
            **base, "status": "NEED_MORE_DATA", "state": "NEED_MORE_DATA",
            "reason": "CAUSAL_TIME_COVERAGE_INSUFFICIENT",
            "families_tested": 0, "trials": 0, "candidates": [],
            "rejected": 0, "need_more_data": 1,
        }
        _write_report(result, report_root)
        return result
    train_end = timestamps[int((len(timestamps) - 1) * 0.60)]
    validation_end = timestamps[int((len(timestamps) - 1) * 0.80)]
    start_ms, end_ms = timestamps[0], timestamps[-1] + 1
    step_ms = int(statistics.median(
        [timestamps[index + 1] - timestamps[index] for index in range(len(timestamps) - 1)
         if timestamps[index + 1] > timestamps[index]]
    ))
    validation_start = train_end + max(1, step_ms)
    specs = compile_trial_specs(families, trials_budget)
    train_ranked: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
    for index, spec in enumerate(specs):
        if time.monotonic() - started > runtime_minutes * 60:
            break
        evaluation = _evaluate(
            rows, spec, start_ms=start_ms, end_ms=train_end,
            trials_total=len(specs), seed=seed + index * 1000,
        )
        metric = evaluation["cost_scenarios"]["15.5"]
        score = metric["net_ev_lower_bound_bps"]
        train_ranked.append((float(score) if score is not None else -math.inf, spec, evaluation))
    best_by_family: dict[str, tuple[float, dict[str, Any], dict[str, Any]]] = {}
    for item in train_ranked:
        family = item[1]["family"]
        if family not in best_by_family or item[0] > best_by_family[family][0]:
            best_by_family[family] = item
    candidates = []
    for family_index, (_, spec, train) in enumerate(best_by_family.values()):
        validation = _evaluate(
            rows, spec, start_ms=validation_start, end_ms=validation_end,
            trials_total=len(specs), seed=seed + 100_000 + family_index * 1000,
        )
        walk_forward = _walk_forward_stability(
            rows, spec, end_ms=validation_end, trials_total=len(specs),
            seed=seed + 200_000 + family_index * 1000,
        )
        state, reasons = _state_for(train, validation, walk_forward)
        candidates.append({
            "trial_id": spec["trial_id"], "family": spec["family"],
            "spec": spec, "state": state, "reasons": reasons,
            "train": {"cost_scenarios": train["cost_scenarios"]},
            "validation": {
                "cost_scenarios": validation["cost_scenarios"],
                "exposure_matched_baselines": validation["baselines"],
            },
            "walk_forward": walk_forward,
            "sealed_holdout": {
                "start_ms": validation_end, "end_ms": end_ms,
                "access_count": 0, "metrics": None,
            },
            "auto_promoted": False,
        })
    priority = {"WATCH_ONLY": 2, "NEED_MORE_DATA": 1, "REJECTED": 0}
    candidates.sort(
        key=lambda row: (
            priority.get(row["state"], -1),
            row["validation"]["cost_scenarios"]["15.5"].get("net_ev_lower_bound_bps") or -math.inf,
        ), reverse=True,
    )
    state = candidates[0]["state"] if candidates else "NEED_MORE_DATA"
    result = {
        **base,
        "status": "COMPLETED",
        "state": state,
        "snapshot": {
            "start_ms": start_ms, "train_end_ms": train_end,
            "validation_start_ms": validation_start,
            "validation_end_ms": validation_end, "holdout_end_ms": end_ms,
            "split": "CHRONOLOGICAL_60_20_20",
            "purging": "OUTCOMES_CANNOT_CROSS_SPLIT_BOUNDARY",
            "embargo_ms": max(1, step_ms),
            "holdout_evaluated": False,
        },
        "families_tested": len(best_by_family),
        "trials": len(train_ranked),
        "candidates": candidates,
        "rejected": sum(row["state"] == "REJECTED" for row in candidates),
        "need_more_data": sum(row["state"] == "NEED_MORE_DATA" for row in candidates),
        "watch_only": sum(row["state"] == "WATCH_ONLY" for row in candidates),
        "duration_seconds": time.monotonic() - started,
        "next_action": "KEEP_COLLECTING_AND_REEVALUATE_NEW_VERIFIED_PARTITIONS",
        "strategy_verdict": "NINGUN EDGE NUEVO VALIDADO",
    }
    _write_report(result, report_root)
    return result


def _write_report(result: dict[str, Any], report_root: Path) -> None:
    report_root.mkdir(parents=True, exist_ok=True)
    _atomic_json(report_root / "challenger_latest.json", result)
    _atomic_json(STATUS_PATH, result)
    lines = [
        "# Continuous Edge Research Challenger",
        "", f"- status: {result.get('status')}",
        f"- state: {result.get('state')}",
        f"- dataset_hash: {result.get('dataset_hash')}",
        f"- trials: {result.get('trials', 0)}",
        f"- holdout_access_count: {result.get('holdout_access_count', 0)}",
        "- research_only: true", "- auto_promotion: false",
        "- FINAL_RECOMMENDATION: NO LIVE", "",
    ]
    (report_root / "challenger_latest.md").write_text("\n".join(lines), encoding="utf-8")


def challenger_status() -> dict[str, Any]:
    value = _read_json(STATUS_PATH, {})
    if not isinstance(value, dict) or not value:
        return {
            "status": "NEED_MORE_DATA", "state": "NEED_MORE_DATA",
            "reason": "CHALLENGER_NOT_RUN", "holdout_access_count": 0,
            **challenger_safety(),
        }
    return value
