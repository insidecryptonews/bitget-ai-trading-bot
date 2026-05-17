from __future__ import annotations

from collections import defaultdict
from typing import Any

from .edge_hardening_utils import FINAL_NO_LIVE, cost_config, format_num, format_pct
from .pre_move_feature_snapshot import PreMoveFeatureSnapshot
from .utils import safe_float, safe_int


START = "PRE MOVE PATTERN MINER START"
END = "PRE MOVE PATTERN MINER END"

LONG_PATTERN_CANDIDATE = "LONG_PATTERN_CANDIDATE"
SHORT_PATTERN_CANDIDATE = "SHORT_PATTERN_CANDIDATE"
WATCH_ONLY = "WATCH_ONLY"
REJECT = "REJECT"
TRAP_PATTERN = "TRAP_PATTERN"
TIME_DEATH_PATTERN = "TIME_DEATH_PATTERN"


class PreMovePatternMiner:
    """Mine repeatable pre-move patterns without opening trades."""

    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        snapshots = PreMoveFeatureSnapshot(self.config, self.db).build(hours=hours).get("snapshots", [])
        patterns = [_build_pattern(key, rows, self.config) for key, rows in _group_snapshots(snapshots).items()]
        patterns.sort(key=lambda row: (safe_float(row.get("net_EV")), safe_float(row.get("net_PF")), safe_int(row.get("samples"))), reverse=True)
        return {
            "hours": max(1, int(hours or 24)),
            "patterns": patterns[:200],
            "top_long_patterns": [row for row in patterns if row.get("decision") == LONG_PATTERN_CANDIDATE][:10],
            "top_short_patterns": [row for row in patterns if row.get("decision") == SHORT_PATTERN_CANDIDATE][:10],
            "trap_patterns": [row for row in patterns if row.get("decision") == TRAP_PATTERN][:10],
            "time_death_patterns": [row for row in patterns if row.get("decision") == TIME_DEATH_PATTERN][:10],
            "rejected_patterns": [row for row in patterns if row.get("decision") == REJECT][:10],
            "watch_only_patterns": [row for row in patterns if row.get("decision") == WATCH_ONLY][:10],
            "final_recommendation": FINAL_NO_LIVE,
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        return "\n".join([
            START,
            f"hours: {payload['hours']}",
            "top_long_patterns:",
            *_pattern_lines(payload["top_long_patterns"]),
            "top_short_patterns:",
            *_pattern_lines(payload["top_short_patterns"]),
            "trap_patterns:",
            *_pattern_lines(payload["trap_patterns"]),
            "time_death_patterns:",
            *_pattern_lines(payload["time_death_patterns"]),
            "rejected_patterns:",
            *_pattern_lines(payload["rejected_patterns"]),
            "watch_only_patterns:",
            *_pattern_lines(payload["watch_only_patterns"]),
            "final_recommendation: NO LIVE",
            END,
        ])


def _group_snapshots(snapshots: list[dict[str, Any]]) -> dict[tuple[str, ...], list[dict[str, Any]]]:
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in snapshots:
        key = (
            str(row.get("symbol") or "NA").upper(),
            str(row.get("side") or "NA").upper(),
            str(row.get("market_regime") or "NA").upper(),
            str(row.get("score_bucket") or "NA").upper(),
            str(row.get("strategy") or "NA"),
            str(row.get("source") or "NA"),
            str(row.get("volatility_proxy") or "missing"),
            str(row.get("volume_proxy") or "missing"),
            str(row.get("btc_eth_alignment_proxy") or "missing"),
            str(row.get("breakout_breakdown_proxy") or "missing"),
        )
        grouped[key].append(row)
    return grouped


def _build_pattern(key: tuple[str, ...], rows: list[dict[str, Any]], config: Any) -> dict[str, Any]:
    costs = cost_config(config)
    samples = len(rows)
    direction = key[1]
    long_hits = sum(1 for row in rows if row.get("classification") == "PRE_LONG_EDGE")
    short_hits = sum(1 for row in rows if row.get("classification") == "PRE_SHORT_EDGE")
    traps = sum(1 for row in rows if str(row.get("classification") or "").startswith("TRAP"))
    time_deaths = sum(1 for row in rows if row.get("classification") == "TIME_DEATH_LIKELY")
    net_ev = _avg([safe_float(row.get("net_EV")) for row in rows])
    net_pf = _avg([safe_float(row.get("net_PF")) for row in rows])
    tp = _avg([safe_float(row.get("recent_TP_ratio")) for row in rows])
    sl = _avg([safe_float(row.get("recent_SL_ratio")) for row in rows])
    time_ratio = _avg([safe_float(row.get("recent_TIME_ratio")) for row in rows])
    mfe = _avg([safe_float(row.get("MFE_before_event")) for row in rows])
    mae = _avg([safe_float(row.get("MAE_before_event")) for row in rows])
    fakeout_rate = traps / max(samples, 1)
    row = {
        "pattern_id": "pattern_" + "_".join(key[:4]).replace(" ", "_"),
        "symbol": key[0],
        "direction": direction,
        "event_type": "MIXED",
        "regime": key[2],
        "score_bucket": key[3],
        "side": direction,
        "strategy": key[4],
        "source": key[5],
        "volatility_bucket": key[6],
        "volume_bucket": key[7],
        "btc_eth_context_bucket": key[8],
        "breakout_breakdown_proxy": key[9],
        "samples": samples,
        "up_event_rate": long_hits / max(samples, 1),
        "down_event_rate": short_hits / max(samples, 1),
        "fakeout_rate": fakeout_rate,
        "TIME_after_signal": time_ratio,
        "TP_after_signal": tp,
        "SL_after_signal": sl,
        "gross_PF": net_pf,
        "net_PF": net_pf,
        "net_EV": net_ev,
        "avg_MFE": mfe,
        "avg_MAE": mae,
        "median_MFE": _median([safe_float(row.get("MFE_before_event")) for row in rows]),
        "median_MAE": _median([safe_float(row.get("MAE_before_event")) for row in rows]),
        "move_capture_ratio": mfe / max(mfe + mae, 0.0001),
        "adverse_before_move_ratio": mae / max(mfe + mae, 0.0001),
        "recent_stability": 1.0 if net_ev > 0 and time_ratio <= costs.max_time_ratio else 0.0,
        "walk_forward_stability": "unknown",
        "anti_overfit_status": "unknown",
        "candidate_ranking_status": "unknown",
    }
    row["decision"] = pattern_decision(row, config)
    row["reason"] = pattern_reason(row, config)
    return row


def pattern_decision(row: dict[str, Any], config: Any) -> str:
    costs = cost_config(config)
    samples = safe_int(row.get("samples"))
    direction = str(row.get("direction") or "").upper()
    if samples < max(3, min(costs.min_samples, 50)):
        return WATCH_ONLY
    if safe_float(row.get("fakeout_rate")) >= 0.35:
        return TRAP_PATTERN
    if safe_float(row.get("net_EV")) <= 0 or safe_float(row.get("net_PF")) < costs.min_net_pf:
        return REJECT
    if safe_float(row.get("TIME_after_signal")) > costs.max_time_ratio and safe_float(row.get("TP_after_signal")) < costs.min_tp_ratio:
        return TIME_DEATH_PATTERN
    if str(row.get("regime") or "").upper() == "RISK_OFF" and safe_float(row.get("TP_after_signal")) < costs.min_tp_ratio:
        return REJECT
    if direction == "LONG" and safe_float(row.get("SL_after_signal")) > 0.15 and safe_float(row.get("TP_after_signal")) < 0.03:
        return REJECT
    if direction == "SHORT" and safe_float(row.get("TIME_after_signal")) > costs.max_time_ratio:
        return REJECT
    if direction == "LONG":
        return LONG_PATTERN_CANDIDATE
    if direction == "SHORT":
        return SHORT_PATTERN_CANDIDATE
    return WATCH_ONLY


def pattern_reason(row: dict[str, Any], config: Any) -> str:
    costs = cost_config(config)
    if safe_int(row.get("samples")) < max(3, min(costs.min_samples, 50)):
        return "sample_too_small"
    if safe_float(row.get("fakeout_rate")) >= 0.35:
        return "fakeout_rate_high"
    if safe_float(row.get("net_EV")) <= 0:
        return "net_ev_not_confirmed"
    if safe_float(row.get("net_PF")) < costs.min_net_pf:
        return "net_pf_below_min"
    if safe_float(row.get("TIME_after_signal")) > costs.max_time_ratio:
        return "time_death_pattern"
    if str(row.get("score_bucket") or "").upper() in {"90-94", "95-100", "90-100"} and str(row.get("symbol") or "NA") == "NA":
        return "generic_bucket_not_actionable"
    return "pre_move_pattern_confirmed"


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2.0


def _pattern_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- {row.get('pattern_id')} {row.get('direction')} samples={row.get('samples')} "
            f"net_EV={format_num(row.get('net_EV'), 4)} net_PF={format_num(row.get('net_PF'))} "
            f"TP={format_pct(row.get('TP_after_signal'))} TIME={format_pct(row.get('TIME_after_signal'))} "
            f"fakeout={format_pct(row.get('fakeout_rate'))} decision={row.get('decision')} reason={row.get('reason')}"
        )
        for row in rows[:8]
    ]
