from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .counterfactual_engine import CounterfactualEngine
from .database import Database
from .research_lab import ResearchMetrics, max_drawdown
from .utils import iso_utc, json_dumps, safe_float, safe_int


@dataclass
class VirtualPortfolioResult:
    labels_loaded: int = 0
    virtual_trades_simulated: int = 0
    virtual_trades_created: int = 0
    summaries_updated: int = 0
    skipped_by_concurrency: int = 0
    errors: int = 0
    best_virtual_strategies: list[dict[str, Any]] = field(default_factory=list)
    worst_virtual_strategies: list[dict[str, Any]] = field(default_factory=list)

    def to_text(self) -> str:
        lines = [
            "Virtual portfolio research-only",
            "===============================",
            f"labels loaded: {self.labels_loaded}",
            f"virtual trades simulated: {self.virtual_trades_simulated}",
            f"virtual trades created: {self.virtual_trades_created}",
            f"summaries updated: {self.summaries_updated}",
            f"skipped by virtual concurrency: {self.skipped_by_concurrency}",
            f"errors: {self.errors}",
            "",
            "best virtual strategies",
            *_summary_lines(self.best_virtual_strategies),
            "",
            "worst virtual strategies",
            *_summary_lines(self.worst_virtual_strategies),
            "",
            "final recommendation: NO LIVE",
        ]
        return "\n".join(lines)


