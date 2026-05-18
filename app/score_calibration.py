from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Iterable

from .edge_hardening_utils import cost_config
from .utils import safe_float, safe_int


START = "SCORE CALIBRATION START"
END = "SCORE CALIBRATION END"
SCORE_BUCKETS = ("0-49", "50-59", "60-69", "70-74", "75-79", "80-84", "85-89", "90-94", "95-100")
PROMISING_SYMBOLS = {"ETHUSDT", "DOGEUSDT", "XRPUSDT", "BTCUSDT", "SOLUSDT"}


class ScoreCalibration:
    """Research-only score quality audit.

    This module only reads labels/path metrics and emits calibration diagnostics.
    It never changes scoring, paper filters, positions, risk, or execution.
    """

    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        hours = max(1, int(hours or 24))
        rows = load_score_rows(self.db, hours=hours)
        costs = cost_config(self.config)
        by_score_bucket = _group_metrics(rows, "score_bucket", costs)
        by_side = _group_metrics(rows, "side", costs)
        by_regime = _group_metrics(rows, "market_regime", costs)
        by_symbol = _group_metrics(rows, "symbol", costs)
        by_source = _group_metrics(rows, "source", costs)
        high_score_failures = _high_score_failures(rows, costs)
        penalty_suggestions = _penalty_suggestions(rows, costs)
        diagnosis = _diagnosis(by_score_bucket, by_side, high_score_failures)
        best_buckets = sorted(by_score_bucket, key=lambda row: (safe_float(row.get("net_EV_est")), safe_float(row.get("net_PF_est"))), reverse=True)[:5]
        worst_buckets = sorted(by_score_bucket, key=lambda row: (safe_float(row.get("net_EV_est")), safe_float(row.get("gross_PF"))))[:5]
        return {
            "hours": hours,
            "samples": len(rows),
            "overall_score_quality": diagnosis["overall_score_quality"],
            "biggest_problem": diagnosis["biggest_problem"],
            "flags": diagnosis["flags"],
            "by_score_bucket": by_score_bucket,
            "by_side": by_side,
            "by_regime": by_regime[:30],
            "by_symbol": by_symbol[:30],
            "by_source": by_source,
            "best_buckets": best_buckets,
            "worst_buckets": worst_buckets,
            "high_score_failures": high_score_failures[:20],
            "penalty_suggestions": penalty_suggestions[:20],
            "recommendation": diagnosis["recommendation"],
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        lines = [
            START,
            f"hours: {payload['hours']}",
            f"samples: {payload['samples']}",
            f"overall_score_quality: {payload['overall_score_quality']}",
            f"biggest_problem: {payload['biggest_problem']}",
            "flags:",
            *_flag_lines(payload["flags"]),
            "score_buckets:",
            *_metric_lines(payload["by_score_bucket"], limit=12),
            "best_buckets:",
            *_metric_lines(payload["best_buckets"], limit=5),
            "worst_buckets:",
            *_metric_lines(payload["worst_buckets"], limit=5),
            "by_side:",
            *_metric_lines(payload["by_side"], limit=8),
            "by_regime:",
            *_metric_lines(payload["by_regime"], limit=10),
            "by_source:",
            *_metric_lines(payload["by_source"], limit=10),
            "high_score_failures:",
            *_failure_lines(payload["high_score_failures"]),
            "penalty_suggestions:",
            *_penalty_lines(payload["penalty_suggestions"]),
            f"recommendation: {payload['recommendation']}",
            "final_recommendation: NO LIVE",
            END,
        ]
        return "\n".join(lines)


def load_score_rows(db: Any, *, hours: int = 24) -> list[dict[str, Any]]:
    since = (datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours or 24)))).isoformat()
    labels = _safe(lambda: db.fetch_labeled_signal_rows_since(since, limit=50000), [])
    paths = _safe(lambda: db.fetch_signal_path_metrics_since(since, limit=50000), [])
    paths_by_obs: dict[str, dict[str, Any]] = {}
    for path in paths:
        obs_id = str(path.get("observation_id") or "")
        if obs_id and obs_id not in paths_by_obs:
            paths_by_obs[obs_id] = path
    rows: list[dict[str, Any]] = []
    seen_obs: set[str] = set()
    for label in labels:
        obs_id = str(label.get("observation_id") or label.get("id") or "")
        path = paths_by_obs.get(obs_id, {})
        row = _normalize_row(label, path)
        if row:
            rows.append(row)
            if obs_id:
                seen_obs.add(obs_id)
    for path in paths:
        obs_id = str(path.get("observation_id") or "")
        if obs_id and obs_id in seen_obs:
            continue
        row = _normalize_row(path, path)
        if row:
            rows.append(row)
    return rows


