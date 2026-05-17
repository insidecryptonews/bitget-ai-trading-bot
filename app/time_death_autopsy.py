from __future__ import annotations

import statistics
from typing import Any

from .edge_hardening_utils import (
    FINAL_NO_LIVE,
    apply_net_costs,
    cost_config,
    fetch_group_metrics,
    format_num,
    format_pct,
    since_iso,
)
from .utils import safe_float, safe_int


START = "TIME DEATH AUTOPSY START"
END = "TIME DEATH AUTOPSY END"

DECISION_REJECT = "REJECT"
DECISION_WATCH = "WATCH_ONLY"
DECISION_EXIT_TEST = "SHADOW_EXIT_TEST"
DECISION_CONFIRMED = "PAPER_CANDIDATE_ONLY_IF_CONFIRMED"


class TimeDeathAutopsyLab:
    """Research-only autopsy of labels that expire by TIME."""

    GROUPS = ("symbol", "side", "market_regime", "score_bucket", "source", "strategy", "policy_id")

    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        hours = max(1, int(hours or 24))
        since = since_iso(hours)
        costs = cost_config(self.config)
        paths = _safe(lambda: self.db.fetch_signal_path_metrics_since(since, limit=50000), [])
        path_stats = _path_stats_by_group(paths)
        groups: list[dict[str, Any]] = []
        for group in self.GROUPS:
            for row in fetch_group_metrics(self.db, since=since, group_key=group, limit=60, min_samples=1):
                item = apply_net_costs(row, costs)
                item.update(path_stats.get((group, str(item.get("group_value") or "").upper()), {}))
                item["max_holding_bars_used"] = safe_int(getattr(self.config, "max_holding_bars", 0))
                item["time_to_expiry_bars"] = max(0.0, safe_float(item["max_holding_bars_used"]) - safe_float(item.get("avg_bars_to_outcome")))
                item["did_price_move_enough_for_smaller_TP"] = safe_float(item.get("avg_MFE")) >= 0.25
                item["did_price_reverse_after_MFE"] = safe_float(item.get("avg_MFE")) >= 0.25 and safe_float(item.get("gross_expectancy")) <= 0
                item["no_movement_ratio"] = safe_float(item.get("no_movement_ratio", 0.0))
                item["stale_or_slow_market_data_warning"] = _latency_warning(self.db, since)
                item["likely_cause"] = likely_cause(item, self.config)
                item["decision"] = decision_for(item, self.config)
                item["reason"] = _reason(item)
                groups.append(item)
        groups.sort(key=lambda item: (safe_float(item.get("time_ratio")), safe_int(item.get("samples"))), reverse=True)
        return {
            "hours": hours,
            "groups": groups[:80],
            "worst_time_groups": groups[:20],
            "exit_test_groups": [row for row in groups if row.get("decision") == DECISION_EXIT_TEST][:20],
            "rejected_groups": [row for row in groups if row.get("decision") == DECISION_REJECT][:20],
            "final_recommendation": FINAL_NO_LIVE,
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        return "\n".join([
            START,
            f"hours: {payload['hours']}",
            "worst_time_groups:",
            *_group_lines(payload["worst_time_groups"]),
            "exit_test_groups:",
            *_group_lines(payload["exit_test_groups"]),
            "rejected_groups:",
            *_group_lines(payload["rejected_groups"]),
            "final_recommendation: NO LIVE",
            END,
        ])


def likely_cause(row: dict[str, Any], config: Any) -> str:
    samples = safe_int(row.get("samples"))
    time_ratio = safe_float(row.get("time_ratio"))
    tp_ratio = safe_float(row.get("tp_ratio"))
    sl_ratio = safe_float(row.get("sl_ratio"))
    pf = safe_float(row.get("gross_PF"))
    avg_mfe = safe_float(row.get("avg_MFE"))
    avg_mae = safe_float(row.get("avg_MAE"))
    group = str(row.get("group_value") or "").upper()
    group_key = str(row.get("group_key") or "")
    if samples < safe_int(getattr(config, "net_edge_min_samples", 500)):
        return "SAMPLE_TOO_SMALL"
    if group_key == "side" and group == "LONG" and tp_ratio <= 0.001 and sl_ratio > 0.15:
        return "BAD_SIDE"
    if group_key == "symbol" and group == "BNBUSDT" and (pf < 1.0 or time_ratio >= 1.0):
        return "BAD_SYMBOL"
    if group_key == "symbol" and group == "BTCUSDT" and time_ratio > 0.85 and tp_ratio < 0.10:
        return "BAD_SYMBOL"
    if group_key == "score_bucket" and group.startswith("70-") and time_ratio > 0.85:
        return "SCORE_FALSE_POSITIVE"
    if group_key == "market_regime" and group == "RISK_OFF" and time_ratio > 0.80:
        return "RISK_OFF_NO_EDGE"
    if group_key == "market_regime" and group in {"CHOPPY_MARKET", "RANGE"} and time_ratio > 0.70:
        return "CHOPPY_MARKET_NO_EDGE"
    if time_ratio > 0.90 and tp_ratio < 0.10:
        return "LOW_VOL_NO_MOVEMENT" if avg_mfe < 0.25 else "TP_TOO_FAR"
    if time_ratio > 0.80 and avg_mfe >= 0.25 and safe_float(row.get("gross_expectancy")) <= 0:
        return "HOLD_TOO_LONG_DECAY"
    if time_ratio > 0.80 and avg_mfe < 0.25:
        return "LOW_VOL_NO_MOVEMENT"
    if avg_mfe >= 0.50 and avg_mae >= 0.50 and tp_ratio < 0.10:
        return "LATE_ENTRY_AFTER_MOVE"
    if time_ratio > 0.60 and safe_float(row.get("time_to_expiry_bars")) <= 2:
        return "HOLD_TOO_SHORT"
    if safe_float(row.get("stale_or_slow_market_data_warning")):
        return "POSSIBLE_LATENCY_FETCH_BOTTLENECK"
    return "UNKNOWN"


def decision_for(row: dict[str, Any], config: Any) -> str:
    samples = safe_int(row.get("samples"))
    min_samples = safe_int(getattr(config, "net_edge_min_samples", 500))
    time_ratio = safe_float(row.get("time_ratio"))
    tp_ratio = safe_float(row.get("tp_ratio"))
    avg_mfe = safe_float(row.get("avg_MFE"))
    cause = str(row.get("likely_cause") or likely_cause(row, config))
    if samples < min_samples:
        return DECISION_WATCH
    if time_ratio > 0.90:
        return DECISION_REJECT
    if time_ratio > 0.80 and tp_ratio < 0.10:
        return DECISION_REJECT
    if cause in {"BAD_SYMBOL", "BAD_SIDE", "RISK_OFF_NO_EDGE", "CHOPPY_MARKET_NO_EDGE", "SCORE_FALSE_POSITIVE"}:
        return DECISION_REJECT
    if time_ratio > 0.70 and avg_mfe >= 0.25:
        return DECISION_EXIT_TEST
    if safe_float(row.get("net_EV")) > 0 and safe_float(row.get("net_PF")) >= safe_float(getattr(config, "net_edge_min_net_pf", 1.2)):
        return DECISION_CONFIRMED
    return DECISION_WATCH


def _path_stats_by_group(paths: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for key in ("symbol", "side", "market_regime", "score_bucket", "source"):
        buckets: dict[str, list[dict[str, Any]]] = {}
        for row in paths:
            buckets.setdefault(str(row.get(key) or "NA").upper(), row)
        # The assignment above intentionally stores latest by key for memory,
        # but we need full lists; rebuild in a compact second pass.
        bucket_lists: dict[str, list[dict[str, Any]]] = {}
        for row in paths:
            bucket_lists.setdefault(str(row.get(key) or "NA").upper(), []).append(row)
        for value, rows in bucket_lists.items():
            out[(key, value)] = _path_stats(rows)
    # Policy ids are synthetic; use symbol/side/regime/bucket as a reasonable proxy.
    policy_lists: dict[str, list[dict[str, Any]]] = {}
    for row in paths:
        policy_id = f"policy_{row.get('symbol') or 'NA'}_{row.get('side') or 'NA'}_{row.get('market_regime') or 'NA'}_{row.get('score_bucket') or 'NA'}"
        policy_lists.setdefault(policy_id.upper(), []).append(row)
    for value, rows in policy_lists.items():
        out[("policy_id", value)] = _path_stats(rows)
    return out


def _path_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    mfe_values = [safe_float(row.get("max_favorable_pct")) for row in rows]
    mae_values = [safe_float(row.get("max_adverse_pct")) for row in rows]
    bars_mfe = [safe_float(row.get("bars_to_mfe")) for row in rows if safe_float(row.get("bars_to_mfe")) > 0]
    bars_mae = [safe_float(row.get("bars_to_mae")) for row in rows if safe_float(row.get("bars_to_mae")) > 0]
    no_move = sum(1 for value in mfe_values if value < 0.10)
    return {
        "avg_MFE": _avg(mfe_values),
        "avg_MAE": _avg(mae_values),
        "median_MFE": statistics.median(mfe_values) if mfe_values else 0.0,
        "median_MAE": statistics.median(mae_values) if mae_values else 0.0,
        "bars_to_max_MFE": _avg(bars_mfe),
        "bars_to_max_MAE": _avg(bars_mae),
        "no_movement_ratio": no_move / max(len(mfe_values), 1),
    }


def _latency_warning(db: Any, since: str) -> bool:
    try:
        rows = db.fetch_latency_metrics_since(since, limit=1000)
    except Exception:
        return False
    values = sorted(safe_float(row.get("duration_ms")) for row in rows if safe_float(row.get("duration_ms")) > 0)
    if not values:
        return False
    p95 = values[min(len(values) - 1, int(len(values) * 0.95))]
    return p95 > 5000


def _reason(row: dict[str, Any]) -> str:
    cause = str(row.get("likely_cause") or "UNKNOWN").lower()
    if safe_float(row.get("net_EV")) <= 0:
        return f"{cause}_net_ev_negative"
    if safe_float(row.get("time_ratio")) > 0.90:
        return f"{cause}_time_above_90"
    return cause


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _safe(fn, fallback):
    try:
        return fn()
    except Exception:
        return fallback


def _group_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- {row.get('group_key')}={row.get('group_value')} samples={row.get('samples')} "
            f"TIME={format_pct(row.get('time_ratio'))} TP={format_pct(row.get('tp_ratio'))} SL={format_pct(row.get('sl_ratio'))} "
            f"PF={format_num(row.get('gross_PF'))} net_PF={format_num(row.get('net_PF'))} net_EV={format_num(row.get('net_EV'), 4)} "
            f"avg_MFE={format_num(row.get('avg_MFE'))} median_MFE={format_num(row.get('median_MFE'))} "
            f"cause={row.get('likely_cause')} decision={row.get('decision')} reason={row.get('reason')}"
        )
        for row in rows[:12]
    ]
