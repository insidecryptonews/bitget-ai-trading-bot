from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .config import BotConfig
from .database import Database
from .utils import safe_float, safe_int


START = "SCORE CALIBRATION START"
END = "SCORE CALIBRATION END"


class ScoreCalibrationLab:
    def __init__(self, config: BotConfig, db: Database) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        hours = max(1, int(hours or 24))
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        buckets = self.db.get_score_calibration_summaries_since(since, min_score=70)
        diagnosis = _diagnosis(buckets)
        return {
            "hours": hours,
            "score_buckets": buckets,
            "diagnosis": diagnosis,
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        lines = [
            START,
            f"hours: {payload['hours']}",
            "score_buckets:",
            *_bucket_lines(payload["score_buckets"]),
            "diagnosis:",
            f"- score_not_monotonic={str(payload['diagnosis']['score_not_monotonic']).lower()}",
            f"- high_score_false_positive={str(payload['diagnosis']['high_score_false_positive']).lower()}",
            f"- best_bucket={payload['diagnosis']['best_bucket']}",
            "recommendation:",
            "- no confiar ciegamente en score alto",
            "- usar score + edge guard + regimen + MFE/MAE",
            "final_recommendation: NO LIVE",
            END,
        ]
        return "\n".join(lines)


def _diagnosis(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_bucket = {str(row.get("group_value")): row for row in rows}
    pf_80 = safe_float(by_bucket.get("80-89", {}).get("profit_factor"))
    pf_90 = max(
        safe_float(by_bucket.get("90-94", {}).get("profit_factor")),
        safe_float(by_bucket.get("95-100", {}).get("profit_factor")),
    )
    best = max(rows, key=lambda row: safe_float(row.get("profit_factor")), default={})
    high_score_rows = [by_bucket.get("90-94", {}), by_bucket.get("95-100", {})]
    high_score_tp = sum(safe_float(row.get("tp_ratio")) for row in high_score_rows) / max(len([row for row in high_score_rows if row]), 1)
    return {
        "score_not_monotonic": bool(pf_80 > 0 and pf_80 > pf_90),
        "high_score_false_positive": bool(pf_90 > 0 and (pf_90 < 1.0 or high_score_tp < 0.02)),
        "best_bucket": str(best.get("group_value") or "insufficient_data"),
        "score_bucket_with_best_pf": str(best.get("group_value") or "insufficient_data"),
        "score_bucket_with_best_tp": _best_by(rows, "tp_ratio"),
        "score_bucket_with_lowest_sl": _lowest_by(rows, "sl_ratio"),
    }


def _best_by(rows: list[dict[str, Any]], key: str) -> str:
    row = max(rows, key=lambda item: safe_float(item.get(key)), default={})
    return str(row.get("group_value") or "insufficient_data")


def _lowest_by(rows: list[dict[str, Any]], key: str) -> str:
    clean = [row for row in rows if safe_int(row.get("total_labels")) > 0]
    row = min(clean, key=lambda item: safe_float(item.get(key)), default={})
    return str(row.get("group_value") or "insufficient_data")


def _bucket_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- insufficient score bucket data"]
    order = {"70-79": 0, "80-89": 1, "90-94": 2, "95-100": 3}
    rows = sorted(rows, key=lambda row: order.get(str(row.get("group_value")), 99))
    return [
        (
            f"- {row.get('group_value')} labels={safe_int(row.get('total_labels'))} "
            f"PF={safe_float(row.get('profit_factor')):.2f} "
            f"TP%={safe_float(row.get('tp_ratio')) * 100:.1f} "
            f"SL%={safe_float(row.get('sl_ratio')) * 100:.1f} "
            f"TIME%={safe_float(row.get('time_ratio')) * 100:.1f}"
        )
        for row in rows
    ]
