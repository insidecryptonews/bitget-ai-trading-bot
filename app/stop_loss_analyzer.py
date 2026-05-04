from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .counterfactual_engine import CounterfactualEngine
from .database import Database
from .explainability_engine import ExplainabilityEngine
from .utils import iso_utc, safe_float, safe_int


class StopLossAnalyzer:
    def __init__(self, db: Database | None = None, logger=None) -> None:
        self.db = db
        self.logger = logger
        self.explainer = ExplainabilityEngine(db, logger)
        self.counterfactuals = CounterfactualEngine(db, logger)

    def analyze_rows(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        sl_rows = [row for row in rows if row.get("first_barrier_hit") == "SL" or safe_int(row.get("label")) == -1]
        tp_rows = [row for row in rows if row.get("first_barrier_hit") in {"TP1", "TP2"} or safe_int(row.get("label")) == 1]
        time_rows = [row for row in rows if row.get("first_barrier_hit") == "TIME" or safe_int(row.get("label")) == 0]
        reason_counts: Counter[str] = Counter()
        for row in sl_rows:
            explanation = self.explainer.explain_row(row)
            reason_counts[explanation["primary_reason"]] += 1
            try:
                import json

                for reason in json.loads(explanation.get("secondary_reasons_json") or "[]"):
                    reason_counts[str(reason)] += 1
            except Exception:
                pass
        clusters = self._clusters(sl_rows, tp_rows, time_rows)
        return {
            "total_sl": len(sl_rows),
            "total_tp": len(tp_rows),
            "total_time": len(time_rows),
            "reason_counts": dict(reason_counts),
            "clusters": clusters,
        }

    def generate(self) -> dict[str, Any]:
        if self.db is None:
            return {"total_sl": 0, "total_tp": 0, "total_time": 0, "reason_counts": {}, "clusters": []}
        rows = self.db.fetch_labeled_signal_rows()
        result = self.analyze_rows(rows)
        for cluster in result["clusters"]:
            self.db.record_stop_loss_failure_cluster(cluster)
        return result

    def report(self) -> str:
        result = self.generate()
        return self.format_report(result)

    @staticmethod
    def format_report(result: dict[str, Any]) -> str:
        lines = ["Stop Loss Analysis", "=================="]
        if result["total_sl"] == 0:
            lines.append("Evidencia insuficiente: no hay stop losses etiquetados.")
            return "\n".join(lines)
        lines.append(f"SL totales: {result['total_sl']}")
        lines.append(f"TP totales: {result['total_tp']}")
        lines.append(f"TIME totales: {result['total_time']}")
        lines.append("")
        lines.append("Top reason codes de SL")
        for reason, count in sorted(result["reason_counts"].items(), key=lambda item: item[1], reverse=True)[:10]:
            lines.append(f"- {reason}: {count}")
        lines.append("")
        lines.append("Top failure clusters")
        for cluster in result["clusters"][:10]:
            lines.append(
                f"- {cluster['cluster_name']}: SL={cluster['total_sl']}, TP={cluster['total_tp']}, "
                f"TIME={cluster['total_time']}, regla={cluster['recommended_rule']}"
            )
        return "\n".join(lines)

    def _clusters(self, sl_rows: list[dict[str, Any]], tp_rows: list[dict[str, Any]], time_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in sl_rows:
            grouped[(
                str(row.get("symbol") or "NA"),
                str(row.get("side") or "NA"),
                str(row.get("strategy_type") or "NA"),
                str(row.get("market_regime") or "NA"),
                str(row.get("score_bucket") or _score_bucket(row)),
            )].append(row)
        clusters = []
        all_rows = sl_rows + tp_rows + time_rows
        for key, items in sorted(grouped.items(), key=lambda item: len(item[1]), reverse=True):
            symbol, side, strategy, regime, score = key
            bucket_rows = [row for row in all_rows if _matches(row, key)]
            reason = self.explainer.explain_row(items[0])["primary_reason"] if items else "UNKNOWN_INSUFFICIENT_CONTEXT"
            cfs = [cf for row in items for cf in self.counterfactuals.simulate_row(row)]
            clusters.append({
                "cluster_name": f"{symbol}_{side}_{strategy}_{regime}_{score}",
                "symbol": symbol,
                "side": side,
                "strategy_type": strategy,
                "market_regime": regime,
                "score_bucket": score,
                "total_sl": len(items),
                "total_tp": sum(1 for row in bucket_rows if row.get("first_barrier_hit") in {"TP1", "TP2"}),
                "total_time": sum(1 for row in bucket_rows if row.get("first_barrier_hit") == "TIME"),
                "avg_adverse_excursion": _mean([safe_float(row.get("max_adverse_excursion")) for row in items]),
                "avg_favorable_before_sl": _mean([safe_float(row.get("max_favorable_excursion")) for row in items]),
                "reverse_would_have_helped_count": sum(1 for cf in cfs if cf["scenario_name"] == "REVERSE_SIDE" and safe_int(cf.get("improved_result"))),
                "wider_stop_would_have_helped_count": sum(1 for cf in cfs if str(cf["scenario_name"]).startswith("WIDER_STOP") and safe_int(cf.get("improved_result"))),
                "closer_tp_would_have_helped_count": sum(1 for cf in cfs if str(cf["scenario_name"]).startswith("CLOSER_TP") and safe_int(cf.get("improved_result"))),
                "no_trade_filter_would_have_helped_count": sum(1 for cf in cfs if str(cf["scenario_name"]).startswith("NO_TRADE") and safe_int(cf.get("avoided_loss"))),
                "primary_reason": reason,
                "recommended_rule": _recommended_rule(reason, symbol, strategy, regime),
                "confidence": min(0.95, len(items) / 100),
                "created_at": iso_utc(),
            })
        return clusters


def _matches(row: dict[str, Any], key: tuple[str, str, str, str, str]) -> bool:
    return (
        str(row.get("symbol") or "NA"),
        str(row.get("side") or "NA"),
        str(row.get("strategy_type") or "NA"),
        str(row.get("market_regime") or "NA"),
        str(row.get("score_bucket") or _score_bucket(row)),
    ) == key


def _recommended_rule(reason: str, symbol: str, strategy: str, regime: str) -> str:
    if reason == "CHOPPY_MARKET":
        return f"Bloquear {symbol} {strategy} en {regime} salvo volumen alto y BTC alineado."
    if reason == "LOW_VOLUME_RELATIVE":
        return f"Exigir volume_relative >= 1.0 para {symbol} {strategy}."
    if reason == "BTC_NOT_ALIGNED":
        return f"No operar {symbol} {strategy} si BTC no acompana."
    if reason == "STOP_TOO_TIGHT":
        return f"Investigar stop minimo mayor para {symbol} {strategy}; no aplicar a live sin validacion."
    return f"Observar {symbol} {strategy} en {regime}; evidencia insuficiente para regla fuerte."


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
    clean = [value for value in values if value]
    return sum(clean) / max(len(clean), 1)
