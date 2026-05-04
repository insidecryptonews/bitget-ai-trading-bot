from __future__ import annotations

from collections import defaultdict
from typing import Any

from .database import Database
from .utils import safe_float, safe_int


FEATURES = [
    "confidence_score",
    "rsi_14",
    "macd_hist",
    "atr_14",
    "normalized_atr",
    "volume_relative",
    "spread_pct",
    "funding_rate",
    "open_interest",
    "distance_to_ema_21",
    "distance_to_ema_50",
    "distance_to_ema_200",
    "momentum_5",
    "momentum_15",
    "range_width_pct",
    "body_pct",
    "upper_wick_pct",
    "lower_wick_pct",
    "bullish_rejection",
    "bearish_rejection",
    "btc_momentum_5",
    "btc_momentum_15",
    "btc_normalized_atr",
    "eth_momentum_5",
    "number_of_symbols_bullish",
    "number_of_symbols_bearish",
    "market_risk_on",
    "market_risk_off",
    "shadow_strategy",
]

CATEGORICAL = ["btc_regime", "market_regime", "strategy_type", "symbol", "side", "original_side", "original_strategy_type", "score_bucket"]


class FeatureAttribution:
    def __init__(self, db: Database | None = None, logger=None) -> None:
        self.db = db
        self.logger = logger

    def analyze_rows(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        labeled = [row for row in rows if row.get("label") is not None]
        if not labeled:
            return {"top_tp": [], "top_sl": [], "top_time": [], "dangerous": [], "promising": [], "unstable": []}
        tp = [row for row in labeled if safe_int(row.get("label")) == 1]
        sl = [row for row in labeled if safe_int(row.get("label")) == -1]
        time_rows = [row for row in labeled if safe_int(row.get("label")) == 0]
        numeric = []
        for feature in FEATURES:
            tp_mean = _mean([safe_float(row.get(feature)) for row in tp])
            sl_mean = _mean([safe_float(row.get(feature)) for row in sl])
            time_mean = _mean([safe_float(row.get(feature)) for row in time_rows])
            numeric.append({
                "feature": feature,
                "tp_mean": tp_mean,
                "sl_mean": sl_mean,
                "time_mean": time_mean,
                "tp_lift_vs_sl": tp_mean - sl_mean,
                "sl_lift_vs_tp": sl_mean - tp_mean,
                "time_lift_vs_decisive": time_mean - _mean([tp_mean, sl_mean]),
            })
        categorical = []
        for feature in CATEGORICAL:
            categorical.extend(_categorical_lift(labeled, feature))
        return {
            "top_tp": sorted(numeric + categorical, key=lambda row: safe_float(row.get("tp_lift_vs_sl")), reverse=True)[:12],
            "top_sl": sorted(numeric + categorical, key=lambda row: safe_float(row.get("sl_lift_vs_tp")), reverse=True)[:12],
            "top_time": sorted(numeric + categorical, key=lambda row: safe_float(row.get("time_lift_vs_decisive")), reverse=True)[:12],
            "dangerous": [row for row in categorical if safe_float(row.get("sl_rate")) > 0.55][:12],
            "promising": [row for row in categorical if safe_float(row.get("tp_rate")) > 0.55 and safe_int(row.get("count")) >= 20][:12],
            "unstable": [row for row in categorical if safe_int(row.get("count")) < 20][:12],
        }

    def report(self) -> str:
        rows = self.db.fetch_labeled_signal_rows() if self.db else []
        result = self.analyze_rows(rows)
        lines = ["Feature Importance", "=================="]
        if not rows:
            lines.append("Evidencia insuficiente.")
            return "\n".join(lines)
        for title, key in [
            ("Variables asociadas a TP", "top_tp"),
            ("Variables asociadas a SL", "top_sl"),
            ("Variables asociadas a TIME", "top_time"),
            ("Variables peligrosas", "dangerous"),
            ("Variables prometedoras", "promising"),
            ("Variables inestables/overfit", "unstable"),
        ]:
            lines.append("")
            lines.append(title)
            for item in result[key][:8]:
                label = item.get("feature")
                if item.get("bucket") is not None:
                    label = f"{label}={item.get('bucket')}"
                lines.append(f"- {label}: {item}")
        return "\n".join(lines)


def _categorical_lift(rows: list[dict[str, Any]], feature: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(feature) or "NA")].append(row)
    output = []
    for bucket, items in grouped.items():
        count = len(items)
        tp_rate = sum(1 for row in items if safe_int(row.get("label")) == 1) / max(count, 1)
        sl_rate = sum(1 for row in items if safe_int(row.get("label")) == -1) / max(count, 1)
        time_rate = sum(1 for row in items if safe_int(row.get("label")) == 0) / max(count, 1)
        output.append({
            "feature": feature,
            "bucket": bucket,
            "count": count,
            "tp_rate": tp_rate,
            "sl_rate": sl_rate,
            "time_rate": time_rate,
            "tp_lift_vs_sl": tp_rate - sl_rate,
            "sl_lift_vs_tp": sl_rate - tp_rate,
            "time_lift_vs_decisive": time_rate - max(tp_rate, sl_rate),
        })
    return output


def _mean(values: list[float]) -> float:
    clean = [value for value in values if value or value == 0]
    return sum(clean) / max(len(clean), 1)