def metric_snapshot(rows: list[dict[str, Any]], config: Any) -> dict[str, Any]:
    return _metrics(rows, cost_config(config))


def score_bucket_for(score: float, existing: Any = "") -> str:
    value = safe_float(score)
    if value > 0:
        if value >= 95:
            return "95-100"
        if value >= 90:
            return "90-94"
        if value >= 85:
            return "85-89"
        if value >= 80:
            return "80-84"
        if value >= 75:
            return "75-79"
        if value >= 70:
            return "70-74"
        if value >= 60:
            return "60-69"
        if value >= 50:
            return "50-59"
        return "0-49"
    existing_text = str(existing or "").upper()
    if existing_text in SCORE_BUCKETS:
        return existing_text
    coarse_map = {
        "70-79": "70-74",
        "80-89": "80-84",
        "90-100": "90-94",
        "<60": "0-49",
        "PROBE": "0-49",
    }
    return coarse_map.get(existing_text, "0-49")


def _normalize_row(row: dict[str, Any], path: dict[str, Any]) -> dict[str, Any]:
    side = str(row.get("side") or path.get("side") or "UNKNOWN").upper()
    source = _source(row, path)
    hit = str(row.get("first_barrier_hit") or path.get("first_barrier_hit") or "").upper()
    status = str(path.get("status") or row.get("status") or "").lower()
    if not hit and status not in {"matured", ""}:
        return {}
    score = safe_float(row.get("confidence_score") if row.get("confidence_score") is not None else row.get("score"))
    return_pct = safe_float(row.get("realized_return_pct") if row.get("realized_return_pct") is not None else path.get("final_return_pct"))
    score_bucket = score_bucket_for(score, row.get("score_bucket") or path.get("score_bucket"))
    return {
        "observation_id": row.get("observation_id") or row.get("id") or path.get("observation_id"),
        "timestamp": row.get("label_timestamp") or row.get("timestamp") or path.get("matured_at") or path.get("updated_at") or path.get("created_at"),
        "symbol": str(row.get("symbol") or path.get("symbol") or "NA").upper(),
        "side": side,
        "market_regime": str(row.get("market_regime") or path.get("market_regime") or "unknown").upper(),
        "score": score,
        "score_bucket": score_bucket,
        "source": source,
        "strategy": str(row.get("strategy_type") or row.get("strategy") or path.get("strategy") or "NA"),
        "first_barrier_hit": hit or _infer_hit(path, return_pct),
        "return_pct": return_pct,
        "mfe": safe_float(row.get("max_favorable_excursion") if row.get("max_favorable_excursion") is not None else path.get("max_favorable_pct")),
        "mae": safe_float(row.get("max_adverse_excursion") if row.get("max_adverse_excursion") is not None else path.get("max_adverse_pct")),
        "bars": safe_float(row.get("bars_to_outcome") if row.get("bars_to_outcome") is not None else path.get("bars_tracked")),
    }


def _source(row: dict[str, Any], path: dict[str, Any]) -> str:
    raw = str(path.get("source") or row.get("source") or "").lower()
    if raw:
        if "market_probe" in raw or raw == "probe":
            return "market_probe"
        if "edge_guard" in raw:
            return "edge_guard_block"
        if "allocator" in raw:
            return "allocator_reject"
        if "reject" in raw or "block" in raw:
            return "rejects"
        if "trade" in raw or "signal" in raw:
            return "trade_signal"
        return raw
    if safe_int(row.get("shadow_strategy")):
        return "shadow_signal"
    return "trade_signal"


