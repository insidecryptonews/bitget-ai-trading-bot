from __future__ import annotations

from typing import Any

from .edge_hardening_utils import FINAL_NO_LIVE, cost_config, format_num, format_pct, since_iso
from .pre_move_pattern_miner import LONG_PATTERN_CANDIDATE, SHORT_PATTERN_CANDIDATE, PreMovePatternMiner
from .utils import safe_float, safe_int


START = "PRE MOVE SIMILARITY SCANNER START"
END = "PRE MOVE SIMILARITY SCANNER END"


class PreMoveSimilarityScanner:
    """Compare recent compact path metrics with mined pre-move patterns."""

    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 6) -> dict[str, Any]:
        hours = max(1, int(hours or 6))
        patterns = PreMovePatternMiner(self.config, self.db).build(hours=max(24, hours)).get("patterns", [])
        recent = _safe(lambda: self.db.fetch_signal_path_metrics_since(since_iso(hours), limit=5000), [])
        matches = [_best_match(row, patterns, self.config) for row in recent]
        matches = [row for row in matches if row]
        matches.sort(key=lambda row: (safe_float(row.get("similarity_score")), safe_float(row.get("historical_net_EV"))), reverse=True)
        return {
            "hours": hours,
            "matches": matches[:100],
            "long_watch": [row for row in matches if row.get("decision") == "LONG_WATCH"][:10],
            "short_watch": [row for row in matches if row.get("decision") == "SHORT_WATCH"][:10],
            "trap_like": [row for row in matches if row.get("reason") == "trap_or_fakeout_risk"][:10],
            "time_death_like": [row for row in matches if row.get("reason") == "time_death_risk"][:10],
            "no_match_count": max(0, len(recent) - len(matches)),
            "final_recommendation": FINAL_NO_LIVE,
        }

    def to_text(self, *, hours: int = 6) -> str:
        payload = self.build(hours=hours)
        return "\n".join([
            START,
            f"hours: {payload['hours']}",
            f"matches: {len(payload['matches'])}",
            f"no_match_count: {payload['no_match_count']}",
            "long_watch:",
            *_match_lines(payload["long_watch"]),
            "short_watch:",
            *_match_lines(payload["short_watch"]),
            "trap_like:",
            *_match_lines(payload["trap_like"]),
            "time_death_like:",
            *_match_lines(payload["time_death_like"]),
            "final_recommendation: NO LIVE",
            END,
        ])


def _best_match(row: dict[str, Any], patterns: list[dict[str, Any]], config: Any) -> dict[str, Any] | None:
    symbol = str(row.get("symbol") or "NA").upper()
    side = str(row.get("side") or "NA").upper()
    regime = str(row.get("market_regime") or "NA").upper()
    bucket = str(row.get("score_bucket") or _score_bucket(safe_int(row.get("score")))).upper()
    best: tuple[float, dict[str, Any] | None] = (0.0, None)
    for pattern in patterns:
        score = 0.0
        if str(pattern.get("symbol") or "").upper() == symbol:
            score += 0.35
        if str(pattern.get("direction") or "").upper() == side:
            score += 0.25
        if str(pattern.get("regime") or "").upper() == regime:
            score += 0.20
        if str(pattern.get("score_bucket") or "").upper() == bucket:
            score += 0.15
        if str(pattern.get("source") or "").lower() == str(row.get("source") or "").lower():
            score += 0.05
        if score > best[0]:
            best = (score, pattern)
    if best[1] is None or best[0] < 0.40:
        return None
    pattern = best[1]
    decision, reason = _decision(pattern, best[0], config)
    return {
        "symbol": symbol,
        "possible_direction": str(pattern.get("direction") or "UNKNOWN"),
        "similarity_score": best[0],
        "matched_pattern_id": pattern.get("pattern_id"),
        "matched_event_type": pattern.get("event_type"),
        "historical_samples": safe_int(pattern.get("samples")),
        "historical_net_EV": safe_float(pattern.get("net_EV")),
        "historical_TP": safe_float(pattern.get("TP_after_signal")),
        "historical_TIME": safe_float(pattern.get("TIME_after_signal")),
        "historical_SL": safe_float(pattern.get("SL_after_signal")),
        "current_regime": regime,
        "current_score": safe_int(row.get("score")),
        "current_side": side,
        "current_strategy": str(row.get("strategy") or row.get("strategy_type") or "NA"),
        "decision": decision,
        "reason": reason,
    }


def _decision(pattern: dict[str, Any], similarity: float, config: Any) -> tuple[str, str]:
    costs = cost_config(config)
    if safe_float(pattern.get("net_EV")) <= 0 or safe_float(pattern.get("net_PF")) < costs.min_net_pf:
        return "REJECT", "historical_net_ev_negative"
    if safe_int(pattern.get("samples")) < max(3, min(costs.min_samples, 50)):
        return "WATCH_ONLY", "sample_too_small"
    if safe_float(pattern.get("TIME_after_signal")) > costs.max_time_ratio:
        return "REJECT" if safe_float(pattern.get("TP_after_signal")) < costs.min_tp_ratio else "WATCH_ONLY", "time_death_risk"
    if safe_float(pattern.get("fakeout_rate")) >= 0.35:
        return "REJECT", "trap_or_fakeout_risk"
    if similarity < 0.70:
        return "WATCH_ONLY", "partial_match"
    if pattern.get("decision") == LONG_PATTERN_CANDIDATE:
        return "LONG_WATCH", "long_pattern_match"
    if pattern.get("decision") == SHORT_PATTERN_CANDIDATE:
        return "SHORT_WATCH", "short_pattern_match"
    return "WATCH_ONLY", "no_actionable_pattern"


def _score_bucket(score: int) -> str:
    if score >= 95:
        return "95-100"
    if score >= 90:
        return "90-94"
    if score >= 80:
        return "80-89"
    if score >= 70:
        return "70-79"
    if score >= 60:
        return "60-69"
    return "<60"


def _match_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- {row.get('symbol')} direction={row.get('possible_direction')} similarity={format_num(row.get('similarity_score'))} "
            f"samples={row.get('historical_samples')} net_EV={format_num(row.get('historical_net_EV'), 4)} "
            f"TP={format_pct(row.get('historical_TP'))} TIME={format_pct(row.get('historical_TIME'))} "
            f"decision={row.get('decision')} reason={row.get('reason')}"
        )
        for row in rows[:8]
    ]


def _safe(fn, fallback):
    try:
        return fn()
    except Exception:
        return fallback
