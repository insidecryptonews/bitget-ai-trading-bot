from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from .database import Database
from .research_lab import ResearchMetrics
from .utils import iso_utc, safe_float, safe_int


class WinAnalyzer:
    def __init__(self, db: Database | None = None, logger=None) -> None:
        self.db = db
        self.logger = logger

    def analyze_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[(
                str(row.get("symbol") or "NA"),
                str(row.get("side") or "NA"),
                str(row.get("strategy_type") or "NA"),
                str(row.get("market_regime") or "NA"),
                str(row.get("score_bucket") or _score_bucket(row)),
            )].append(row)
        clusters = []
        for key, items in sorted(grouped.items(), key=lambda item: _tp_count(item[1]), reverse=True):
            metrics = ResearchMetrics.calculate(items)
            symbol, side, strategy, regime, score = key
            common = {
                "avg_rsi_14": _mean([safe_float(row.get("rsi_14")) for row in items if safe_int(row.get("label")) == 1]),
                "avg_normalized_atr": _mean([safe_float(row.get("normalized_atr")) for row in items if safe_int(row.get("label")) == 1]),
                "avg_volume_relative": _mean([safe_float(row.get("volume_relative")) for row in items if safe_int(row.get("label")) == 1]),
                "btc_alignment_rate": _mean([1.0 if _btc_aligned(row) else 0.0 for row in items if safe_int(row.get("label")) == 1]),
            }
            clusters.append({
                "cluster_name": f"{symbol}_{side}_{strategy}_{regime}_{score}",
                "symbol": symbol,
                "side": side,
                "strategy_type": strategy,
                "market_regime": regime,
                "score_bucket": score,
                "total_tp": safe_int(metrics["tp1_count"] + metrics["tp2_count"]),
                "total_sl": safe_int(metrics["sl_count"]),
                "total_time": safe_int(metrics["time_count"]),
                "win_rate": metrics["win_rate"],
                "profit_factor": metrics["profit_factor"],
                "expectancy": metrics["expectancy"],
                "common_features_json": json.dumps(common, sort_keys=True),
                "recommended_rule": _recommended_rule(metrics, symbol, strategy, regime),
                "confidence": min(0.95, metrics["total_labels"] / 100),
                "created_at": iso_utc(),
            })
        return clusters

    def generate(self) -> list[dict[str, Any]]:
        if self.db is None:
            return []
        clusters = self.analyze_rows(self.db.fetch_labeled_signal_rows())
        for cluster in clusters:
            self.db.record_win_cluster(cluster)
        return clusters

    def report(self) -> str:
        clusters = self.generate()
        return self.format_report(clusters)

    @staticmethod
    def format_report(clusters: list[dict[str, Any]]) -> str:
        lines = ["Win Analysis", "============"]
        winners = [cluster for cluster in clusters if cluster["total_tp"] > 0]
        if not winners:
            lines.append("Evidencia insuficiente: no hay TP etiquetados.")
            return "\n".join(lines)
        lines.append("Top win clusters")
        for cluster in winners[:10]:
            lines.append(
                f"- {cluster['cluster_name']}: TP={cluster['total_tp']}, SL={cluster['total_sl']}, "
                f"PF={cluster['profit_factor']:.2f}, rule={cluster['recommended_rule']}"
            )
        return "\n".join(lines)


def _tp_count(rows: list[dict[str, Any]]) -> int:
    return sum(1 for row in rows if row.get("first_barrier_hit") in {"TP1", "TP2"} or safe_int(row.get("label")) == 1)


def _btc_aligned(row: dict[str, Any]) -> bool:
    side = str(row.get("side") or "").upper()
    btc = safe_float(row.get("btc_momentum_5")) + safe_float(row.get("btc_momentum_15"))
    return (side == "LONG" and btc >= 0) or (side == "SHORT" and btc <= 0)


def _recommended_rule(metrics: dict[str, float], symbol: str, strategy: str, regime: str) -> str:
    if metrics["total_labels"] < 100:
        return "OBSERVE_ONLY: evidencia insuficiente."
    if metrics["profit_factor"] >= 1.2 and metrics["expectancy"] > 0:
        return f"ALLOW_ONLY research: {symbol} {strategy} en {regime}, pendiente walk-forward."
    return "No potenciar; edge insuficiente."


def _score_bucket(row: dict[str, Any]) -> str:
    score = safe_float(row.get("confidence_score"))
    if score >= 90:
        return "90+"
    if score >= 85:
        return "85-89"
    if score >= 80:
        return "80-84"
    if score >= 75:
        return "75-79"
    return "<75"


def _mean(values: list[float]) -> float:
    clean = [value for value in values if value or value == 0]
    return sum(clean) / max(len(clean), 1)