def _infer_hit(path: dict[str, Any], return_pct: float) -> str:
    if str(path.get("status") or "").lower() != "matured":
        return ""
    if return_pct > 0:
        return "TP1"
    if return_pct < 0:
        return "SL"
    return "TIME"


def _group_metrics(rows: list[dict[str, Any]], key: str, costs: Any) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(key) or "unknown")].append(row)
    metrics = [_metrics(group_rows, costs, group_key=key, group_value=value) for value, group_rows in groups.items()]
    if key == "score_bucket":
        order = {bucket: index for index, bucket in enumerate(SCORE_BUCKETS)}
        metrics.sort(key=lambda item: order.get(str(item.get("group_value")), 99))
    else:
        metrics.sort(key=lambda item: (safe_int(item.get("samples")), safe_float(item.get("net_EV_est"))), reverse=True)
    return metrics


def _metrics(rows: list[dict[str, Any]], costs: Any, *, group_key: str = "", group_value: str = "") -> dict[str, Any]:
    samples = len(rows)
    hits = Counter(_hit_class(row.get("first_barrier_hit")) for row in rows)
    returns = [safe_float(row.get("return_pct")) for row in rows]
    mfes = [safe_float(row.get("mfe")) for row in rows]
    maes = [safe_float(row.get("mae")) for row in rows]
    bars = [safe_float(row.get("bars")) for row in rows]
    gross_gains = sum(value for value in returns if value > 0)
    gross_losses = abs(sum(value for value in returns if value < 0))
    avg_bars = sum(bars) / max(len(bars), 1)
    total_cost = _cost_pct(costs, avg_bars)
    net_returns = [value - total_cost for value in returns]
    net_gains = sum(value for value in net_returns if value > 0)
    net_losses = abs(sum(value for value in net_returns if value < 0))
    net_ev = sum(net_returns) / max(samples, 1)
    row = {
        "group_key": group_key,
        "group_value": group_value,
        "samples": samples,
        "tp_count": hits["TP"],
        "sl_count": hits["SL"],
        "time_count": hits["TIME"],
        "tp_ratio": hits["TP"] / max(samples, 1),
        "sl_ratio": hits["SL"] / max(samples, 1),
        "time_ratio": hits["TIME"] / max(samples, 1),
        "gross_PF": gross_gains / gross_losses if gross_losses > 0 else 999.0 if gross_gains > 0 else 0.0,
        "gross_expectancy": sum(returns) / max(samples, 1),
        "estimated_fee_slippage_funding": total_cost,
        "net_EV_est": net_ev,
        "net_PF_est": net_gains / net_losses if net_losses > 0 else 999.0 if net_gains > 0 else 0.0,
        "avg_MFE": sum(mfes) / max(len(mfes), 1),
        "avg_MAE": sum(maes) / max(len(maes), 1),
        "median_MFE": median(mfes) if mfes else 0.0,
        "median_MAE": median(maes) if maes else 0.0,
        "drawdown_proxy": max([abs(value) for value in returns if value < 0] or [0.0]),
        "confidence": _confidence(samples),
    }
    row["monotonicity_status"] = "not_applicable" if group_key != "score_bucket" else "pending"
    return row