class VirtualPortfolioResearch:
    """Simulates research-only virtual positions from labeled signals."""

    def __init__(self, db: Database, logger=None) -> None:
        self.db = db
        self.logger = logger
        self.counterfactuals = CounterfactualEngine(db, logger)

    def simulate(self, *, limit: int = 50000, max_concurrent: int = 1000) -> VirtualPortfolioResult:
        limit = max(0, int(limit or 0))
        max_concurrent = max(1, int(max_concurrent or 1000))
        result = VirtualPortfolioResult()
        try:
            rows = self.db.fetch_phase2_labeled_rows(limit=limit, missing_only=False)
        except Exception as exc:
            self._warn("virtual portfolio no pudo leer labels: %s", exc)
            result.errors += 1
            return result

        result.labels_loaded = len(rows)
        grouped: dict[str, list[dict[str, Any]]] = {}
        active_until_by_variant: dict[str, list[int]] = {}
        for index, row in enumerate(rows):
            variants = self._variants_for_row(row)
            for variant in variants:
                name = variant["variant_name"]
                active = [end for end in active_until_by_variant.get(name, []) if end > index]
                active_until_by_variant[name] = active
                if len(active) >= max_concurrent:
                    result.skipped_by_concurrency += 1
                    continue
                try:
                    trade = self._trade_from_variant(row, variant)
                    result.virtual_trades_simulated += 1
                    active.append(index + max(1, safe_int(trade.get("bars_to_outcome"), 1)))
                    grouped.setdefault(name, []).append(trade)
                    if self.db.record_virtual_research_trade_once(trade):
                        result.virtual_trades_created += 1
                except Exception as exc:
                    self._warn("virtual portfolio fallo en %s: %s", name, exc)
                    result.errors += 1

        summaries = [self._summary(name, trades) for name, trades in grouped.items()]
        summaries.sort(key=lambda item: (safe_float(item.get("profit_factor")), safe_float(item.get("expectancy"))), reverse=True)
        for summary in summaries:
            try:
                self.db.upsert_virtual_strategy_summary(summary)
                result.summaries_updated += 1
            except Exception as exc:
                self._warn("virtual portfolio no pudo guardar summary %s: %s", summary.get("variant_name"), exc)
                result.errors += 1
        result.best_virtual_strategies = summaries[:5]
        result.worst_virtual_strategies = list(reversed(summaries[-5:])) if summaries else []
        return result

    def _variants_for_row(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        variants = [{"variant_name": "NORMAL", "params": {"mode": "normal"}}]
        variants.append({"variant_name": "REVERSE_SIDE", "params": {"reverse": True}})
        rsi = safe_float(row.get("rsi_14"))
        volume = safe_float(row.get("volume_relative"))
        distance_ema21 = abs(safe_float(row.get("distance_to_ema_21")))
        if 45 <= rsi <= 65:
            variants.append({"variant_name": "FILTER_RSI_45_65", "params": {"rsi_min": 45, "rsi_max": 65}})
        if 40 <= rsi <= 70:
            variants.append({"variant_name": "FILTER_RSI_40_70", "params": {"rsi_min": 40, "rsi_max": 70}})
        if volume >= 1.2:
            variants.append({"variant_name": "FILTER_VOLUME_RELATIVE_GE_1_2", "params": {"volume_relative_min": 1.2}})
        if volume >= 1.8:
            variants.append({"variant_name": "FILTER_VOLUME_RELATIVE_GE_1_8", "params": {"volume_relative_min": 1.8}})
        if distance_ema21 <= 0.015:
            variants.append({"variant_name": "FILTER_DISTANCE_EMA21_NEAR", "params": {"max_abs_distance_to_ema_21": 0.015}})
        for key, prefix in (
            ("strategy_type", "FILTER_STRATEGY"),
            ("symbol", "FILTER_SYMBOL"),
            ("market_regime", "FILTER_REGIME"),
        ):
            value = str(row.get(key) or "NA").upper().replace(" ", "_")
            variants.append({"variant_name": f"{prefix}_{value}", "params": {key: value}})
        return variants

    def _trade_from_variant(self, row: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(row)
        normalized["observation_id"] = safe_int(normalized.get("observation_id") or normalized.get("id"))
        normalized["label_id"] = safe_int(normalized.get("label_id"))
        if variant["variant_name"] == "REVERSE_SIDE":
            reverse = [
                item for item in self.counterfactuals.simulate_row(normalized)
                if item.get("scenario_name") == "REVERSE_SIDE"
            ][0]
            side = reverse["simulated_side"]
            stop = reverse["simulated_sl"]
            tp1 = reverse["simulated_tp1"]
            tp2 = reverse["simulated_tp2"]
            label = safe_int(reverse["simulated_label"])
            outcome = reverse["simulated_first_barrier_hit"]
            ret = safe_float(reverse["simulated_return_pct"])
        else:
            side = normalized.get("side")
            stop = normalized.get("stop_loss")
            tp1 = normalized.get("take_profit_1")
            tp2 = normalized.get("take_profit_2")
            label = safe_int(normalized.get("label"))
            outcome = str(normalized.get("first_barrier_hit") or "TIME")
            ret = safe_float(normalized.get("realized_return_pct"))
        return {
            "observation_id": normalized["observation_id"],
            "label_id": normalized["label_id"],
            "variant_name": variant["variant_name"],
            "params_json": json_dumps(variant.get("params", {})),
            "symbol": normalized.get("symbol"),
            "strategy_type": normalized.get("strategy_type"),
            "market_regime": normalized.get("market_regime"),
            "virtual_side": side,
            "entry_price": safe_float(normalized.get("entry_price")),
            "stop_loss": safe_float(stop),
            "take_profit_1": safe_float(tp1),
            "take_profit_2": safe_float(tp2),
            "outcome": outcome,
            "label": label,
            "return_pct": ret,
            "bars_to_outcome": safe_int(normalized.get("bars_to_outcome"), 1),
            "created_at": iso_utc(),
        }

    def _summary(self, variant_name: str, trades: list[dict[str, Any]]) -> dict[str, Any]:
        metric_rows = [
            {
                "label": trade.get("label"),
                "first_barrier_hit": trade.get("outcome"),
                "realized_return_pct": trade.get("return_pct"),
            }
            for trade in trades
        ]
        metrics = ResearchMetrics.calculate(metric_rows)
        decisive = [trade for trade in trades if trade.get("outcome") in {"TP1", "TP2", "SL"}]
        wins = sum(1 for trade in decisive if safe_int(trade.get("label")) == 1)
        first = trades[0] if trades else {}
        pf = safe_float(metrics.get("profit_factor"))
        expectancy = safe_float(metrics.get("expectancy"))
        return {
            "variant_name": variant_name,
            "params_json": first.get("params_json", "{}"),
            "symbol": _summary_dimension(variant_name, "FILTER_SYMBOL_"),
            "strategy_type": _summary_dimension(variant_name, "FILTER_STRATEGY_"),
            "market_regime": _summary_dimension(variant_name, "FILTER_REGIME_"),
            "total_trades": len(trades),
            "tp_count": safe_int(metrics["tp1_count"] + metrics["tp2_count"]),
            "sl_count": safe_int(metrics["sl_count"]),
            "time_count": safe_int(metrics["time_count"]),
            "profit_factor": pf,
            "expectancy": expectancy,
            "decisive_win_rate": wins / max(len(decisive), 1),
            "max_drawdown_estimated": max_drawdown([safe_float(trade.get("return_pct")) for trade in trades]),
            "score": _score(len(trades), pf, expectancy),
            "last_updated": iso_utc(),
        }

    def _warn(self, message: str, *args: Any) -> None:
        if self.logger:
            self.logger.warning(message, *args)


def _summary_dimension(variant_name: str, prefix: str) -> str:
    return variant_name[len(prefix):] if variant_name.startswith(prefix) else "ALL"


def _score(total: int, profit_factor: float, expectancy: float) -> float:
    sample = min(total / 300, 1.0)
    pf_score = min(profit_factor / 2, 1.0)
    exp_score = 1.0 if expectancy > 0 else 0.0
    return max(0.0, sample * 0.4 + pf_score * 0.4 + exp_score * 0.2)


def _summary_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- sin evidencia suficiente"]
    return [
        (
            f"- {row.get('variant_name')}: trades={safe_int(row.get('total_trades'))}, "
            f"PF={safe_float(row.get('profit_factor')):.2f}, "
            f"expectancy={safe_float(row.get('expectancy')):.5f}, "
            f"WR decisivo={safe_float(row.get('decisive_win_rate')):.1%}"
        )
        for row in rows
    ]
