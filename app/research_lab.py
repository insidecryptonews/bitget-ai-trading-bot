from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import PROJECT_ROOT, BotConfig, load_config
from .database import Database
from .logger import setup_logger
from .utils import json_dumps, safe_float, safe_int


MIN_CANDIDATE_LABELS = 100
MIN_CANDIDATE_PROFIT_FACTOR = 1.2
REPORT_FILES = {
    "research_lab_report.md",
    "best_strategies.json",
    "rejected_strategies.json",
    "recommended_config.env",
    "feature_importance.csv",
    "walkforward_summary.csv",
    "reverse_vs_normal.csv",
    "tp_sl_optimizer.csv",
}
OUTCOME_COLUMNS = {
    "label",
    "first_barrier_hit",
    "realized_return_pct",
    "simulated_pnl",
    "holding_bars",
}


@dataclass(frozen=True)
class WalkForwardSplit:
    train: list[dict[str, Any]]
    validation: list[dict[str, Any]]
    test: list[dict[str, Any]]


class ResearchDatasetBuilder:
    """Builds a research-only dataset from stored observations and labels."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def build(self) -> list[dict[str, Any]]:
        observations = self.db.fetch_signal_observations()
        labels_by_observation: dict[int, dict[str, Any]] = {}
        for label in self.db.fetch_signal_labels():
            observation_id = safe_int(label.get("observation_id"))
            if observation_id:
                labels_by_observation[observation_id] = label

        dataset: list[dict[str, Any]] = []
        for observation in observations:
            observation_id = safe_int(observation.get("id"))
            label = labels_by_observation.get(observation_id, {})
            row = self._base_row(observation, label)
            row.update(self._engineer_features(row))
            dataset.append(row)
        return sorted(dataset, key=lambda row: str(row.get("timestamp") or ""))

    def feature_columns(self, dataset: list[dict[str, Any]]) -> list[str]:
        if not dataset:
            return []
        return sorted(
            key
            for key in dataset[0]
            if key not in OUTCOME_COLUMNS
            and key not in {"observation_id", "timestamp"}
        )

    def _base_row(self, observation: dict[str, Any], label: dict[str, Any]) -> dict[str, Any]:
        side = str(observation.get("side") or "").upper()
        strategy_type = str(observation.get("strategy_type") or "NA")
        return {
            "observation_id": safe_int(observation.get("id")),
            "timestamp": observation.get("timestamp"),
            "symbol": observation.get("symbol"),
            "side": side,
            "strategy_type": strategy_type,
            "shadow_strategy": safe_int(observation.get("shadow_strategy")),
            "strategy_variant_id": observation.get("strategy_variant_id"),
            "variant_params_json": observation.get("variant_params_json"),
            "original_side": observation.get("original_side") or side,
            "original_strategy_type": observation.get("original_strategy_type") or strategy_type,
            "score_bucket": observation.get("score_bucket") or score_bucket(safe_float(observation.get("confidence_score"))),
            "label": label.get("label"),
            "first_barrier_hit": label.get("first_barrier_hit"),
            "realized_return_pct": label.get("realized_return_pct"),
            "simulated_pnl": label.get("simulated_pnl"),
            "holding_bars": label.get("bars_to_outcome"),
            "confidence_score": safe_float(observation.get("confidence_score")),
            "entry_price": safe_float(observation.get("entry_price")),
            "stop_loss": safe_float(observation.get("stop_loss")),
            "take_profit_1": safe_float(observation.get("take_profit_1")),
            "take_profit_2": safe_float(observation.get("take_profit_2")),
            "risk_reward_ratio": safe_float(observation.get("risk_reward_ratio")),
            "leverage_recommendation": safe_int(observation.get("leverage_recommendation")),
            "market_regime": observation.get("market_regime"),
            "btc_regime": observation.get("btc_regime"),
            "market_risk_on": safe_int(observation.get("market_risk_on")),
            "market_risk_off": safe_int(observation.get("market_risk_off")),
            "number_of_symbols_bullish": safe_int(observation.get("number_of_symbols_bullish")),
            "number_of_symbols_bearish": safe_int(observation.get("number_of_symbols_bearish")),
            "spread_pct": safe_float(observation.get("spread_pct")),
            "funding_rate": safe_float(observation.get("funding_rate")),
            "open_interest": safe_float(observation.get("open_interest")),
            "volume_24h_usdt": safe_float(observation.get("volume_24h_usdt")),
            "volume_relative": safe_float(observation.get("volume_relative")),
            "rsi_14": safe_float(observation.get("rsi_14")),
            "macd_hist": safe_float(observation.get("macd_hist")),
            "atr_14": safe_float(observation.get("atr_14")),
            "normalized_atr": safe_float(observation.get("normalized_atr")),
            "distance_to_ema_21": safe_float(observation.get("distance_to_ema_21")),
            "distance_to_ema_50": safe_float(observation.get("distance_to_ema_50")),
            "distance_to_ema_200": safe_float(observation.get("distance_to_ema_200")),
            "momentum_5": safe_float(observation.get("momentum_5")),
            "momentum_15": safe_float(observation.get("momentum_15")),
            "btc_momentum_5": safe_float(observation.get("btc_momentum_5")),
            "btc_momentum_15": safe_float(observation.get("btc_momentum_15")),
            "btc_normalized_atr": safe_float(observation.get("btc_normalized_atr")),
            "eth_momentum_5": safe_float(observation.get("eth_momentum_5")),
            "range_width_pct": safe_float(observation.get("range_width_pct")),
            "body_pct": safe_float(observation.get("body_pct")),
            "upper_wick_pct": safe_float(observation.get("upper_wick_pct")),
            "lower_wick_pct": safe_float(observation.get("lower_wick_pct")),
            "bullish_rejection": safe_int(observation.get("bullish_rejection")),
            "bearish_rejection": safe_int(observation.get("bearish_rejection")),
            "kronos_predicted_return_pct": safe_float(observation.get("kronos_predicted_return_pct")),
            "kronos_direction": observation.get("kronos_direction"),
            "kronos_confidence_score": safe_float(observation.get("kronos_confidence_score")),
            "kronos_disagreement": safe_int(observation.get("kronos_disagreement")),
        }

    def _engineer_features(self, row: dict[str, Any]) -> dict[str, Any]:
        entry = safe_float(row.get("entry_price"))
        stop = safe_float(row.get("stop_loss"))
        tp1 = safe_float(row.get("take_profit_1"))
        tp2 = safe_float(row.get("take_profit_2"))
        spread = safe_float(row.get("spread_pct"))
        stop_distance = abs(entry - stop) / entry if entry > 0 and stop > 0 else 0.0
        tp1_distance = abs(tp1 - entry) / entry if entry > 0 and tp1 > 0 else 0.0
        tp2_distance = abs(tp2 - entry) / entry if entry > 0 and tp2 > 0 else 0.0
        side = str(row.get("side") or "").upper()
        btc_momentum = safe_float(row.get("btc_momentum_5")) + safe_float(row.get("btc_momentum_15"))
        eth_momentum = safe_float(row.get("eth_momentum_5"))
        broadly_bullish = safe_int(row.get("number_of_symbols_bullish")) > safe_int(row.get("number_of_symbols_bearish"))
        broadly_bearish = safe_int(row.get("number_of_symbols_bearish")) > safe_int(row.get("number_of_symbols_bullish"))
        timestamp = parse_timestamp(row.get("timestamp"))
        return {
            "stop_distance_pct": stop_distance,
            "tp1_distance_pct": tp1_distance,
            "tp2_distance_pct": tp2_distance,
            "tp1_to_sl_ratio": tp1_distance / stop_distance if stop_distance > 0 else 0.0,
            "tp2_to_sl_ratio": tp2_distance / stop_distance if stop_distance > 0 else 0.0,
            "fee_adjusted_expected_return": max(tp1_distance * safe_float(row.get("risk_reward_ratio")), 0.0) - spread - 0.001,
            "is_high_spread": int(spread >= 0.0015),
            "is_low_volume": int(safe_float(row.get("volume_24h_usdt")) < 20_000_000),
            "is_high_volume_spike": int(safe_float(row.get("volume_relative")) >= 1.8),
            "is_btc_aligned": int((side == "LONG" and btc_momentum >= 0) or (side == "SHORT" and btc_momentum <= 0)),
            "is_eth_aligned": int((side == "LONG" and eth_momentum >= 0) or (side == "SHORT" and eth_momentum <= 0)),
            "is_market_broadly_bullish": int(broadly_bullish),
            "is_market_broadly_bearish": int(broadly_bearish),
            "is_choppy": int(str(row.get("market_regime") or "").upper() == "CHOPPY_MARKET"),
            "trend_strength_score": trend_strength(row),
            "volatility_bucket": numeric_bucket(safe_float(row.get("normalized_atr")), [0.006, 0.012, 0.02, 0.03]),
            "liquidity_bucket": numeric_bucket(safe_float(row.get("volume_24h_usdt")), [20_000_000, 100_000_000, 500_000_000, 1_000_000_000]),
            "rsi_bucket": numeric_bucket(safe_float(row.get("rsi_14")), [30, 45, 60, 72]),
            "score_bucket": row.get("score_bucket") or score_bucket(safe_float(row.get("confidence_score"))),
            "session_bucket": session_bucket(timestamp.hour),
            "weekday": timestamp.weekday(),
            "hour_utc": timestamp.hour,
        }


class ResearchMetrics:
    @staticmethod
    def calculate(rows: list[dict[str, Any]]) -> dict[str, float]:
        labeled = [row for row in rows if row.get("label") is not None]
        returns = [return_pct(row) for row in labeled]
        wins = [value for value in returns if value > 0]
        losses = [value for value in returns if value < 0]
        gains = sum(wins)
        loss_abs = abs(sum(losses))
        labels = [safe_int(row.get("label")) for row in labeled]
        total = len(labeled)
        tp1 = sum(1 for row in labeled if row.get("first_barrier_hit") == "TP1")
        tp2 = sum(1 for row in labeled if row.get("first_barrier_hit") == "TP2")
        sl = sum(1 for row in labeled if row.get("first_barrier_hit") == "SL")
        time_count = sum(1 for row in labeled if row.get("first_barrier_hit") == "TIME")
        avg_return = mean(returns)
        median_return = statistics.median(returns) if returns else 0.0
        std_return = statistics.pstdev(returns) if len(returns) > 1 else 0.0
        max_dd = max_drawdown(returns)
        consecutive_losses = max_consecutive_losses(returns)
        return {
            "total_labels": float(total),
            "tp1_count": float(tp1),
            "tp2_count": float(tp2),
            "sl_count": float(sl),
            "time_count": float(time_count),
            "win_rate": sum(1 for label in labels if label == 1) / max(total, 1),
            "loss_rate": sum(1 for label in labels if label == -1) / max(total, 1),
            "time_ratio": time_count / max(total, 1),
            "sl_ratio": sl / max(total, 1),
            "tp1_ratio": tp1 / max(total, 1),
            "tp2_ratio": tp2 / max(total, 1),
            "profit_factor": profit_factor_from_returns(returns),
            "expectancy": avg_return,
            "avg_return": avg_return,
            "median_return": median_return,
            "std_return": std_return,
            "sharpe_like_score": avg_return / std_return if std_return > 0 else 0.0,
            "max_drawdown_estimated": max_dd,
            "consecutive_losses_estimated": float(consecutive_losses),
            "average_holding_bars": mean([safe_float(row.get("holding_bars")) for row in labeled]),
            "fee_adjusted_expectancy": mean([return_pct(row) - safe_float(row.get("spread_pct")) - 0.001 for row in labeled]),
            "robustness_score": robustness_score(total, profit_factor_from_returns(returns), avg_return, max_dd),
            "walk_forward_score": 0.0,
            "overfitting_risk_score": 1.0,
        }


class ResearchRanker:
    def __init__(self, min_labels: int = MIN_CANDIDATE_LABELS, min_profit_factor: float = MIN_CANDIDATE_PROFIT_FACTOR) -> None:
        self.min_labels = min_labels
        self.min_profit_factor = min_profit_factor

    def rank(self, dataset: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        hypotheses = self._hypotheses(dataset)
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for item in hypotheses:
            metrics = ResearchMetrics.calculate(item["rows"])
            walk = evaluate_basic_walkforward(item["rows"])
            metrics["walk_forward_score"] = walk["walk_forward_score"]
            metrics["overfitting_risk_score"] = walk["overfitting_risk_score"]
            status = self._status(metrics)
            record = {
                "name": item["name"],
                "kind": item["kind"],
                "filters": item["filters"],
                "status": status,
                "metrics": metrics,
                "walkforward": walk,
            }
            if status.startswith("CANDIDATE") or status == "WATCHLIST_WEAK_EDGE":
                accepted.append(record)
            else:
                rejected.append(record)
        accepted.sort(key=lambda row: (row["metrics"]["profit_factor"], row["metrics"]["expectancy"]), reverse=True)
        rejected.sort(key=lambda row: (row["metrics"]["total_labels"], row["metrics"]["profit_factor"]), reverse=True)
        return accepted, rejected

    def _status(self, metrics: dict[str, float]) -> str:
        if metrics["total_labels"] < self.min_labels:
            return "REJECTED_TOO_FEW_SAMPLES"
        if metrics["profit_factor"] < self.min_profit_factor or metrics["expectancy"] <= 0:
            return "REJECTED_NO_EDGE"
        if metrics["walk_forward_score"] < 0.5:
            return "REJECTED_OVERFITTING_RISK"
        if metrics["profit_factor"] < 1.5 or metrics["win_rate"] < 0.55:
            return "WATCHLIST_WEAK_EDGE"
        return "CANDIDATE_PAPER_ONLY"

    def _hypotheses(self, dataset: list[dict[str, Any]]) -> list[dict[str, Any]]:
        labeled = [row for row in dataset if row.get("label") is not None]
        groups: list[dict[str, Any]] = [
            {"name": "all_labeled", "kind": "global", "filters": {}, "rows": labeled},
        ]
        for key in ("strategy_type", "symbol", "market_regime", "side", "score_bucket", "rsi_bucket", "volatility_bucket"):
            for value, rows in group_by(labeled, key).items():
                groups.append({"name": f"{key}={value}", "kind": key, "filters": {key: value}, "rows": rows})
        normal = [row for row in labeled if safe_int(row.get("shadow_strategy")) == 0]
        reverse = [row for row in labeled if is_reverse(row)]
        groups.append({"name": "normal_only", "kind": "direction", "filters": {"shadow_strategy": 0}, "rows": normal})
        groups.append({"name": "reverse_only", "kind": "direction", "filters": {"reverse": True}, "rows": reverse})
        return groups


class ResearchLab:
    def __init__(self, db: Database, config: BotConfig, logger=None, reports_dir: Path | None = None) -> None:
        self.db = db
        self.config = config
        self.logger = logger
        self.reports_dir = reports_dir or PROJECT_ROOT / "reports"
        self.builder = ResearchDatasetBuilder(db)
        self.ranker = ResearchRanker()

    def discover(self) -> dict[str, Any]:
        dataset = self.builder.build()
        accepted, rejected = self.ranker.rank(dataset)
        report = self.build_markdown_report(dataset, accepted, rejected)
        self.write_reports(dataset, accepted, rejected, report)
        best = accepted[0] if accepted else None
        return {
            "dataset_rows": len(dataset),
            "labels": len([row for row in dataset if row.get("label") is not None]),
            "shadow_labels": len([row for row in dataset if row.get("label") is not None and safe_int(row.get("shadow_strategy")) == 1]),
            "best_candidate": best,
            "accepted": accepted,
            "rejected": rejected,
            "reports_dir": str(self.reports_dir),
            "live_recommendation": "DO NOT ACTIVATE LIVE",
        }

    def export(self, export_dir: Path | None = None) -> Path:
        target = export_dir or self.reports_dir
        dataset = self.builder.build()
        accepted, rejected = self.ranker.rank(dataset)
        report = self.build_markdown_report(dataset, accepted, rejected)
        self.write_reports(dataset, accepted, rejected, report, target)
        return target

    def recommend_config(self, target_dir: Path | None = None) -> Path:
        target = target_dir or self.reports_dir
        target.mkdir(parents=True, exist_ok=True)
        dataset = self.builder.build()
        accepted, _ = self.ranker.rank(dataset)
        path = target / "recommended_config.env"
        path.write_text(self._recommended_config_text(dataset, accepted), encoding="utf-8")
        return path

    def explain_report(self) -> str:
        from .explainability_engine import ExplainabilityEngine

        report = ExplainabilityEngine(self.db, self.logger).report()
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        (self.reports_dir / "explainability_report.md").write_text(report + "\n", encoding="utf-8")
        return report

    def sl_report(self) -> str:
        from .stop_loss_analyzer import StopLossAnalyzer

        analyzer = StopLossAnalyzer(self.db, self.logger)
        result = analyzer.generate()
        report = analyzer.report()
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        (self.reports_dir / "sl_report.md").write_text(report + "\n", encoding="utf-8")
        write_csv(self.reports_dir / "failure_clusters.csv", result.get("clusters", []))
        write_csv(self.reports_dir / "stop_loss_analysis.csv", [
            {"reason": reason, "count": count}
            for reason, count in result.get("reason_counts", {}).items()
        ])
        return report

    def win_report(self) -> str:
        from .win_analyzer import WinAnalyzer

        analyzer = WinAnalyzer(self.db, self.logger)
        clusters = analyzer.generate()
        report = analyzer.report()
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        (self.reports_dir / "win_report.md").write_text(report + "\n", encoding="utf-8")
        write_csv(self.reports_dir / "win_clusters.csv", clusters)
        return report

    def counterfactuals_report(self) -> str:
        from .counterfactual_engine import CounterfactualEngine

        engine = CounterfactualEngine(self.db, self.logger)
        results = engine.generate()
        report = engine.summary(results)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        (self.reports_dir / "counterfactual_summary.md").write_text(report + "\n", encoding="utf-8")
        write_csv(self.reports_dir / "counterfactual_results.csv", results)
        return report

    def feature_importance_report(self) -> str:
        from .feature_attribution import FeatureAttribution

        report = FeatureAttribution(self.db, self.logger).report()
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        (self.reports_dir / "feature_importance.md").write_text(report + "\n", encoding="utf-8")
        return report

    def recommend_rules_report(self) -> str:
        from .rule_miner import RuleMiner

        return RuleMiner(self.db, self.logger).report()

    def full_report(self) -> str:
        from .full_research_report import FullResearchReporter

        report = FullResearchReporter(self.db, self.config, self.logger, reports_dir=self.reports_dir).build_report()
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        (self.reports_dir / "full_research_lab_report.md").write_text(report + "\n", encoding="utf-8")
        return report

    def kronos_once(self, limit: int = 100) -> str:
        from .kronos_research import KronosResearch

        return KronosResearch(self.config, self.db, self.logger).run_once(limit=limit).to_text()

    def kronos_evaluate(self) -> str:
        from .kronos_research import KronosEvaluator

        return KronosEvaluator(self.db).report()

    def reconcile_paper(self) -> str:
        from .paper_reconciler import PaperReconciler

        return PaperReconciler(self.config, self.db, self.logger).reconcile().to_text()

    def strategy_lab(self, limit: int = 20000, safe_mode: bool = True) -> str:
        from .strategy_lab import StrategyLab

        return StrategyLab(self.db, self.logger).run(limit=limit, safe_mode=safe_mode).to_text()

    def daily_summary(self, hours: int = 24) -> str:
        from .daily_summary import DailyResearchSummary

        return DailyResearchSummary(self.config, self.db, self.logger).build(hours=hours)

    def training_summary(self, hours: int = 6) -> str:
        from .training_summary import TrainingSummary

        return TrainingSummary(self.config, self.db).build(hours=hours)

    def acceleration_plan(self, hours: int = 24) -> str:
        from .training_summary import TrainingSummary

        return TrainingSummary(self.config, self.db).acceleration_plan(hours=hours)

    def shadow_opportunity(self, hours: int = 24) -> str:
        from .shadow_opportunity_lab import ShadowOpportunityLab

        return ShadowOpportunityLab(self.config, self.db).to_text(hours=hours)

    def edge_guard(self, hours: int = 24) -> str:
        from .edge_guard import EdgeGuard

        return EdgeGuard(self.config, self.db).to_text(hours=hours)

    def tp_sl_lab(self, hours: int = 24) -> str:
        from .tp_sl_horizon_lab import TpSlHorizonLab

        return TpSlHorizonLab(self.config, self.db).to_text(hours=hours)

    def exit_simulation(self, hours: int = 24) -> str:
        from .exit_simulation_lab import ExitSimulationLab

        return ExitSimulationLab(self.config, self.db).to_text(hours=hours)

    def score_calibration(self, hours: int = 24) -> str:
        from .score_calibration_lab import ScoreCalibrationLab

        return ScoreCalibrationLab(self.config, self.db).to_text(hours=hours)

    def shadow_experiments(self, hours: int = 24) -> str:
        from .shadow_experiments import ShadowExperimentsLab

        return ShadowExperimentsLab(self.config, self.db).to_text(hours=hours)

    def evolution_score(self, hours: int = 24) -> str:
        from .evolution_score import EvolutionScore

        return EvolutionScore(self.config, self.db).to_text(hours=hours)

    def mfe_mae_diagnostic(self, hours: int = 24) -> str:
        from .mfe_mae_diagnostic import MfeMaeDiagnostic

        return MfeMaeDiagnostic(self.config, self.db).to_text(hours=hours)

    def mfe_mae_smoke_test(self) -> str:
        from .mfe_mae_smoke_test import MfeMaeSmokeTest

        return MfeMaeSmokeTest(self.config, self.db, self.logger).to_text()

    def catalyst_add(
        self,
        *,
        catalyst_id: str,
        title: str,
        symbols: str,
        category: str,
        direction: str,
        severity: str,
        confidence: float,
        hours_back: int,
        hours_forward: int,
    ) -> str:
        from .catalyst_registry import CatalystRegistry

        saved = CatalystRegistry(self.config, self.db).add_manual(
            catalyst_id=catalyst_id,
            title=title,
            symbols=[item.strip() for item in symbols.split(",") if item.strip()],
            category=category,
            direction=direction,
            severity=severity,
            confidence=confidence,
            hours_back=hours_back,
            hours_forward=hours_forward,
        )
        return f"CATALYST ADD OK\nid={saved}\ncatalyst_id={catalyst_id}\nfinal_recommendation: NO LIVE"

    def catalyst_list(self, hours: int = 72) -> str:
        from .catalyst_registry import CatalystRegistry

        rows = CatalystRegistry(self.config, self.db).list(hours=hours)
        lines = ["CATALYST LIST START", f"hours: {hours}"]
        lines.extend(
            f"- catalyst_id={row.get('catalyst_id')} category={row.get('category')} direction={row.get('direction')} symbols={row.get('symbols')}"
            for row in rows[:50]
        )
        if not rows:
            lines.append("- none")
        lines.extend(["final_recommendation: NO LIVE", "CATALYST LIST END"])
        return "\n".join(lines)

    def catalyst_summary(self, hours: int = 24) -> str:
        from .catalyst_registry import CatalystRegistry

        return CatalystRegistry(self.config, self.db).to_summary_text(hours=hours)

    def catalyst_ingest(self, hours: int = 48) -> str:
        from .news_catalyst_ingestor import NewsCatalystIngestor

        return NewsCatalystIngestor(self.config, self.db, self.logger).run(hours=hours).to_text()

    def news_risk_gate(self, hours: int = 24) -> str:
        from .news_risk_gate import NewsRiskGate

        return NewsRiskGate(self.config, self.db).to_text(hours=hours)

    def paper_policy_lab(self, hours: int = 24) -> str:
        from .paper_policy_lab import PaperPolicyLab

        return PaperPolicyLab(self.config, self.db).to_text(hours=hours)

    def paper_policy_orchestrator(self, hours: int = 24) -> str:
        from .paper_policy_orchestrator import PaperPolicyOrchestrator

        return PaperPolicyOrchestrator(self.config, self.db).to_text(hours=hours)

    def walk_forward(self, hours: int = 24) -> str:
        from .walk_forward_validation import WalkForwardValidation

        return WalkForwardValidation(self.config, self.db).to_text(hours=hours)

    def policy_backtest(self, hours: int = 24) -> str:
        from .policy_backtest import PolicyBacktest

        return PolicyBacktest(self.config, self.db).to_text(hours=hours)

    def exit_policy_backtest(self, hours: int = 24) -> str:
        from .exit_policy_backtest import ExitPolicyBacktest

        return ExitPolicyBacktest(self.config, self.db).to_text(hours=hours)

    def net_edge_lab(self, hours: int = 24) -> str:
        from .net_edge_lab import NetEdgeLab

        return NetEdgeLab(self.config, self.db).to_text(hours=hours)

    def anti_overfit_gate(self, hours: int = 24) -> str:
        from .anti_overfit_gate import AntiOverfitGate

        return AntiOverfitGate(self.config, self.db).to_text(hours=hours)

    def ev_slippage_calibration_gate(self, hours: int = 24) -> str:
        from .ev_slippage_calibration_gate import EvSlippageCalibrationGate

        return EvSlippageCalibrationGate(self.config, self.db).to_text(hours=hours)

    def policy_stability_matrix(self, hours: int = 24) -> str:
        from .policy_stability_matrix import PolicyStabilityMatrix

        return PolicyStabilityMatrix(self.config, self.db).to_text(hours=hours)

    def candidate_ranking(self, hours: int = 24) -> str:
        from .candidate_ranking import CandidateRanking

        return CandidateRanking(self.config, self.db).to_text(hours=hours)

    def decision_ledger_audit(self, hours: int = 24) -> str:
        from .decision_ledger_audit import DecisionLedgerAudit

        return DecisionLedgerAudit(self.config, self.db).to_text(hours=hours)

    def adaptive_exit_backtest(self, hours: int = 24) -> str:
        from .adaptive_exit_backtest import AdaptiveExitBacktest

        return AdaptiveExitBacktest(self.config, self.db).to_text(hours=hours)

    def sizing_safety_lab(self, hours: int = 24) -> str:
        from .sizing_safety_lab import SizingSafetyLab

        return SizingSafetyLab(self.config, self.db).to_text(hours=hours)

    def structured_output_guard_smoke_test(self) -> str:
        from .structured_output_guard import smoke_test_text

        return smoke_test_text()

    def vps_runtime_health(self) -> str:
        from .vps_runtime_health import VpsRuntimeHealth

        return VpsRuntimeHealth(self.config, self.db, self.logger).to_text()

    def post_migration_backup(self, hours: int = 168) -> str:
        from .post_migration_backup import PostMigrationBackup

        return PostMigrationBackup(self.config, self.db, self.logger).to_text(hours=hours)

    def data_restore_benchmark(self) -> str:
        from .data_restore_benchmark import DataRestoreBenchmark

        return DataRestoreBenchmark(self.config, self.db, self.logger).to_text(dry_run=True)

    def fast_runtime_readiness(self, hours: int = 24) -> str:
        from .fast_runtime_readiness import FastRuntimeReadiness

        return FastRuntimeReadiness(self.config, self.db).to_text(hours=hours)

    def websocket_migration_plan(self, hours: int = 24) -> str:
        from .websocket_migration_plan import WebsocketMigrationPlan

        return WebsocketMigrationPlan(self.config, self.db).to_text(hours=hours)

    def fast_runtime_smoke_test(self) -> str:
        from .fast_runtime_smoke_test import FastRuntimeSmokeTest

        return FastRuntimeSmokeTest(self.config, self.db, self.logger).to_text()

    def edge_hardening_smoke_test(self) -> str:
        from .edge_hardening_smoke_test import EdgeHardeningSmokeTest

        return EdgeHardeningSmokeTest(self.config, self.db, self.logger).to_text()

    def high_value_patterns_smoke_test(self) -> str:
        from .high_value_patterns_smoke_test import HighValuePatternsSmokeTest

        return HighValuePatternsSmokeTest(self.config, self.db, self.logger).to_text()

    def policy_news_smoke_test(self) -> str:
        from .policy_news_smoke_test import PolicyNewsSmokeTest

        return PolicyNewsSmokeTest(self.config, self.db, self.logger).to_text()

    def time_death_lab(self, hours: int = 24) -> str:
        from .time_death_lab import TimeDeathLab

        return TimeDeathLab(self.config, self.db).to_text(hours=hours)

    def time_death_autopsy(self, hours: int = 24) -> str:
        from .time_death_autopsy import TimeDeathAutopsyLab

        return TimeDeathAutopsyLab(self.config, self.db).to_text(hours=hours)

    def time_death_filter_proposal(self, hours: int = 24) -> str:
        from .time_death_filter_proposal import TimeDeathFilterProposal

        return TimeDeathFilterProposal(self.config, self.db).to_text(hours=hours)

    def exit_cause_backtest(self, hours: int = 24) -> str:
        from .exit_cause_backtest import ExitCauseBacktest

        return ExitCauseBacktest(self.config, self.db).to_text(hours=hours)

    def time_death_smoke_test(self) -> str:
        from .time_death_smoke_test import TimeDeathSmokeTest

        return TimeDeathSmokeTest(self.config, self.db, self.logger).to_text()

    def adaptive_exit_policy(self, hours: int = 24) -> str:
        from .adaptive_exit_policy_lab import AdaptiveExitPolicyLab

        return AdaptiveExitPolicyLab(self.config, self.db).to_text(hours=hours)

    def latency_audit(self, hours: int = 24) -> str:
        from .latency_audit import LatencyAudit

        return LatencyAudit(self.config, self.db).to_text(hours=hours)

    def fast_execution_readiness(self) -> str:
        from .fast_execution_readiness import FastExecutionReadiness

        return FastExecutionReadiness(self.config, self.db).to_text()

    def data_vault_status(self) -> str:
        from .data_vault import DataVault

        return DataVault(self.config, self.db, self.logger).status_text()

    def data_export(self, hours: int = 168, upload: bool = False) -> str:
        from .data_vault import DataVault

        return DataVault(self.config, self.db, self.logger).export_text(hours=hours, upload=upload)

    def data_import(self, file: str, apply: bool = False) -> str:
        from .data_vault import DataVault

        return DataVault(self.config, self.db, self.logger).import_text(file=file, apply=apply)

    def migration_readiness(self) -> str:
        from .data_vault import DataVault

        return DataVault(self.config, self.db, self.logger).migration_readiness_text()

    def migration_readiness_deep_check(self) -> str:
        from .data_vault import DataVault

        return DataVault(self.config, self.db, self.logger).migration_readiness_deep_check_text()

    def data_upload_latest(self) -> str:
        from .data_vault import DataVault

        return DataVault(self.config, self.db, self.logger).upload_latest_text()

    def data_download_latest(self) -> str:
        from .data_vault import DataVault

        return DataVault(self.config, self.db, self.logger).download_latest_text()

    def data_restore_latest(self, apply: bool = False) -> str:
        from .data_vault import DataVault

        return DataVault(self.config, self.db, self.logger).restore_latest_text(apply=apply)

    def data_vault_prune(self, apply: bool = False) -> str:
        from .data_vault import DataVault

        return DataVault(self.config, self.db, self.logger).prune_text(apply=apply)

    def data_vault_smoke_test(self) -> str:
        from .data_vault_smoke_test import DataVaultSmokeTest

        return DataVaultSmokeTest(self.config, self.db, self.logger).to_text()

    def exit_latency_vault_smoke_test(self) -> str:
        from .exit_latency_vault_smoke_test import ExitLatencyVaultSmokeTest

        return ExitLatencyVaultSmokeTest(self.config, self.db, self.logger).to_text()

    def phase_readiness_smoke_test(self) -> str:
        from .phase_readiness_smoke_test import PhaseReadinessSmokeTest

        return PhaseReadinessSmokeTest(self.config, self.db, self.logger).to_text()

    def vps_migration_guide(self) -> str:
        from .vps_migration import build_vps_migration_guide

        return build_vps_migration_guide(self.config)

    def vps_preflight(self) -> str:
        from .vps_migration import VpsPreflight

        return VpsPreflight(self.config, self.db, self.logger).to_text()

    def fast_runtime_plan(self, hours: int = 24) -> str:
        from .fast_runtime_plan import FastRuntimePlan

        return FastRuntimePlan(self.config, self.db).to_text(hours=hours)

    def vps_migration_smoke_test(self) -> str:
        from .vps_migration_smoke_test import VpsMigrationSmokeTest

        return VpsMigrationSmokeTest(self.config, self.db, self.logger).to_text()

    def build_markdown_report(
        self,
        dataset: list[dict[str, Any]] | None = None,
        accepted: list[dict[str, Any]] | None = None,
        rejected: list[dict[str, Any]] | None = None,
    ) -> str:
        dataset = dataset if dataset is not None else self.builder.build()
        if accepted is None or rejected is None:
            accepted, rejected = self.ranker.rank(dataset)
        labeled = [row for row in dataset if row.get("label") is not None]
        overall = ResearchMetrics.calculate(labeled)
        best = accepted[0] if accepted else None
        worst = worst_strategy(dataset)
        reverse_rows = [row for row in labeled if is_reverse(row)]
        normal_rows = [row for row in labeled if safe_int(row.get("shadow_strategy")) == 0]

        lines = [
            "# Research Lab Report",
            "",
            "## Resumen ejecutivo",
            "",
            "- Recomendacion live: **NO ACTIVAR LIVE**. Esta fase es research-only.",
            f"- Dataset: {len(dataset)} observaciones, {len(labeled)} labels, {len(reverse_rows)} reverse/shadow labels.",
            f"- Profit factor global: {overall['profit_factor']:.2f}. Expectancy: {overall['expectancy']:.5f}.",
            f"- Mejor candidato: {best['name'] if best else 'sin candidato con evidencia suficiente'}.",
            f"- Peor estrategia: {worst or 'sin evidencia suficiente'}.",
            "",
            "## Estado del dataset",
            "",
            f"- Observaciones totales: {len(dataset)}",
            f"- Labels totales: {len(labeled)}",
            f"- Labels TIME: {safe_int(overall['time_count'])}",
            f"- Labels SL: {safe_int(overall['sl_count'])}",
            f"- Labels TP1: {safe_int(overall['tp1_count'])}",
            f"- Labels TP2: {safe_int(overall['tp2_count'])}",
            "",
            "## Normal vs reverse",
            "",
            f"- Normal: labels={len(normal_rows)}, PF={ResearchMetrics.calculate(normal_rows)['profit_factor']:.2f}",
            f"- Reverse: labels={len(reverse_rows)}, PF={ResearchMetrics.calculate(reverse_rows)['profit_factor']:.2f}",
            "",
            "## Estrategias candidatas",
            "",
        ]
        if not accepted:
            lines.append("- Sin estrategias candidatas. Evidencia insuficiente o edge negativo.")
        else:
            for item in accepted[:10]:
                metrics = item["metrics"]
                lines.append(
                    f"- {item['name']}: {item['status']}, labels={metrics['total_labels']:.0f}, "
                    f"PF={metrics['profit_factor']:.2f}, WR={metrics['win_rate']:.1%}, "
                    f"WF={metrics['walk_forward_score']:.2f}"
                )
        lines.extend([
            "",
            "## Rechazos principales",
            "",
        ])
        for item in rejected[:10]:
            metrics = item["metrics"]
            lines.append(
                f"- {item['name']}: {item['status']}, labels={metrics['total_labels']:.0f}, "
                f"PF={metrics['profit_factor']:.2f}, expectancy={metrics['expectancy']:.5f}"
            )
        lines.extend([
            "",
            "## Simbolos y regimenes",
            "",
            *markdown_group_lines("Simbolos", dataset, "symbol"),
            "",
            *markdown_group_lines("Regimenes", dataset, "market_regime"),
            "",
            "## TP/SL recomendado",
            "",
            "- No implementado en fase 1. El optimizador TP/SL avanzado queda bloqueado hasta que esta fase pase tests.",
            "",
            "## Configuracion recomendada",
            "",
            "- Ver `recommended_config.env`. Nunca activa `LIVE_TRADING=true` ni `DRY_RUN=false`.",
            "",
            "## Riesgos y limitaciones",
            "",
            "- El dataset puede estar sesgado por periodos de mercado concretos.",
            "- Las labels TIME excesivas debilitan cualquier conclusion.",
            "- Walk-forward basico solo valida estabilidad temporal inicial; no sustituye paper prolongado.",
            "",
            "## Proxima accion sugerida",
            "",
            "- Mantener Railway en PAPER + research hasta tener PF > 1.2 estable y al menos 100 labels por hipotesis.",
        ])
        return "\n".join(lines) + "\n"

    def write_reports(
        self,
        dataset: list[dict[str, Any]],
        accepted: list[dict[str, Any]],
        rejected: list[dict[str, Any]],
        markdown: str,
        target_dir: Path | None = None,
    ) -> Path:
        target = target_dir or self.reports_dir
        target.mkdir(parents=True, exist_ok=True)
        (target / "research_lab_report.md").write_text(markdown, encoding="utf-8")
        (target / "best_strategies.json").write_text(json_dumps(accepted), encoding="utf-8")
        (target / "rejected_strategies.json").write_text(json_dumps(rejected), encoding="utf-8")
        (target / "recommended_config.env").write_text(self._recommended_config_text(dataset, accepted), encoding="utf-8")
        write_csv(target / "walkforward_summary.csv", walkforward_summary_rows(accepted + rejected))
        write_csv(target / "reverse_vs_normal.csv", reverse_vs_normal_rows(dataset))
        write_csv(target / "feature_importance.csv", feature_importance_rows(dataset))
        write_csv(
            target / "tp_sl_optimizer.csv",
            [{"status": "deferred_phase_1", "reason": "TP/SL optimizer avanzado no implementado hasta que fase 1 pase tests"}],
        )
        return target

    def _recommended_config_text(self, dataset: list[dict[str, Any]], accepted: list[dict[str, Any]]) -> str:
        best = accepted[0] if accepted else None
        symbols = recommended_symbols(dataset, best)
        score = recommended_score(best)
        lines = [
            "# Generated by app.research_lab",
            "# Research-only recommendation. Revisar manualmente antes de tocar produccion.",
            "# NO_LIVE_RECOMMENDED=true",
            "LIVE_TRADING=false",
            "DRY_RUN=true",
            "PAPER_TRADING=true",
            "ENABLE_META_MODEL=true",
            "META_MODEL_MODE=observe_only",
            f"MIN_SCORE_TO_TRADE={score}",
            f"SYMBOLS={','.join(symbols)}",
            f"MAX_HOLDING_BARS={self.config.max_holding_bars}",
            f"MIN_RISK_REWARD={self.config.min_risk_reward}",
        ]
        if best:
            lines.append(f"# BEST_RESEARCH_CANDIDATE={best['name']}")
            lines.append(f"# BEST_RESEARCH_STATUS={best['status']}")
        else:
            lines.append("# BEST_RESEARCH_CANDIDATE=none")
        return "\n".join(lines) + "\n"


def make_basic_walkforward_splits(rows: list[dict[str, Any]], max_blocks: int = 6) -> list[WalkForwardSplit]:
    ordered = sorted([row for row in rows if row.get("label") is not None], key=lambda row: str(row.get("timestamp") or ""))
    if len(ordered) < 3:
        return []
    block_count = min(max_blocks, max(3, int(math.sqrt(len(ordered)))))
    block_size = max(1, len(ordered) // block_count)
    blocks = [ordered[index:index + block_size] for index in range(0, len(ordered), block_size)]
    blocks = [block for block in blocks if block]
    if len(blocks) < 3:
        return []
    splits: list[WalkForwardSplit] = []
    for index in range(0, len(blocks) - 2):
        splits.append(WalkForwardSplit(train=blocks[index], validation=blocks[index + 1], test=blocks[index + 2]))
    return splits


def evaluate_basic_walkforward(rows: list[dict[str, Any]]) -> dict[str, Any]:
    splits = make_basic_walkforward_splits(rows)
    if not splits:
        return {
            "windows": 0,
            "walk_forward_score": 0.0,
            "overfitting_risk_score": 1.0,
            "stability_score": 0.0,
            "test_profit_factors": [],
            "reason": "historico insuficiente",
        }
    test_metrics = [ResearchMetrics.calculate(split.test) for split in splits]
    pfs = [metrics["profit_factor"] for metrics in test_metrics]
    expectancies = [metrics["expectancy"] for metrics in test_metrics]
    positive_windows = sum(1 for metrics in test_metrics if metrics["profit_factor"] >= MIN_CANDIDATE_PROFIT_FACTOR and metrics["expectancy"] > 0)
    stability = positive_windows / max(len(test_metrics), 1)
    dispersion = statistics.pstdev(pfs) if len(pfs) > 1 else 0.0
    overfitting = min(1.0, max(0.0, (1.0 - stability) + min(dispersion / 5.0, 0.5)))
    return {
        "windows": len(splits),
        "walk_forward_score": stability,
        "overfitting_risk_score": overfitting,
        "stability_score": stability,
        "test_profit_factors": pfs,
        "test_expectancies": expectancies,
        "reason": "estable" if stability >= 0.5 else "inestable o sin edge fuera de muestra",
    }


def walkforward_summary_rows(strategies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for strategy in strategies:
        walk = strategy.get("walkforward") or {}
        rows.append({
            "name": strategy.get("name"),
            "status": strategy.get("status"),
            "windows": walk.get("windows", 0),
            "walk_forward_score": walk.get("walk_forward_score", 0.0),
            "overfitting_risk_score": walk.get("overfitting_risk_score", 1.0),
            "reason": walk.get("reason", ""),
            "test_profit_factors": json.dumps(walk.get("test_profit_factors", [])),
        })
    return rows


def reverse_vs_normal_rows(dataset: list[dict[str, Any]]) -> list[dict[str, Any]]:
    labeled = [row for row in dataset if row.get("label") is not None]
    if not labeled:
        return [{
            "status": "no_labeled_rows",
            "symbol": "",
            "market_regime": "",
            "score_bucket": "",
            "strategy": "",
            "normal_labels": 0,
            "normal_profit_factor": 0.0,
            "reverse_labels": 0,
            "reverse_profit_factor": 0.0,
            "evidence": "insufficient",
        }]
    keys = sorted({
        (
            str(row.get("symbol") or "NA"),
            str(row.get("market_regime") or "NA"),
            str(row.get("score_bucket") or "NA"),
            str(row.get("original_strategy_type") or row.get("strategy_type") or "NA"),
        )
        for row in labeled
    })
    rows: list[dict[str, Any]] = []
    for symbol, regime, score, strategy in keys:
        bucket = [
            row for row in labeled
            if str(row.get("symbol") or "NA") == symbol
            and str(row.get("market_regime") or "NA") == regime
            and str(row.get("score_bucket") or "NA") == score
            and str(row.get("original_strategy_type") or row.get("strategy_type") or "NA") == strategy
        ]
        normal = [row for row in bucket if safe_int(row.get("shadow_strategy")) == 0]
        reverse = [row for row in bucket if is_reverse(row)]
        rows.append({
            "symbol": symbol,
            "market_regime": regime,
            "score_bucket": score,
            "strategy": strategy,
            "normal_labels": len(normal),
            "normal_profit_factor": ResearchMetrics.calculate(normal)["profit_factor"],
            "reverse_labels": len(reverse),
            "reverse_profit_factor": ResearchMetrics.calculate(reverse)["profit_factor"],
            "evidence": "sufficient" if len(reverse) >= MIN_CANDIDATE_LABELS else "insufficient",
        })
    return rows


def feature_importance_rows(dataset: list[dict[str, Any]]) -> list[dict[str, Any]]:
    labeled = [row for row in dataset if row.get("label") is not None]
    wins = [row for row in labeled if safe_int(row.get("label")) == 1]
    losses = [row for row in labeled if safe_int(row.get("label")) == -1]
    numeric_features = [
        "confidence_score",
        "spread_pct",
        "volume_relative",
        "rsi_14",
        "normalized_atr",
        "momentum_5",
        "momentum_15",
        "stop_distance_pct",
        "tp1_to_sl_ratio",
    ]
    rows: list[dict[str, Any]] = []
    for feature in numeric_features:
        win_mean = mean([safe_float(row.get(feature)) for row in wins])
        loss_mean = mean([safe_float(row.get(feature)) for row in losses])
        rows.append({
            "feature": feature,
            "win_mean": win_mean,
            "loss_mean": loss_mean,
            "mean_difference": win_mean - loss_mean,
            "method": "simple_win_loss_mean_delta_phase_1",
        })
    return rows


def markdown_group_lines(title: str, dataset: list[dict[str, Any]], key: str) -> list[str]:
    labeled = [row for row in dataset if row.get("label") is not None]
    groups = sorted(group_by(labeled, key).items(), key=lambda item: ResearchMetrics.calculate(item[1])["profit_factor"], reverse=True)
    lines = [f"### {title}"]
    if not groups:
        lines.append("- Sin labels suficientes.")
        return lines
    for value, rows in groups[:8]:
        metrics = ResearchMetrics.calculate(rows)
        lines.append(
            f"- {value}: labels={metrics['total_labels']:.0f}, PF={metrics['profit_factor']:.2f}, "
            f"WR={metrics['win_rate']:.1%}, expectancy={metrics['expectancy']:.5f}"
        )
    return lines


def worst_strategy(dataset: list[dict[str, Any]]) -> str:
    strategies = group_by([row for row in dataset if row.get("label") is not None], "strategy_type")
    enough = [(name, ResearchMetrics.calculate(rows)) for name, rows in strategies.items() if len(rows) >= 20]
    if not enough:
        return ""
    enough.sort(key=lambda item: (item[1]["profit_factor"], item[1]["expectancy"]))
    return str(enough[0][0])


def recommended_symbols(dataset: list[dict[str, Any]], best: dict[str, Any] | None) -> list[str]:
    if not best:
        return sorted({str(row.get("symbol")) for row in dataset if row.get("symbol")})[:5] or ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    symbols = {
        str(row.get("symbol"))
        for row in dataset
        if row.get("symbol")
        and row.get("label") is not None
        and return_pct(row) > 0
    }
    return sorted(symbols)[:8] or ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def recommended_score(best: dict[str, Any] | None) -> int:
    if not best:
        return 80
    score = best.get("filters", {}).get("score_bucket")
    if isinstance(score, str):
        first = score.split("-")[0].replace("+", "")
        return safe_int(first, 80)
    return 80


def return_pct(row: dict[str, Any]) -> float:
    value = row.get("realized_return_pct")
    if value is not None:
        return safe_float(value)
    label = safe_int(row.get("label"))
    if label == 1:
        return safe_float(row.get("tp1_distance_pct"))
    if label == -1:
        return -safe_float(row.get("stop_distance_pct"))
    return 0.0


def profit_factor_from_returns(values: list[float]) -> float:
    gains = sum(value for value in values if value > 0)
    losses = abs(sum(value for value in values if value < 0))
    if losses > 0:
        return gains / losses
    return 999.0 if gains > 0 else 0.0


def max_drawdown(values: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return abs(max_dd)


def max_consecutive_losses(values: list[float]) -> int:
    best = 0
    current = 0
    for value in values:
        if value < 0:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def robustness_score(total: int, profit_factor: float, expectancy: float, drawdown: float) -> float:
    sample_score = min(total / MIN_CANDIDATE_LABELS, 1.0)
    pf_score = min(profit_factor / 2.0, 1.0)
    expectancy_score = 1.0 if expectancy > 0 else 0.0
    drawdown_penalty = min(drawdown * 5.0, 1.0)
    return max(0.0, (sample_score * 0.35) + (pf_score * 0.35) + (expectancy_score * 0.2) - (drawdown_penalty * 0.1))


def trend_strength(row: dict[str, Any]) -> float:
    raw = (
        abs(safe_float(row.get("momentum_5"))) * 10
        + abs(safe_float(row.get("momentum_15"))) * 8
        + abs(safe_float(row.get("distance_to_ema_21"))) * 4
        + abs(safe_float(row.get("distance_to_ema_50"))) * 3
    )
    return min(raw, 1.0)


def score_bucket(score: float) -> str:
    if score >= 90:
        return "90+"
    if score >= 85:
        return "85-89"
    if score >= 80:
        return "80-84"
    if score >= 75:
        return "75-79"
    if score >= 70:
        return "70-74"
    if score >= 65:
        return "65-69"
    if score >= 60:
        return "60-64"
    return "<60"


def numeric_bucket(value: float, edges: list[float]) -> str:
    previous = "-inf"
    for edge in edges:
        if value < edge:
            return f"{previous}..{edge:g}"
        previous = f"{edge:g}"
    return f"{previous}..inf"


def session_bucket(hour_utc: int) -> str:
    if 0 <= hour_utc < 7:
        return "asia"
    if 7 <= hour_utc < 13:
        return "europe"
    if 13 <= hour_utc < 21:
        return "us"
    return "overnight"


def parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def group_by(rows: Iterable[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key) or "NA")].append(row)
    return dict(grouped)


def is_reverse(row: dict[str, Any]) -> bool:
    raw = row.get("variant_params_json") or "{}"
    try:
        params = json.loads(raw)
    except Exception:
        params = {}
    return safe_int(row.get("shadow_strategy")) == 1 and params.get("reverse") is True


def mean(values: Iterable[float]) -> float:
    clean = [value for value in values if math.isfinite(value)]
    return sum(clean) / len(clean) if clean else 0.0


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in keys})


def _print_discovery(result: dict[str, Any]) -> None:
    best = result.get("best_candidate")
    print("Research Lab Discovery")
    print("======================")
    print(f"Dataset: {result['dataset_rows']} observations, {result['labels']} labels, {result['shadow_labels']} shadow labels")
    print(f"Live recommendation: {result['live_recommendation']}")
    print(f"Best candidate: {best['name'] if best else 'none'}")
    print(f"Reason: {'candidate paper-only' if best else 'insufficient edge / PF below threshold / overfitting risk'}")
    print(f"Reports saved to {result['reports_dir']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Research Lab offline tools")
    parser.add_argument(
        "command",
        choices=[
            "discover",
            "report",
            "export",
            "recommend-config",
            "explain",
            "sl-report",
            "win-report",
            "counterfactuals",
            "feature-importance",
            "recommend-rules",
            "full-report",
            "phase2-persist",
            "autopilot-once",
            "virtual-portfolio",
            "kronos-once",
            "kronos-evaluate",
            "reconcile-paper",
            "strategy-lab",
            "daily-summary",
            "training-summary",
            "acceleration-plan",
            "shadow-opportunity",
            "edge-guard",
            "tp-sl-lab",
            "exit-simulation",
            "score-calibration",
            "shadow-experiments",
            "evolution-score",
            "mfe-mae-diagnostic",
            "mfe-mae-smoke-test",
            "catalyst-add",
            "catalyst-list",
            "catalyst-summary",
            "catalyst-ingest",
            "news-risk-gate",
            "paper-policy-lab",
            "paper-policy-orchestrator",
            "walk-forward",
            "policy-backtest",
            "exit-policy-backtest",
            "time-death-autopsy",
            "time-death-filter-proposal",
            "exit-cause-backtest",
            "time-death-smoke-test",
            "net-edge-lab",
            "anti-overfit-gate",
            "ev-slippage-calibration-gate",
            "policy-stability-matrix",
            "candidate-ranking",
            "decision-ledger-audit",
            "adaptive-exit-backtest",
            "sizing-safety-lab",
            "structured-output-guard-smoke-test",
            "vps-runtime-health",
            "post-migration-backup",
            "data-restore-benchmark",
            "fast-runtime-readiness",
            "websocket-migration-plan",
            "fast-runtime-smoke-test",
            "edge-hardening-smoke-test",
            "high-value-patterns-smoke-test",
            "policy-news-smoke-test",
            "time-death-lab",
            "adaptive-exit-policy",
            "latency-audit",
            "fast-execution-readiness",
            "data-vault-status",
            "data-export",
            "data-import",
            "data-upload-latest",
            "data-download-latest",
            "data-restore-latest",
            "data-vault-prune",
            "data-vault-smoke-test",
            "migration-readiness",
            "migration-readiness-deep-check",
            "exit-latency-vault-smoke-test",
            "phase-readiness-smoke-test",
            "vps-migration-guide",
            "vps-preflight",
            "fast-runtime-plan",
            "vps-migration-smoke-test",
        ],
    )
    parser.add_argument("--limit", type=int, default=None, help="Maximo de labels a procesar en phase2-persist.")
    parser.add_argument("--batch-size", type=int, default=None, help="Tamano de lote para phase2-persist.")
    parser.add_argument("--max-concurrent", type=int, default=None, help="Maximo de posiciones virtuales concurrentes.")
    parser.add_argument("--hours", type=int, default=24, help="Ventana de horas para daily-summary.")
    parser.add_argument("--safe-mode", action="store_true", default=True, help="Limita memoria y filas para Strategy Lab.")
    parser.add_argument("--unsafe-mode", action="store_false", dest="safe_mode", help="Desactiva el limite seguro de Strategy Lab.")
    parser.add_argument("--id", default="", help="Catalyst id manual.")
    parser.add_argument("--title", default="", help="Titulo del catalyst manual.")
    parser.add_argument("--symbols", default="", help="Simbolos afectados, separados por coma.")
    parser.add_argument("--category", default="other", help="Categoria catalyst.")
    parser.add_argument("--direction", default="unknown", help="Direccion catalyst.")
    parser.add_argument("--severity", default="low", help="Severidad catalyst.")
    parser.add_argument("--confidence", type=float, default=0.5, help="Confianza catalyst 0-1.")
    parser.add_argument("--hours-back", type=int, default=0, help="Horas hacia atras para ventana catalyst.")
    parser.add_argument("--hours-forward", type=int, default=24, help="Horas hacia delante para ventana catalyst.")
    parser.add_argument("--file", default="", help="Backup data-vault para importar.")
    parser.add_argument("--apply", action="store_true", help="Aplica data-import. Sin esto, import es dry-run.")
    parser.add_argument("--dry-run", action="store_true", help="Fuerza data-import dry-run.")
    parser.add_argument("--upload", action="store_true", help="Sube data-export si external storage esta configurado.")
    args = parser.parse_args()
    config = load_config()
    logger = setup_logger()
    db = Database(config, logger)
    db.initialize()
    lab = ResearchLab(db, config, logger)
    if args.command == "discover":
        _print_discovery(lab.discover())
    elif args.command == "report":
        result = lab.discover()
        print((Path(result["reports_dir"]) / "research_lab_report.md").read_text(encoding="utf-8"))
    elif args.command == "export":
        path = lab.export()
        print(f"Research Lab export saved to {path}")
    elif args.command == "recommend-config":
        path = lab.recommend_config()
        print(f"Recommended config saved to {path}")
    elif args.command == "explain":
        print(lab.explain_report())
    elif args.command == "sl-report":
        print(lab.sl_report())
    elif args.command == "win-report":
        print(lab.win_report())
    elif args.command == "counterfactuals":
        print(lab.counterfactuals_report())
    elif args.command == "feature-importance":
        print(lab.feature_importance_report())
    elif args.command == "recommend-rules":
        print(lab.recommend_rules_report())
    elif args.command == "full-report":
        print(lab.full_report())
    elif args.command == "phase2-persist":
        from .phase2_persist import Phase2Persister

        limit = args.limit if args.limit is not None else config.phase2_persist_max_labels_per_run
        batch_size = args.batch_size if args.batch_size is not None else config.phase2_persist_batch_size
        result = Phase2Persister(db, logger).persist(limit=limit, batch_size=batch_size)
        print(result.to_text())
    elif args.command == "virtual-portfolio":
        from .virtual_portfolio import VirtualPortfolioResearch

        limit = args.limit if args.limit is not None else config.virtual_portfolio_max_labels_per_run
        max_concurrent = args.max_concurrent if args.max_concurrent is not None else config.virtual_max_concurrent_positions
        result = VirtualPortfolioResearch(db, logger).simulate(limit=limit, max_concurrent=max_concurrent)
        print(result.to_text())
    elif args.command == "autopilot-once":
        from .research_autopilot import ResearchAutopilot

        print(ResearchAutopilot(config, db, logger).run_once().to_text())
    elif args.command == "kronos-once":
        limit = args.limit if args.limit is not None else 100
        print(lab.kronos_once(limit=limit))
    elif args.command == "kronos-evaluate":
        print(lab.kronos_evaluate())
    elif args.command == "reconcile-paper":
        print(lab.reconcile_paper())
    elif args.command == "strategy-lab":
        limit = args.limit if args.limit is not None else 20000
        print(lab.strategy_lab(limit=limit, safe_mode=args.safe_mode))
    elif args.command == "daily-summary":
        print(lab.daily_summary(hours=args.hours))
    elif args.command == "training-summary":
        print(lab.training_summary(hours=args.hours))
    elif args.command == "acceleration-plan":
        print(lab.acceleration_plan(hours=args.hours))
    elif args.command == "shadow-opportunity":
        print(lab.shadow_opportunity(hours=args.hours))
    elif args.command == "edge-guard":
        print(lab.edge_guard(hours=args.hours))
    elif args.command == "tp-sl-lab":
        print(lab.tp_sl_lab(hours=args.hours))
    elif args.command == "exit-simulation":
        print(lab.exit_simulation(hours=args.hours))
    elif args.command == "score-calibration":
        print(lab.score_calibration(hours=args.hours))
    elif args.command == "shadow-experiments":
        print(lab.shadow_experiments(hours=args.hours))
    elif args.command == "evolution-score":
        print(lab.evolution_score(hours=args.hours))
    elif args.command == "mfe-mae-diagnostic":
        print(lab.mfe_mae_diagnostic(hours=args.hours))
    elif args.command == "mfe-mae-smoke-test":
        print(lab.mfe_mae_smoke_test())
    elif args.command == "catalyst-add":
        print(lab.catalyst_add(
            catalyst_id=args.id,
            title=args.title,
            symbols=args.symbols,
            category=args.category,
            direction=args.direction,
            severity=args.severity,
            confidence=args.confidence,
            hours_back=args.hours_back,
            hours_forward=args.hours_forward,
        ))
    elif args.command == "catalyst-list":
        print(lab.catalyst_list(hours=args.hours))
    elif args.command == "catalyst-summary":
        print(lab.catalyst_summary(hours=args.hours))
    elif args.command == "catalyst-ingest":
        print(lab.catalyst_ingest(hours=args.hours))
    elif args.command == "news-risk-gate":
        print(lab.news_risk_gate(hours=args.hours))
    elif args.command == "paper-policy-lab":
        print(lab.paper_policy_lab(hours=args.hours))
    elif args.command == "paper-policy-orchestrator":
        print(lab.paper_policy_orchestrator(hours=args.hours))
    elif args.command == "walk-forward":
        print(lab.walk_forward(hours=args.hours))
    elif args.command == "policy-backtest":
        print(lab.policy_backtest(hours=args.hours))
    elif args.command == "exit-policy-backtest":
        print(lab.exit_policy_backtest(hours=args.hours))
    elif args.command == "time-death-autopsy":
        print(lab.time_death_autopsy(hours=args.hours))
    elif args.command == "time-death-filter-proposal":
        print(lab.time_death_filter_proposal(hours=args.hours))
    elif args.command == "exit-cause-backtest":
        print(lab.exit_cause_backtest(hours=args.hours))
    elif args.command == "time-death-smoke-test":
        print(lab.time_death_smoke_test())
    elif args.command == "net-edge-lab":
        print(lab.net_edge_lab(hours=args.hours))
    elif args.command == "anti-overfit-gate":
        print(lab.anti_overfit_gate(hours=args.hours))
    elif args.command == "ev-slippage-calibration-gate":
        print(lab.ev_slippage_calibration_gate(hours=args.hours))
    elif args.command == "policy-stability-matrix":
        print(lab.policy_stability_matrix(hours=args.hours))
    elif args.command == "candidate-ranking":
        print(lab.candidate_ranking(hours=args.hours))
    elif args.command == "decision-ledger-audit":
        print(lab.decision_ledger_audit(hours=args.hours))
    elif args.command == "adaptive-exit-backtest":
        print(lab.adaptive_exit_backtest(hours=args.hours))
    elif args.command == "sizing-safety-lab":
        print(lab.sizing_safety_lab(hours=args.hours))
    elif args.command == "structured-output-guard-smoke-test":
        print(lab.structured_output_guard_smoke_test())
    elif args.command == "vps-runtime-health":
        print(lab.vps_runtime_health())
    elif args.command == "post-migration-backup":
        print(lab.post_migration_backup(hours=args.hours))
    elif args.command == "data-restore-benchmark":
        print(lab.data_restore_benchmark())
    elif args.command == "fast-runtime-readiness":
        print(lab.fast_runtime_readiness(hours=args.hours))
    elif args.command == "websocket-migration-plan":
        print(lab.websocket_migration_plan(hours=args.hours))
    elif args.command == "fast-runtime-smoke-test":
        print(lab.fast_runtime_smoke_test())
    elif args.command == "edge-hardening-smoke-test":
        print(lab.edge_hardening_smoke_test())
    elif args.command == "high-value-patterns-smoke-test":
        print(lab.high_value_patterns_smoke_test())
    elif args.command == "policy-news-smoke-test":
        print(lab.policy_news_smoke_test())
    elif args.command == "time-death-lab":
        print(lab.time_death_lab(hours=args.hours))
    elif args.command == "adaptive-exit-policy":
        print(lab.adaptive_exit_policy(hours=args.hours))
    elif args.command == "latency-audit":
        print(lab.latency_audit(hours=args.hours))
    elif args.command == "fast-execution-readiness":
        print(lab.fast_execution_readiness())
    elif args.command == "data-vault-status":
        print(lab.data_vault_status())
    elif args.command == "data-export":
        print(lab.data_export(hours=args.hours, upload=args.upload))
    elif args.command == "data-import":
        if not args.file:
            raise SystemExit("--file es obligatorio para data-import")
        print(lab.data_import(file=args.file, apply=args.apply and not args.dry_run))
    elif args.command == "data-upload-latest":
        print(lab.data_upload_latest())
    elif args.command == "data-download-latest":
        print(lab.data_download_latest())
    elif args.command == "data-restore-latest":
        print(lab.data_restore_latest(apply=args.apply and not args.dry_run))
    elif args.command == "data-vault-prune":
        print(lab.data_vault_prune(apply=args.apply and not args.dry_run))
    elif args.command == "data-vault-smoke-test":
        print(lab.data_vault_smoke_test())
    elif args.command == "migration-readiness":
        print(lab.migration_readiness())
    elif args.command == "migration-readiness-deep-check":
        print(lab.migration_readiness_deep_check())
    elif args.command == "exit-latency-vault-smoke-test":
        print(lab.exit_latency_vault_smoke_test())
    elif args.command == "phase-readiness-smoke-test":
        print(lab.phase_readiness_smoke_test())
    elif args.command == "vps-migration-guide":
        print(lab.vps_migration_guide())
    elif args.command == "vps-preflight":
        print(lab.vps_preflight())
    elif args.command == "fast-runtime-plan":
        print(lab.fast_runtime_plan(hours=args.hours))
    elif args.command == "vps-migration-smoke-test":
        print(lab.vps_migration_smoke_test())


if __name__ == "__main__":
    main()