def _diagnosis(score_rows: list[dict[str, Any]], side_rows: list[dict[str, Any]], high_score_failures: list[dict[str, Any]]) -> dict[str, Any]:
    by_bucket = {str(row.get("group_value")): row for row in score_rows}
    ordered = [by_bucket[bucket] for bucket in SCORE_BUCKETS if bucket in by_bucket and safe_int(by_bucket[bucket].get("samples")) > 0]
    violations = 0
    prev = None
    for row in ordered:
        current = safe_float(row.get("net_EV_est"))
        row["monotonicity_status"] = "OK"
        if prev is not None and current + 0.00001 < prev:
            row["monotonicity_status"] = "UNDERPERFORMS_PREVIOUS_BUCKET"
            violations += 1
        prev = current
    mid = [by_bucket.get(bucket, {}) for bucket in ("70-74", "75-79", "80-84", "85-89")]
    high = [by_bucket.get(bucket, {}) for bucket in ("90-94", "95-100")]
    mid_ev = _avg([safe_float(row.get("net_EV_est")) for row in mid if row])
    high_ev = _avg([safe_float(row.get("net_EV_est")) for row in high if row])
    high_sl = max([safe_float(row.get("sl_ratio")) for row in high if row] or [0.0])
    high_time = max([safe_float(row.get("time_ratio")) for row in high if row] or [0.0])
    long_row = next((row for row in side_rows if str(row.get("group_value")).upper() == "LONG"), {})
    short_row = next((row for row in side_rows if str(row.get("group_value")).upper() == "SHORT"), {})
    flags = {
        "score_not_monotonic": violations > 0 or (high and mid and high_ev < mid_ev),
        "high_score_underperforms_low_score": bool(high and mid and high_ev < mid_ev),
        "high_score_high_SL": high_sl >= 0.20,
        "high_score_high_TIME": high_time >= 0.80,
        "high_score_negative_net_EV": any(safe_float(row.get("net_EV_est")) < 0 for row in high if row),
        "low_sample_fake_edge": any(safe_int(row.get("samples")) < 250 and safe_float(row.get("gross_PF")) > 2.0 for row in score_rows),
        "gross_edge_net_negative": any(safe_float(row.get("gross_PF")) > 1.2 and safe_float(row.get("net_EV_est")) <= 0 for row in score_rows),
        "long_bad_side": bool(long_row and safe_float(long_row.get("net_EV_est")) <= 0 and safe_float(long_row.get("sl_ratio")) > safe_float(long_row.get("tp_ratio"))),
        "short_promising_but_unconfirmed": bool(short_row and safe_float(short_row.get("gross_PF")) > 1.0 and safe_int(short_row.get("samples")) < 750),
    }
    if flags["high_score_negative_net_EV"]:
        problem = "negative_net_EV"
    elif flags["score_not_monotonic"]:
        problem = "score_not_monotonic"
    elif flags["high_score_high_SL"]:
        problem = "high_score_sl"
    elif flags["high_score_high_TIME"]:
        problem = "high_time"
    elif not ordered:
        problem = "insufficient_samples"
    else:
        problem = "insufficient_samples" if sum(safe_int(row.get("samples")) for row in ordered) < 250 else "none"
    if problem in {"negative_net_EV", "score_not_monotonic", "high_score_sl"}:
        quality = "BAD"
        recommendation = "DO_NOT_USE_SCORE_FOR_ACTION"
    elif problem in {"high_time", "insufficient_samples"} or high_score_failures:
        quality = "MIXED"
        recommendation = "KEEP_RESEARCH"
    else:
        quality = "GOOD"
        recommendation = "BUILD_INCUBATOR"
    return {"flags": flags, "biggest_problem": problem, "overall_score_quality": quality, "recommendation": recommendation}


def _high_score_failures(rows: list[dict[str, Any]], costs: Any) -> list[dict[str, Any]]:
    groups = _context_groups([row for row in rows if safe_float(row.get("score")) >= 85 or str(row.get("score_bucket")) in {"85-89", "90-94", "95-100"}])
    failures: list[dict[str, Any]] = []
    for key, group_rows in groups.items():
        metrics = _metrics(group_rows, costs)
        problems = _failure_problems(metrics)
        if problems:
            symbol, side, regime, bucket = key
            failures.append({
                "symbol": symbol,
                "side": side,
                "market_regime": regime,
                "score_bucket": bucket,
                "samples": metrics["samples"],
                "TP": metrics["tp_ratio"],
                "SL": metrics["sl_ratio"],
                "TIME": metrics["time_ratio"],
                "net_EV_est": metrics["net_EV_est"],
                "net_PF_est": metrics["net_PF_est"],
                "problem": ",".join(problems),
            })
    failures.sort(key=lambda row: (safe_float(row.get("net_EV_est")), -safe_int(row.get("samples"))))
    return failures


def _penalty_suggestions(rows: list[dict[str, Any]], costs: Any) -> list[dict[str, Any]]:
    suggestions = []
    for failure in _high_score_failures(rows, costs):
        penalty = -15
        if "high_sl" in str(failure.get("problem")):
            penalty -= 5
        if "negative_ev" in str(failure.get("problem")):
            penalty -= 5
        suggestions.append({
            **failure,
            "suggested_penalty": penalty,
            "action": "SHADOW_ONLY_DO_NOT_APPLY",
        })
    return suggestions


