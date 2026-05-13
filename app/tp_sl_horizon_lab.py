from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .config import BotConfig
from .database import Database
from .utils import safe_float, safe_int


START = "TP SL HORIZON LAB START"
END = "TP SL HORIZON LAB END"


class TpSlHorizonLab:
    """Cheap TP/SL horizon research using existing labels and group aggregates."""

    def __init__(self, config: BotConfig, db: Database) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        hours = max(1, int(hours or 24))
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        since_iso = since.isoformat()
        labels = self.db.get_high_score_label_summary_since(since_iso, self.config.min_score_to_trade)
        by_regime = self.db.get_shadow_opportunity_group_summaries_since(
            since_iso,
            min_score=self.config.min_score_to_trade,
            group_key="market_regime",
            limit=10,
        )
        by_symbol = self.db.get_shadow_opportunity_group_summaries_since(
            since_iso,
            min_score=self.config.min_score_to_trade,
            group_key="symbol",
            limit=10,
        )
        by_score = self.db.get_shadow_opportunity_group_summaries_since(
            since_iso,
            min_score=self.config.min_score_to_trade,
            group_key="score_bucket",
            limit=10,
        )
        current = _metrics(labels)
        return {
            "hours": hours,
            "current": current,
            "problem": _problem(current),
            "price_path_status": "insufficient_price_path_data",
            "candidate_adjustments": _candidate_adjustments(current, by_regime),
            "score_calibration": _score_calibration(by_score),
            "recommended_experiments": _recommended_experiments(by_symbol, by_regime, by_score),
            "by_score_bucket": by_score,
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        current = payload["current"]
        lines = [
            START,
            f"hours: {payload['hours']}",
            "current:",
            (
                f"- PF={current['profit_factor']:.2f} "
                f"TIME%={current['time_ratio'] * 100:.1f} "
                f"SL%={current['sl_ratio'] * 100:.1f} "
                f"TP%={current['tp_ratio'] * 100:.1f}"
            ),
            f"price_path_status: {payload['price_path_status']}",
            "problem:",
            f"- {payload['problem']}",
            "candidate_adjustments:",
            *[f"- {item}" for item in payload["candidate_adjustments"]],
            "score_calibration:",
            *[f"- {item}" for item in payload["score_calibration"]],
            "recommended_experiments:",
            *[f"{idx}. {item}" for idx, item in enumerate(payload["recommended_experiments"], start=1)],
            "final_recommendation: NO LIVE",
            END,
        ]
        return "\n".join(lines)


def _metrics(labels: dict[str, Any]) -> dict[str, float]:
    total = safe_float(labels.get("total_labels"))
    tp = safe_float(labels.get("tp1_count")) + safe_float(labels.get("tp2_count"))
    return {
        "labels": total,
        "profit_factor": safe_float(labels.get("profit_factor")),
        "time_ratio": safe_float(labels.get("time_count")) / max(total, 1.0) if total else 0.0,
        "sl_ratio": safe_float(labels.get("sl_count")) / max(total, 1.0) if total else 0.0,
        "tp_ratio": tp / max(total, 1.0) if total else 0.0,
    }


def _problem(current: dict[str, float]) -> str:
    if current["labels"] <= 0:
        return "insufficient_labels"
    if current["tp_ratio"] < 0.01:
        return "low_tp"
    if current["time_ratio"] > 0.60:
        return "too_many_time"
    if current["sl_ratio"] > current["tp_ratio"] * 2:
        return "too_many_sl"
    return "needs_more_validation"


def _candidate_adjustments(current: dict[str, float], by_regime: list[dict[str, Any]]) -> list[str]:
    items: list[str] = []
    if current["tp_ratio"] < 0.05:
        items.append("reduce TP distance")
    if current["time_ratio"] > 0.60:
        items.append("shorten/extend max holding in shadow")
    bad_regimes = [str(row.get("group_value")) for row in by_regime if safe_float(row.get("profit_factor")) < 1.0]
    if bad_regimes:
        items.append("avoid " + "/".join(bad_regimes[:4]))
    items.append("prefer symbols/regimes with PF>1.2")
    return items


def _score_calibration(by_score: list[dict[str, Any]]) -> list[str]:
    by_bucket = {str(row.get("group_value")): row for row in by_score}
    pf_80 = safe_float(by_bucket.get("80-89", {}).get("profit_factor"))
    pf_90 = safe_float(by_bucket.get("90-100", {}).get("profit_factor"))
    lines = [
        (
            f"{row.get('group_value')} PF={safe_float(row.get('profit_factor')):.2f} "
            f"TP%={safe_float(row.get('tp_ratio')) * 100:.1f} "
            f"TIME%={safe_float(row.get('time_ratio')) * 100:.1f} "
            f"SL%={safe_float(row.get('sl_ratio')) * 100:.1f}"
        )
        for row in by_score
    ]
    if pf_80 > 0 and pf_90 > 0 and pf_80 > pf_90:
        lines.append("80-89 better than 90-100 -> score_not_monotonic")
    if not lines:
        lines.append("insufficient score bucket data")
    return lines


def _recommended_experiments(by_symbol: list[dict[str, Any]], by_regime: list[dict[str, Any]], by_score: list[dict[str, Any]]) -> list[str]:
    good_symbols = [str(row.get("group_value")) for row in by_symbol if safe_float(row.get("profit_factor")) >= 1.2]
    bad_regimes = [str(row.get("group_value")) for row in by_regime if safe_float(row.get("profit_factor")) < 1.0]
    experiments = [
        "TP lower / tighter target in shadow",
        "filter score 90-100 unless group edge confirms",
        "avoid RANGE/RISK_OFF/TREND_UP when PF<1",
        "symbol allowlist research-only: " + (", ".join(good_symbols[:5]) if good_symbols else "insufficient evidence"),
    ]
    if bad_regimes:
        experiments.append("regime blacklist research-only: " + ", ".join(bad_regimes[:5]))
    return experiments