def _failure_problems(metrics: dict[str, Any]) -> list[str]:
    problems = []
    if safe_float(metrics.get("tp_ratio")) < 0.05:
        problems.append("tp_low")
    if safe_float(metrics.get("sl_ratio")) > 0.20:
        problems.append("high_sl")
    if safe_float(metrics.get("time_ratio")) > 0.80:
        problems.append("high_time")
    if safe_float(metrics.get("net_EV_est")) < 0:
        problems.append("negative_ev")
    if safe_float(metrics.get("net_PF_est")) < 1.0:
        problems.append("net_pf_below_1")
    return problems


def _context_groups(rows: Iterable[dict[str, Any]]) -> dict[tuple[str, str, str, str], list[dict[str, Any]]]:
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (
            str(row.get("symbol") or "NA").upper(),
            str(row.get("side") or "NA").upper(),
            str(row.get("market_regime") or "unknown").upper(),
            str(row.get("score_bucket") or "0-49"),
        )
        groups[key].append(row)
    return groups


def _hit_class(hit: Any) -> str:
    text = str(hit or "").upper()
    if text.startswith("TP"):
        return "TP"
    if text == "SL":
        return "SL"
    return "TIME"


def _cost_pct(costs: Any, avg_bars: float) -> float:
    fee_pct = (2.0 * safe_float(costs.taker_fee_bps)) / 100.0
    slippage_pct = (2.0 * safe_float(costs.slippage_bps)) / 100.0
    funding_pct = max(0.0, ((avg_bars * 5.0) / 480.0) * safe_float(costs.funding_bps_per_8h) / 100.0)
    return fee_pct + slippage_pct + funding_pct


def _confidence(samples: int) -> str:
    if samples >= 750:
        return "HIGH"
    if samples >= 250:
        return "MEDIUM"
    return "LOW"


def _avg(values: list[float]) -> float:
    return sum(values) / max(len(values), 1)


def _safe(callback: Any, fallback: Any) -> Any:
    try:
        return callback()
    except Exception:
        return fallback


def _flag_lines(flags: dict[str, Any]) -> list[str]:
    return [f"- {key}={str(value).lower()}" for key, value in flags.items()] if flags else ["- none"]


def _metric_lines(rows: list[dict[str, Any]], *, limit: int = 10) -> list[str]:
    if not rows:
        return ["- none"]
    out = []
    for row in rows[:limit]:
        out.append(
            "- "
            f"{row.get('group_key')}={row.get('group_value')} samples={row.get('samples')} "
            f"TP%={safe_float(row.get('tp_ratio')) * 100:.1f} SL%={safe_float(row.get('sl_ratio')) * 100:.1f} "
            f"TIME%={safe_float(row.get('time_ratio')) * 100:.1f} gross_PF={safe_float(row.get('gross_PF')):.2f} "
            f"net_PF={safe_float(row.get('net_PF_est')):.2f} net_EV={safe_float(row.get('net_EV_est')):.4f} "
            f"avg_MFE={safe_float(row.get('avg_MFE')):.3f} avg_MAE={safe_float(row.get('avg_MAE')):.3f} "
            f"confidence={row.get('confidence')} monotonicity={row.get('monotonicity_status')}"
        )
    return out


def _failure_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- {row.get('symbol')} {row.get('side')} {row.get('market_regime')} {row.get('score_bucket')} "
            f"samples={row.get('samples')} TP%={safe_float(row.get('TP')) * 100:.1f} "
            f"SL%={safe_float(row.get('SL')) * 100:.1f} TIME%={safe_float(row.get('TIME')) * 100:.1f} "
            f"net_EV={safe_float(row.get('net_EV_est')):.4f} problem={row.get('problem')}"
        )
        for row in rows[:20]
    ]


def _penalty_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- symbol={row.get('symbol')} side={row.get('side')} regime={row.get('market_regime')} "
            f"score_bucket={row.get('score_bucket')} problem={row.get('problem')} "
            f"suggested_penalty={row.get('suggested_penalty')} action={row.get('action')}"
        )
        for row in rows[:20]
    ]
