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

    def exit_label_calibration_v2(self, hours: int = 24) -> str:
        from .exit_label_calibration_v2 import ExitLabelCalibrationV2

        return ExitLabelCalibrationV2(self.config, self.db).to_text(hours=hours)

    def score_calibration(self, hours: int = 24) -> str:
        from .score_calibration import ScoreCalibration

        return ScoreCalibration(self.config, self.db).to_text(hours=hours)

    def score_calibration_smoke_test(self) -> str:
        from .score_calibration_smoke_test import score_calibration_smoke_text

        return score_calibration_smoke_text(self.config)

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

    def candidate_incubator(self, hours: int = 24) -> str:
        from .candidate_incubator import CandidateIncubator

        return CandidateIncubator(self.config, self.db).to_text(hours=hours)

    def candidate_incubator_smoke_test(self) -> str:
        from .candidate_incubator_smoke_test import candidate_incubator_smoke_text

        return candidate_incubator_smoke_text(self.config)

    def training_data_integrity(self, hours: int = 24) -> str:
        from .training_data_integrity import TrainingDataIntegrity

        return TrainingDataIntegrity(self.config, self.db).to_text(hours=hours)

    def training_data_integrity_smoke_test(self) -> str:
        from .training_data_integrity import TrainingDataIntegritySmokeTest

        return TrainingDataIntegritySmokeTest(self.config, self.db, self.logger).to_text()

    def worker_health_audit(self) -> str:
        from .worker_health_audit import WorkerHealthAudit

        return WorkerHealthAudit(self.config, self.db, self.logger).to_text()

    def worker_health_audit_smoke_test(self) -> str:
        from .worker_health_audit import WorkerHealthAuditSmokeTest

        return WorkerHealthAuditSmokeTest(self.config, self.db, self.logger).to_text()

    def data_vault_audit(self) -> str:
        from .data_vault_audit import DataVaultAudit

        return DataVaultAudit(self.config, self.db, self.logger).to_text()

    def dashboard_data_binding_audit(self) -> str:
        from .dashboard_data_binding_audit import DashboardDataBindingAudit

        return DashboardDataBindingAudit(self.config, self.db, self.logger).to_text()

    def dashboard_data_binding_smoke_test(self) -> str:
        from .dashboard_data_binding_audit import DashboardDataBindingSmokeTest

        return DashboardDataBindingSmokeTest(self.config, self.db, self.logger).to_text()

    def data_pipeline_diagnosis(self, hours: int = 24) -> str:
        from .data_pipeline_diagnosis import DataPipelineDiagnosis

        return DataPipelineDiagnosis(self.config, self.db).to_text(hours=hours)

    def data_pipeline_diagnosis_smoke_test(self) -> str:
        from .data_pipeline_diagnosis import DataPipelineDiagnosisSmokeTest

        return DataPipelineDiagnosisSmokeTest(self.config, self.db, self.logger).to_text()

    def relation_repair_audit(self, hours: int = 24) -> str:
        from .relation_repair_audit import RelationRepairAudit

        return RelationRepairAudit(self.config, self.db).to_text(hours=hours)

    def relation_repair_audit_smoke_test(self) -> str:
        from .relation_repair_audit import RelationRepairAuditSmokeTest

        return RelationRepairAuditSmokeTest(self.config, self.db, self.logger).to_text()

    def label_quality_v2(self, hours: int = 24) -> str:
        from .label_quality_v2 import LabelQualityV2

        return LabelQualityV2(self.config, self.db).to_text(hours=hours)

    def label_quality_v2_smoke_test(self) -> str:
        from .label_quality_v2 import LabelQualityV2SmokeTest

        return LabelQualityV2SmokeTest(self.config, self.db, self.logger).to_text()

    def cost_model_inventory(self) -> str:
        from .bitget_cost_model_audit import BitgetCostModelAudit

        return BitgetCostModelAudit(self.config, self.db).inventory_text()

    def bitget_cost_model_audit(self, hours: int = 24) -> str:
        from .bitget_cost_model_audit import BitgetCostModelAudit

        return BitgetCostModelAudit(self.config, self.db).to_text(hours=hours)

    def bitget_cost_model_smoke_test(self) -> str:
        from .bitget_cost_model_audit import BitgetCostModelSmokeTest

        return BitgetCostModelSmokeTest(self.config, self.db, self.logger).to_text()

    def margin_mode_audit(self) -> str:
        from .margin_mode_audit import MarginModeAudit

        return MarginModeAudit(self.config, self.db).to_text()

    def margin_mode_audit_smoke_test(self) -> str:
        from .margin_mode_audit import MarginModeAuditSmokeTest

        return MarginModeAuditSmokeTest(self.config, self.db, self.logger).to_text()

    def core_corrections(self, hours: int = 24) -> str:
        from .core_corrections import CoreCorrections

        return CoreCorrections(self.config, self.db).to_text(hours=hours)

    def core_corrections_smoke_test(self) -> str:
        from .core_corrections import core_corrections_smoke_text

        return core_corrections_smoke_text(self.config, self.db)

    def cost_model_correction_smoke_test(self) -> str:
        from .core_corrections import cost_model_correction_smoke_text

        return cost_model_correction_smoke_text()

    def funding_model_smoke_test(self) -> str:
        from .core_corrections import funding_model_smoke_text

        return funding_model_smoke_text()

    def labeler_guard_smoke_test(self) -> str:
        from .data_guards import labeler_guard_smoke_text

        return labeler_guard_smoke_text()

    def duplicate_guard_smoke_test(self) -> str:
        from .data_guards import duplicate_guard_smoke_text

        return duplicate_guard_smoke_text()

    def candidate_actionability_smoke_test(self) -> str:
        from .candidate_incubator_smoke_test import candidate_incubator_smoke_text

        return candidate_incubator_smoke_text(self.config)

    def execution_safety_audit(self) -> str:
        from .execution_safety import ExecutionSafetyAudit

        return ExecutionSafetyAudit(self.config, self.db).to_text()

    def net_rr_audit(self, hours: int = 24) -> str:
        from .execution_safety import net_rr_audit_text

        return net_rr_audit_text(hours=hours)

    def dynamic_exit_policy_audit(self, hours: int = 24) -> str:
        from .execution_safety import dynamic_exit_policy_audit_text

        return dynamic_exit_policy_audit_text(hours=hours)

    def structural_stop_audit(self, hours: int = 24) -> str:
        from .execution_safety import structural_stop_audit_text

        return structural_stop_audit_text(hours=hours)

    def execution_safety_smoke_test(self) -> str:
        from .execution_safety import execution_safety_smoke_text

        return execution_safety_smoke_text(self.config)

    def net_rr_smoke_test(self) -> str:
        from .net_rr import net_rr_smoke_text

        return net_rr_smoke_text()

    def dynamic_exit_policy_smoke_test(self) -> str:
        from .dynamic_exit_policy import dynamic_exit_policy_smoke_text

        return dynamic_exit_policy_smoke_text()

    def structural_stop_smoke_test(self) -> str:
        from .structural_stop import structural_stop_smoke_text

        return structural_stop_smoke_text()

    def fresh_balance_risk_smoke_test(self) -> str:
        from .execution_safety import fresh_balance_risk_smoke_text

        return fresh_balance_risk_smoke_text()

    def execution_idempotency_smoke_test(self) -> str:
        from .execution_safety import execution_idempotency_smoke_text

        return execution_idempotency_smoke_text()

    def emergency_failsafe_smoke_test(self) -> str:
        from .execution_safety import emergency_failsafe_smoke_text

        return emergency_failsafe_smoke_text()

    def circuit_breaker_magnitude_smoke_test(self) -> str:
        from .execution_safety import circuit_breaker_magnitude_smoke_text

        return circuit_breaker_magnitude_smoke_text()

    def clock_drift_smoke_test(self) -> str:
        from .execution_safety import clock_drift_smoke_text

        return clock_drift_smoke_text()

    def config_hardening_smoke_test(self) -> str:
        from .execution_safety import config_hardening_smoke_text

        return config_hardening_smoke_text(self.config)

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

    def pre_move_event_labeler(self, hours: int = 24) -> str:
        from .pre_move_event_labeler import PreMoveEventLabeler

        return PreMoveEventLabeler(self.config, self.db).to_text(hours=hours)

    def pre_move_feature_snapshot(self, hours: int = 24) -> str:
        from .pre_move_feature_snapshot import PreMoveFeatureSnapshot

        return PreMoveFeatureSnapshot(self.config, self.db).to_text(hours=hours)

    def pre_move_pattern_miner(self, hours: int = 24) -> str:
        from .pre_move_pattern_miner import PreMovePatternMiner

        return PreMovePatternMiner(self.config, self.db).to_text(hours=hours)

    def pre_move_similarity_scanner(self, hours: int = 6) -> str:
        from .pre_move_similarity_scanner import PreMoveSimilarityScanner

        return PreMoveSimilarityScanner(self.config, self.db).to_text(hours=hours)

    def pre_move_smoke_test(self) -> str:
        from .pre_move_smoke_test import PreMoveSmokeTest

        return PreMoveSmokeTest(self.config, self.db, self.logger).to_text()

    def dashboard_pro_smoke_test(self) -> str:
        from .dashboard_pro_smoke_test import DashboardProSmokeTest

        return DashboardProSmokeTest(self.config, self.db, self.logger).to_text()

    def dashboard_beauty_exit_calibration_smoke_test(self) -> str:
        from .dashboard_beauty_exit_calibration_smoke_test import DashboardBeautyExitCalibrationSmokeTest

        return DashboardBeautyExitCalibrationSmokeTest(self.config, self.db, self.logger).to_text()

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

    def security_audit(self) -> str:
        from .bot_integrity_audit import security_audit_text

        return security_audit_text(self.config, self.db)

    def label_time_audit(self, hours: int = 24) -> str:
        from .bot_integrity_audit import label_time_audit_text

        return label_time_audit_text(self.config, self.db, hours=hours)

    def paper_trading_audit(self, hours: int = 24) -> str:
        from .bot_integrity_audit import paper_trading_audit_text

        return paper_trading_audit_text(self.config, self.db, hours=hours)

    def research_modules_audit(self, hours: int = 24) -> str:
        from .bot_integrity_audit import research_modules_audit_text

        return research_modules_audit_text(self.config, self.db, hours=hours)

    def bot_integrity_audit(self, hours: int = 24) -> str:
        from .bot_integrity_audit import bot_integrity_audit_text

        return bot_integrity_audit_text(self.config, self.db, hours=hours)

    def bot_integrity_audit_smoke_test(self) -> str:
        from .bot_integrity_audit import BotIntegrityAuditSmokeTest

        return BotIntegrityAuditSmokeTest(self.config, self.db, self.logger).to_text()

    def dashboard_ui_v3_smoke_test(self) -> str:
        from .dashboard_ui_v3_smoke_test import DashboardUiV3SmokeTest

        return DashboardUiV3SmokeTest(self.config, self.db, self.logger).to_text()

    def dashboard_report_timeout_smoke_test(self) -> str:
        import time

        from .dashboard_pro import DashboardProReporter

        reporter = DashboardProReporter(self.config, self.db, self.logger)
        timeout_section = reporter._run_section("slow_section", lambda: (time.sleep(0.08) or "late"), timeout_seconds=0.01)
        secret_section = reporter._run_section("secret_section", lambda: "API_KEY=abc123\nfinal_recommendation: NO LIVE", timeout_seconds=1.0)
        checks = {
            "timeout_returns_partial_section": timeout_section.status == "timeout" and "SECTION_TIMEOUT" in timeout_section.text,
            "warning_recorded": "SECTION_TIMEOUT" in timeout_section.warning,
            "secrets_sanitized": "abc123" not in secret_section.text and "***" in secret_section.text,
            "final_recommendation_no_live": "NO LIVE" in secret_section.text,
        }
        lines = ["DASHBOARD REPORT TIMEOUT SMOKE TEST START"]
        lines.extend(f"{key}: {str(value).lower()}" for key, value in checks.items())
        lines.extend(
            [
                "report_status_if_timeout: PARTIAL_REPORT",
                "backup_restore_live_executed: false",
                "LIVE_TRADING=false",
                "DRY_RUN=true",
                "PAPER_TRADING=true",
                "ENABLE_PAPER_POLICY_FILTER=false",
                "final_recommendation: NO LIVE",
                f"result: {'PASS' if all(checks.values()) else 'FAIL'}",
                "DASHBOARD REPORT TIMEOUT SMOKE TEST END",
            ]
        )
        return "\n".join(lines)

    def exit_policy_v3_backtest(self, hours: int = 24) -> str:
        from .exit_policy_v3_backtest import ExitPolicyV3Backtest

        return ExitPolicyV3Backtest(self.config, self.db).to_text(hours=hours)

    def exit_policy_v3_smoke_test(self) -> str:
        from .exit_policy_v3 import exit_policy_v3_smoke_text
        from .exit_policy_v3_backtest import exit_policy_v3_backtest_smoke_text

        return exit_policy_v3_smoke_text() + "\n\n" + exit_policy_v3_backtest_smoke_text()

    def sudden_move_detector(self, hours: int = 24) -> str:
        from .sudden_move_detector import SuddenMoveDetector

        return SuddenMoveDetector(self.config, self.db).to_text(hours=hours)

    def sudden_move_smoke_test(self) -> str:
        from .sudden_move_detector import sudden_move_smoke_text

        return sudden_move_smoke_text()

    def pre_move_v2(self, hours: int = 24) -> str:
        from .pre_move_intelligence_v2 import PreMoveIntelligenceV2

        return PreMoveIntelligenceV2(self.config, self.db).to_text(hours=hours)

    def pre_move_v2_smoke_test(self) -> str:
        from .pre_move_intelligence_v2 import pre_move_v2_smoke_text

        return pre_move_v2_smoke_text()

    def walk_forward_validator(self, hours: int = 72) -> str:
        from .walk_forward_validator import WalkForwardValidator

        return WalkForwardValidator(self.config, self.db).to_text(hours=hours)

    def walk_forward_smoke_test(self) -> str:
        from .walk_forward_validator import walk_forward_smoke_text

        return walk_forward_smoke_text()

    def anti_overfit_v2(self, hours: int = 72) -> str:
        from .anti_overfit_matrix_v2 import AntiOverfitMatrixV2

        return AntiOverfitMatrixV2(self.config, self.db).to_text(hours=hours)

    def anti_overfit_v2_smoke_test(self) -> str:
        from .anti_overfit_matrix_v2 import anti_overfit_v2_smoke_text

        return anti_overfit_v2_smoke_text()

    def candidate_promotion_v2(self, hours: int = 24) -> str:
        from .candidate_promotion_v2 import CandidatePromotionV2

        return CandidatePromotionV2(self.config, self.db).to_text(hours=hours)

    def candidate_promotion_v2_smoke_test(self) -> str:
        from .candidate_promotion_v2 import candidate_promotion_v2_smoke_text

        return candidate_promotion_v2_smoke_text()

    def shadow_strategy_simulator(self, hours: int = 72) -> str:
        from .shadow_strategy_simulator import ShadowStrategySimulator

        return ShadowStrategySimulator(self.config, self.db).to_text(hours=hours)

    def shadow_strategy_simulator_smoke_test(self) -> str:
        from .shadow_strategy_simulator import shadow_strategy_simulator_smoke_text

        return shadow_strategy_simulator_smoke_text()

    def operational_intelligence_audit(self, hours: int = 24) -> str:
        from .operational_intelligence import OperationalIntelligenceAudit

        return OperationalIntelligenceAudit(self.config, self.db).to_text(hours=hours)

    def strategy_research_library(self, hours: int = 72) -> str:
        from .strategy_research_library import StrategyResearchLibrary

        return StrategyResearchLibrary(self.config, self.db).to_text(hours=hours)

    def strategy_research_library_smoke_test(self) -> str:
        from .strategy_research_library import strategy_research_library_smoke_text

        return strategy_research_library_smoke_text()

    def real_strategy_backtest(self, hours: int = 72) -> str:
        from .real_strategy_backtester import real_strategy_backtest_text

        return real_strategy_backtest_text(self.config, self.db, hours=hours)

    def real_strategy_backtest_multi(
        self,
        hours: int = 72,
        symbols: list[str] | None = None,
        timeframe: str = "5m",
    ) -> str:
        """Multi-symbol real strategy backtester CLI entry point.

        Runs the SignalEngine vela-by-vela against persisted OHLCV for every
        requested symbol, returning a per-symbol breakdown plus aggregated TOTAL.
        Missing data on any symbol is reported as NEED_DATA without crashing.
        Pure research/offline: never sends orders or touches the exchange.
        """
        from .real_strategy_backtester import real_strategy_backtest_multi_text

        return real_strategy_backtest_multi_text(
            self.config, self.db, hours=hours, symbols=symbols, timeframe=timeframe,
        )

    def real_strategy_backtest_breakdown(
        self,
        hours: int = 72,
        symbols: list[str] | None = None,
        timeframe: str = "5m",
        group_by: str = "symbol",
        min_trades: int = 30,
        top_n: int = 25,
    ) -> str:
        """Deep breakdown of multi-symbol backtester output by any dimension."""
        from .backtest_breakdown import run_breakdown_text

        return run_breakdown_text(
            self.config, self.db,
            hours=hours, symbols=symbols, timeframe=timeframe,
            group_by=group_by, min_trades=min_trades, top_n=top_n,
        )

    def final_policy_builder(
        self,
        hours: int = 72,
        symbols: list[str] | None = None,
        timeframe: str = "5m",
        group_by: str = "symbol,side,regime,score_bucket",
        min_trades: int = 100,
        folds: int = 4,
        data_quality_status: str = "OK",
        label_quality_status: str = "OK",
    ) -> str:
        """Build a candidate policy from the multi-symbol breakdown + walk-forward.

        Never auto-activates anything. Returns the policy as text + JSON-renderable.
        """
        from .backtest_breakdown import collect_trade_records, build_breakdown, parse_group_by
        from .walk_forward_runner import build_walk_forward, WF_PASS
        from .cost_stress import evaluate_cost_stress
        from .final_research_policy_builder import (
            PolicyBuildInput, build_policy, render_policy_text,
        )

        tokens = parse_group_by(group_by)
        records = collect_trade_records(
            self.config, self.db, hours=hours, symbols=symbols, timeframe=timeframe,
        )
        breakdown = build_breakdown(
            records, group_by=tokens, min_trades=min_trades, top_n=25,
            hours=hours, timeframe=timeframe,
        )
        wf = build_walk_forward(records, folds=folds, min_trades_per_setup=min_trades)
        wf_status = wf.overall_status
        stress = evaluate_cost_stress([record.gross_return_pct for record in records])
        policy = build_policy(PolicyBuildInput(
            breakdown=breakdown,
            data_quality_status=data_quality_status,
            label_quality_status=label_quality_status,
            walk_forward_status=wf_status,
            cost_stress_status=stress.cost_stress_status,
            cost_stress_reasons=list(stress.reasons),
            time_exit_autopsy_status="UNKNOWN",
            dynamic_hold_status="UNKNOWN",
            profit_protection_status="UNKNOWN",
            entry_exhaustion_status="UNKNOWN",
            anti_overfit_status="UNKNOWN",
            reversal_lab_status="RESEARCH_ONLY",
            validation_hours=hours,
        ))
        return render_policy_text(policy)

    def cost_stress_summary(
        self,
        hours: int = 72,
        symbols: list[str] | None = None,
        timeframe: str = "5m",
    ) -> str:
        """Re-evaluate the multi-symbol backtester output under stricter costs."""
        from .backtest_breakdown import collect_trade_records
        from .cost_stress import evaluate_cost_stress, render_cost_stress_text

        records = collect_trade_records(
            self.config, self.db, hours=hours, symbols=symbols, timeframe=timeframe,
        )
        grosses = [r.gross_return_pct for r in records]
        report = evaluate_cost_stress(grosses)
        return render_cost_stress_text(report)

    def profit_lock_lab(self, symbol: str = "BTCUSDT", hours: int = 72, timeframe: str = "5m") -> str:
        from .exit_labs import run_profit_lock_lab, render_exit_lab_text
        return render_exit_lab_text(run_profit_lock_lab(self.config, self.db, symbol=symbol, hours=hours, timeframe=timeframe))

    def fast_exit_lab(self, symbol: str = "BTCUSDT", hours: int = 72, timeframe: str = "5m") -> str:
        from .exit_labs import run_fast_exit_lab, render_exit_lab_text
        return render_exit_lab_text(run_fast_exit_lab(self.config, self.db, symbol=symbol, hours=hours, timeframe=timeframe))

    def time_death_reducer_lab(self, symbol: str = "BTCUSDT", hours: int = 72, timeframe: str = "5m") -> str:
        from .exit_labs import run_time_death_reducer_lab, render_exit_lab_text
        return render_exit_lab_text(run_time_death_reducer_lab(self.config, self.db, symbol=symbol, hours=hours, timeframe=timeframe))

    def time_exit_autopsy_v2(self, hours: int = 72, symbols: list[str] | None = None, timeframe: str = "5m") -> str:
        from .time_exit_autopsy_v2 import time_exit_autopsy_v2_text
        return time_exit_autopsy_v2_text(self.config, self.db, hours=hours, timeframe=timeframe, symbols=symbols)

    def dynamic_hold_lab(self, hours: int = 72, symbols: list[str] | None = None, timeframe: str = "5m") -> str:
        from .dynamic_hold_lab import dynamic_hold_lab_text
        return dynamic_hold_lab_text(self.config, self.db, hours=hours, timeframe=timeframe, symbols=symbols)

    def entry_exhaustion_lab(self, hours: int = 72, symbols: list[str] | None = None, timeframe: str = "5m") -> str:
        from .entry_exhaustion_lab import entry_exhaustion_lab_text
        return entry_exhaustion_lab_text(self.config, self.db, hours=hours, timeframe=timeframe, symbols=symbols)

    def reversal_candidate_lab(self, hours: int = 72, symbols: list[str] | None = None, timeframe: str = "5m") -> str:
        from .reversal_candidate_lab import reversal_candidate_lab_text
        return reversal_candidate_lab_text(self.config, self.db, hours=hours, timeframe=timeframe, symbols=symbols)

    def exit_policy_v2(self, hours: int = 72, symbols: list[str] | None = None, timeframe: str = "5m") -> str:
        from .exit_policy_v2 import exit_policy_v2_text
        return exit_policy_v2_text(self.config, self.db, hours=hours, timeframe=timeframe, symbols=symbols)

    def phase8_candidate_validator(
        self,
        hours: int = 720,
        symbols: list[str] | None = None,
        timeframe: str = "5m",
        min_trades: int = 200,
        folds: int = 4,
    ) -> str:
        from .phase8_candidate_validator import phase8_candidate_validator_text
        return phase8_candidate_validator_text(
            self.config,
            self.db,
            hours=hours,
            timeframe=timeframe,
            symbols=symbols,
            min_trades=min_trades,
            folds=folds,
        )

    def phase8_cost_stress(
        self,
        hours: int = 720,
        symbols: list[str] | None = None,
        timeframe: str = "5m",
        policy: str = "late_entry_block_plus_dynamic_hold",
    ) -> str:
        from .phase8_candidate_validator import phase8_cost_stress_text
        return phase8_cost_stress_text(
            self.config,
            self.db,
            hours=hours,
            timeframe=timeframe,
            symbols=symbols,
            policy=policy,
        )

    def dot_regime_diagnosis(
        self,
        hours: int = 720,
        symbols: list[str] | None = None,
        timeframe: str = "5m",
        folds: int = 4,
    ) -> str:
        from .dot_regime_diagnosis import dot_regime_diagnosis_text
        return dot_regime_diagnosis_text(
            self.config, self.db, hours=hours, timeframe=timeframe, symbols=symbols, folds=folds,
        )

    def dot_regime_filter_lab(
        self,
        hours: int = 720,
        symbols: list[str] | None = None,
        timeframe: str = "5m",
        folds: int = 4,
    ) -> str:
        from .dot_regime_filter_lab import dot_regime_filter_lab_text
        return dot_regime_filter_lab_text(
            self.config, self.db, hours=hours, timeframe=timeframe, symbols=symbols, folds=folds,
        )

    def phase9_paper_readiness(
        self,
        hours: int = 720,
        symbols: list[str] | None = None,
        timeframe: str = "5m",
        min_trades: int = 250,
        folds: int = 4,
    ) -> str:
        from .phase9_paper_readiness_validator import phase9_paper_readiness_text
        return phase9_paper_readiness_text(
            self.config,
            self.db,
            hours=hours,
            timeframe=timeframe,
            symbols=symbols,
            min_trades=min_trades,
            folds=folds,
        )

    def net_profit_lock_lab(
        self,
        hours: int = 720,
        symbols: list[str] | None = None,
        timeframe: str = "5m",
    ) -> str:
        from .net_profit_lock_lab import net_profit_lock_text
        return net_profit_lock_text(self.config, self.db, hours=hours, timeframe=timeframe, symbols=symbols)

    # ResearchOps V5 ---------------------------------------------------------

    def ohlcv_freshness_status(
        self,
        symbols: list[str] | None = None,
        timeframes: list[str] | None = None,
    ) -> str:
        from .ohlcv_freshness_manager import (
            freshness_status,
            render_freshness_matrix_text,
        )
        report = freshness_status(
            self.db,
            symbols=symbols,
            timeframes=timeframes,
            config=self.config,
        )
        return render_freshness_matrix_text(report)

    def ohlcv_freshness_refresh(
        self,
        symbols: list[str] | None = None,
        timeframes: list[str] | None = None,
        hours: int = 120,
        dry_run: bool = True,
        allow_real_writes: bool = False,
    ) -> str:
        from .ohlcv_freshness_manager import (
            refresh,
            render_refresh_report_text,
        )
        report = refresh(
            self.db,
            config=self.config,
            symbols=symbols,
            timeframes=timeframes,
            hours=hours,
            dry_run=dry_run,
            allow_real_writes=allow_real_writes,
            logger=self.logger,
        )
        return render_refresh_report_text(report)

    def training_clean_view_audit(self, hours: int = 24) -> str:
        from .training_data_clean_view import (
            run_training_data_clean_view,
            render_training_data_clean_view_text,
        )
        report = run_training_data_clean_view(self.db, hours=hours)
        return render_training_data_clean_view_text(report)

    def shadow_multi_trade_status(self, hours: int = 24) -> str:
        from .shadow_multi_trade_learning import (
            run_shadow_multi_trade,
            render_shadow_multi_trade_text,
        )
        report = run_shadow_multi_trade(
            self.config, self.db, hours=hours, timeframe="5m",
        )
        return render_shadow_multi_trade_text(report)

    def shadow_multi_trade_replay(
        self,
        hours: int = 720,
        symbols: list[str] | None = None,
        timeframe: str = "5m",
    ) -> str:
        from .shadow_multi_trade_learning import (
            run_shadow_multi_trade,
            render_shadow_multi_trade_text,
        )
        report = run_shadow_multi_trade(
            self.config, self.db,
            hours=hours, timeframe=timeframe, symbols=symbols,
        )
        return render_shadow_multi_trade_text(report)

    def capital_leverage_sim(
        self,
        hours: int = 720,
        symbols: list[str] | None = None,
        timeframe: str = "5m",
        capital_total_usdt: float = 40.0,
        margins: tuple[float, ...] = (2.0, 5.0, 10.0, 20.0),
        leverages: tuple[int, ...] = (1, 3, 5, 10, 20, 50),
    ) -> str:
        from .capital_leverage_simulator import (
            run_capital_leverage_simulator,
            render_capital_leverage_text,
        )
        report = run_capital_leverage_simulator(
            self.config, self.db,
            hours=hours, timeframe=timeframe, symbols=symbols,
            capital_total_usdt=capital_total_usdt,
            margins=margins, leverages=leverages,
        )
        return render_capital_leverage_text(report)

    def fee_aware_exit_trainer(
        self,
        hours: int = 720,
        symbols: list[str] | None = None,
        timeframe: str = "5m",
    ) -> str:
        from .fee_aware_exit_trainer import (
            run_fee_aware_exit_trainer,
            render_fee_aware_exit_text,
        )
        report = run_fee_aware_exit_trainer(
            self.config, self.db,
            hours=hours, timeframe=timeframe, symbols=symbols,
        )
        return render_fee_aware_exit_text(report)

    def strategy_research_enhancer(
        self,
        hours: int = 24,
        symbols: list[str] | None = None,
        timeframe: str = "5m",
        data_quality_status: str | None = None,
    ) -> str:
        from .strategy_research_enhancer import strategy_research_enhancer_text
        return strategy_research_enhancer_text(
            self.config, self.db,
            hours=hours, timeframe=timeframe, symbols=symbols,
            data_quality_status=data_quality_status,
        )

    def clean_research_metrics(
        self,
        hours: int = 24,
        symbols: list[str] | None = None,
        timeframes: list[str] | None = None,
    ) -> str:
        from .clean_research_metrics import (
            get_clean_research_metrics,
            render_clean_metrics_text,
        )
        report = get_clean_research_metrics(
            self.db, hours=hours, symbols=symbols, timeframes=timeframes,
        )
        return render_clean_metrics_text(report)

    # ResearchOps V7 ---------------------------------------------------------

    def data_pipeline_root_cause(
        self,
        hours: int = 24,
        symbols: list[str] | None = None,
        timeframes: list[str] | None = None,
    ) -> str:
        from .data_pipeline_root_cause import (
            render_data_pipeline_root_cause_text,
            run_data_pipeline_root_cause,
        )
        report = run_data_pipeline_root_cause(
            self.db, hours=hours, symbols=symbols, timeframes=timeframes,
        )
        return render_data_pipeline_root_cause_text(report)

    def clean_strategy_lab(
        self,
        hours: int = 24,
        symbols: list[str] | None = None,
        timeframe: str = "5m",
    ) -> str:
        from .clean_strategy_lab import (
            render_clean_strategy_lab_text,
            run_clean_strategy_lab,
        )
        report = run_clean_strategy_lab(
            self.config, self.db,
            hours=hours, timeframe=timeframe, symbols=symbols,
        )
        return render_clean_strategy_lab_text(report)

    def capital_scaling_simulator(
        self,
        base_clean_net_ev_pct: float = 0.0,
        base_clean_pf: float = 0.0,
        trades_per_window: int = 100,
        data_quality_status: str = "UNKNOWN",
        ohlcv_actionable: bool = False,
    ) -> str:
        from .capital_scaling_simulator import (
            render_capital_scaling_text,
            run_capital_scaling_simulator,
        )
        report = run_capital_scaling_simulator(
            base_clean_net_ev_pct=base_clean_net_ev_pct,
            base_clean_pf=base_clean_pf,
            trades_per_window=trades_per_window,
            data_quality_status=data_quality_status,
            ohlcv_actionable=ohlcv_actionable,
        )
        return render_capital_scaling_text(report)

    def research_pack_v7(self, hours: int = 24) -> str:
        from .research_pack_v7 import build_research_pack_v7, render_research_pack_v7_text
        payload = build_research_pack_v7(
            self.config, self.db,
            hours=min(int(hours), 24),
            include_strategy_lab=True,
            include_capital_scaling=True,
        )
        return render_research_pack_v7_text(payload)

    # ResearchOps V7.5 ------------------------------------------------------

    def duplicate_guard_hook_status(self) -> str:
        from .duplicate_guard_hook import (
            get_global_hook,
            render_duplicate_guard_hook_stats_text,
        )
        return render_duplicate_guard_hook_stats_text(get_global_hook().stats())

    def funding_cost_model(self, hours: int = 720) -> str:
        from .funding_cost_model import render_funding_summary_text, summarise_funding
        return render_funding_summary_text(summarise_funding(self.db, trades=[], hours=hours))

    def liquidation_model_bitget(
        self,
        symbol: str = "DOTUSDT",
        leverage: int = 5,
        capital_usdt: float = 40.0,
        margin_per_trade_usdt: float = 5.0,
    ) -> str:
        from .liquidation_model_bitget import (
            evaluate_liquidation,
            render_liquidation_text,
        )
        verdict = evaluate_liquidation(
            symbol=symbol, leverage=leverage,
            capital_usdt=capital_usdt,
            margin_per_trade_usdt=margin_per_trade_usdt,
        )
        return render_liquidation_text(verdict)

    def walk_forward_v2(
        self,
        hours: int = 720,
        timeframe: str = "5m",
        symbols: list[str] | None = None,
        train_days: int = 30,
        test_days: int = 7,
        step_days: int = 7,
    ) -> str:
        # WF V2 trabaja con la lista de trades reconstruida desde el
        # backtester. Si no hay datos en la DB, devuelve NEED_MORE_DATA.
        from .backtest_breakdown import collect_trade_records
        from .walk_forward_runner_v2 import (
            render_walk_forward_v2_text,
            run_walk_forward_v2,
        )
        records = collect_trade_records(
            self.config, self.db, hours=hours, symbols=symbols, timeframe=timeframe,
        )
        trades = [
            {
                "entry_time": getattr(r, "entry_time", "") or "",
                "net_return_pct": getattr(r, "net_return_pct", 0.0) or 0.0,
            }
            for r in records
        ]
        report = run_walk_forward_v2(
            trades=trades,
            train_days=train_days,
            test_days=test_days,
            step_days=step_days,
            symbols=symbols or [],
            timeframe=timeframe,
        )
        return render_walk_forward_v2_text(report)

    def research_pack_v7_5(self, hours: int = 24) -> str:
        from .research_pack_v7_5 import build_research_pack_v7_5, render_research_pack_v7_5_text
        payload = build_research_pack_v7_5(
            self.config, self.db, hours=min(int(hours), 24),
        )
        return render_research_pack_v7_5_text(payload)

    # ---- V8/V9 foundation CLI surface ----

    def auto_data_enrichment_status(
        self,
        hours: int = 24,
        timeframe: str = "5m",
        symbols: list[str] | None = None,
    ) -> str:
        from .auto_data_enrichment import summarise_enrichment
        from .phase8_research_utils import parse_symbols
        sym_list = parse_symbols(symbols, self.config) or ["BTCUSDT", "ETHUSDT", "DOTUSDT"]
        out = summarise_enrichment(self.db, symbols=sym_list, timeframe=timeframe, hours=int(hours))
        lines = ["AUTO DATA ENRICHMENT STATUS START"]
        lines.append(f"timeframe: {out['timeframe']}")
        lines.append(f"hours: {out['hours']}")
        for snap in out["snapshots"]:
            lines.append(
                f"symbol={snap['symbol']} overall={snap['overall_status']} "
                f"need_data={','.join(snap['need_data_reasons']) or 'none'}"
            )
        lines.append(f"symbols_ok: {','.join(out['symbols_ok']) or 'none'}")
        lines.append(f"symbols_partial: {','.join(out['symbols_partial']) or 'none'}")
        lines.append(f"symbols_need_data: {','.join(out['symbols_need_data']) or 'none'}")
        lines.append(f"research_only: {str(out['research_only']).lower()}")
        lines.append(f"final_recommendation: {out['final_recommendation']}")
        lines.append("AUTO DATA ENRICHMENT STATUS END")
        return "\n".join(lines)

    def exit_intelligence_lab(
        self,
        hours: int = 24,
        timeframe: str = "5m",
        symbols: list[str] | None = None,
    ) -> str:
        from .exit_intelligence_lab import run_exit_intelligence
        # Research-only: empty sample default; integrators may pass shadow trades.
        report = run_exit_intelligence([], hours=int(hours), timeframe=timeframe, symbols=symbols)
        lines = ["EXIT INTELLIGENCE LAB START"]
        lines.append(f"hours: {report.hours} timeframe: {report.timeframe}")
        lines.append(f"samples: {report.samples} need_more_data: {str(report.need_more_data).lower()}")
        lines.append(f"best_policy: {report.best_policy} best_delta_pct: {report.best_delta_pct:.4f}")
        for p in report.policies:
            lines.append(
                f"policy={p.policy} n={p.sample_count} avg_net={p.avg_net_pct:.4f} "
                f"delta={p.delta_net_vs_baseline_pct:.4f} time_deaths_pct={p.time_deaths_pct:.4f}"
            )
        lines.append(f"research_only: {str(report.research_only).lower()}")
        lines.append(f"final_recommendation: {report.final_recommendation}")
        lines.append("EXIT INTELLIGENCE LAB END")
        return "\n".join(lines)

    def strategy_experiment_registry_snapshot(self) -> str:
        from .strategy_experiment_registry import StrategyExperimentRegistry
        reg = StrategyExperimentRegistry()
        snap = reg.snapshot()
        lines = ["STRATEGY EXPERIMENT REGISTRY START"]
        lines.append(f"total: {snap['total']}")
        for state, count in snap["by_state"].items():
            lines.append(f"by_state {state}: {count}")
        lines.append(f"research_only: {str(snap['research_only']).lower()}")
        lines.append(f"final_recommendation: {snap['final_recommendation']}")
        lines.append("STRATEGY EXPERIMENT REGISTRY END")
        return "\n".join(lines)

    def shadow_candidate_lifecycle_status(self, hours: int = 24) -> str:
        from .shadow_candidate_lifecycle import summarise_lifecycle
        out = summarise_lifecycle([])
        lines = ["SHADOW CANDIDATE LIFECYCLE START"]
        lines.append(f"total: {out['total']}")
        for state, count in out["by_proposed_state"].items():
            lines.append(f"by_proposed_state {state}: {count}")
        lines.append(f"research_only: {str(out['research_only']).lower()}")
        lines.append(f"final_recommendation: {out['final_recommendation']}")
        lines.append("SHADOW CANDIDATE LIFECYCLE END")
        return "\n".join(lines)

    # ---- V8.1 Event Foundation CLI surface ----

    def _event_store(self):
        from .events.event_store import EventStore
        return EventStore()

    def event_catalyst_status(self) -> str:
        store = self._event_store()
        snap = store.snapshot()
        lines = ["EVENT CATALYST STATUS START"]
        lines.append(f"base_path: {snap['base_path']}")
        lines.append(f"raw_count: {snap['raw_count']}")
        lines.append(f"canonical_count: {snap['canonical_count']}")
        lines.append(f"candidates_count: {snap['candidates_count']}")
        lines.append(f"runs_count: {snap['runs_count']}")
        for fam, n in snap["by_family"].items():
            lines.append(f"by_family {fam}: {n}")
        for s, n in snap["by_status"].items():
            lines.append(f"by_status {s}: {n}")
        lines.append(f"research_only: {str(snap['research_only']).lower()}")
        lines.append(f"final_recommendation: {snap['final_recommendation']}")
        lines.append("EVENT CATALYST STATUS END")
        return "\n".join(lines)

    def listing_tracker_audit(self, hours: int = 720) -> str:
        from .events.listing_tracker import build_listing_audit
        window_days = max(1, int(hours) // 24)
        report = build_listing_audit(self.db, window_days=window_days)
        lines = ["LISTING TRACKER AUDIT START"]
        lines.append(f"window_days: {report.window_days}")
        lines.append(f"records: {len(report.records)}")
        if report.need_data_reasons:
            lines.append(f"need_data: {','.join(report.need_data_reasons)}")
        for r in report.records[:25]:
            lines.append(
                f"symbol={r['symbol']} venue={r['venue']} "
                f"age_days={r.get('age_days')} fdv_usd={r.get('fdv_usd')}"
            )
        lines.append(f"research_only: {str(report.research_only).lower()}")
        lines.append(f"final_recommendation: {report.final_recommendation}")
        lines.append("LISTING TRACKER AUDIT END")
        return "\n".join(lines)

    def unlock_watchlist_audit(self, hours: int = 1440) -> str:
        from .events.unlock_watchlist import build_unlock_audit
        window_days = max(1, int(hours) // 24)
        report = build_unlock_audit(self.db, window_days=window_days)
        lines = ["UNLOCK WATCHLIST AUDIT START"]
        lines.append(f"window_days: {report.window_days}")
        lines.append(f"records: {len(report.records)}")
        lines.append(f"conflicts: {len(report.conflicts)}")
        if report.need_data_reasons:
            lines.append(f"need_data: {','.join(report.need_data_reasons)}")
        for c in report.conflicts[:25]:
            lines.append(
                f"conflict token={c.get('token')} field={c.get('field')} "
                f"a={c.get('source_a')}:{c.get('value_a')} b={c.get('source_b')}:{c.get('value_b')}"
            )
        lines.append(f"research_only: {str(report.research_only).lower()}")
        lines.append(f"final_recommendation: {report.final_recommendation}")
        lines.append("UNLOCK WATCHLIST AUDIT END")
        return "\n".join(lines)

    def perp_availability_audit(self, symbols: list[str] | None = None) -> str:
        from .events.perp_availability_checker import (
            batch_check_perp_availability,
            summarise_perp_audit,
        )
        from .phase8_research_utils import parse_symbols
        sym_list = parse_symbols(symbols, self.config) or [
            "BTCUSDT", "ETHUSDT", "DOTUSDT",
        ]
        results = batch_check_perp_availability(self.db, symbols=sym_list)
        summary = summarise_perp_audit(results)
        lines = ["PERP AVAILABILITY AUDIT START"]
        lines.append(f"total: {summary['total']}")
        lines.append(f"with_perp_bitget: {summary['with_perp_bitget']}")
        lines.append(f"without_perp_bitget: {summary['without_perp_bitget']}")
        if summary["missing_methods"]:
            lines.append(f"missing_methods: {','.join(summary['missing_methods'])}")
        for r in results:
            lines.append(
                f"symbol={r.symbol} perp={r.perp_available_bitget} "
                f"perp_symbol={r.perp_symbol_bitget or '-'} venues={r.venue_count}"
            )
        lines.append(f"research_only: {str(summary['research_only']).lower()}")
        lines.append(f"final_recommendation: {summary['final_recommendation']}")
        lines.append("PERP AVAILABILITY AUDIT END")
        return "\n".join(lines)

    def shortability_score_audit(self, symbols: list[str] | None = None) -> str:
        from .events.perp_availability_checker import batch_check_perp_availability
        from .events.shortability_score import (
            batch_shortability,
            summarise_shortability,
        )
        from .phase8_research_utils import parse_symbols
        sym_list = parse_symbols(symbols, self.config) or [
            "BTCUSDT", "ETHUSDT", "DOTUSDT",
        ]
        perp = batch_check_perp_availability(self.db, symbols=sym_list)
        pairs = [(r.symbol, r.perp_available_bitget) for r in perp]
        results = batch_shortability(self.db, symbols_with_perp=pairs)
        summary = summarise_shortability(results)
        lines = ["SHORTABILITY SCORE AUDIT START"]
        lines.append(f"total: {summary['total']}")
        lines.append(f"ok: {summary['ok']}")
        lines.append(f"need_data: {summary['need_data']}")
        lines.append(f"no_perp: {summary['no_perp']}")
        for r in results:
            score = r.shortability_score
            score_s = f"{score:.4f}" if score is not None else "NA"
            lines.append(
                f"symbol={r.symbol} status={r.score_status} score={score_s}"
            )
        lines.append(f"research_only: {str(summary['research_only']).lower()}")
        lines.append(f"final_recommendation: {summary['final_recommendation']}")
        lines.append("SHORTABILITY SCORE AUDIT END")
        return "\n".join(lines)

    def event_candidate_registry_status(self) -> str:
        from .events.event_candidate_registry import summarise
        store = self._event_store()
        snap = summarise(store)
        lines = ["EVENT CANDIDATE REGISTRY STATUS START"]
        lines.append(f"total: {snap['candidates_count']}")
        for fam, n in snap["by_family"].items():
            lines.append(f"by_family {fam}: {n}")
        for s, n in snap["by_status"].items():
            lines.append(f"by_status {s}: {n}")
        lines.append(f"research_only: {str(snap['research_only']).lower()}")
        lines.append(f"final_recommendation: {snap['final_recommendation']}")
        lines.append("EVENT CANDIDATE REGISTRY STATUS END")
        return "\n".join(lines)

    def research_pack_event_v1(self, symbols: list[str] | None = None) -> str:
        from .events.research_pack_event_v1 import (
            build_event_pack_v1,
            render_event_pack_v1_text,
        )
        from .phase8_research_utils import parse_symbols
        sym_list = parse_symbols(symbols, self.config) or [
            "BTCUSDT", "ETHUSDT", "DOTUSDT",
        ]
        payload = build_event_pack_v1(
            self.config, self.db, sample_symbols=sym_list,
        )
        return render_event_pack_v1_text(payload)

    # ---- V8.2 Bidirectional Forensics + Campaign + Exit Lab ----

    @staticmethod
    def _v82_safety_footer() -> list[str]:
        """V8.2.1 — common safety footer for every V8.2 CLI command."""
        return [
            "research_only: true",
            "paper_filter_enabled: false",
            "can_send_real_orders: false",
            "final_recommendation: NO LIVE",
        ]

    @staticmethod
    def _v82_heavy_warning(hours: int) -> str | None:
        if int(hours) > 168:
            return f"heavy_window_warning: hours={hours} above 168; CLI proceeds, endpoints would SKIP_HEAVY"
        return None

    def bidirectional_funnel(self, hours: int = 168, side: str | None = None) -> str:
        from .labs.bidirectional_forensic_lab import build_funnel
        report = build_funnel(self.db, hours=int(hours), side_filter=side or None)
        lines = ["BIDIRECTIONAL FUNNEL START"]
        lines.append(f"hours: {report.hours} status: {report.status}")
        lines.append(f"total_signals: {report.total_signals}")
        for k, v in report.by_side.items():
            lines.append(f"by_side {k}: {v}")
        for k, v in report.by_regime.items():
            lines.append(f"by_regime {k}: {v}")
        for k, v in report.by_score_bucket.items():
            lines.append(f"by_score_bucket {k}: {v}")
        for k, v in report.by_reject_reason.items():
            lines.append(f"by_reject_reason {k}: {v}")
        for k, v in report.gross_ev_avg_by_side.items():
            lines.append(f"gross_ev_avg_by_side {k}: {v:.4f}")
        for k, v in report.net_ev_avg_by_side.items():
            lines.append(f"net_ev_avg_by_side {k}: {v:.4f}")
        if report.need_data_reasons:
            lines.append(f"need_data: {','.join(report.need_data_reasons)}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("BIDIRECTIONAL FUNNEL END")
        return "\n".join(lines)

    def missed_opportunities_cli(self, hours: int = 168, side: str = "SHORT") -> str:
        from .labs.bidirectional_forensic_lab import missed_opportunities
        report = missed_opportunities(self.db, side=side, hours=int(hours))
        lines = [f"MISSED OPPORTUNITIES {side.upper()} START"]
        lines.append(f"hours: {report.hours} status: {report.status} top_n: {report.top_n}")
        for cand in report.candidates[:20]:
            lines.append(
                f"symbol={cand.get('symbol')} regime={cand.get('regime')} score={cand.get('score')} "
                f"ret_1h={cand.get('ret_1h_pct')} would_have_worked={cand.get('would_have_worked_estimate')} reason={cand.get('reason')}"
            )
        if report.need_data_reasons:
            lines.append(f"need_data: {','.join(report.need_data_reasons)}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("MISSED OPPORTUNITIES END")
        return "\n".join(lines)

    def blocked_counterfactual_cli(self, hours: int = 168, side: str = "SHORT") -> str:
        from .labs.bidirectional_forensic_lab import blocked_that_would_have_worked
        report = blocked_that_would_have_worked(self.db, side=side, hours=int(hours))
        lines = [f"BLOCKED COUNTERFACTUAL {side.upper()} START"]
        lines.append(f"hours: {report.hours} status: {report.status}")
        for cand in report.candidates[:20]:
            lines.append(
                f"symbol={cand.get('symbol')} score={cand.get('score')} ret_1h={cand.get('ret_1h_pct')} "
                f"would_have_worked={cand.get('would_have_worked_estimate')} reason={cand.get('reason')}"
            )
        if report.need_data_reasons:
            lines.append(f"need_data: {','.join(report.need_data_reasons)}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("BLOCKED COUNTERFACTUAL END")
        return "\n".join(lines)

    def failed_executed_cli(self, hours: int = 168, side: str = "SHORT") -> str:
        from .labs.bidirectional_forensic_lab import failed_executed
        report = failed_executed(self.db, side=side, hours=int(hours))
        lines = [f"FAILED EXECUTED {side.upper()} START"]
        lines.append(f"hours: {report.hours} status: {report.status}")
        for f in report.failures[:20]:
            lines.append(
                f"symbol={f.get('symbol')} regime={f.get('regime')} outcome={f.get('outcome')} "
                f"realized={f.get('realized_pct')} mfe={f.get('mfe_pct')} reason={f.get('failure_reason')}"
            )
        if report.need_data_reasons:
            lines.append(f"need_data: {','.join(report.need_data_reasons)}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("FAILED EXECUTED END")
        return "\n".join(lines)

    def good_not_monetized_cli(self, hours: int = 168, side: str = "SHORT") -> str:
        from .labs.bidirectional_forensic_lab import good_not_monetized
        report = good_not_monetized(self.db, side=side, hours=int(hours))
        lines = [f"GOOD NOT MONETIZED {side.upper()} START"]
        lines.append(f"hours: {report.hours} status: {report.status}")
        for c in report.cases[:20]:
            lines.append(
                f"symbol={c.get('symbol')} realized={c.get('realized_pct')} mfe={c.get('mfe_pct')} "
                f"capture={c.get('mfe_capture_ratio')} cause={c.get('likely_cause')}"
            )
        if report.need_data_reasons:
            lines.append(f"need_data: {','.join(report.need_data_reasons)}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("GOOD NOT MONETIZED END")
        return "\n".join(lines)

    def score_asymmetry_audit_cli(self, hours: int = 168) -> str:
        from .labs.score_asymmetry_audit import audit
        r = audit(self.db, hours=int(hours))
        lines = ["SCORE ASYMMETRY AUDIT START"]
        lines.append(f"hours: {r.hours} status: {r.status}")
        lines.append(f"median_long: {r.median_long:.2f} median_short: {r.median_short:.2f}")
        lines.append(f"gap_long_minus_short: {r.gap_long_minus_short:.2f}")
        lines.append(f"long_pass%: {r.pct_long_pass_min_score * 100:.1f}")
        lines.append(f"short_pass%: {r.pct_short_pass_min_score * 100:.1f}")
        lines.append(f"long_in_bull n={r.long_in_bull.get('count', 0)}")
        lines.append(f"short_in_bear n={r.short_in_bear.get('count', 0)}")
        if r.need_data_reasons:
            lines.append(f"need_data: {','.join(r.need_data_reasons)}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("SCORE ASYMMETRY AUDIT END")
        return "\n".join(lines)

    def _score_sim_lines(self, header: str, r: Any) -> str:
        lines = [header]
        lines.append(f"hours: {r.hours} status: {r.status} name: {r.name}")
        lines.append(f"samples_long: {r.samples_long} samples_short: {r.samples_short}")
        lines.append(f"baseline_long_pass%: {r.baseline_long_pass_pct * 100:.1f}")
        lines.append(f"new_long_pass%: {r.new_long_pass_pct * 100:.1f}")
        lines.append(f"baseline_short_pass%: {r.baseline_short_pass_pct * 100:.1f}")
        lines.append(f"new_short_pass%: {r.new_short_pass_pct * 100:.1f}")
        lines.append(f"delta_long_pass: {r.delta_long_pass}")
        lines.append(f"delta_short_pass: {r.delta_short_pass}")
        if r.need_data_reasons:
            lines.append(f"need_data: {','.join(r.need_data_reasons)}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(r.hours)
        if warning:
            lines.append(warning)
        lines.append(header.replace("START", "END"))
        return "\n".join(lines)

    def score_symmetric_simulation_cli(self, hours: int = 168) -> str:
        from .labs.score_asymmetry_audit import simulate_symmetric_regime
        return self._score_sim_lines(
            "SCORE SYMMETRIC SIMULATION START", simulate_symmetric_regime(self.db, hours=int(hours))
        )

    def score_atr_softened_simulation_cli(self, hours: int = 168) -> str:
        from .labs.score_asymmetry_audit import simulate_atr_softening
        return self._score_sim_lines(
            "SCORE ATR SOFTENED SIMULATION START", simulate_atr_softening(self.db, hours=int(hours))
        )

    def score_high_vol_directional_simulation_cli(self, hours: int = 168) -> str:
        from .labs.score_asymmetry_audit import simulate_high_vol_directional
        return self._score_sim_lines(
            "SCORE HIGH VOL DIRECTIONAL SIMULATION START",
            simulate_high_vol_directional(self.db, hours=int(hours)),
        )

    def regime_router_simulation_cli(self, hours: int = 168) -> str:
        from .labs.regime_router_simulator import simulate_router
        r = simulate_router(self.db, hours=int(hours))
        lines = ["REGIME ROUTER SIMULATION START"]
        lines.append(f"hours: {r.hours} status: {r.status} samples: {r.samples}")
        for state, count in r.by_state.items():
            cov = r.coverage_pct.get(state, 0.0)
            lines.append(f"by_state {state}: {count} coverage%={cov * 100:.1f}")
        if r.need_data_reasons:
            lines.append(f"need_data: {','.join(r.need_data_reasons)}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("REGIME ROUTER SIMULATION END")
        return "\n".join(lines)

    def trend_campaign_sim_cli(self, hours: int = 168, side: str = "SHORT", max_adds: int = 3) -> str:
        from .labs.trend_campaign_simulator import run_campaign_simulation
        variants = tuple(sorted({0, 1, 2, 3, 5, 8, int(max_adds)}))
        r = run_campaign_simulation(self.db, side=side, hours=int(hours), max_adds_variants=variants)
        lines = [f"TREND CAMPAIGN SIM {side.upper()} START"]
        lines.append(f"hours: {r.hours} samples: {r.samples} status: {r.status}")
        lines.append(f"optimal_adds: {r.optimal_adds}")
        for v in r.variants:
            lines.append(
                f"adds_max={v.get('adds_max')} samples={v.get('samples')} net_ev={v.get('net_ev_avg_pct'):.4f} "
                f"pf={v.get('pf'):.2f} hit%={v.get('hit_rate') * 100:.1f} "
                f"avg_adds={v.get('avg_adds_executed'):.2f} high_risk={v.get('high_risk_flag')}"
            )
        for ins in r.insights[:10]:
            lines.append(f"insight: {ins}")
        if r.need_data_reasons:
            lines.append(f"need_data: {','.join(r.need_data_reasons)}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("TREND CAMPAIGN SIM END")
        return "\n".join(lines)

    def profit_lock_sim_cli(self, hours: int = 168, side: str = "SHORT", policy: str = "all") -> str:
        from .labs.profit_lock_simulator import (
            ALL_POLICIES,
            POLICY_BASELINE,
            run_profit_lock_simulation,
        )
        if policy and policy.lower() != "all":
            policies = [POLICY_BASELINE, policy]
        else:
            policies = list(ALL_POLICIES)
        r = run_profit_lock_simulation(self.db, side=side, hours=int(hours), policies=policies)
        lines = [f"PROFIT LOCK SIM {side.upper()} START"]
        lines.append(f"hours: {r.hours} samples: {r.samples} status: {r.status}")
        lines.append(f"baseline_policy: {r.baseline_policy}")
        lines.append(f"best_policy: {r.best_policy} best_delta_pct: {r.best_delta_pct:.4f}")
        for p in r.policies:
            lines.append(
                f"policy={p.get('policy')} samples={p.get('samples')} net_ev={p.get('net_ev_avg_pct'):.4f} "
                f"delta_net_ev={p.get('delta_net_ev_vs_baseline_pct'):.4f} "
                f"mfe_capture={p.get('avg_mfe_capture_pct'):.2f} tp%={p.get('tp_rate') * 100:.1f} "
                f"sl%={p.get('sl_rate') * 100:.1f} time%={p.get('time_rate') * 100:.1f}"
            )
        if r.need_data_reasons:
            lines.append(f"need_data: {','.join(r.need_data_reasons)}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("PROFIT LOCK SIM END")
        return "\n".join(lines)

    def research_pack_bidirectional_v1_cli(self, hours: int = 168) -> str:
        from .labs.research_pack_bidirectional_v1 import build_pack, render_pack_text
        payload = build_pack(self.db, hours=int(hours))
        text = render_pack_text(payload)
        warning = self._v82_heavy_warning(hours)
        if warning:
            text += "\n" + warning
        return text

    # ---- V8.2.4 Counterfactual Training Dataset CLI ----

    def future_returns_bridge_cli(
        self, hours: int = 168, side: str | None = None, top_n: int = 20,
    ) -> str:
        from .labs.future_returns_bridge import (
            batch_compute_future_returns,
            summarise_future_returns,
        )
        ok = hasattr(self.db, "fetch_signal_observations")
        if not ok:
            obs: list[dict] = []
        else:
            try:
                rows_raw = self.db.fetch_signal_observations(
                    hours=int(hours), side=side, limit=int(top_n) * 4,
                )
            except Exception:
                rows_raw = []
            obs = list(rows_raw)[: int(top_n) * 4]
        results = batch_compute_future_returns(self.db, observations=obs)
        summary = summarise_future_returns(results)
        lines = ["FUTURE RETURNS BRIDGE START"]
        lines.append(f"hours: {int(hours)} side: {side or 'ALL'} samples: {len(results)}")
        lines.append(
            f"summary: total={summary['total']} ok={summary['ok']} "
            f"partial={summary['partial']} need_data={summary['need_data']} "
            f"tp_first={summary['tp_first_count']} sl_first={summary['sl_first_count']} "
            f"time={summary['time_count']}"
        )
        for r in results[: int(top_n)]:
            lines.append(
                f"symbol={r.symbol} side={r.side} hit={r.first_barrier_hit} "
                f"mfe={r.mfe_pct} mae={r.mae_pct} ret_1h={r.returns_by_horizon_pct.get('60m')}"
            )
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("FUTURE RETURNS BRIDGE END")
        return "\n".join(lines)

    def edgeguard_counterfactual_cli(self, hours: int = 168, top_n: int = 20) -> str:
        from .labs.edgeguard_counterfactual_lab import analyze_edgeguard_blocks
        report = analyze_edgeguard_blocks(self.db, hours=int(hours), top_n=int(top_n))
        lines = ["EDGEGUARD COUNTERFACTUAL START"]
        lines.append(f"hours: {report.hours} status: {report.status}")
        lines.append(f"total_edgeguard_blocks: {report.total_edgeguard_blocks}")
        lines.append(f"estimated_winners: {report.estimated_winners}")
        lines.append(f"estimated_losers: {report.estimated_losers}")
        lines.append(f"need_data: {report.need_data}")
        lines.append(f"gross_ev_avg_pct: {report.gross_ev_avg_pct:.4f}")
        lines.append(f"net_ev_avg_pct: {report.net_ev_avg_pct:.4f}")
        for k, v in report.blocks_by_side.items():
            lines.append(f"by_side {k}: {v}")
        for k, v in report.blocks_by_reason.items():
            lines.append(f"by_reason {k}: {v}")
        for o in report.top_blocked_winners[: int(top_n)]:
            lines.append(
                f"WINNER symbol={o.get('symbol')} side={o.get('side')} "
                f"net={o.get('net_ev_est_pct')} reason={o.get('edgeguard_reason')}"
            )
        for o in report.top_blocked_losers[: int(top_n)]:
            lines.append(
                f"LOSER symbol={o.get('symbol')} side={o.get('side')} "
                f"net={o.get('net_ev_est_pct')} reason={o.get('edgeguard_reason')}"
            )
        if report.need_data_reasons:
            lines.append(f"need_data: {','.join(report.need_data_reasons)}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("EDGEGUARD COUNTERFACTUAL END")
        return "\n".join(lines)

    def counterfactual_training_dataset_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.counterfactual_training_dataset import build_dataset
        _, summary = build_dataset(self.db, hours=int(hours), limit=int(limit))
        return self._render_training_summary(summary, hours=hours, limit=limit)

    def export_counterfactual_training_dataset_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.counterfactual_training_dataset import build_dataset, export_dataset
        dataset, summary = build_dataset(self.db, hours=int(hours), limit=int(limit))
        manifest = export_dataset(dataset, summary)
        lines = ["EXPORT COUNTERFACTUAL TRAINING DATASET START"]
        lines.append(f"hours: {int(hours)} limit: {int(limit)} rows: {summary.total_rows}")
        lines.append(f"base_dir: {manifest.get('base_dir')}")
        for f in manifest.get("files") or []:
            lines.append(f"file: {f.get('name')} size={f.get('size_bytes')} sha1={f.get('sha1')}")
        z = manifest.get("zip")
        if z:
            lines.append(f"zip: {z.get('name')} size={z.get('size_bytes')} sha1={z.get('sha1')}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("EXPORT COUNTERFACTUAL TRAINING DATASET END")
        return "\n".join(lines)

    def training_dataset_summary_cli(self, hours: int = 168) -> str:
        from .labs.counterfactual_training_dataset import build_dataset
        _, summary = build_dataset(self.db, hours=int(hours), limit=50000)
        return self._render_training_summary(summary, hours=hours, limit=50000)

    # ---- V8.2.5 Counterfactual Quality CLI ----

    def counterfactual_dedup_audit_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.counterfactual_dedup_audit import audit_dedup
        r = audit_dedup(self.db, hours=int(hours), limit=int(limit))
        lines = ["COUNTERFACTUAL DEDUP AUDIT START"]
        lines.append(f"hours: {r.hours} status: {r.status}")
        lines.append(f"total_rows: {r.total_rows}")
        lines.append(f"evaluable_rows: {r.evaluable_rows}")
        lines.append(f"duplicate_rows: {r.duplicate_rows}")
        lines.append(f"unique_outcomes: {r.unique_outcomes}")
        lines.append(f"duplicate_ratio: {r.duplicate_ratio:.4f}")
        for k, v in r.raw_metrics.items():
            lines.append(f"raw_{k}: {v}")
        for k, v in r.dedup_metrics.items():
            lines.append(f"dedup_{k}: {v}")
        for entry in r.inflated_symbols[:10]:
            lines.append(
                f"INFLATED symbol={entry['symbol']} raw_net_ev={entry['raw_net_ev']:.4f} "
                f"dedup_net_ev={entry['dedup_net_ev']:.4f} inflation_factor={entry['inflation_factor']:.2f}"
            )
        for entry in r.top_duplicate_fingerprints[:10]:
            lines.append(
                f"DUP fingerprint={entry['fingerprint']} count={entry['count']} "
                f"symbol={entry['symbol']} side={entry['side']}"
            )
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("COUNTERFACTUAL DEDUP AUDIT END")
        return "\n".join(lines)

    def short_sign_barrier_audit_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.short_sign_barrier_audit import audit_short_sign
        r = audit_short_sign(self.db, hours=int(hours), limit=int(limit))
        lines = ["SHORT SIGN BARRIER AUDIT START"]
        lines.append(f"hours: {r.hours} status: {r.status}")
        lines.append(f"total_short_rows: {r.total_short_rows}")
        lines.append(f"evaluable_short_rows: {r.evaluable_short_rows}")
        lines.append(f"suspicious_short_rows: {r.suspicious_short_rows}")
        lines.append(f"suspicious_ratio: {r.suspicious_ratio:.4f}")
        lines.append(f"verdict: {r.verdict}")
        for k, v in r.by_classification.items():
            lines.append(f"by_classification {k}: {v}")
        for c in r.examples_top_50[:20]:
            lines.append(
                f"SUS symbol={c.get('symbol')} ret_4h={c.get('ret_4h_pct')} "
                f"mfe={c.get('mfe_pct')} mae={c.get('mae_pct')} "
                f"barrier={c.get('first_barrier_hit')} class={c.get('classification')}"
            )
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("SHORT SIGN BARRIER AUDIT END")
        return "\n".join(lines)

    def score_calibration_audit_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.score_calibration_audit import audit_score_calibration
        r = audit_score_calibration(self.db, hours=int(hours), limit=int(limit))
        lines = ["SCORE CALIBRATION AUDIT START"]
        lines.append(f"hours: {r.hours} status: {r.status}")
        lines.append(f"samples: {r.samples}")
        lines.append(f"monotonicity_status: {r.monotonicity_status}")
        lines.append(f"corr_score_vs_net_pnl: {r.correlation_score_vs_net_pnl:.4f}")
        lines.append(f"corr_score_vs_win: {r.correlation_score_vs_win:.4f}")
        for b in r.score_bucket_table:
            lines.append(
                f"bucket={b['bucket']} n={b['count']} winrate={b['winrate']:.4f} "
                f"net_ev={b['net_ev_avg_pct']:.4f}"
            )
        for w in r.warnings:
            lines.append(f"warning: {w}")
        if r.recommendation:
            lines.append(f"recommendation: {r.recommendation}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("SCORE CALIBRATION AUDIT END")
        return "\n".join(lines)

    def counterfactual_cost_stress_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.counterfactual_cost_stress import stress_costs
        r = stress_costs(self.db, hours=int(hours), limit=int(limit))
        lines = ["COUNTERFACTUAL COST STRESS START"]
        lines.append(f"hours: {r.hours} status: {r.status} samples: {r.samples}")
        lines.append(f"dedup_used: {r.dedup_used}")
        for entry in r.by_cost_level:
            lines.append(
                f"cost={entry['cost_pct']:.2f}% net_ev={entry['net_ev_avg_pct']:.4f} "
                f"survives={entry['survives']}"
            )
        lines.append(f"surviving_groups: {len(r.surviving_groups)}")
        lines.append(f"optimistic_only_groups: {len(r.optimistic_only_groups)}")
        for g in r.surviving_groups[:10]:
            lines.append(
                f"SURVIVE side={g.get('side')} symbol={g.get('symbol')} "
                f"regime={g.get('regime')} count={g.get('count')}"
            )
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("COUNTERFACTUAL COST STRESS END")
        return "\n".join(lines)

    def export_counterfactual_clean_v2_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.counterfactual_clean_export_v2 import export_clean_v2
        manifest = export_clean_v2(self.db, hours=int(hours), limit=int(limit))
        lines = ["EXPORT COUNTERFACTUAL CLEAN V2 START"]
        lines.append(f"hours: {int(hours)} limit: {int(limit)}")
        lines.append(f"base_dir: {manifest.get('base_dir')}")
        for f in manifest.get("files") or []:
            lines.append(f"file: {f.get('name')} size={f.get('size_bytes')} sha1={f.get('sha1')}")
        z = manifest.get("zip")
        if z:
            lines.append(f"zip: {z.get('name')} size={z.get('size_bytes')} sha1={z.get('sha1')}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("EXPORT COUNTERFACTUAL CLEAN V2 END")
        return "\n".join(lines)

    def research_pack_counterfactual_quality_v1_cli(
        self, hours: int = 168, limit: int = 50000,
    ) -> str:
        from .labs.counterfactual_clean_export_v2 import build_pack, render_pack_text
        payload = build_pack(self.db, hours=int(hours), limit=int(limit))
        text = render_pack_text(payload)
        warning = self._v82_heavy_warning(hours)
        if warning:
            text += "\n" + warning
        return text

    # ---- V8.2.6 Candidate Rule Miner + WF + Short Debug + Score Sandbox ----

    def candidate_rule_miner_v826_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.candidate_rule_miner_v8_2_6 import mine_candidate_rules
        from .labs.score_recalibration_sandbox_v8_2_6 import sandbox_recalibration
        from .labs.short_barrier_debug_v8_2_6 import debug_short_barriers
        from .labs.counterfactual_training_dataset import build_dataset
        dataset, _ = build_dataset(self.db, hours=int(hours), limit=int(limit))
        short = debug_short_barriers(self.db, hours=hours, limit=limit, rows=dataset)
        recal = sandbox_recalibration(self.db, hours=hours, limit=limit, rows=dataset)
        score_ok = recal.old_monotonicity == "PASS"
        r = mine_candidate_rules(
            self.db, hours=hours, limit=limit, rows=dataset,
            short_verdict=short.verdict, score_calibration_ok=score_ok,
        )
        lines = ["CANDIDATE RULE MINER V8.2.6 START"]
        lines.append(f"hours: {r.hours} status: {r.status}")
        lines.append(f"short_verdict: {r.short_verdict} short_excluded: {r.short_excluded}")
        lines.append(f"total_rules: {r.total_rules}")
        for status, count in r.by_status.items():
            lines.append(f"by_status {status}: {count}")
        for rule in r.candidate_rules[:20]:
            lines.append(
                f"CANDIDATE {rule.get('rule_id')} n={rule.get('samples')} "
                f"net_ev={rule.get('net_ev_avg_pct'):.4f} pf={rule.get('pf'):.2f} "
                f"status={rule.get('rule_status')} reason={rule.get('rule_reason')}"
            )
        for rule in r.watch_only_rules[:10]:
            lines.append(
                f"WATCH {rule.get('rule_id')} n={rule.get('samples')} "
                f"net_ev={rule.get('net_ev_avg_pct'):.4f} reason={rule.get('rule_reason')}"
            )
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("CANDIDATE RULE MINER V8.2.6 END")
        return "\n".join(lines)

    def candidate_rule_walkforward_v826_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.candidate_rule_miner_v8_2_6 import mine_candidate_rules
        from .labs.candidate_rule_walkforward_v8_2_6 import run_walkforward
        from .labs.score_recalibration_sandbox_v8_2_6 import sandbox_recalibration
        from .labs.short_barrier_debug_v8_2_6 import debug_short_barriers
        from .labs.counterfactual_training_dataset import build_dataset
        dataset, _ = build_dataset(self.db, hours=int(hours), limit=int(limit))
        short = debug_short_barriers(self.db, hours=hours, limit=limit, rows=dataset)
        recal = sandbox_recalibration(self.db, hours=hours, limit=limit, rows=dataset)
        score_ok = recal.old_monotonicity == "PASS"
        miner = mine_candidate_rules(
            self.db, hours=hours, limit=limit, rows=dataset,
            short_verdict=short.verdict, score_calibration_ok=score_ok,
        )
        candidates = miner.candidate_rules + miner.watch_only_rules
        wf = run_walkforward(
            self.db, hours=hours, limit=limit, rows=dataset, rules=candidates,
        )
        lines = ["CANDIDATE RULE WALKFORWARD V8.2.6 START"]
        lines.append(f"hours: {wf.hours} status: {wf.status}")
        lines.append(f"rules_evaluated: {wf.rules_evaluated}")
        for dec, count in wf.by_decision.items():
            lines.append(f"by_decision {dec}: {count}")
        for r in wf.results[:20]:
            lines.append(
                f"WF {r.get('rule_id')} total={r.get('total_samples')} "
                f"train_ev={r.get('train_net_ev_pct'):.4f} "
                f"test_ev={r.get('test_net_ev_pct'):.4f} "
                f"decision={r.get('decision')} reason={r.get('reason')}"
            )
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("CANDIDATE RULE WALKFORWARD V8.2.6 END")
        return "\n".join(lines)

    def short_barrier_debug_v826_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.short_barrier_debug_v8_2_6 import debug_short_barriers
        r = debug_short_barriers(self.db, hours=int(hours), limit=int(limit))
        lines = ["SHORT BARRIER DEBUG V8.2.6 START"]
        lines.append(f"hours: {r.hours} status: {r.status}")
        lines.append(f"total_short_rows: {r.total_short_rows}")
        lines.append(f"evaluable_short_rows: {r.evaluable_short_rows}")
        lines.append(f"trusted_count: {r.trusted_count}")
        lines.append(f"legitimate_stop_before_drop: {r.legitimate_stop_before_drop}")
        lines.append(f"possible_sign_bug: {r.possible_sign_bug}")
        lines.append(f"possible_barrier_bug: {r.possible_barrier_bug}")
        lines.append(f"same_bar_ambiguous: {r.same_bar_ambiguous}")
        lines.append(f"needs_path: {r.needs_path}")
        lines.append(f"verdict: {r.verdict}")
        for c in r.examples_top_100[:20]:
            lines.append(
                f"CASE symbol={c.get('symbol')} class={c.get('classification')} "
                f"barrier_inv={c.get('barrier_inverted')} orient_ok={c.get('mfe_mae_orientation_ok')} "
                f"same_bar={c.get('same_bar_suspected')}"
            )
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("SHORT BARRIER DEBUG V8.2.6 END")
        return "\n".join(lines)

    def score_recalibration_sandbox_v826_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.score_recalibration_sandbox_v8_2_6 import sandbox_recalibration
        r = sandbox_recalibration(self.db, hours=int(hours), limit=int(limit))
        lines = ["SCORE RECALIBRATION SANDBOX V8.2.6 START"]
        lines.append(f"hours: {r.hours} status: {r.status} samples: {r.samples}")
        lines.append(f"old_correlation: {r.old_correlation:.4f}")
        lines.append(f"recalibrated_correlation: {r.recalibrated_correlation:.4f}")
        lines.append(f"delta_correlation: {r.delta_correlation:.4f}")
        lines.append(f"old_monotonicity: {r.old_monotonicity}")
        lines.append(f"recalibrated_monotonicity: {r.recalibrated_monotonicity}")
        lines.append(f"recommendation: {r.recommendation}")
        for bucket, rec_score in r.bucket_to_recalibrated_score.items():
            lines.append(f"bucket_to_recalibrated_score {bucket}: {rec_score:.4f}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("SCORE RECALIBRATION SANDBOX V8.2.6 END")
        return "\n".join(lines)

    def export_research_v826_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.research_export_v8_2_6 import export_research_v826
        manifest = export_research_v826(self.db, hours=int(hours), limit=int(limit))
        lines = ["EXPORT RESEARCH V8.2.6 START"]
        lines.append(f"hours: {int(hours)} limit: {int(limit)}")
        lines.append(f"base_dir: {manifest.get('base_dir')}")
        for f in manifest.get("files") or []:
            lines.append(f"file: {f.get('name')} size={f.get('size_bytes')} sha1={f.get('sha1')}")
        z = manifest.get("zip")
        if z:
            lines.append(f"zip: {z.get('name')} size={z.get('size_bytes')} sha1={z.get('sha1')}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("EXPORT RESEARCH V8.2.6 END")
        return "\n".join(lines)

    def research_pack_v826_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.research_export_v8_2_6 import build_pack_v826, render_pack_v826_text
        payload = build_pack_v826(self.db, hours=int(hours), limit=int(limit))
        text = render_pack_v826_text(payload)
        warning = self._v82_heavy_warning(hours)
        if warning:
            text += "\n" + warning
        return text

    # ---- V8.2.7 Strict OOS + Final Gate + Short Verdict Fix ----

    def strict_oos_rule_selector_v827_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.short_barrier_debug_v8_2_7 import debug_short_barriers_v827
        from .labs.strict_oos_rule_selector_v8_2_7 import select_rules_strict_oos
        from .labs.score_calibration_audit import audit_score_calibration
        from .labs.counterfactual_training_dataset import build_dataset
        dataset, _ = build_dataset(self.db, hours=int(hours), limit=int(limit))
        short = debug_short_barriers_v827(self.db, hours=hours, limit=limit, rows=dataset)
        recal = audit_score_calibration(self.db, hours=hours, limit=limit, rows=dataset)
        score_ok = recal.monotonicity_status == "PASS"
        r = select_rules_strict_oos(
            self.db, hours=hours, limit=limit, rows=dataset,
            short_verdict=short.verdict, score_calibration_ok=score_ok,
        )
        lines = ["STRICT OOS RULE SELECTOR V8.2.7 START"]
        lines.append(f"hours: {r.hours} status: {r.status}")
        lines.append(f"short_verdict: {r.short_verdict} short_excluded: {r.short_excluded}")
        lines.append(f"score_calibration_ok: {r.score_calibration_ok}")
        lines.append(f"total_dataset_rows: {r.total_dataset_rows}")
        lines.append(f"evaluable_rows: {r.evaluable_rows}")
        lines.append(f"split: train={r.train_size} validation={r.validation_size} test={r.test_size}")
        lines.append(f"total_rules_evaluated: {r.total_rules_evaluated}")
        for gate, count in r.by_final_gate.items():
            lines.append(f"by_final_gate {gate}: {count}")
        for rule in r.paper_sandbox_candidates[:10]:
            lines.append(
                f"PAPER_SANDBOX {rule.get('rule_id')} train_n={rule.get('train_samples')} "
                f"test_n={rule.get('test_samples')} test_ev={rule.get('test_net_ev_pct'):.4f} "
                f"test_pf={rule.get('test_pf'):.2f} degr={rule.get('degradation_train_to_test_pct'):.2f}"
            )
        for rule in r.research_candidates[:10]:
            lines.append(
                f"RESEARCH {rule.get('rule_id')} reason={rule.get('reject_reason')}"
            )
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("STRICT OOS RULE SELECTOR V8.2.7 END")
        return "\n".join(lines)

    def short_barrier_debug_v827_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.short_barrier_debug_v8_2_7 import debug_short_barriers_v827
        r = debug_short_barriers_v827(self.db, hours=int(hours), limit=int(limit))
        lines = ["SHORT BARRIER DEBUG V8.2.7 START"]
        lines.append(f"hours: {r.hours} status: {r.status}")
        lines.append(f"total_short_rows: {r.total_short_rows}")
        lines.append(f"evaluable_short_rows: {r.evaluable_short_rows}")
        lines.append(f"trusted_count: {r.trusted_count}")
        lines.append(f"legitimate_stop_before_drop: {r.legitimate_stop_before_drop}")
        lines.append(f"possible_sign_bug: {r.possible_sign_bug}")
        lines.append(f"possible_barrier_bug: {r.possible_barrier_bug}")
        lines.append(f"same_bar_ambiguous: {r.same_bar_ambiguous}")
        lines.append(f"needs_path: {r.needs_path}")
        lines.append(f"suspicious_ratio: {r.suspicious_ratio:.4f}")
        lines.append(f"sign_bug_ratio: {r.sign_bug_ratio:.4f}")
        lines.append(f"barrier_bug_ratio: {r.barrier_bug_ratio:.4f}")
        lines.append(f"same_bar_ratio: {r.same_bar_ratio:.4f}")
        lines.append(f"verdict: {r.verdict}")
        for c in r.examples_top_100[:20]:
            lines.append(
                f"CASE symbol={c.get('symbol')} class={c.get('classification')} "
                f"ret_4h={c.get('ret_4h_pct')} mfe={c.get('mfe_pct')} mae={c.get('mae_pct')}"
            )
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("SHORT BARRIER DEBUG V8.2.7 END")
        return "\n".join(lines)

    def final_rule_gate_v827_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.final_rule_gate_v8_2_7 import run_final_gate
        r = run_final_gate(self.db, hours=int(hours), limit=int(limit))
        lines = ["FINAL RULE GATE V8.2.7 START"]
        lines.append(f"hours: {r.hours} status: {r.status}")
        lines.append(f"short_verdict: {r.short_verdict}")
        lines.append(f"score_monotonicity: {r.score_monotonicity}")
        lines.append(f"duplicate_ratio: {r.duplicate_ratio:.4f}")
        lines.append(f"total_rules_mined: {r.total_rules_mined}")
        lines.append(f"rejected: {r.rejected}")
        lines.append(f"watch_only: {r.watch_only}")
        lines.append(f"research_candidates: {r.research_candidates}")
        lines.append(f"paper_sandbox_candidates: {r.paper_sandbox_candidates}")
        lines.append(f"need_more_data: {r.need_more_data}")
        if r.no_paper_candidates_marker:
            lines.append(f"no_paper_candidates_marker: {r.no_paper_candidates_marker}")
        for entry in r.reasons_top[:10]:
            lines.append(f"reason {entry['reason']}: {entry['count']}")
        for rule in r.paper_sandbox_rules[:5]:
            lines.append(f"PAPER_SANDBOX {rule.get('rule_id')}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("FINAL RULE GATE V8.2.7 END")
        return "\n".join(lines)

    def export_research_v827_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.research_export_v8_2_7 import export_research_v827
        manifest = export_research_v827(self.db, hours=int(hours), limit=int(limit))
        lines = ["EXPORT RESEARCH V8.2.7 START"]
        lines.append(f"hours: {int(hours)} limit: {int(limit)}")
        lines.append(f"base_dir: {manifest.get('base_dir')}")
        for f in manifest.get("files") or []:
            lines.append(f"file: {f.get('name')} size={f.get('size_bytes')} sha1={f.get('sha1')}")
        z = manifest.get("zip")
        if z:
            lines.append(f"zip: {z.get('name')} size={z.get('size_bytes')} sha1={z.get('sha1')}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("EXPORT RESEARCH V8.2.7 END")
        return "\n".join(lines)

    def research_pack_v827_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.research_export_v8_2_7 import build_pack_v827, render_pack_v827_text
        payload = build_pack_v827(self.db, hours=int(hours), limit=int(limit))
        text = render_pack_v827_text(payload)
        warning = self._v82_heavy_warning(hours)
        if warning:
            text += "\n" + warning
        return text

    # ---- V8.2.8 Dual-Side Root Cause Fix ----

    def dual_side_barrier_audit_v828_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.dual_side_barrier_truth_audit_v8_2_8 import audit_dual_side_barriers
        r = audit_dual_side_barriers(self.db, hours=int(hours), limit=int(limit))
        lines = ["DUAL SIDE BARRIER AUDIT V8.2.8 START"]
        lines.append(f"hours: {r.hours} status: {r.status}")
        lines.append(f"long_verdict: {r.long_verdict}")
        lines.append(f"short_verdict: {r.short_verdict}")
        for side_label, metrics in (("LONG", r.long_metrics), ("SHORT", r.short_metrics)):
            lines.append(
                f"{side_label} samples={metrics.total_rows} evaluable={metrics.evaluable_rows} "
                f"suspicious_ratio={metrics.suspicious_ratio:.4f} "
                f"sign_bug_ratio={metrics.sign_bug_ratio:.4f} "
                f"barrier_bug_ratio={metrics.barrier_bug_ratio:.4f}"
            )
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("DUAL SIDE BARRIER AUDIT V8.2.8 END")
        return "\n".join(lines)

    def duplicate_root_cause_v828_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.duplicate_source_root_cause_v8_2_8 import audit_duplicate_root_cause
        r = audit_duplicate_root_cause(self.db, hours=int(hours), limit=int(limit))
        lines = ["DUPLICATE ROOT CAUSE V8.2.8 START"]
        lines.append(f"hours: {r.hours} status: {r.status}")
        lines.append(f"evaluable_rows: {r.evaluable_rows}")
        lines.append(f"duplicate_ratio: {r.duplicate_ratio:.4f}")
        for cause, count in r.by_root_cause.items():
            lines.append(f"root_cause {cause}: {count}")
        for fix in r.proposed_fixes:
            lines.append(f"proposed_fix: {fix}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("DUPLICATE ROOT CAUSE V8.2.8 END")
        return "\n".join(lines)

    def side_aware_score_calibration_v828_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.side_aware_score_calibration_v8_2_8 import calibrate_score_by_side
        r = calibrate_score_by_side(self.db, hours=int(hours), limit=int(limit))
        lines = ["SIDE AWARE SCORE CALIBRATION V8.2.8 START"]
        lines.append(f"hours: {r.hours} status: {r.status} samples: {r.samples}")
        lines.append(f"global_corr_net: {r.global_correlation_score_vs_net_pnl:.4f}")
        lines.append(f"score_usable_long: {r.score_usable_long}")
        lines.append(f"score_usable_short: {r.score_usable_short}")
        lines.append(f"long_usefulness: {r.long_block.usefulness}")
        lines.append(f"short_usefulness: {r.short_block.usefulness}")
        lines.append(f"score_only_diagnostic: {r.score_only_diagnostic}")
        lines.append(f"score_excluded_as_gate: {r.score_excluded_as_gate}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("SIDE AWARE SCORE CALIBRATION V8.2.8 END")
        return "\n".join(lines)

    def rebound_regime_turn_lab_v828_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.rebound_regime_turn_lab_v8_2_8 import detect_rebound_setups
        r = detect_rebound_setups(self.db, hours=int(hours), limit=int(limit))
        lines = ["REBOUND REGIME TURN LAB V8.2.8 START"]
        lines.append(f"hours: {r.hours} status: {r.status}")
        lines.append(f"rebound_candidates_count: {r.rebound_candidates_count}")
        lines.append(f"rebound_good_count: {r.rebound_good_count}")
        lines.append(f"rebound_bad_count: {r.rebound_bad_count}")
        lines.append(f"net_ev_est_pct: {r.net_ev_est_pct:.4f}")
        lines.append(f"readiness: {r.readiness}")
        lines.append(f"detection_mode: {r.report_detection_mode}")
        lines.append(
            f"used_future_return_features: "
            f"{str(bool(r.used_future_return_features)).lower()}"
        )
        lines.append(f"prefix_only_count: {r.prefix_only_count}")
        lines.append(f"need_data_count: {r.need_data_count}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("REBOUND REGIME TURN LAB V8.2.8 END")
        return "\n".join(lines)

    def export_research_v828_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.research_export_v8_2_8 import export_research_v828
        manifest = export_research_v828(self.db, hours=int(hours), limit=int(limit))
        lines = ["EXPORT RESEARCH V8.2.8 START"]
        lines.append(f"hours: {int(hours)} limit: {int(limit)}")
        lines.append(f"base_dir: {manifest.get('base_dir')}")
        for f in manifest.get("files") or []:
            lines.append(f"file: {f.get('name')} size={f.get('size_bytes')} sha1={f.get('sha1')}")
        z = manifest.get("zip")
        if z:
            lines.append(f"zip: {z.get('name')} size={z.get('size_bytes')} sha1={z.get('sha1')}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("EXPORT RESEARCH V8.2.8 END")
        return "\n".join(lines)

    def research_pack_v828_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.research_export_v8_2_8 import build_pack_v828, render_pack_v828_text
        payload = build_pack_v828(self.db, hours=int(hours), limit=int(limit))
        text = render_pack_v828_text(payload)
        warning = self._v82_heavy_warning(hours)
        if warning:
            text += "\n" + warning
        return text

    # ---- V8.2.9 Rebound LONG Strict OOS + Exit Monetization ----

    def rebound_long_candidates_v829_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.rebound_long_candidate_extractor_v8_2_9 import (
            extract_rebound_long_candidates,
        )
        r = extract_rebound_long_candidates(self.db, hours=int(hours), limit=int(limit))
        lines = ["REBOUND LONG CANDIDATES V8.2.9 START"]
        lines.append(f"hours: {r.hours} status: {r.status}")
        lines.append(f"raw_signals: {r.raw_signals}")
        lines.append(f"long_signals: {r.long_signals}")
        lines.append(f"candidates_count: {r.candidates_count}")
        lines.append(f"prefix_only_count: {r.prefix_only_count}")
        lines.append(f"need_data_count: {r.need_data_count}")
        lines.append(
            f"used_future_return_features: "
            f"{str(bool(r.used_future_return_features)).lower()}"
        )
        for k, v in r.by_candidate_reason.items():
            lines.append(f"by_reason {k}: {v}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("REBOUND LONG CANDIDATES V8.2.9 END")
        return "\n".join(lines)

    def edgeguard_repeat_dedup_v829_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.counterfactual_training_dataset import build_dataset
        from .labs.edgeguard_repeat_dedup_v8_2_9 import dedup_edgeguard_repeats
        dataset, _ = build_dataset(self.db, hours=int(hours), limit=int(limit))
        _, report = dedup_edgeguard_repeats(dataset, hours=int(hours))
        lines = ["EDGEGUARD REPEAT DEDUP V8.2.9 START"]
        lines.append(f"hours: {report.hours} status: {report.status}")
        lines.append(f"raw_rows: {report.raw_rows}")
        lines.append(f"dedup_rows: {report.dedup_rows}")
        lines.append(
            f"duplicate_ratio_before: {report.duplicate_ratio_before:.4f}"
        )
        lines.append(
            f"duplicate_ratio_after: {report.duplicate_ratio_after:.4f}"
        )
        lines.append(
            f"edgeguard_repeat_blocks_removed: "
            f"{report.edgeguard_repeat_blocks_removed}"
        )
        lines.append(
            f"unique_independent_candidates: "
            f"{report.unique_independent_candidates}"
        )
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("EDGEGUARD REPEAT DEDUP V8.2.9 END")
        return "\n".join(lines)

    def score_gate_sandbox_v829_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.rebound_long_candidate_extractor_v8_2_9 import (
            extract_rebound_long_candidates,
        )
        from .labs.score_gate_sandbox_v8_2_9 import run_score_gate_sandbox
        extractor = extract_rebound_long_candidates(
            self.db, hours=int(hours), limit=int(limit),
        )
        sandbox = run_score_gate_sandbox(
            extractor.candidates, hours=int(hours),
            score_anti_calibrated=True,
        )
        lines = ["SCORE GATE SANDBOX V8.2.9 START"]
        lines.append(f"hours: {sandbox.hours} status: {sandbox.status}")
        lines.append(f"candidates_total: {sandbox.candidates_total}")
        lines.append(
            f"score_anti_calibrated_input: "
            f"{str(sandbox.score_anti_calibrated_input).lower()}"
        )
        lines.append(
            f"score_used_as_positive_gate: "
            f"{str(sandbox.score_used_as_positive_gate).lower()}"
        )
        lines.append(f"best_variant: {sandbox.best_variant or 'NONE'}")
        for v in sandbox.variants:
            lines.append(
                f"variant {v['variant']}: samples={v['samples']} "
                f"winrate={v['winrate']:.2f} oos={v['oos_status']}"
            )
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("SCORE GATE SANDBOX V8.2.9 END")
        return "\n".join(lines)

    def exit_monetization_audit_v829_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.exit_monetization_audit_v8_2_9 import (
            run_exit_monetization_audit,
        )
        r = run_exit_monetization_audit(
            self.db, hours=int(hours), limit=int(limit),
        )
        lines = ["EXIT MONETIZATION AUDIT V8.2.9 START"]
        lines.append(f"hours: {r.hours} status: {r.status}")
        lines.append(f"rows_audited: {r.rows_audited}")
        lines.append(f"rows_with_outcome: {r.rows_with_outcome}")
        lines.append(f"horizon_close_count: {r.horizon_close_count}")
        lines.append(
            f"horizon_close_with_high_mfe: {r.horizon_close_with_high_mfe}"
        )
        lines.append(
            f"horizon_close_problem_detected: "
            f"{str(r.horizon_close_problem_detected).lower()}"
        )
        lines.append(
            f"avg_profit_capture_ratio: {r.avg_profit_capture_ratio:.4f}"
        )
        lines.append(
            f"avg_missed_profit_pct: {r.avg_missed_profit_pct:.4f}"
        )
        lines.append(f"best_policy: {r.best_policy}")
        lines.append(f"best_policy_test_status: {r.best_policy_test_status}")
        for k, v in r.answers.items():
            lines.append(f"answers {k}: {str(v).lower()}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("EXIT MONETIZATION AUDIT V8.2.9 END")
        return "\n".join(lines)

    def rebound_long_strict_oos_v829_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.counterfactual_training_dataset import build_dataset
        from .labs.edgeguard_repeat_dedup_v8_2_9 import dedup_edgeguard_repeats
        from .labs.rebound_long_candidate_extractor_v8_2_9 import (
            extract_rebound_long_candidates,
        )
        from .labs.rebound_long_strict_oos_v8_2_9 import run_strict_oos_rebound
        dataset, _ = build_dataset(self.db, hours=int(hours), limit=int(limit))
        extractor = extract_rebound_long_candidates(
            self.db, hours=int(hours), limit=int(limit), rows=dataset,
        )
        _, dedup_report = dedup_edgeguard_repeats(dataset, hours=int(hours))
        deduped, _ = dedup_edgeguard_repeats(extractor.candidates, hours=int(hours))
        strict = run_strict_oos_rebound(
            deduped, hours=int(hours),
            score_anti_calibrated=True,
            duplicate_ratio_after=dedup_report.duplicate_ratio_before,
        )
        lines = ["REBOUND LONG STRICT OOS V8.2.9 START"]
        lines.append(f"hours: {strict.hours} status: {strict.status}")
        lines.append(f"candidates_total: {strict.candidates_total}")
        lines.append(
            f"duplicate_ratio_after: {strict.duplicate_ratio_after:.4f}"
        )
        lines.append(
            f"final_status_top_level: {strict.final_status_top_level}"
        )
        for k, v in strict.by_final_status.items():
            lines.append(f"by_final_status {k}: {v}")
        lines.append(
            f"score_used_as_gate: {str(strict.score_used_as_gate).lower()}"
        )
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("REBOUND LONG STRICT OOS V8.2.9 END")
        return "\n".join(lines)

    def adversarial_research_audit_v829_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.adversarial_research_audit_v8_2_9 import audit_v829
        r = audit_v829(hours=int(hours))
        lines = ["ADVERSARIAL RESEARCH AUDIT V8.2.9 START"]
        lines.append(f"hours: {r.hours} status: {r.status}")
        lines.append(f"audit_status: {r.audit_status}")
        lines.append(
            f"score_anti_calibrated: {str(r.score_anti_calibrated).lower()}"
        )
        lines.append(
            f"score_used_as_gate: {str(r.score_used_as_gate).lower()}"
        )
        lines.append(
            f"exit_policy_used_future_returns: "
            f"{str(r.exit_policy_used_future_returns).lower()}"
        )
        for f in r.findings:
            lines.append(f"finding {f['category']}: {f['message']}")
        lines.append(f"blockers: {','.join(r.blockers) if r.blockers else 'NONE'}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("ADVERSARIAL RESEARCH AUDIT V8.2.9 END")
        return "\n".join(lines)

    def export_research_v829_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.research_export_v8_2_9 import export_research_v829
        manifest = export_research_v829(self.db, hours=int(hours), limit=int(limit))
        lines = ["EXPORT RESEARCH V8.2.9 START"]
        lines.append(f"hours: {int(hours)} limit: {int(limit)}")
        lines.append(f"base_dir: {manifest.get('base_dir')}")
        for f in manifest.get("files") or []:
            lines.append(
                f"file: {f.get('name')} size={f.get('size_bytes')} "
                f"sha1={f.get('sha1')}"
            )
        zip_info = manifest.get("zip") or {}
        if zip_info:
            lines.append(
                f"zip: {zip_info.get('name')} size={zip_info.get('size_bytes')} "
                f"sha1={zip_info.get('sha1')}"
            )
        lines.append(
            f"strict_oos_status: {manifest.get('strict_oos_status')}"
        )
        lines.append(
            f"paper_sandbox_candidates: "
            f"{manifest.get('paper_sandbox_candidates')}"
        )
        lines.append(f"best_exit_policy: {manifest.get('best_exit_policy')}")
        lines.append(
            f"adversarial_audit_status: "
            f"{manifest.get('adversarial_audit_status')}"
        )
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("EXPORT RESEARCH V8.2.9 END")
        return "\n".join(lines)

    def research_pack_v829_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.research_export_v8_2_9 import build_pack_v829, render_pack_v829_text
        payload = build_pack_v829(self.db, hours=int(hours), limit=int(limit))
        text = render_pack_v829_text(payload)
        warning = self._v82_heavy_warning(hours)
        if warning:
            text += "\n" + warning
        return text

    # ---- V8.2.9.5 Signal Path Metrics Bridge + Real Outcomes ----

    def _v8295_candidates_and_paths(self, hours: int, limit: int):
        from .labs.counterfactual_training_dataset import build_dataset
        from .labs.edgeguard_repeat_dedup_v8_2_9 import dedup_edgeguard_repeats
        from .labs.rebound_long_candidate_extractor_v8_2_9 import (
            extract_rebound_long_candidates,
        )
        from .labs.research_export_v8_2_9 import _fetch_path_rows
        dataset, _ = build_dataset(self.db, hours=int(hours), limit=int(limit))
        extractor = extract_rebound_long_candidates(
            self.db, hours=int(hours), limit=int(limit), rows=dataset,
        )
        deduped, _ = dedup_edgeguard_repeats(extractor.candidates, hours=int(hours))
        path_rows = _fetch_path_rows(self.db, deduped, hours=int(hours), limit=int(limit))
        return deduped, path_rows

    def signal_path_bridge_v8295_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.signal_path_metrics_bridge_v8_2_9_5 import bridge_candidates
        deduped, path_rows = self._v8295_candidates_and_paths(hours, limit)
        r = bridge_candidates(deduped, path_rows, hours=int(hours))
        lines = ["SIGNAL PATH METRICS BRIDGE V8.2.9.5 START"]
        lines.append(f"hours: {r.hours} status: {r.status}")
        lines.append(f"total_candidates: {r.total_candidates}")
        lines.append(f"path_found_count: {r.path_found_count}")
        lines.append(f"path_missing_count: {r.path_missing_count}")
        lines.append(f"path_ambiguous_count: {r.path_ambiguous_count}")
        lines.append(f"path_coverage_ratio: {r.path_coverage_ratio:.4f}")
        lines.append(f"proxy_sign_mismatch_ratio: {r.proxy_sign_mismatch_ratio:.4f}")
        lines.append(f"proxy_net_ev_avg: {r.proxy_net_ev_avg:.4f}")
        lines.append(f"real_net_ev_avg: {r.real_net_ev_avg:.4f}")
        lines.append(f"real_winrate: {r.real_winrate:.4f}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("SIGNAL PATH METRICS BRIDGE V8.2.9.5 END")
        return "\n".join(lines)

    def canonical_real_outcome_v8295_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.canonical_outcome_real_v8_2_9_5 import canonicalize_real
        deduped, path_rows = self._v8295_candidates_and_paths(hours, limit)
        r = canonicalize_real(deduped, path_rows, hours=int(hours))
        lines = ["CANONICAL REAL OUTCOME V8.2.9.5 START"]
        lines.append(f"hours: {r.hours} status: {r.status}")
        lines.append(f"rows_audited: {r.rows_audited}")
        lines.append(f"real_path_count: {r.real_path_count}")
        lines.append(f"ohlcv_replay_count: {r.ohlcv_replay_count}")
        lines.append(f"proxy_only_count: {r.proxy_only_count}")
        lines.append(f"need_data_count: {r.need_data_count}")
        lines.append(f"canonical_real_ok_ratio: {r.canonical_real_ok_ratio:.4f}")
        lines.append(f"canonical_source_top: {r.canonical_source_top or 'NONE'}")
        for k, v in r.by_source.items():
            lines.append(f"by_source {k}: {v}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("CANONICAL REAL OUTCOME V8.2.9.5 END")
        return "\n".join(lines)

    def strategy_tournament_real_v8295_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.strategy_tournament_real_outcomes_v8_2_9_5 import (
            run_tournament_real,
        )
        deduped, path_rows = self._v8295_candidates_and_paths(hours, limit)
        r = run_tournament_real(deduped, path_rows, hours=int(hours))
        lines = ["STRATEGY TOURNAMENT REAL OUTCOMES V8.2.9.5 START"]
        lines.append(f"hours: {r.hours} status: {r.status}")
        lines.append(f"candidates_input: {r.candidates_input}")
        lines.append(f"canonical_real_ok_ratio: {r.canonical_real_ok_ratio:.4f}")
        lines.append(f"real_rows_used: {r.real_rows_used}")
        lines.append(f"coverage_sufficient: {str(r.coverage_sufficient).lower()}")
        lines.append(f"tournament_real_status: {r.tournament_real_status}")
        lines.append(
            f"tournament_real_best_strategy: {r.tournament_real_best_strategy or 'NONE'}"
        )
        lines.append(
            f"tournament_real_best_status: {r.tournament_real_best_status}"
        )
        lines.append(
            f"paper_sandbox_candidates_real: {r.paper_sandbox_candidates_real}"
        )
        for res in r.results:
            lines.append(
                f"strategy {res['name']}: status={res['status']} "
                f"test_ev_realistic={res['test_net_ev_realistic_pct']:.4f}"
            )
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("STRATEGY TOURNAMENT REAL OUTCOMES V8.2.9.5 END")
        return "\n".join(lines)

    def export_research_v8295_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.research_export_v8_2_9 import export_research_v829
        manifest = export_research_v829(self.db, hours=int(hours), limit=int(limit))
        lines = ["EXPORT RESEARCH V8.2.9.5 START"]
        lines.append(f"hours: {int(hours)} limit: {int(limit)}")
        lines.append(f"base_dir: {manifest.get('base_dir')}")
        lines.append(f"version: {manifest.get('version')}")
        lines.append(
            f"signal_path_metrics_coverage_ratio: "
            f"{manifest.get('signal_path_metrics_coverage_ratio')}"
        )
        lines.append(
            f"canonical_real_ok_ratio: {manifest.get('canonical_real_ok_ratio')}"
        )
        lines.append(
            f"tournament_real_status: {manifest.get('tournament_real_status')}"
        )
        lines.append(
            f"paper_sandbox_candidates_real: "
            f"{manifest.get('paper_sandbox_candidates_real')}"
        )
        zip_info = manifest.get("zip") or {}
        if zip_info:
            lines.append(f"zip: {zip_info.get('name')} sha1={zip_info.get('sha1')}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("EXPORT RESEARCH V8.2.9.5 END")
        return "\n".join(lines)

    def research_pack_v8295_cli(self, hours: int = 168, limit: int = 50000) -> str:
        from .labs.research_export_v8_2_9 import build_pack_v829, render_pack_v829_text
        payload = build_pack_v829(self.db, hours=int(hours), limit=int(limit))
        text = render_pack_v829_text(payload)
        warning = self._v82_heavy_warning(hours)
        if warning:
            text += "\n" + warning
        return text

    # ---- V8.2.9.6 aliases (schema compatibility fix; same lab funcs) ----
    # The bridge / canonical / tournament / export now honour matured
    # status + numeric final_return_pct (V8.2.9.6). These aliases give
    # clean traceability without breaking the v8295 commands.

    def signal_path_bridge_v8296_cli(self, hours: int = 168, limit: int = 50000) -> str:
        return self.signal_path_bridge_v8295_cli(hours=hours, limit=limit).replace(
            "V8.2.9.5", "V8.2.9.6", 2
        )

    def canonical_real_outcome_v8296_cli(self, hours: int = 168, limit: int = 50000) -> str:
        return self.canonical_real_outcome_v8295_cli(hours=hours, limit=limit).replace(
            "V8.2.9.5", "V8.2.9.6", 2
        )

    def strategy_tournament_real_v8296_cli(self, hours: int = 168, limit: int = 50000) -> str:
        return self.strategy_tournament_real_v8295_cli(hours=hours, limit=limit).replace(
            "V8.2.9.5", "V8.2.9.6", 2
        )

    def export_research_v8296_cli(self, hours: int = 168, limit: int = 50000) -> str:
        return self.export_research_v8295_cli(hours=hours, limit=limit).replace(
            "V8.2.9.5", "V8.2.9.6", 2
        )

    def research_pack_v8296_cli(self, hours: int = 168, limit: int = 50000) -> str:
        return self.research_pack_v8295_cli(hours=hours, limit=limit)

    # ---- ResearchOps V10 — Edge Discovery Foundation (research-only) ----

    def edge_data_foundation_v10_cli(
        self, hours: int = 24, external_data_path: str = "",
    ) -> str:
        from .labs.edge_data_foundation_v10 import (
            assess_foundation,
            load_external_data,
            render_readiness_text,
        )
        rows, source_label = load_external_data(external_data_path or None)
        r = assess_foundation(
            rows, source_label=source_label,
            required_data=["funding_rate", "open_interest", "liquidation_usd"],
            value_fields=("funding_rate", "open_interest", "liquidation_usd", "metric_value"),
        )
        return "\n".join(render_readiness_text("EDGE DATA FOUNDATION V10", r))

    def funding_oi_liquidation_research_v10_cli(
        self, hours: int = 24, external_data_path: str = "",
    ) -> str:
        from .labs.funding_oi_liquidation_research_v10 import (
            run_funding_oi_liquidation_research,
        )
        r = run_funding_oi_liquidation_research(
            hours=int(hours), external_data_path=external_data_path or None,
        )
        lines = ["FUNDING OI LIQUIDATION RESEARCH V10 START"]
        lines.append(f"hours: {r.hours} source_label: {r.source_label or 'NONE'}")
        lines.append(f"rows_loaded: {r.rows_loaded}")
        lines.append(f"valid_rows: {r.valid_rows}")
        lines.append(f"funding_points: {r.funding_points}")
        lines.append(f"oi_points: {r.oi_points}")
        lines.append(f"liquidation_points: {r.liquidation_points}")
        lines.append(f"funding_extreme_events: {r.funding_extreme_events}")
        lines.append(f"oi_extreme_events: {r.oi_extreme_events}")
        lines.append(f"oi_price_divergence_events: {r.oi_price_divergence_events}")
        lines.append(f"crowded_long_flush_events: {r.crowded_long_flush_events}")
        lines.append(f"crowded_short_squeeze_events: {r.crowded_short_squeeze_events}")
        lines.append(f"event_count: {r.event_count}")
        lines.append(f"data_quality_status: {r.data_quality_status}")
        lines.append(f"freshness_status: {r.freshness_status}")
        lines.append(f"event_study_ready: {str(r.event_study_ready).lower()}")
        lines.append(f"backtest_ready: {str(r.backtest_ready).lower()}")
        lines.append(f"best_hypothesis: {r.best_hypothesis}")
        lines.append(
            "required_data_missing: "
            + (",".join(r.required_data_missing) if r.required_data_missing else "NONE")
        )
        lines.append("blockers: " + (",".join(r.blockers) if r.blockers else "NONE"))
        lines.append(f"decision: {r.decision}")
        lines.extend(self._v82_safety_footer())
        lines.append("FUNDING OI LIQUIDATION RESEARCH V10 END")
        return "\n".join(lines)

    def token_unlock_post_listing_research_v10_cli(
        self, hours: int = 720, external_data_path: str = "",
    ) -> str:
        from .labs.token_unlock_post_listing_research_v10 import (
            run_unlock_post_listing_research,
        )
        r = run_unlock_post_listing_research(
            hours=int(hours), external_data_path=external_data_path or None,
        )
        lines = ["TOKEN UNLOCK POST LISTING RESEARCH V10 START"]
        lines.append(f"hours: {r.hours} source_label: {r.source_label or 'NONE'}")
        lines.append(f"events_loaded: {r.events_loaded}")
        lines.append(f"valid_events: {r.valid_events}")
        lines.append(f"embargoed_events: {r.embargoed_events}")
        lines.append(
            f"not_actionable_low_reliability: {r.not_actionable_low_reliability}"
        )
        lines.append(f"material_unlock_events: {r.material_unlock_events}")
        lines.append(f"high_fdv_events: {r.high_fdv_events}")
        lines.append(f"risk_score: {r.risk_score}")
        lines.append(f"short_bias_score: {r.short_bias_score}")
        lines.append(
            "affected_symbols: "
            + (",".join(r.affected_symbols) if r.affected_symbols else "NONE")
        )
        lines.append(f"data_quality_status: {r.data_quality_status}")
        lines.append(f"event_study_ready: {str(r.event_study_ready).lower()}")
        lines.append(
            "required_data_missing: "
            + (",".join(r.required_data_missing) if r.required_data_missing else "NONE")
        )
        lines.append("blockers: " + (",".join(r.blockers) if r.blockers else "NONE"))
        lines.append(f"decision: {r.decision}")
        lines.extend(self._v82_safety_footer())
        lines.append("TOKEN UNLOCK POST LISTING RESEARCH V10 END")
        return "\n".join(lines)

    def intraday_volatility_breakdown_v10_cli(
        self, hours: int = 168, symbols: str = "", timeframe: str = "5m",
    ) -> str:
        from .labs.intraday_volatility_breakdown_v10 import (
            run_intraday_volatility_breakdown,
        )
        sym_list = [s.strip().upper() for s in (symbols or "").split(",") if s.strip()] or None
        r = run_intraday_volatility_breakdown(
            self.db, symbols=sym_list, timeframe=timeframe, hours=int(hours),
        )
        lines = ["INTRADAY VOLATILITY BREAKDOWN V10 START"]
        lines.append(f"hours: {r.hours} timeframe: {r.timeframe}")
        lines.append(
            "symbols_requested: "
            + (",".join(r.symbols_requested) if r.symbols_requested else "NONE")
        )
        lines.append(
            "symbols_with_data: "
            + (",".join(r.symbols_with_data) if r.symbols_with_data else "NONE")
        )
        lines.append(f"bars_loaded: {r.bars_loaded}")
        lines.append(f"freshness_status: {r.freshness_status}")
        lines.append(f"data_quality_status: {r.data_quality_status}")
        lines.append(f"rules_evaluated: {r.rules_evaluated}")
        lines.append("blockers: " + (",".join(r.blockers) if r.blockers else "NONE"))
        if r.best_rule:
            br = r.best_rule
            lines.append(
                f"best_rule: {br.get('rule_id')} trades={br.get('trades')} "
                f"net_ev_pct={br.get('net_ev_pct')} net_pf={br.get('net_pf')} "
                f"winrate={br.get('winrate')} decision={br.get('decision')}"
            )
        lines.append(f"decision: {r.decision}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("INTRADAY VOLATILITY BREAKDOWN V10 END")
        return "\n".join(lines)

    def micro_tp_viability_v10_cli(self, hours: int = 168) -> str:
        from .labs.micro_tp_viability_v10 import run_micro_tp_viability
        r = run_micro_tp_viability(hours=int(hours))
        lines = ["MICRO TP VIABILITY V10 START"]
        lines.append(f"hours: {r.hours}")
        lines.append(f"combos_evaluated: {r.combos_evaluated}")
        lines.append(f"realistic_feasible_combos: {r.realistic_feasible_combos}")
        lines.append(f"maker_maker_feasible_combos: {r.maker_maker_feasible_combos}")
        lines.append(
            f"best_realistic_min_winrate: {r.best_realistic_min_winrate}"
        )
        lines.append(f"gross_green_net_negative: {str(r.gross_green_net_negative).lower()}")
        lines.append(f"viable_after_costs: {str(r.viable_after_costs).lower()}")
        lines.append(f"maker_only_required: {str(r.maker_only_required).lower()}")
        lines.append(f"need_websocket: {str(r.need_websocket).lower()}")
        lines.append("blockers: " + (",".join(r.blockers) if r.blockers else "NONE"))
        lines.append(f"decision: {r.decision}")
        lines.extend(self._v82_safety_footer())
        lines.append("MICRO TP VIABILITY V10 END")
        return "\n".join(lines)

    def event_catalyst_layer_v10_cli(
        self, hours: int = 720, external_data_path: str = "",
    ) -> str:
        from .labs.event_catalyst_layer_v10 import run_event_catalyst_layer
        r = run_event_catalyst_layer(
            hours=int(hours), external_data_path=external_data_path or None,
        )
        lines = ["EVENT CATALYST LAYER V10 START"]
        lines.append(f"hours: {r.hours} source_label: {r.source_label or 'NONE'}")
        lines.append(f"rows_loaded: {r.rows_loaded}")
        lines.append(f"valid_events: {r.valid_events}")
        lines.append(f"invalid_events: {r.invalid_events}")
        lines.append(f"embargoed_events: {r.embargoed_events}")
        lines.append(
            f"not_actionable_low_reliability: {r.not_actionable_low_reliability}"
        )
        for k, v in r.by_type.items():
            lines.append(f"by_type {k}: {v}")
        for k, v in r.by_actionability.items():
            lines.append(f"by_actionability {k}: {v}")
        lines.append(f"data_quality_status: {r.data_quality_status}")
        lines.append(f"decision: {r.decision}")
        lines.extend(self._v82_safety_footer())
        lines.append("EVENT CATALYST LAYER V10 END")
        return "\n".join(lines)

    def edge_discovery_orchestrator_v10_cli(
        self, hours: int = 24, external_data_path: str = "",
        symbols: str = "", timeframe: str = "5m",
    ) -> str:
        from .labs.edge_discovery_orchestrator_v10 import (
            run_edge_discovery_orchestrator,
        )
        # Pull clock-drift status + clean-N from the existing reliability
        # layer when available; default to UNKNOWN (which blocks pre-live).
        clock_status = "UNKNOWN"
        clean_n = 0
        try:
            from .execution_safety import check_clock_drift
            clock_status = str(
                (check_clock_drift(exchange_time=None) or {}).get("clock_drift_status")
                or "UNKNOWN"
            ).upper()
        except Exception:
            clock_status = "UNKNOWN"
        try:
            from .score_calibration import load_score_rows
            clean_n = len(load_score_rows(self.db, hours=int(hours)))
        except Exception:
            clean_n = 0
        sym_list = [s.strip().upper() for s in (symbols or "").split(",") if s.strip()] or None
        r = run_edge_discovery_orchestrator(
            self.db, hours=int(hours), external_data_path=external_data_path or None,
            clock_drift_status=clock_status, clean_n=clean_n,
            data_quality_status="OK" if clean_n >= 40 else "NEED_DATA",
            symbols=sym_list, timeframe=timeframe, run_volatility=False,
        )
        lines = ["EDGE DISCOVERY ORCHESTRATOR V10 START"]
        lines.append(f"hours: {r.hours}")
        lines.append(f"clock_drift_status: {r.clock_drift_status}")
        lines.append(f"pre_live_clock_gate: {r.pre_live_clock_gate}")
        lines.append(f"clean_n: {clean_n}")
        for fam in r.families:
            lines.append(
                f"family {fam['family_id']}: status={fam['family_status']} "
                f"native={fam['native_decision']}"
            )
        lines.append(f"best_family: {r.best_family or 'NONE'}")
        lines.append(f"best_family_status: {r.best_family_status}")
        lines.append(f"best_next_experiment: {r.best_next_experiment or 'NONE'}")
        lines.append(
            "rejected_families: "
            + (",".join(r.rejected_families) if r.rejected_families else "NONE")
        )
        lines.append(
            "required_data: " + (",".join(r.required_data) if r.required_data else "NONE")
        )
        lines.append(
            "global_blockers: "
            + (",".join(r.global_blockers) if r.global_blockers else "NONE")
        )
        lines.append(f"next_action: {r.next_action}")
        lines.append(f"shadow_ready: {str(r.shadow_ready).lower()}")
        lines.append(f"paper_ready: {str(r.paper_ready).lower()}")
        lines.append(f"live_ready: {str(r.live_ready).lower()}")
        lines.extend(self._v82_safety_footer())
        lines.append("EDGE DISCOVERY ORCHESTRATOR V10 END")
        return "\n".join(lines)

    def alpha_ensemble_v10_cli(
        self, hours: int = 2160, symbols: str = "", timeframe: str = "15m",
    ) -> str:
        from .labs.alpha_ensemble_v10 import run_alpha_ensemble
        sym_list = [s.strip().upper() for s in (symbols or "").split(",") if s.strip()] or None
        r = run_alpha_ensemble(self.db, symbols=sym_list, timeframe=timeframe, hours=int(hours))
        lines = ["ALPHA ENSEMBLE V10 START"]
        lines.append(f"hours: {r.hours} timeframe: {r.timeframe}")
        lines.append(
            "symbols_with_data: "
            + (",".join(r.symbols_with_data) if r.symbols_with_data else "NONE")
        )
        lines.append(f"bars_loaded: {r.bars_loaded}")
        lines.append(f"total_trades: {r.total_trades}")
        lines.append(f"cost_pct: {r.cost_pct}")
        lines.append(f"net_ev_pct: {r.net_ev_pct}")
        lines.append(f"net_pf: {r.net_pf}")
        lines.append(f"winrate: {r.winrate}")
        lines.append(f"trade_sharpe: {r.trade_sharpe}")
        lines.append(f"cagr_pct: {r.cagr_pct}")
        lines.append(f"max_drawdown_pct: {r.max_drawdown_pct}")
        lines.append(f"final_equity_mult: {r.final_equity_mult}")
        lines.append(f"concentration: {r.concentration} top_symbol: {r.top_symbol or 'NONE'}")
        for ps in r.per_strategy:
            lines.append(
                f"strategy {ps['strategy']}: trades={ps['trades']} "
                f"net_ev_pct={ps['net_ev_pct']} net_pf={ps['net_pf']} "
                f"winrate={ps['winrate']} avg_r={ps['avg_r']}"
            )
        for pair, c in r.correlation.items():
            lines.append(f"correlation {pair}: {c}")
        lines.append(
            f"oos_trades: {r.oos_trades} oos_net_ev_pct: {r.oos_net_ev_pct} "
            f"oos_net_pf: {r.oos_net_pf} oos_sign_consistent: {str(r.oos_sign_consistent).lower()}"
        )
        lines.append(f"oos_method: {r.oos_method}")
        lines.append(f"walk_forward_ready: {str(r.walk_forward_ready).lower()}")
        lines.append(f"walk_forward_status: {r.walk_forward_status}")
        for cs in r.cost_stress:
            lines.append(
                f"cost_stress cost={cs['cost_pct']} trades={cs['trades']} "
                f"net_ev_pct={cs['net_ev_pct']} net_pf={cs['net_pf']} pass={str(cs['pass']).lower()}"
            )
        lines.append(f"cost_stress_all_pass: {str(r.cost_stress_all_pass).lower()}")
        lines.append("blockers: " + (",".join(r.blockers) if r.blockers else "NONE"))
        lines.append(f"verdict: {r.verdict}")
        lines.append(f"paper_ready: {str(r.paper_ready).lower()}")
        lines.append(f"live_ready: {str(r.live_ready).lower()}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("ALPHA ENSEMBLE V10 END")
        return "\n".join(lines)

    # ---- ResearchOps V10.1 — External Edge Data + Event Study ----

    _V101_RAW = "external_data/raw"
    _V101_CLEAN = "external_data/clean"
    _V101_REPORTS = "external_data/reports"

    def _v101_load_clean(self, dataset: str):
        """Read raw (or clean) local files for a dataset and validate them.
        Returns (clean_rows, report). No DB, no network."""
        from .labs.external_edge_ingest_v10_1 import ingest_rows, read_input_dir
        rows, _used = read_input_dir(f"{self._V101_RAW}/{dataset}")
        if not rows:
            rows, _used = read_input_dir(f"{self._V101_CLEAN}/{dataset}")
        report, clean = ingest_rows(rows, dataset)
        return clean, report

    def external_edge_ingest_v101_cli(
        self, dataset: str = "perp_market_state",
        input_path: str = "", input_dir: str = "",
    ) -> str:
        from .labs.external_edge_ingest_v10_1 import ingest_file_or_dir
        if not input_path and not input_dir:
            input_dir = f"{self._V101_RAW}/{dataset}"
        r = ingest_file_or_dir(
            dataset, input_path=input_path or None, input_dir=input_dir or None,
            clean_dir=self._V101_CLEAN, reports_dir=self._V101_REPORTS, write=True,
        )
        lines = ["EXTERNAL EDGE INGEST V10.1 START"]
        lines.append(f"dataset: {r.dataset}")
        lines.append("inputs: " + (",".join(r.inputs) if r.inputs else "NONE"))
        lines.append(f"rows_raw: {r.rows_raw}")
        lines.append(f"rows_valid: {r.rows_valid}")
        lines.append(f"rows_invalid: {r.rows_invalid}")
        lines.append(f"duplicate_count: {r.duplicate_count}")
        lines.append(f"gap_count: {r.gap_count}")
        lines.append("symbols: " + (",".join(r.symbols) if r.symbols else "NONE"))
        lines.append(f"min_timestamp: {r.min_timestamp_iso or 'NONE'}")
        lines.append(f"max_timestamp: {r.max_timestamp_iso or 'NONE'}")
        lines.append(f"invalid_rate: {r.invalid_rate}")
        lines.append(f"duplicate_rate: {r.duplicate_rate}")
        lines.append(f"gap_rate: {r.gap_rate}")
        lines.append(f"top_error: {r.top_error or 'NONE'}")
        lines.append(f"data_quality_status: {r.data_quality_status}")
        lines.append(f"output_clean_csv: {r.output_clean_csv or 'NONE'}")
        lines.append(f"output_clean_ndjson: {r.output_clean_ndjson or 'NONE'}")
        lines.append(f"db_writes: {r.db_writes}")
        lines.extend(self._v82_safety_footer())
        lines.append("EXTERNAL EDGE INGEST V10.1 END")
        return "\n".join(lines)

    def external_data_health_v101_cli(self) -> str:
        from .labs.external_edge_schemas_v10_1 import ALL_DATASETS
        lines = ["EXTERNAL DATA HEALTH V10.1 START"]
        any_data = False
        for ds in ALL_DATASETS:
            clean, rep = self._v101_load_clean(ds)
            if rep.rows_raw > 0:
                any_data = True
            lines.append(
                f"dataset {ds}: rows_raw={rep.rows_raw} rows_valid={rep.rows_valid} "
                f"duplicates={rep.duplicate_count} gaps={rep.gap_count} "
                f"status={rep.data_quality_status}"
            )
        lines.append(f"any_external_data: {str(any_data).lower()}")
        lines.append(
            "overall_status: "
            + ("DATA_AVAILABLE_RESEARCH_ONLY" if any_data else "NEED_DATA")
        )
        lines.extend(self._v82_safety_footer())
        lines.append("EXTERNAL DATA HEALTH V10.1 END")
        return "\n".join(lines)

    def external_event_study_v101_cli(self, module: str = "funding_oi_liq", hours: int = 720) -> str:
        from .labs.external_event_study_v10_1 import (
            DEFAULT_HORIZONS_H,
            EVENT_HORIZONS_H,
            FUNDING_OI_LOOKBACK_BARS,
            build_market_series,
            define_big_unlock_events,
            define_funding_oi_extreme_events,
            define_post_listing_events,
            run_event_study,
        )
        market_clean, _mrep = self._v101_load_clean("perp_market_state")
        mbs = build_market_series(market_clean)
        module = (module or "funding_oi_liq").strip().lower()
        if module in ("funding_oi_liq", "funding", "funding_oi"):
            events = define_funding_oi_extreme_events(mbs)
            horizons = DEFAULT_HORIZONS_H
            primary = 24.0
            lookback_bars = FUNDING_OI_LOOKBACK_BARS
        elif module in ("unlocks", "unlock", "token_unlock"):
            unlock_clean, _ = self._v101_load_clean("token_unlock_events")
            events = define_big_unlock_events(unlock_clean)
            horizons = EVENT_HORIZONS_H
            primary = 168.0
            lookback_bars = 0
        elif module in ("listings", "listing", "post_listing"):
            listing_clean, _ = self._v101_load_clean("listing_events")
            events = define_post_listing_events(listing_clean)
            horizons = EVENT_HORIZONS_H
            primary = 168.0
            lookback_bars = 0
        else:
            return f"EXTERNAL EVENT STUDY V10.1: unknown module '{module}'"
        # --hours <= 0 => no window filter (use all available data, transparent).
        hours_arg = int(hours) if (hours is not None and int(hours) > 0) else None
        r = run_event_study(
            events, mbs, module=module, horizons_h=horizons, primary_horizon_h=primary,
            cost=0.0018, bootstrap_n=2000, baseline_n=500, seed=7,
            hours=hours_arg, lookback_bars_for_events=lookback_bars,
        )
        lines = ["EXTERNAL EVENT STUDY V10.1 START"]
        lines.append(f"module: {r.module}")
        lines.append(f"hours_requested: {r.hours_requested if r.hours_requested is not None else 'null'}")
        lines.append(f"filter_applied: {str(r.filter_applied).lower()}")
        lines.append(f"reference_now: {r.reference_now_iso or 'NONE'}")
        lines.append(f"cutoff_timestamp: {r.cutoff_timestamp or 'NONE'}")
        lines.append(f"lookback_required: {str(r.lookback_required).lower()}")
        lines.append(f"lookback_ms: {r.lookback_ms}")
        lines.append(f"effective_start_timestamp: {r.effective_start_timestamp or 'NONE'}")
        lines.append(f"rows_before_filter: {r.rows_before_filter}")
        lines.append(f"rows_after_filter: {r.rows_after_filter}")
        lines.append(f"events_before_filter: {r.events_before_filter}")
        lines.append(f"events_after_filter: {r.events_after_filter}")
        lines.append(f"event_count: {r.event_count}")
        lines.append(f"matched_events: {r.matched_events}")
        lines.append("symbols: " + (",".join(r.symbols) if r.symbols else "NONE"))
        lines.append(f"primary_horizon_h: {r.primary_horizon_h}")
        lines.append(f"cost_pct: {r.cost_pct}")
        lines.append(f"net_ev_pct: {r.net_ev_pct}")
        lines.append(f"gross_ev_pct: {r.gross_ev_pct}")
        lines.append(f"winrate: {r.winrate}")
        lines.append(f"baseline_net_ev_pct: {r.baseline_net_ev_pct}")
        lines.append(f"edge_vs_baseline_pct: {r.edge_vs_baseline_pct}")
        lines.append(f"bootstrap_ci_low: {r.bootstrap_ci_low} bootstrap_ci_high: {r.bootstrap_ci_high}")
        for p in r.per_horizon:
            lines.append(
                f"horizon {p['horizon_h']}h: net_ev_pct={p['net_ev_pct']} "
                f"winrate={p['winrate']} samples={p['samples']}"
            )
        lines.append(f"avg_mfe_pct: {r.avg_mfe_pct} avg_mae_pct: {r.avg_mae_pct}")
        lines.append(f"median_time_to_tp_h: {r.median_time_to_tp_h}")
        lines.append(f"median_time_to_sl_h: {r.median_time_to_sl_h}")
        lines.append(f"one_event_dominance: {r.one_event_dominance}")
        lines.append(f"one_symbol_dominance: {r.one_symbol_dominance} top_symbol: {r.top_symbol or 'NONE'}")
        lines.append(f"sample_count: {r.sample_count}")
        lines.append("blockers: " + (",".join(r.blockers) if r.blockers else "NONE"))
        lines.append(f"status: {r.status}")
        lines.append(f"paper_ready: {str(r.paper_ready).lower()}")
        lines.append(f"live_ready: {str(r.live_ready).lower()}")
        lines.extend(self._v82_safety_footer())
        lines.append("EXTERNAL EVENT STUDY V10.1 END")
        return "\n".join(lines)

    def external_funding_oi_diagnostics_v101_cli(self, hours: int = 2160) -> str:
        import json
        from datetime import datetime, timezone
        from pathlib import Path

        import csv as _csv

        from .labs.external_funding_oi_diagnostics_v10_1 import (
            STATUS_NEED_DATA,
            TABLE_COLUMNS,
            diagnostics_table_rows,
            run_funding_oi_diagnostics,
        )
        market_clean, mrep = self._v101_load_clean("perp_market_state")
        liq_clean, _lrep = self._v101_load_clean("perp_liquidations")
        r = run_funding_oi_diagnostics(market_clean, liq_clean, hours=int(hours))

        # Data-quality note: missing_oi_usd_close share of raw market rows.
        miss = int((mrep.error_breakdown or {}).get("missing_oi_usd_close", 0))
        raw = int(mrep.rows_raw or 0)
        miss_ratio = round(miss / raw, 4) if raw else 0.0

        lines = ["EXTERNAL FUNDING/OI DIAGNOSTICS V10.1 START"]
        lines.append(f"hours: {r.hours} cost_pct: {r.cost_pct}")
        lines.append("symbols: " + (",".join(r.symbols) if r.symbols else "NONE"))
        lines.append(f"market_rows: {r.market_rows} liq_rows: {r.liq_rows}")
        lines.append(f"missing_oi_usd_close: {miss} ({miss_ratio:.2%} of raw market rows)")
        lines.append(f"buckets_evaluated: {r.buckets_evaluated}")
        lines.append(f"rejected_count: {r.rejected_count} need_more_count: {r.need_more_count}")
        lines.append("watch_only: " + (",".join(r.watch_only) if r.watch_only else "NONE"))
        lines.append("research_green: " + (",".join(r.research_green) if r.research_green else "NONE"))
        for t in r.top_by_net_ev_24h:
            lines.append(
                f"top_bucket {t['name']}[{t['scope']}]: net_ev_24h={t['net_ev_24h']} "
                f"matched={t['matched_events']} edge_vs_baseline={t['edge_vs_baseline_pct']} "
                f"ci_low={t['bootstrap_ci_low']} status={t['status']}"
            )
        lines.append(f"report_status: {r.status}")
        # Persist a research report (external_data/reports is git-ignored).
        report_path = "NONE"
        csv_path = "NONE"
        if r.status != STATUS_NEED_DATA:
            try:
                rdir = Path("external_data/reports")
                rdir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                payload = r.as_dict()
                payload["missing_oi_usd_close"] = miss
                payload["missing_oi_usd_close_ratio"] = miss_ratio
                p = rdir / f"funding_oi_diagnostics_{stamp}.json"
                p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
                report_path = str(p)
                # FIX-4: auditable CSV table (bucket_id..exact_blocker..status).
                table = diagnostics_table_rows(r)
                cp = rdir / f"funding_oi_diagnostics_{stamp}.csv"
                with cp.open("w", encoding="utf-8", newline="") as fh:
                    w = _csv.DictWriter(fh, fieldnames=TABLE_COLUMNS)
                    w.writeheader()
                    for row in table:
                        w.writerow(row)
                csv_path = str(cp)
            except OSError:
                report_path = "WRITE_FAILED"
        lines.append(f"report_json: {report_path}")
        lines.append(f"report_csv: {csv_path}")
        lines.append(f"paper_ready: {str(r.paper_ready).lower()}")
        lines.append(f"live_ready: {str(r.live_ready).lower()}")
        lines.extend(self._v82_safety_footer())
        lines.append("EXTERNAL FUNDING/OI DIAGNOSTICS V10.1 END")
        return "\n".join(lines)

    def external_funding_oi_stability_v101_cli(self, hours: int = 2160) -> str:
        import csv as _csv
        import json
        from datetime import datetime, timezone
        from pathlib import Path

        from .labs.external_funding_oi_stability_v10_1 import (
            STABILITY_TABLE_COLUMNS,
            STATUS_NEED_DATA,
            run_funding_oi_stability,
            stability_table_rows,
        )
        market_clean, mrep = self._v101_load_clean("perp_market_state")
        liq_clean, _lrep = self._v101_load_clean("perp_liquidations")
        miss = int((mrep.error_breakdown or {}).get("missing_oi_usd_close", 0))
        raw = int(mrep.rows_raw or 0)
        miss_ratio = (miss / raw) if raw else 0.0
        r = run_funding_oi_stability(market_clean, liq_clean, hours=int(hours),
                                     missing_oi_ratio=miss_ratio)

        lines = ["EXTERNAL FUNDING/OI STABILITY V10.1 START"]
        lines.append(f"hours: {r.hours} cost_x1: {r.cost_x1}")
        lines.append("symbols: " + (",".join(r.symbols) if r.symbols else "NONE"))
        lines.append(f"market_rows: {r.market_rows} liq_rows: {r.liq_rows}")
        lines.append(f"missing_oi_ratio: {r.missing_oi_ratio:.4f} ({r.missing_oi_ratio:.2%})")
        lines.append("stability_green: " + (",".join(r.stability_green) if r.stability_green else "NONE"))
        lines.append("watch_only: " + (",".join(r.watch_only) if r.watch_only else "NONE"))
        for b in r.buckets:
            lines.append(
                f"bucket {b['bucket_id']}[{b['symbol_scope']}]: "
                f"total_matched={b['total_matched']} status={b['stability_status']} "
                f"fh_net24={b['first_half_net_ev_24h']} sh_net24={b['second_half_net_ev_24h']} "
                f"cost_x2_net24={b['cost_x2_net_ev_24h']} horizon_risk={str(b['horizon_risk']).lower()} "
                f"missing_oi_risk={str(b['missing_oi_risk']).lower()} "
                f"regime_unstable={str(b['regime_unstable']).lower()} blocker={b['stability_blocker'] or 'NONE'}"
            )
        nrd = r.next_research_decision or {}
        lines.append(f"next_research_decision.best_candidate: {nrd.get('best_candidate') or 'NONE'}")
        lines.append(f"next_research_decision.eth_specific_candidate: {str(nrd.get('eth_specific_candidate', False)).lower()}")
        lines.append(f"next_research_decision.suggested_next_code_prompt_type: {nrd.get('suggested_next_code_prompt_type', '')}")
        lines.append(f"next_research_decision.max_label: {nrd.get('max_label', '')}")
        lines.append(f"next_research_decision.recommendation: {nrd.get('recommendation', '')}")
        lines.append(f"report_status: {r.status}")
        report_path = "NONE"
        csv_path = "NONE"
        if r.status != STATUS_NEED_DATA:
            try:
                rdir = Path("external_data/reports")
                rdir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                p = rdir / f"funding_oi_stability_{stamp}.json"
                p.write_text(json.dumps(r.as_dict(), indent=2, default=str), encoding="utf-8")
                report_path = str(p)
                cp = rdir / f"funding_oi_stability_{stamp}.csv"
                with cp.open("w", encoding="utf-8", newline="") as fh:
                    w = _csv.DictWriter(fh, fieldnames=STABILITY_TABLE_COLUMNS)
                    w.writeheader()
                    for row in stability_table_rows(r):
                        w.writerow(row)
                csv_path = str(cp)
            except OSError:
                report_path = "WRITE_FAILED"
        lines.append(f"report_json: {report_path}")
        lines.append(f"report_csv: {csv_path}")
        lines.append(f"paper_ready: {str(r.paper_ready).lower()}")
        lines.append(f"live_ready: {str(r.live_ready).lower()}")
        lines.extend(self._v82_safety_footer())
        lines.append("EXTERNAL FUNDING/OI STABILITY V10.1 END")
        return "\n".join(lines)

    def external_missing_oi_audit_v102_cli(self, hours: int = 2160) -> str:
        import csv as _csv
        import json
        from datetime import datetime, timezone
        from pathlib import Path

        from .labs.external_edge_ingest_v10_1 import read_input_dir
        from .labs.external_missing_oi_audit_v10_2 import (
            AUDIT_TABLE_COLUMNS,
            STATUS_NEED_MORE,
            audit_table_rows,
            run_missing_oi_audit,
        )
        # Missing OI is only visible in RAW (clean drops rows lacking OI).
        raw_rows, _used = read_input_dir(f"{self._V101_RAW}/perp_market_state")
        r = run_missing_oi_audit(raw_rows, hours=int(hours))
        lines = ["EXTERNAL MISSING OI AUDIT V10.2 START"]
        lines.append(f"hours: {r.hours}")
        lines.append(f"total_rows: {r.total_rows} rows_missing_oi: {r.rows_missing_oi}")
        lines.append(f"missing_ratio_global: {r.missing_ratio_global:.4f} ({r.missing_ratio_global:.2%})")
        for sym, d in sorted(r.per_symbol.items()):
            lines.append(f"per_symbol {sym}: missing={d['missing']}/{d['total']} ratio={d['ratio']}")
        lines.append(f"worst_symbol: {r.worst_symbol or 'NONE'} eth_worse_than_btc: {str(r.eth_worse_than_btc).lower()}")
        lines.append(f"first_half_missing_ratio: {r.first_half_missing_ratio} second_half_missing_ratio: {r.second_half_missing_ratio}")
        lines.append(f"max_consecutive_missing: {r.max_consecutive_missing} clustered_fraction: {r.clustered_fraction} clustered: {str(r.clustered).lower()}")
        lines.append(f"worst_day: {r.worst_day or 'NONE'} worst_day_ratio: {r.worst_day_ratio}")
        lines.append(f"funding_extreme_bars: {r.funding_extreme_bars} with_missing_oi: {r.funding_extreme_with_missing_oi} ratio: {r.funding_extreme_missing_ratio}")
        for n in r.notes:
            lines.append(f"note: {n}")
        lines.append(f"status: {r.status}")
        lines.append("recommendations: " + (",".join(r.recommendations) if r.recommendations else "NONE"))
        lines.append(f"primary_recommendation: {r.primary_recommendation or 'NONE'}")
        report_json = "NONE"
        report_csv = "NONE"
        if r.status != STATUS_NEED_MORE:
            try:
                rdir = Path("external_data/reports")
                rdir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                p = rdir / f"missing_oi_audit_{stamp}.json"
                p.write_text(json.dumps(r.as_dict(), indent=2, default=str), encoding="utf-8")
                report_json = str(p)
                cp = rdir / f"missing_oi_audit_{stamp}.csv"
                with cp.open("w", encoding="utf-8", newline="") as fh:
                    w = _csv.DictWriter(fh, fieldnames=AUDIT_TABLE_COLUMNS)
                    w.writeheader()
                    for row in audit_table_rows(r):
                        w.writerow(row)
                report_csv = str(cp)
            except OSError:
                report_json = "WRITE_FAILED"
        lines.append(f"report_json: {report_json}")
        lines.append(f"report_csv: {report_csv}")
        lines.extend(self._v82_safety_footer())
        lines.append("EXTERNAL MISSING OI AUDIT V10.2 END")
        return "\n".join(lines)

    def external_long_history_validation_v102_cli(self, hours: int = 8760) -> str:
        import json
        from datetime import datetime, timezone
        from pathlib import Path

        from .labs.external_edge_ingest_v10_1 import read_input_dir
        from .labs.external_long_history_validation_v10_2 import (
            STATUS_NEED_DATA,
            run_long_history_validation,
        )
        market_clean, _mrep = self._v101_load_clean("perp_market_state")
        liq_clean, _lrep = self._v101_load_clean("perp_liquidations")
        raw_rows, _used = read_input_dir(f"{self._V101_RAW}/perp_market_state")
        r = run_long_history_validation(market_clean, liq_clean, raw_rows, hours=int(hours))
        lines = ["EXTERNAL LONG HISTORY VALIDATION V10.2 START"]
        lines.append(f"hours: {r.hours}")
        lines.append(f"market_rows: {r.market_rows} liq_rows: {r.liq_rows}")
        lines.append("symbols: " + (",".join(r.symbols) if r.symbols else "NONE"))
        lines.append(f"days_covered: {r.days_covered}")
        lines.append(f"history_status: {r.history_status}")
        lines.append(f"data_health.status: {r.data_health.get('status')}")
        lines.append(
            f"missing_oi_audit.status: {r.missing_oi_audit.get('status')} "
            f"global={r.missing_oi_audit.get('missing_ratio_global')} "
            f"primary_rec={r.missing_oi_audit.get('primary_recommendation')}"
        )
        lines.append(
            "stability_green: "
            + (",".join(r.stability_summary.get("stability_green", [])) or "NONE")
        )
        lines.append(
            "stability_watch_only: "
            + (",".join(r.stability_summary.get("watch_only", [])) or "NONE")
        )
        nrd = r.next_research_decision or {}
        lines.append(f"next_research_decision.history_status: {nrd.get('history_status')}")
        lines.append(f"next_research_decision.any_stability_green: {str(nrd.get('any_stability_green', False)).lower()}")
        lines.append(f"next_research_decision.eth_specific_candidate: {str(nrd.get('eth_specific_candidate', False)).lower()}")
        lines.append(f"next_research_decision.suggested_next_code_prompt_type: {nrd.get('suggested_next_code_prompt_type')}")
        lines.append(f"next_research_decision.dashboard_next_phase: {nrd.get('dashboard_next_phase')}")
        lines.append(f"next_research_decision.max_label: {nrd.get('max_label')}")
        lines.append(f"next_research_decision.rationale: {nrd.get('rationale')}")
        lines.append(f"report_status: {r.status}")
        report_json = "NONE"
        if r.status != STATUS_NEED_DATA:
            try:
                rdir = Path("external_data/reports")
                rdir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                p = rdir / f"long_history_validation_{stamp}.json"
                p.write_text(json.dumps(r.as_dict(), indent=2, default=str), encoding="utf-8")
                report_json = str(p)
            except OSError:
                report_json = "WRITE_FAILED"
        lines.append(f"report_json: {report_json}")
        lines.append(f"paper_ready: {str(r.paper_ready).lower()}")
        lines.append(f"live_ready: {str(r.live_ready).lower()}")
        lines.extend(self._v82_safety_footer())
        lines.append("EXTERNAL LONG HISTORY VALIDATION V10.2 END")
        return "\n".join(lines)

    def strategy_replay_backtest_v103_cli(self, hours: int = 8760) -> str:
        import json as _json
        from pathlib import Path as _Path

        from .labs.external_edge_ingest_v10_1 import read_input_dir
        from .labs.external_missing_oi_audit_v10_2 import run_missing_oi_audit
        from .labs.strategy_replay_backtest_v103_stub import run_replay_backtest_stub
        market_clean, _mrep = self._v101_load_clean("perp_market_state")
        # Missing OI from raw.
        raw_rows, _u = read_input_dir(f"{self._V101_RAW}/perp_market_state")
        miss = run_missing_oi_audit(raw_rows, hours=int(hours)).missing_ratio_global
        # Detect undercoverage from the most recent chunked-fetch report (read-only).
        undercoverage = False
        try:
            rdir = _Path("external_data/reports")
            reps = sorted(rdir.glob("coinalyze_chunked_fetch_*.json")) if rdir.exists() else []
            if reps:
                latest = _json.loads(reps[-1].read_text(encoding="utf-8"))
                undercoverage = bool(latest.get("undercoverage")) or latest.get("report_status") == "UNDERCOVERAGE"
        except (OSError, ValueError):
            undercoverage = False
        r = run_replay_backtest_stub(market_clean, undercoverage=undercoverage,
                                     missing_oi_ratio=miss, uses_oi=False)
        lines = ["STRATEGY REPLAY BACKTEST V10.3 (STUB) START"]
        lines.append(f"candidate: {r.candidate}")
        lines.append(f"days_covered: {r.days_covered} min_days_required: {r.min_days_required}")
        lines.append(f"undercoverage: {str(r.undercoverage).lower()}")
        lines.append(f"missing_oi_ratio: {r.missing_oi_ratio} uses_oi: {str(r.uses_oi).lower()}")
        lines.append(f"engine_implemented: {str(r.engine_implemented).lower()}")
        lines.append(f"status: {r.status}")
        lines.append(f"blocker: {r.blocker or 'NONE'}")
        lines.append(f"note: {r.note}")
        lines.append("promotion_ladder: " + " -> ".join(r.promotion_ladder))
        lines.append(f"paper_ready: {str(r.paper_ready).lower()}")
        lines.append(f"live_ready: {str(r.live_ready).lower()}")
        lines.extend(self._v82_safety_footer())
        lines.append("STRATEGY REPLAY BACKTEST V10.3 (STUB) END")
        return "\n".join(lines)

    def external_data_source_audit_v103_cli(self, hours: int = 8760) -> str:
        from .labs.external_edge_ingest_v10_1 import read_input_dir
        from .labs.external_data_provider_registry_v10_3 import run_data_source_audit
        market_clean, _m = self._v101_load_clean("perp_market_state")
        raw_rows, _u = read_input_dir(f"{self._V101_RAW}/perp_market_state")
        r = run_data_source_audit(market_clean, raw_rows, hours=int(hours))
        lines = ["EXTERNAL DATA SOURCE AUDIT V10.3 START"]
        lines.append(f"current_provider: {r.current_provider}")
        lines.append(f"current_clean_days: {r.current_clean_days}")
        lines.append(f"required_min_history_days: {r.required_min_history_days}")
        lines.append(f"stronger_history_days: {r.stronger_history_days}")
        lines.append(f"current_history_status: {r.current_history_status}")
        lines.append(f"current_missing_oi_ratio: {r.current_missing_oi_ratio}")
        lines.append(f"missing_oi_status: {r.missing_oi_status}")
        lines.append(f"oi_bucket_policy: {r.oi_bucket_policy}")
        lines.append(f"data_classification: {r.data_classification}")
        lines.append(f"backtester_readiness: {r.backtester_readiness}")
        lines.append(f"recommended_next_provider: {r.recommended_next_provider}")
        lines.append("provider_candidates: " + (",".join(r.provider_candidates) if r.provider_candidates else "NONE"))
        lines.append("data_blockers: " + (",".join(r.data_blockers) if r.data_blockers else "NONE"))
        lines.append("allowed_actions: " + ",".join(r.allowed_actions))
        lines.append("blocked_actions: " + ",".join(r.blocked_actions))
        lines.append(f"paper_ready: {str(r.paper_ready).lower()}")
        lines.append(f"live_ready: {str(r.live_ready).lower()}")
        lines.extend(self._v82_safety_footer())
        lines.append("EXTERNAL DATA SOURCE AUDIT V10.3 END")
        return "\n".join(lines)

    def external_provider_readiness_v103_cli(self) -> str:
        from .labs.external_data_provider_registry_v10_3 import run_provider_readiness
        r = run_provider_readiness()
        lines = ["EXTERNAL PROVIDER READINESS V10.3 START"]
        lines.append(f"current_provider: {r.current_provider}")
        lines.append(f"required_min_history_days: {r.required_min_history_days}")
        lines.append(f"stronger_history_days: {r.stronger_history_days}")
        lines.append(f"recommended_next_provider: {r.recommended_next_provider}")
        lines.append("provider_candidates: " + (",".join(r.provider_candidates) if r.provider_candidates else "NONE"))
        lines.append("needs_manual_verification: " + (",".join(r.needs_manual_verification) if r.needs_manual_verification else "NONE"))
        for p in r.providers:
            lines.append(
                f"provider {p['provider_id']}: status={p['status']} "
                f"bitget={p['bitget_perp_support']} hist_days={p['expected_history_days']} "
                f"180d={p['suitable_for_180d']} 365d={p['suitable_for_365d']} paid={p['paid_data_risk']}"
            )
        lines.append(f"paper_ready: {str(r.paper_ready).lower()}")
        lines.append(f"live_ready: {str(r.live_ready).lower()}")
        lines.extend(self._v82_safety_footer())
        lines.append("EXTERNAL PROVIDER READINESS V10.3 END")
        return "\n".join(lines)

    # ---------------------------------------------------------------
    # V10.4 — provider verification, acquisition plan, research intake,
    # edge hunter contract, read-only trader dashboard (all research-only).
    # ---------------------------------------------------------------
    def external_provider_verification_v104_cli(self) -> str:
        from .labs.external_provider_verification_v10_4 import (
            run_provider_verification,
        )
        r = run_provider_verification()
        lines = ["EXTERNAL PROVIDER VERIFICATION V10.4 START"]
        lines.append(f"primary_candidate: {r.primary_candidate or 'NEEDS_MANUAL_VERIFICATION'}")
        lines.append(f"fallback_candidate: {r.fallback_candidate or 'NEEDS_MANUAL_VERIFICATION'}")
        lines.append(f"cross_check_provider: {r.cross_check_provider or 'NEEDS_MANUAL_VERIFICATION'}")
        lines.append(f"proxy_provider: {r.proxy_provider or 'NEEDS_MANUAL_VERIFICATION'}")
        lines.append(f"any_paid_download_authorized: {str(r.any_paid_download_authorized).lower()}")
        lines.append(f"no_paid_download_without_authorization: {str(r.no_paid_download_without_authorization).lower()}")
        for p in r.providers:
            pending = ",".join(p["manual_checks_pending"]) if p["manual_checks_pending"] else "NONE"
            lines.append(
                f"provider {p['provider_id']}: status={p['status']} "
                f"recommendation={p['recommendation']} bitget={p['bitget_perp_support']} "
                f"180d={p['suitable_for_180d']} 365d={p['suitable_for_365d']} "
                f"paid={p['paid_data_risk']} verification_complete={str(p['verification_complete']).lower()} "
                f"paid_download_authorized={str(p['paid_download_authorized']).lower()}"
            )
            lines.append(f"  manual_checks_pending: {pending}")
        lines.append(f"paper_ready: {str(r.paper_ready).lower()}")
        lines.append(f"live_ready: {str(r.live_ready).lower()}")
        lines.extend(self._v82_safety_footer())
        lines.append("EXTERNAL PROVIDER VERIFICATION V10.4 END")
        return "\n".join(lines)

    def external_data_acquisition_plan_v104_cli(self) -> str:
        from .labs.external_data_acquisition_plan_v10_4 import (
            ACQUISITION_DIRS,
            MANIFEST_AUTHORIZATION_FIELDS,
            MANIFEST_REQUIRED_FIELDS,
            MAX_DUP_RATIO,
            MAX_GAP_RATIO,
            MIN_COVERAGE_RATIO,
            build_importer_contract,
            evaluate_acquisition_manifest,
        )
        contract = build_importer_contract()
        # With no real staged manifest, the gate must block (proves no-replace).
        empty_eval = evaluate_acquisition_manifest(None)
        # V10.4.1 — a perfect-quality manifest WITHOUT explicit human
        # authorization must still be blocked (proves the authorization gate).
        perfect_unauthorized = {f: 0 for f in MANIFEST_REQUIRED_FIELDS}
        perfect_unauthorized.update({
            "source_provider": "tardis_dev", "license_terms": "research",
            "rows_by_type": {"perp_market_state": 4320},
            "missing_oi_ratio": 0.02, "missing_oi_status": "DATA_OK",
            "gap_count": 0, "duplicate_count": 0,
            "coverage_ratio": 0.97, "clean_days": 200.0,
            "checksums_sha256": {"perp_market_state.csv": "x"},
        })
        unauth_eval = evaluate_acquisition_manifest(perfect_unauthorized)
        lines = ["EXTERNAL DATA ACQUISITION PLAN V10.4 START"]
        for key, path in ACQUISITION_DIRS.items():
            lines.append(f"dir {key}: {path}")
        lines.append("manifest_required_fields: " + ",".join(MANIFEST_REQUIRED_FIELDS))
        lines.append("manifest_authorization_fields: " + ",".join(MANIFEST_AUTHORIZATION_FIELDS))
        lines.append(f"authorization_rule: {contract['authorization_rule']}")
        lines.append(f"min_coverage_ratio: {MIN_COVERAGE_RATIO}")
        lines.append(f"max_gap_ratio: {MAX_GAP_RATIO}")
        lines.append(f"max_duplicate_ratio: {MAX_DUP_RATIO}")
        lines.append("expected_input_files: " + ",".join(contract["expected_input_files"]))
        lines.append("blocks_import: " + ",".join(contract["blocks_import"]))
        lines.append("never: " + ",".join(contract["never"]))
        lines.append(f"atomic_promote: {contract['atomic_promote']}")
        lines.append(f"rollback: {contract['rollback']}")
        lines.append(f"no_manifest_eval_status: {empty_eval.status}")
        lines.append(f"no_manifest_promote_allowed: {str(empty_eval.promote_allowed).lower()}")
        lines.append(f"no_manifest_do_not_replace_raw: {str(empty_eval.do_not_replace_raw).lower()}")
        lines.append(f"perfect_quality_without_authorization_status: {unauth_eval.status}")
        lines.append(f"perfect_quality_without_authorization_promote_allowed: {str(unauth_eval.promote_allowed).lower()}")
        lines.append("perfect_quality_without_authorization_blockers: " + ",".join(unauth_eval.blockers))
        lines.append(f"paid_download_requires_authorization: {str(empty_eval.paid_download_requires_authorization).lower()}")
        lines.append(f"paper_ready: {str(empty_eval.paper_ready).lower()}")
        lines.append(f"live_ready: {str(empty_eval.live_ready).lower()}")
        lines.extend(self._v82_safety_footer())
        lines.append("EXTERNAL DATA ACQUISITION PLAN V10.4 END")
        return "\n".join(lines)

    def external_research_intake_v104_cli(self) -> str:
        from .labs.external_research_intake_v10_4 import (
            IDEA_ONLY,
            NEEDS_BACKTEST,
            NEEDS_DATA,
            NEEDS_RISK_REVIEW,
            NEEDS_WALK_FORWARD,
            PAPER_CANDIDATE_PENDING,
            REJECT_LOOKAHEAD,
            REJECT_OVERFIT,
            REJECT_UNTRADABLE,
            SHADOW_ELIGIBLE,
            run_research_intake,
        )
        # No external ideas are auto-loaded (no invented data). This prints the
        # intake contract + an empty backlog so the invariants are auditable.
        rep = run_research_intake(None)
        statuses = [IDEA_ONLY, NEEDS_DATA, NEEDS_BACKTEST, NEEDS_WALK_FORWARD,
                    NEEDS_RISK_REVIEW, REJECT_LOOKAHEAD, REJECT_OVERFIT,
                    REJECT_UNTRADABLE, SHADOW_ELIGIBLE, PAPER_CANDIDATE_PENDING]
        lines = ["EXTERNAL RESEARCH INTAKE V10.4 START"]
        lines.append("intake_statuses: " + ",".join(statuses))
        lines.append("classification_ceiling: SHADOW_ELIGIBLE")
        lines.append("rule: no_idea_can_enable_paper_filter_or_live")
        lines.append("rule: unknown_risk_is_not_safe_needs_risk_review")
        lines.append(f"ideas_count: {rep.ideas_count}")
        lines.append("by_status: " + (",".join(f"{k}={v}" for k, v in rep.by_status.items()) if rep.by_status else "NONE"))
        lines.append("shadow_eligible: " + (",".join(rep.shadow_eligible) if rep.shadow_eligible else "NONE"))
        lines.append("rejected: " + (",".join(rep.rejected) if rep.rejected else "NONE"))
        lines.append(f"paper_filter_enabled: {str(rep.paper_filter_enabled).lower()}")
        lines.append(f"paper_ready: {str(rep.paper_ready).lower()}")
        lines.append(f"live_ready: {str(rep.live_ready).lower()}")
        lines.extend(self._v82_safety_footer())
        lines.append("EXTERNAL RESEARCH INTAKE V10.4 END")
        return "\n".join(lines)

    def edge_hunter_contract_v104_cli(self) -> str:
        from .labs.edge_hunter_contract_v10_4 import (
            MIN_HISTORY_DAYS,
            MIN_SAMPLES,
            build_edge_hunter_contract,
        )
        c = build_edge_hunter_contract()
        lines = ["EDGE HUNTER CONTRACT V10.4 START"]
        lines.append("operational: false")
        lines.append(f"minimum_samples: {MIN_SAMPLES}")
        lines.append(f"minimum_history_days: {MIN_HISTORY_DAYS}")
        lines.append("candidate_definition: " + ",".join(c["candidate_definition"]))
        lines.append("metrics_required: " + ",".join(c["metrics_required"]))
        lines.append("validation: " + ",".join(c["validation"]))
        lines.append("anti_lookahead: " + ",".join(c["anti_lookahead"]))
        lines.append("reject_reasons: " + ",".join(c["reject_reasons"]))
        lines.append("promotion_ladder: " + ",".join(c["promotion_ladder"]))
        lines.append(f"output_ceiling: {c['output_ceiling']}")
        lines.append("never: " + ",".join(c["never"]))
        lines.append("paper_ready: false")
        lines.append("live_ready: false")
        lines.extend(self._v82_safety_footer())
        lines.append("EDGE HUNTER CONTRACT V10.4 END")
        return "\n".join(lines)

    def trader_dashboard_contract_v104_cli(self) -> str:
        from .labs.external_data_provider_registry_v10_3 import (
            run_data_source_audit,
            run_provider_readiness,
        )
        from .labs.external_edge_ingest_v10_1 import read_input_dir
        from .labs.trader_dashboard_v104 import (
            LOCK_TOOLTIP,
            build_dashboard_view_model,
            dashboard_contract,
            render_dashboard_html,
        )
        contract = dashboard_contract()
        # Build the view-model from REAL read-only state (no invented numbers).
        try:
            market_clean, _m = self._v101_load_clean("perp_market_state")
            raw_rows, _u = read_input_dir(f"{self._V101_RAW}/perp_market_state")
            audit = run_data_source_audit(market_clean, raw_rows)
            data_readiness = audit.as_dict() if hasattr(audit, "as_dict") else None
        except Exception:
            data_readiness = None
        try:
            pr = run_provider_readiness()
            provider_readiness = pr.as_dict() if hasattr(pr, "as_dict") else None
        except Exception:
            provider_readiness = None
        vm = build_dashboard_view_model(
            data_readiness=data_readiness, provider_readiness=provider_readiness,
        )
        html = render_dashboard_html(vm)
        lower = html.lower()
        import re as _re
        # Every /api/ URL referenced anywhere in the page must live under the
        # read-only researchops v104 namespace (poll + warm endpoints).
        fetch_targets = _re.findall(r'"(/api/[^"]+)"', html)
        fetch_readonly_only = all(
            t.startswith("/api/researchops/v104/") for t in fetch_targets
        ) and bool(fetch_targets)
        lines = ["TRADER DASHBOARD CONTRACT V10.4 START"]
        lines.append(f"name: {contract['name']}")
        lines.append(f"read_only: {str(contract['read_only']).lower()}")
        lines.append(f"route: {contract['route']}")
        lines.append(f"near_real_time: {str(contract['near_real_time']).lower()}")
        lines.append(f"poll_method: {contract['poll_method']}")
        lines.append(f"poll_endpoint: {contract['poll_endpoint']}")
        lines.append(f"default_refresh_seconds: {contract['default_refresh_seconds']}")
        lines.append("automatic_endpoints: " + ",".join(contract["automatic_endpoints"]))
        lines.append(f"heavy_panels_mode: {contract['heavy_panels_mode']}")
        lines.append(f"heavy_refresh_mode: {contract['heavy_refresh_mode']}")
        lines.append(f"polling_never_computes_heavy_work: {str(contract['polling_never_computes_heavy_work']).lower()}")
        lines.append(f"unknown_endpoint_behavior: {contract['unknown_endpoint_behavior']}")
        lines.append(f"errors_sanitized: {str(contract['errors_sanitized']).lower()}")
        lines.append("readonly_api_endpoints: " + ",".join(contract["readonly_api_endpoints"]))
        lines.append("panels: " + ",".join(contract["panels"]))
        lines.append("mutable_endpoints: " + (",".join(contract["mutable_endpoints"]) if contract["mutable_endpoints"] else "NONE"))
        lines.append(f"post_forms: {contract['post_forms']}")
        lines.append("disabled_controls: " + ",".join(contract["disabled_controls"]))
        lines.append("guarantees: " + ",".join(contract["guarantees"]))
        lines.append(f"lock_tooltip: {LOCK_TOOLTIP}")
        lines.append(f"render_html_bytes: {len(html)}")
        lines.append(f"html_has_no_live_banner: {str('NO LIVE' in html).lower()}")
        lines.append(f"html_has_research_only: {str('RESEARCH ONLY' in html).lower()}")
        lines.append(f"html_has_disabled_buttons: {str('disabled' in lower).lower()}")
        lines.append(f"html_has_last_update_timestamp: {str('last-update' in html).lower()}")
        lines.append(f"html_has_connection_states: {str('STALE' in html and 'ERROR' in html and 'LOADING' in html).lower()}")
        lines.append(f"html_has_post_form: {str('<form' in lower).lower()}")
        lines.append(f"html_fetch_targets_readonly_only: {str(fetch_readonly_only).lower()}")
        lines.append(f"live_allowed: {str(vm['live_allowed']).lower()}")
        lines.append("paper_ready: false")
        lines.append("live_ready: false")
        lines.extend(self._v82_safety_footer())
        lines.append("TRADER DASHBOARD CONTRACT V10.4 END")
        return "\n".join(lines)

    # ---------------------------------------------------------------
    # V10.4.3 — runtime health audit, learning/edge diagnostic and
    # runtime efficiency diagnostic (all read-only; no writes, no APIs).
    # ---------------------------------------------------------------
    def _v1043_fetch_local_health(self) -> tuple[dict, str]:
        """Best-effort GET of the bot's OWN local /health (no external API).
        Returns (payload, source) where source is ok|unavailable."""
        import json as _json
        import urllib.request as _url
        port = int(getattr(self.config, "port", 8080) or 8080)
        try:
            with _url.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
                return _json.loads(resp.read().decode("utf-8")), "ok"
        except Exception:
            return {}, "unavailable"

    def _v1043_git_commit(self) -> str:
        import subprocess
        try:
            out = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            return out.stdout.strip() if out.returncode == 0 else "unknown"
        except Exception:
            return "unknown"

    def runtime_health_audit_v104_cli(self) -> str:
        from .labs.runtime_audit_v10_4_3 import (
            NEEDS_RUNTIME_CONTEXT,
            build_runtime_health_audit,
            count_db_tables,
        )
        from .labs.trader_dashboard_v104 import dashboard_contract
        health, source = self._v1043_fetch_local_health()
        report = build_runtime_health_audit(
            config=self.config,
            db_counts=count_db_tables(self.db),
            health=health,
            health_source=source,
            git_commit=self._v1043_git_commit(),
            dashboard_contract=dashboard_contract(),
            log_audit=NEEDS_RUNTIME_CONTEXT,
        )
        lines = ["RUNTIME HEALTH AUDIT V10.4.3 START"]
        lines.append(f"git_commit: {report['git_commit']}")
        for key, value in report["runtime"].items():
            lines.append(f"runtime {key}: {value}")
        for key, value in report["safety"].items():
            lines.append(f"safety {key}: {str(value).lower()}")
        for key, value in report["dashboard"].items():
            lines.append(f"dashboard {key}: {value}")
        lines.append(f"log_audit: {report['log_audit']}")
        for table, count in report["db_counts"].items():
            lines.append(f"db {table}: {count}")
        lines.append("warnings: " + (",".join(report["warnings"]) or "NONE"))
        lines.append("attention: " + (",".join(report["attention"]) or "NONE"))
        lines.append("unsafe_blockers: " + (",".join(report["unsafe_blockers"]) or "NONE"))
        lines.append(f"verdict: {report['verdict']}")
        lines.append(f"paper_ready: {str(report['paper_ready']).lower()}")
        lines.append(f"live_ready: {str(report['live_ready']).lower()}")
        lines.extend(self._v82_safety_footer())
        lines.append("RUNTIME HEALTH AUDIT V10.4.3 END")
        return "\n".join(lines)

    def learning_edge_diagnostic_v104_cli(self, hours: int = 24) -> str:
        from .candidate_ranking import CandidateRanking
        from .labs.runtime_audit_v10_4_3 import (
            build_learning_edge_diagnostic,
            count_db_tables,
        )
        from .net_edge_lab import NetEdgeLab
        try:
            ranking = CandidateRanking(self.config, self.db).build(hours=int(hours))
        except Exception:
            ranking = {"status": "unavailable"}
        try:
            net_edge = NetEdgeLab(self.config, self.db).build(hours=int(hours))
        except Exception:
            net_edge = {}
        # V10.4.3.1 — derive history/OI blockers from a REAL data-readiness
        # snapshot when available; otherwise the diagnostic reports UNKNOWN.
        try:
            from .labs.external_data_provider_registry_v10_3 import run_data_source_audit
            from .labs.external_edge_ingest_v10_1 import read_input_dir
            market_clean, _m = self._v101_load_clean("perp_market_state")
            raw_rows, _u = read_input_dir(f"{self._V101_RAW}/perp_market_state")
            audit = run_data_source_audit(market_clean, raw_rows, hours=8760)
            data_readiness = audit.as_dict() if hasattr(audit, "as_dict") else None
        except Exception:
            data_readiness = None
        report = build_learning_edge_diagnostic(
            db_counts=count_db_tables(self.db),
            ranking=ranking,
            net_edge=net_edge,
            data_readiness=data_readiness,
        )
        lines = ["LEARNING EDGE DIAGNOSTIC V10.4.3 START"]
        lines.append(f"hours: {int(hours)}")
        lines.append(f"learning_status: {report['learning_status']}")
        for key, value in report["learning_infra"].items():
            lines.append(f"learning {key}: {value}")
        lines.append("learning_gaps:")
        lines.extend(f"- {g}" for g in report["learning_gaps"])
        for key, value in report["data_readiness_derived"].items():
            lines.append(f"data_readiness {key}: {value}")
        lines.append(f"edge_status: {report['edge_status']}")
        lines.append(f"candidate_ranking_status: {report['candidate_ranking_status']}")
        lines.append(f"top_candidates_count: {report['top_candidates_count']}")
        lines.append(f"validated_top_candidates_count: {report['validated_top_candidates_count']}")
        lines.append(f"watchlist_count: {report['watchlist_count']}")
        lines.append(f"reject_count: {report['reject_count']}")
        for reason, count in sorted(report["reject_reasons"].items()):
            lines.append(f"reject_reason {reason}: {count}")
        lines.append("top_blockers:")
        lines.extend(f"- {b}" for b in report["top_blockers"])
        lines.append("false_hope_warnings:")
        lines.extend(f"- {w}" for w in (report["false_hope_warnings"] or ["NONE"]))
        lines.append("highest_value_next_steps:")
        lines.extend(f"- {s}" for s in report["highest_value_next_steps"])
        lines.append("what_not_to_do:")
        lines.extend(f"- {w}" for w in report["what_not_to_do"])
        lines.append(f"paper_ready: {str(report['paper_ready']).lower()}")
        lines.append(f"live_ready: {str(report['live_ready']).lower()}")
        lines.extend(self._v82_safety_footer())
        lines.append("LEARNING EDGE DIAGNOSTIC V10.4.3 END")
        return "\n".join(lines)

    def runtime_efficiency_diagnostic_v104_cli(self) -> str:
        from .labs.runtime_audit_v10_4_3 import (
            build_runtime_efficiency,
            count_db_tables,
        )
        memory_mb: float | None = None
        try:
            import resource
            memory_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
        except Exception:
            memory_mb = None
        report = build_runtime_efficiency(
            config=self.config,
            db_counts=count_db_tables(self.db),
            memory_mb=memory_mb,
        )
        lines = ["RUNTIME EFFICIENCY DIAGNOSTIC V10.4.3 START"]
        lines.append(f"scan_interval_seconds: {report['scan_interval_seconds']}")
        lines.append(f"worker_lightweight_mode: {str(report['worker_lightweight_mode']).lower()}")
        lines.append(f"latency_metrics_rows: {report['latency_metrics_rows']}")
        lines.append(f"signal_path_metrics_rows: {report['signal_path_metrics_rows']}")
        lines.append(f"memory_mb: {report['memory_mb']}")
        lines.append(f"cpu: {report['cpu']}")
        lines.append("findings:")
        lines.extend(f"- {f}" for f in report["findings"])
        lines.append("recommendations_read_only:")
        lines.extend(f"- {r}" for r in report["recommendations_read_only"])
        lines.append(f"auto_tuning_applied: {str(report['auto_tuning_applied']).lower()}")
        lines.extend(self._v82_safety_footer())
        lines.append("RUNTIME EFFICIENCY DIAGNOSTIC V10.4.3 END")
        return "\n".join(lines)

    # ---------------------------------------------------------------
    # V10.5 — provider verification scorecards, data readiness and
    # command-center dashboard contract (all read-only, no network).
    # ---------------------------------------------------------------
    def provider_verification_v105_cli(self) -> str:
        from .labs.provider_verification_v10_5 import (
            COMMERCIAL_CHECKS,
            QUALITY_CHECKS,
            REQUIRED_DATA_TYPES,
            REQUIRED_HISTORY,
            REQUIRED_SYMBOLS,
            REQUIRED_TIMEFRAMES,
            run_provider_verification_v105,
        )
        rep = run_provider_verification_v105()
        lines = ["PROVIDER VERIFICATION V10.5 START"]
        lines.append(f"primary: {rep.primary}")
        lines.append(f"fallback: {rep.fallback}")
        lines.append(f"cross_check: {rep.cross_check}")
        lines.append(f"proxy_only: {rep.proxy_only}")
        lines.append("symbols_required: " + ",".join(REQUIRED_SYMBOLS))
        lines.append(f"required_history_days: min={REQUIRED_HISTORY['minimum_days']} preferred={REQUIRED_HISTORY['preferred_days']}")
        lines.append("required_data_types: " + ",".join(REQUIRED_DATA_TYPES))
        lines.append("required_timeframes: " + ",".join(REQUIRED_TIMEFRAMES))
        lines.append("quality_checks: " + ",".join(QUALITY_CHECKS))
        lines.append("commercial_checks: " + ",".join(COMMERCIAL_CHECKS))
        for p in rep.providers:
            lines.append(
                f"provider {p['provider_name']}: role={p['role']} status={p['status']} "
                f"bitget_perp={p['bitget_perp_supported']} "
                f"history_confirmed={p['history_confirmed']} "
                f"sample_received={str(p['sample_received']).lower()} "
                f"sample_validated={str(p['sample_validated']).lower()} "
                f"paid_download_authorized={str(p['paid_download_authorized']).lower()}"
            )
            lines.append(f"  notes: {p['notes']}")
        lines.append("sample_rule: must obtain and schema-validate BTCUSDT+ETHUSDT 7-30d sample before any purchase")
        lines.append(f"any_provider_ready_for_authorization: {str(rep.any_provider_ready_for_authorization).lower()}")
        lines.append(f"any_paid_download_authorized: {str(rep.any_paid_download_authorized).lower()}")
        lines.append(f"no_external_calls_made: {str(rep.no_external_calls_made).lower()}")
        lines.append(f"paper_ready: {str(rep.paper_ready).lower()}")
        lines.append(f"live_ready: {str(rep.live_ready).lower()}")
        lines.extend(self._v82_safety_footer())
        lines.append("PROVIDER VERIFICATION V10.5 END")
        return "\n".join(lines)

    def data_readiness_v105_cli(self) -> str:
        from .labs.data_foundation_v10_5 import build_data_readiness_v105
        from .labs.provider_verification_v10_5 import run_provider_verification_v105
        try:
            from .labs.external_data_provider_registry_v10_3 import run_data_source_audit
            from .labs.external_edge_ingest_v10_1 import read_input_dir
            market_clean, _m = self._v101_load_clean("perp_market_state")
            raw_rows, _u = read_input_dir(f"{self._V101_RAW}/perp_market_state")
            audit = run_data_source_audit(market_clean, raw_rows, hours=8760)
            snapshot = audit.as_dict() if hasattr(audit, "as_dict") else None
        except Exception:
            snapshot = None
        report = build_data_readiness_v105(
            data_readiness_snapshot=snapshot,
            provider_report=run_provider_verification_v105().as_dict(),
        )
        lines = ["DATA READINESS V10.5 START"]
        lines.append(f"status: {report.status}")
        lines.append(f"clean_days: {report.clean_days}")
        lines.append(f"history_status: {report.history_status}")
        lines.append(f"oi_status: {report.oi_status}")
        lines.append(f"oi_bucket_policy: {report.oi_bucket_policy}")
        lines.append(f"funding_status: {report.funding_status}")
        lines.append(f"liquidations_status: {report.liquidations_status}")
        lines.append(f"backtester_readiness: {report.backtester_readiness}")
        lines.append(f"provider_readiness: {report.provider_readiness}")
        lines.append("top_blockers:")
        lines.extend(f"- {b}" for b in report.top_blockers)
        lines.append(f"next_required_human_action: {report.next_required_human_action}")
        lines.append(f"paper_ready: {str(report.paper_ready).lower()}")
        lines.append(f"live_ready: {str(report.live_ready).lower()}")
        lines.extend(self._v82_safety_footer())
        lines.append("DATA READINESS V10.5 END")
        return "\n".join(lines)

    def trader_dashboard_contract_v105_cli(self) -> str:
        from .labs.trader_dashboard_v104 import (
            DISABLED_CONTROLS,
            LOCK_TOOLTIP,
            build_dashboard_view_model,
            dashboard_contract,
            render_dashboard_html,
        )
        contract = dashboard_contract()
        vm = build_dashboard_view_model()
        html = render_dashboard_html(vm)
        lower = html.lower()
        import re as _re
        fetch_targets = _re.findall(r'"(/api/[^"]+)"', html)
        fetch_readonly_only = all(
            t.startswith("/api/researchops/v104/") for t in fetch_targets
        ) and bool(fetch_targets)
        sections = {
            "mission_bar": "MISSION BAR" in html or "mission-bar" in html,
            "pipeline": "PIPELINE" in html,
            "why_no_edge": "WHY NO TRADE" in html.upper() or "WHY NO EDGE" in html.upper(),
            "provider_panel": "Provider Readiness" in html or "PROVIDER READINESS" in html.upper(),
            "learning_panel": "LEARNING" in html.upper(),
            "strategy_lab": "STRATEGY RESEARCH LAB" in html.upper(),
            "ssh_tunnel_help": "SSH TUNNEL" in html.upper(),
        }
        locked_extras = {
            "copy_trading": "Copy Trading" in html,
            "leverage_control": "Leverage Control" in html,
            "casino_mode": "777" in html,
        }
        lines = ["TRADER DASHBOARD CONTRACT V10.5 START"]
        lines.append(f"route: {contract['route']}")
        lines.append(f"read_only: {str(contract['read_only']).lower()}")
        lines.append("methods: GET_only")
        lines.append(f"mutable_endpoints: {','.join(contract['mutable_endpoints']) or 'NONE'}")
        lines.append(f"post_forms: {contract['post_forms']}")
        lines.append(f"heavy_panels_mode: {contract.get('heavy_panels_mode', 'CACHE_PEEK_ONLY')}")
        lines.append(f"heavy_refresh_mode: {contract.get('heavy_refresh_mode', 'CLI_ONLY')}")
        for name, present in sections.items():
            lines.append(f"section {name}: {str(present).lower()}")
        lines.append("disabled_controls: " + ",".join(DISABLED_CONTROLS))
        for name, present in locked_extras.items():
            lines.append(f"locked_control {name}: {str(present).lower()}")
        lines.append(f"lock_tooltip: {LOCK_TOOLTIP}")
        lines.append(f"html_has_no_live: {str('NO LIVE' in html).lower()}")
        lines.append(f"html_has_research_only: {str('RESEARCH ONLY' in html).lower()}")
        lines.append(f"html_has_post_form: {str('<form' in lower).lower()}")
        lines.append(f"html_fetch_targets_readonly_only: {str(fetch_readonly_only).lower()}")
        lines.append(f"html_exposes_token_value: false")
        lines.append("live_toggle_functional: false")
        lines.append("paper_filter_toggle_functional: false")
        lines.append("leverage_controls_functional: false")
        lines.append("copy_trading_functional: false")
        lines.append("casino_spin_functional: false")
        lines.append("paper_ready: false")
        lines.append("live_ready: false")
        lines.extend(self._v82_safety_footer())
        lines.append("TRADER DASHBOARD CONTRACT V10.5 END")
        return "\n".join(lines)

    def rebound_sign_integrity_v8293_cli(
        self, hours: int = 168, limit: int = 50000,
    ) -> str:
        from .labs.counterfactual_training_dataset import build_dataset
        from .labs.edgeguard_repeat_dedup_v8_2_9 import dedup_edgeguard_repeats
        from .labs.rebound_long_candidate_extractor_v8_2_9 import (
            extract_rebound_long_candidates,
        )
        from .labs.rebound_outcome_sign_integrity_v8_2_9_3 import (
            audit_sign_integrity,
        )
        dataset, _ = build_dataset(self.db, hours=int(hours), limit=int(limit))
        extractor = extract_rebound_long_candidates(
            self.db, hours=int(hours), limit=int(limit), rows=dataset,
        )
        deduped, _ = dedup_edgeguard_repeats(extractor.candidates, hours=int(hours))
        r = audit_sign_integrity(deduped, dataset_rows=dataset, hours=int(hours))
        lines = ["REBOUND SIGN INTEGRITY V8.2.9.3 START"]
        lines.append(f"hours: {r.hours} status: {r.status}")
        lines.append(f"total_candidates: {r.total_candidates}")
        lines.append(f"sign_bug_count: {r.sign_bug_count}")
        lines.append(f"sign_bug_ratio: {r.sign_bug_ratio:.4f}")
        lines.append(
            f"outcome_field_mismatch_count: {r.outcome_field_mismatch_count}"
        )
        for k, v in r.by_mismatch_type.items():
            lines.append(f"by_mismatch {k}: {v}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("REBOUND SIGN INTEGRITY V8.2.9.3 END")
        return "\n".join(lines)

    def canonical_outcome_v8293_cli(
        self, hours: int = 168, limit: int = 50000,
    ) -> str:
        from .labs.counterfactual_training_dataset import build_dataset
        from .labs.edgeguard_repeat_dedup_v8_2_9 import dedup_edgeguard_repeats
        from .labs.outcome_field_canonicalizer_v8_2_9_3 import canonicalize_rows
        from .labs.rebound_long_candidate_extractor_v8_2_9 import (
            extract_rebound_long_candidates,
        )
        dataset, _ = build_dataset(self.db, hours=int(hours), limit=int(limit))
        extractor = extract_rebound_long_candidates(
            self.db, hours=int(hours), limit=int(limit), rows=dataset,
        )
        deduped, _ = dedup_edgeguard_repeats(extractor.candidates, hours=int(hours))
        r = canonicalize_rows(deduped, hours=int(hours))
        lines = ["CANONICAL OUTCOME V8.2.9.3 START"]
        lines.append(f"hours: {r.hours} status: {r.status}")
        lines.append(f"rows_audited: {r.rows_audited}")
        lines.append(f"ok_count: {r.ok_count}")
        lines.append(f"need_ohlcv_path_count: {r.need_ohlcv_path_count}")
        lines.append(f"field_mismatch_count: {r.field_mismatch_count}")
        lines.append(f"sign_suspect_count: {r.sign_suspect_count}")
        lines.append(f"need_data_count: {r.need_data_count}")
        lines.append(
            f"canonical_outcome_ok_ratio: {r.canonical_outcome_ok_ratio:.4f}"
        )
        lines.append(
            f"canonical_outcome_source_top: {r.canonical_outcome_source_top or 'NONE'}"
        )
        for k, v in r.by_source.items():
            lines.append(f"by_source {k}: {v}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("CANONICAL OUTCOME V8.2.9.3 END")
        return "\n".join(lines)

    def exit_bar_replay_v8293_cli(
        self, hours: int = 168, limit: int = 50000,
    ) -> str:
        from .labs.counterfactual_training_dataset import build_dataset
        from .labs.edgeguard_repeat_dedup_v8_2_9 import dedup_edgeguard_repeats
        from .labs.exit_bar_by_bar_replay_v8_2_9_3 import run_bar_by_bar_replay
        from .labs.rebound_long_candidate_extractor_v8_2_9 import (
            extract_rebound_long_candidates,
        )
        dataset, _ = build_dataset(self.db, hours=int(hours), limit=int(limit))
        extractor = extract_rebound_long_candidates(
            self.db, hours=int(hours), limit=int(limit), rows=dataset,
        )
        deduped, _ = dedup_edgeguard_repeats(extractor.candidates, hours=int(hours))
        r = run_bar_by_bar_replay(deduped, hours=int(hours))
        lines = ["EXIT BAR-BY-BAR REPLAY V8.2.9.3 START"]
        lines.append(f"hours: {r.hours} status: {r.status}")
        lines.append(f"rows_audited: {r.rows_audited}")
        lines.append(f"replay_rows: {r.replay_rows}")
        lines.append(f"need_data_rows: {r.need_data_rows}")
        lines.append(
            f"bar_by_bar_replay_available: "
            f"{str(r.bar_by_bar_replay_available).lower()}"
        )
        lines.append(
            f"best_policy_bar_by_bar: {r.best_policy_bar_by_bar or 'NONE'}"
        )
        lines.append(
            f"best_policy_bar_by_bar_status: {r.best_policy_bar_by_bar_status}"
        )
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("EXIT BAR-BY-BAR REPLAY V8.2.9.3 END")
        return "\n".join(lines)

    def rebound_strict_oos_canonical_v8293_cli(
        self, hours: int = 168, limit: int = 50000,
    ) -> str:
        from .labs.counterfactual_training_dataset import build_dataset
        from .labs.edgeguard_repeat_dedup_v8_2_9 import dedup_edgeguard_repeats
        from .labs.rebound_long_candidate_extractor_v8_2_9 import (
            extract_rebound_long_candidates,
        )
        from .labs.rebound_long_strict_oos_canonical_v8_2_9_3 import (
            run_strict_oos_canonical,
        )
        dataset, _ = build_dataset(self.db, hours=int(hours), limit=int(limit))
        extractor = extract_rebound_long_candidates(
            self.db, hours=int(hours), limit=int(limit), rows=dataset,
        )
        deduped, dedup_report = dedup_edgeguard_repeats(
            extractor.candidates, hours=int(hours),
        )
        r = run_strict_oos_canonical(
            deduped, hours=int(hours),
            score_anti_calibrated=True,
            duplicate_ratio_after=dedup_report.duplicate_ratio_after,
            input_is_deduped=True,
        )
        lines = ["REBOUND STRICT OOS CANONICAL V8.2.9.3 START"]
        lines.append(f"hours: {r.hours} status: {r.status}")
        lines.append(f"candidates_input: {r.candidates_input}")
        lines.append(
            f"candidates_with_canonical_ok: {r.candidates_with_canonical_ok}"
        )
        lines.append(f"canonical_ok_ratio: {r.canonical_ok_ratio:.4f}")
        lines.append(f"sign_bug_ratio: {r.sign_bug_ratio:.4f}")
        lines.append(f"final_status_top_level: {r.final_status_top_level}")
        lines.append(
            f"rejected_for_sign_bug: {str(r.rejected_for_sign_bug).lower()}"
        )
        lines.append(
            f"rejected_for_canonical_insufficient: "
            f"{str(r.rejected_for_canonical_insufficient).lower()}"
        )
        for k, v in r.by_final_status.items():
            lines.append(f"by_final_status {k}: {v}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("REBOUND STRICT OOS CANONICAL V8.2.9.3 END")
        return "\n".join(lines)

    def rebound_outcome_reconciliation_v829_cli(
        self, hours: int = 168, limit: int = 50000,
    ) -> str:
        from .labs.rebound_outcome_reconciliation_v8_2_9 import (
            reconcile_rebound_outcome,
        )
        r = reconcile_rebound_outcome(self.db, hours=int(hours), limit=int(limit))
        lines = ["REBOUND OUTCOME RECONCILIATION V8.2.9 START"]
        lines.append(f"hours: {r.hours} status: {r.status}")
        lines.append(f"candidates_v828_like: {r.candidates_v828_like}")
        lines.append(f"candidates_v829_raw: {r.candidates_v829_raw}")
        lines.append(f"candidates_v829_dedup: {r.candidates_v829_dedup}")
        lines.append(f"winrate_v828_like: {r.winrate_v828_like:.4f}")
        lines.append(f"winrate_v829_raw: {r.winrate_v829_raw:.4f}")
        lines.append(f"winrate_v829_dedup: {r.winrate_v829_dedup:.4f}")
        lines.append(f"net_ev_before_cost: {r.net_ev_before_cost:.4f}")
        lines.append(f"net_ev_after_cost_0_25: {r.net_ev_after_cost_0_25:.4f}")
        lines.append(f"sign_bug_count: {r.sign_bug_count}")
        lines.append(
            f"outcome_field_mismatch_count: {r.outcome_field_mismatch_count}"
        )
        lines.append(f"reason_for_gap: {r.reason_for_gap}")
        lines.append(
            f"used_future_return_features: "
            f"{str(bool(r.used_future_return_features)).lower()}"
        )
        for note in r.notes:
            lines.append(f"note: {note}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("REBOUND OUTCOME RECONCILIATION V8.2.9 END")
        return "\n".join(lines)

    def _render_training_summary(self, summary, *, hours: int, limit: int) -> str:
        lines = ["COUNTERFACTUAL TRAINING SUMMARY START"]
        lines.append(f"hours: {int(hours)} limit: {int(limit)}")
        lines.append(f"status: {summary.status}")
        lines.append(f"total_rows: {summary.total_rows}")
        lines.append(f"use_for_training_count: {summary.use_for_training_count}")
        lines.append(f"need_data_count: {summary.need_data_count}")
        lines.append(f"blocked_winner_count: {summary.blocked_winner_count}")
        lines.append(f"blocked_loser_count: {summary.blocked_loser_count}")
        lines.append(f"good_not_monetized_count: {summary.good_not_monetized_count}")
        lines.append(f"net_ev_avg_est_pct: {summary.net_ev_avg_est_pct:.4f}")
        for k, v in summary.by_label.items():
            lines.append(f"by_label {k}: {v}")
        for k, v in summary.by_side.items():
            lines.append(f"by_side {k}: {v}")
        lines.extend(self._v82_safety_footer())
        warning = self._v82_heavy_warning(hours)
        if warning:
            lines.append(warning)
        lines.append("COUNTERFACTUAL TRAINING SUMMARY END")
        return "\n".join(lines)

    def validation_gates_v9_status(self, hours: int = 24) -> str:
        from .validation_gates_v9 import run_validation_gates_v9
        report = run_validation_gates_v9(strategy_id="placeholder", net_returns=[])
        lines = ["VALIDATION GATES V9 START"]
        lines.append(f"strategy_id: {report.strategy_id}")
        lines.append(f"samples: {report.samples} overall: {report.overall_status}")
        lines.append(f"pass: {report.pass_count} fail: {report.fail_count} need_data: {report.need_data_count}")
        for g in report.gates:
            lines.append(f"gate={g.name} status={g.status} reason={g.reason}")
        lines.append(f"research_only: {str(report.research_only).lower()}")
        lines.append(f"final_recommendation: {report.final_recommendation}")
        lines.append("VALIDATION GATES V9 END")
        return "\n".join(lines)

    def fast_signal_shadow(
        self,
        hours: int = 72,
        symbols: list[str] | None = None,
        timeframe: str = "5m",
    ) -> str:
        from .fast_signal_shadow import fast_signal_shadow_text
        return fast_signal_shadow_text(self.config, self.db, hours=hours, timeframe=timeframe, symbols=symbols)

    def research_pack(self, hours: int = 24) -> str:
        from .research_pack import build_research_pack, render_research_pack_text
        return render_research_pack_text(build_research_pack(self.config, self.db, hours=hours))

    def research_cockpit(
        self,
        latest_backtest_decision: str = "UNKNOWN",
        latest_breakdown_decision: str = "UNKNOWN",
        latest_policy_decision: str = "UNKNOWN",
    ) -> str:
        from .research_cockpit import build_cockpit_state, render_cockpit_text
        state = build_cockpit_state(
            self.config, self.db,
            mode="paper",
            latest_backtest_decision=latest_backtest_decision,
            latest_breakdown_decision=latest_breakdown_decision,
            latest_policy_decision=latest_policy_decision,
        )
        return render_cockpit_text(state)

    def trade_replay_export(
        self,
        symbol: str = "BTCUSDT",
        hours: int = 72,
        timeframe: str = "5m",
        max_candles: int = 1200,
        max_trades: int = 200,
    ) -> str:
        """Emit a JSON payload with OHLCV candles + simulated trades for a symbol.

        Intended for a future chart/UI consumer. NO exchange calls, NO real orders.
        """
        from .trade_replay_export import (
            build_replay_payload, export_replay_json,
        )

        payload = build_replay_payload(
            self.config, self.db,
            symbol=symbol, hours=hours, timeframe=timeframe,
            max_candles=max_candles, max_trades=max_trades,
        )
        return export_replay_json(payload)

    def real_strategy_backtester_smoke_test(self) -> str:
        from .real_strategy_backtester import real_strategy_backtester_smoke_text

        return real_strategy_backtester_smoke_text(self.config)

    def ohlcv_replay_loader_audit(self, hours: int = 72) -> str:
        from .ohlcv_replay_loader import ohlcv_replay_loader_audit_text

        return ohlcv_replay_loader_audit_text(self.config, self.db, hours=hours)

    def ohlcv_replay_loader_smoke_test(self) -> str:
        from .ohlcv_replay_loader import ohlcv_replay_loader_smoke_text

        return ohlcv_replay_loader_smoke_text()

    def duplicate_module_audit(self) -> str:
        from .duplicate_module_audit import duplicate_module_audit_text

        return duplicate_module_audit_text()

    def duplicate_module_audit_smoke_test(self) -> str:
        from .duplicate_module_audit import duplicate_module_audit_smoke_text

        return duplicate_module_audit_smoke_text()

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


def build_argument_parser() -> argparse.ArgumentParser:
    """V8.2.3 — Build the ``research_lab`` argparse parser without parsing.

    Extracted from :func:`main` so tests can verify the parser is
    constructible (no ``conflicting option string`` errors) without going
    through ``sys.argv`` or subprocesses. Single source of truth for both
    runtime and tests.
    """
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
            "exit-label-calibration-v2",
            "score-calibration",
            "score-calibration-smoke-test",
            "candidate-incubator",
            "candidate-incubator-smoke-test",
            "training-data-integrity",
            "training-data-integrity-smoke-test",
            "worker-health-audit",
            "worker-health-audit-smoke-test",
            "data-vault-audit",
            "dashboard-data-binding-audit",
            "dashboard-data-binding-smoke-test",
            "data-pipeline-diagnosis",
            "data-pipeline-diagnosis-smoke-test",
            "relation-repair-audit",
            "relation-repair-audit-smoke-test",
            "label-quality-v2",
            "label-quality-v2-smoke-test",
            "cost-model-inventory",
            "bitget-cost-model-audit",
            "bitget-cost-model-smoke-test",
            "margin-mode-audit",
            "margin-mode-audit-smoke-test",
            "core-corrections",
            "core-corrections-smoke-test",
            "cost-model-correction-smoke-test",
            "funding-model-smoke-test",
            "labeler-guard-smoke-test",
            "duplicate-guard-smoke-test",
            "candidate-actionability-smoke-test",
            "execution-safety-audit",
            "net-rr-audit",
            "dynamic-exit-policy-audit",
            "structural-stop-audit",
            "execution-safety-smoke-test",
            "net-rr-smoke-test",
            "dynamic-exit-policy-smoke-test",
            "structural-stop-smoke-test",
            "fresh-balance-risk-smoke-test",
            "execution-idempotency-smoke-test",
            "emergency-failsafe-smoke-test",
            "circuit-breaker-magnitude-smoke-test",
            "clock-drift-smoke-test",
            "config-hardening-smoke-test",
            "exit-policy-v3-smoke-test",
            "exit-policy-v3-backtest",
            "sudden-move-smoke-test",
            "sudden-move-detector",
            "pre-move-v2-smoke-test",
            "pre-move-v2",
            "walk-forward-smoke-test",
            "walk-forward-validator",
            "anti-overfit-v2-smoke-test",
            "anti-overfit-v2",
            "candidate-promotion-v2-smoke-test",
            "candidate-promotion-v2",
            "shadow-strategy-simulator-smoke-test",
            "shadow-strategy-simulator",
            "operational-intelligence-audit",
            "strategy-research-library-smoke-test",
            "strategy-research-library",
            "real-strategy-backtester-smoke-test",
            "real-strategy-backtest",
            "real-strategy-backtest-multi",
            "real-strategy-backtest-breakdown",
            "final-policy-builder",
            "trade-replay-export",
            "cost-stress-summary",
            "profit-lock-lab",
            "fast-exit-lab",
            "time-death-reducer-lab",
            "time-exit-autopsy-v2",
            "dynamic-hold-lab",
            "entry-exhaustion-lab",
            "reversal-candidate-lab",
            "exit-policy-v2",
            "phase8-candidate-validator",
            "phase8-cost-stress",
            "dot-regime-diagnosis",
            "dot-regime-filter-lab",
            "phase9-paper-readiness",
            "net-profit-lock-lab",
            "fast-signal-shadow",
            "research-pack",
            "research-cockpit",
            "ohlcv-freshness-status",
            "ohlcv-freshness-refresh",
            "training-clean-view-audit",
            "shadow-multi-trade-status",
            "shadow-multi-trade-replay",
            "capital-leverage-sim",
            "fee-aware-exit-trainer",
            "strategy-research-enhancer",
            "clean-research-metrics",
            "data-pipeline-root-cause",
            "clean-strategy-lab",
            "capital-scaling-simulator",
            "research-pack-v7",
            "duplicate-guard-hook-status",
            "funding-cost-model",
            "liquidation-model-bitget",
            "walk-forward-v2",
            "research-pack-v7-5",
            "auto-data-enrichment-status",
            "exit-intelligence-lab",
            "strategy-experiment-registry",
            "shadow-candidate-lifecycle",
            "validation-gates-v9",
            "event-catalyst-status",
            "listing-tracker-audit",
            "unlock-watchlist-audit",
            "perp-availability-audit",
            "shortability-score-audit",
            "event-candidate-registry-status",
            "research-pack-event-v1",
            "bidirectional-funnel",
            "missed-opportunities",
            "blocked-counterfactual",
            "failed-executed",
            "good-not-monetized",
            "score-asymmetry-audit",
            "score-symmetric-simulation",
            "score-atr-softened-simulation",
            "score-high-vol-directional-simulation",
            "regime-router-simulation",
            "trend-campaign-sim",
            "profit-lock-sim",
            "research-pack-bidirectional-v1",
            "future-returns-bridge",
            "edgeguard-counterfactual",
            "counterfactual-training-dataset",
            "export-counterfactual-training-dataset",
            "training-dataset-summary",
            "counterfactual-dedup-audit",
            "short-sign-barrier-audit",
            "score-calibration-audit",
            "counterfactual-cost-stress",
            "export-counterfactual-clean-v2",
            "research-pack-counterfactual-quality-v1",
            "candidate-rule-miner-v826",
            "candidate-rule-walkforward-v826",
            "short-barrier-debug-v826",
            "score-recalibration-sandbox-v826",
            "export-research-v826",
            "research-pack-v826",
            "strict-oos-rule-selector-v827",
            "short-barrier-debug-v827",
            "final-rule-gate-v827",
            "export-research-v827",
            "research-pack-v827",
            "dual-side-barrier-audit-v828",
            "duplicate-root-cause-v828",
            "side-aware-score-calibration-v828",
            "rebound-regime-turn-lab-v828",
            "export-research-v828",
            "research-pack-v828",
            "rebound-long-candidates-v829",
            "edgeguard-repeat-dedup-v829",
            "score-gate-sandbox-v829",
            "exit-monetization-audit-v829",
            "rebound-long-strict-oos-v829",
            "adversarial-research-audit-v829",
            "export-research-v829",
            "research-pack-v829",
            "rebound-outcome-reconciliation-v829",
            "rebound-sign-integrity-v8293",
            "canonical-outcome-v8293",
            "exit-bar-replay-v8293",
            "rebound-strict-oos-canonical-v8293",
            "signal-path-bridge-v8295",
            "canonical-real-outcome-v8295",
            "strategy-tournament-real-v8295",
            "export-research-v8295",
            "research-pack-v8295",
            "signal-path-bridge-v8296",
            "canonical-real-outcome-v8296",
            "strategy-tournament-real-v8296",
            "export-research-v8296",
            "research-pack-v8296",
            "edge-data-foundation-v10",
            "funding-oi-liquidation-research-v10",
            "token-unlock-post-listing-research-v10",
            "intraday-volatility-breakdown-v10",
            "micro-tp-viability-v10",
            "event-catalyst-layer-v10",
            "edge-discovery-orchestrator-v10",
            "alpha-ensemble-v10",
            "external-edge-ingest-v101",
            "external-data-health-v101",
            "external-event-study-v101",
            "external-funding-oi-diagnostics-v101",
            "external-funding-oi-stability-v101",
            "external-missing-oi-audit-v102",
            "external-long-history-validation-v102",
            "strategy-replay-backtest-v103",
            "external-data-source-audit-v103",
            "external-provider-readiness-v103",
            "external-provider-verification-v104",
            "external-data-acquisition-plan-v104",
            "external-research-intake-v104",
            "edge-hunter-contract-v104",
            "trader-dashboard-contract-v104",
            "runtime-health-audit-v104",
            "learning-edge-diagnostic-v104",
            "runtime-efficiency-diagnostic-v104",
            "provider-verification-v105",
            "data-readiness-v105",
            "trader-dashboard-contract-v105",
            "ohlcv-replay-loader-smoke-test",
            "ohlcv-replay-loader-audit",
            "duplicate-module-audit-smoke-test",
            "duplicate-module-audit",
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
            "pre-move-event-labeler",
            "pre-move-feature-snapshot",
            "pre-move-pattern-miner",
            "pre-move-similarity-scanner",
            "pre-move-smoke-test",
            "dashboard-pro-smoke-test",
            "dashboard-beauty-exit-calibration-smoke-test",
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
            "bot-integrity-audit",
            "security-audit",
            "label-time-audit",
            "paper-trading-audit",
            "research-modules-audit",
            "bot-integrity-audit-smoke-test",
            "dashboard-ui-v3-smoke-test",
            "dashboard-report-timeout-smoke-test",
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
    parser.add_argument("--side", default="", help="Lado para labs V8.2: LONG o SHORT.")
    parser.add_argument("--max-adds", type=int, default=3, help="Max adds para trend-campaign-sim.")
    # V8.2.3 — ``--policy`` already exists below (declared by Phase 8 / cost
    # stress / validator helpers). Re-declaring it here triggered
    # ``argparse.ArgumentError: argument --policy: conflicting option string``.
    # The dispatch for ``profit-lock-sim`` now treats the legacy Phase 8
    # default (``late_entry_block_plus_dynamic_hold``) as ``"all"``.
    parser.add_argument("--timeframe", default="5m", help="OHLCV timeframe para real-strategy-backtest-multi (default: 5m).")
    parser.add_argument("--group-by", default="symbol", help="Group-by tokens for real-strategy-backtest-breakdown (comma-separated, e.g. 'symbol,side,regime').")
    parser.add_argument("--min-trades", type=int, default=30, help="Min trades for breakdown/policy gate.")
    parser.add_argument("--top", type=int, default=25, help="Top-N groups to surface in breakdown.")
    parser.add_argument("--folds", type=int, default=4, help="Folds for walk-forward in final-policy-builder.")
    parser.add_argument("--data-quality-status", default="OK", help="Data quality status (OK/WARNING/BAD) passed to final-policy-builder.")
    parser.add_argument("--label-quality-status", default="OK", help="Label quality status (OK/WARNING/BAD) passed to final-policy-builder.")
    parser.add_argument("--max-candles", type=int, default=1200, help="Max candles for trade-replay-export.")
    parser.add_argument("--max-trades", type=int, default=200, help="Max trades for trade-replay-export.")
    parser.add_argument("--policy", default="late_entry_block_plus_dynamic_hold", help="Phase 8 policy name for cost stress / validator helpers.")
    parser.add_argument("--timeframes", default="", help="Comma-separated timeframes for OHLCV freshness manager (default 5m,15m,1h).")
    parser.add_argument("--allow-real-writes", action="store_true", help="Explicitly allow OHLCV freshness refresh to write to DB (ResearchOps V5).")
    parser.add_argument("--capital", type=float, default=40.0, help="Capital total USDT for capital-leverage-sim (default 40).")
    parser.add_argument("--margins", default="2,5,10,20", help="Margins (csv USDT) for capital-leverage-sim (default 2,5,10,20).")
    parser.add_argument("--leverages", default="1,3,5,10,20,50", help="Leverages (csv) for capital-leverage-sim (default 1,3,5,10,20,50).")
    parser.add_argument("--external-data-path", default="", help="Local CSV/JSON path with external edge data (funding/OI/liq/unlock/catalyst) for ResearchOps V10 labs. No network, no APIs.")
    parser.add_argument("--dataset", default="perp_market_state", help="V10.1 dataset: perp_market_state|perp_liquidations|token_unlock_events|listing_events.")
    parser.add_argument("--input", default="", help="V10.1 single local input file (CSV/JSON/NDJSON) for external-edge-ingest-v101.")
    parser.add_argument("--input-dir", default="", help="V10.1 input directory of local files for external-edge-ingest-v101.")
    parser.add_argument("--module", default="funding_oi_liq", help="V10.1 event-study module: funding_oi_liq|unlocks|listings.")
    return parser


def main() -> None:
    parser = build_argument_parser()
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
    elif args.command == "exit-label-calibration-v2":
        print(lab.exit_label_calibration_v2(hours=args.hours))
    elif args.command == "score-calibration":
        print(lab.score_calibration(hours=args.hours))
    elif args.command == "score-calibration-smoke-test":
        print(lab.score_calibration_smoke_test())
    elif args.command == "candidate-incubator":
        print(lab.candidate_incubator(hours=args.hours))
    elif args.command == "candidate-incubator-smoke-test":
        print(lab.candidate_incubator_smoke_test())
    elif args.command == "training-data-integrity":
        print(lab.training_data_integrity(hours=args.hours))
    elif args.command == "training-data-integrity-smoke-test":
        print(lab.training_data_integrity_smoke_test())
    elif args.command == "worker-health-audit":
        print(lab.worker_health_audit())
    elif args.command == "worker-health-audit-smoke-test":
        print(lab.worker_health_audit_smoke_test())
    elif args.command == "data-vault-audit":
        print(lab.data_vault_audit())
    elif args.command == "dashboard-data-binding-audit":
        print(lab.dashboard_data_binding_audit())
    elif args.command == "dashboard-data-binding-smoke-test":
        print(lab.dashboard_data_binding_smoke_test())
    elif args.command == "data-pipeline-diagnosis":
        print(lab.data_pipeline_diagnosis(hours=args.hours))
    elif args.command == "data-pipeline-diagnosis-smoke-test":
        print(lab.data_pipeline_diagnosis_smoke_test())
    elif args.command == "relation-repair-audit":
        print(lab.relation_repair_audit(hours=args.hours))
    elif args.command == "relation-repair-audit-smoke-test":
        print(lab.relation_repair_audit_smoke_test())
    elif args.command == "label-quality-v2":
        print(lab.label_quality_v2(hours=args.hours))
    elif args.command == "label-quality-v2-smoke-test":
        print(lab.label_quality_v2_smoke_test())
    elif args.command == "cost-model-inventory":
        print(lab.cost_model_inventory())
    elif args.command == "bitget-cost-model-audit":
        print(lab.bitget_cost_model_audit(hours=args.hours))
    elif args.command == "bitget-cost-model-smoke-test":
        print(lab.bitget_cost_model_smoke_test())
    elif args.command == "margin-mode-audit":
        print(lab.margin_mode_audit())
    elif args.command == "margin-mode-audit-smoke-test":
        print(lab.margin_mode_audit_smoke_test())
    elif args.command == "core-corrections":
        print(lab.core_corrections(hours=args.hours))
    elif args.command == "core-corrections-smoke-test":
        print(lab.core_corrections_smoke_test())
    elif args.command == "cost-model-correction-smoke-test":
        print(lab.cost_model_correction_smoke_test())
    elif args.command == "funding-model-smoke-test":
        print(lab.funding_model_smoke_test())
    elif args.command == "labeler-guard-smoke-test":
        print(lab.labeler_guard_smoke_test())
    elif args.command == "duplicate-guard-smoke-test":
        print(lab.duplicate_guard_smoke_test())
    elif args.command == "candidate-actionability-smoke-test":
        print(lab.candidate_actionability_smoke_test())
    elif args.command == "execution-safety-audit":
        print(lab.execution_safety_audit())
    elif args.command == "net-rr-audit":
        print(lab.net_rr_audit(hours=args.hours))
    elif args.command == "dynamic-exit-policy-audit":
        print(lab.dynamic_exit_policy_audit(hours=args.hours))
    elif args.command == "structural-stop-audit":
        print(lab.structural_stop_audit(hours=args.hours))
    elif args.command == "execution-safety-smoke-test":
        print(lab.execution_safety_smoke_test())
    elif args.command == "net-rr-smoke-test":
        print(lab.net_rr_smoke_test())
    elif args.command == "dynamic-exit-policy-smoke-test":
        print(lab.dynamic_exit_policy_smoke_test())
    elif args.command == "structural-stop-smoke-test":
        print(lab.structural_stop_smoke_test())
    elif args.command == "fresh-balance-risk-smoke-test":
        print(lab.fresh_balance_risk_smoke_test())
    elif args.command == "execution-idempotency-smoke-test":
        print(lab.execution_idempotency_smoke_test())
    elif args.command == "emergency-failsafe-smoke-test":
        print(lab.emergency_failsafe_smoke_test())
    elif args.command == "circuit-breaker-magnitude-smoke-test":
        print(lab.circuit_breaker_magnitude_smoke_test())
    elif args.command == "clock-drift-smoke-test":
        print(lab.clock_drift_smoke_test())
    elif args.command == "config-hardening-smoke-test":
        print(lab.config_hardening_smoke_test())
    elif args.command == "exit-policy-v3-smoke-test":
        print(lab.exit_policy_v3_smoke_test())
    elif args.command == "exit-policy-v3-backtest":
        print(lab.exit_policy_v3_backtest(hours=args.hours))
    elif args.command == "sudden-move-smoke-test":
        print(lab.sudden_move_smoke_test())
    elif args.command == "sudden-move-detector":
        print(lab.sudden_move_detector(hours=args.hours))
    elif args.command == "pre-move-v2-smoke-test":
        print(lab.pre_move_v2_smoke_test())
    elif args.command == "pre-move-v2":
        print(lab.pre_move_v2(hours=args.hours))
    elif args.command == "walk-forward-smoke-test":
        print(lab.walk_forward_smoke_test())
    elif args.command == "walk-forward-validator":
        print(lab.walk_forward_validator(hours=args.hours))
    elif args.command == "anti-overfit-v2-smoke-test":
        print(lab.anti_overfit_v2_smoke_test())
    elif args.command == "anti-overfit-v2":
        print(lab.anti_overfit_v2(hours=args.hours))
    elif args.command == "candidate-promotion-v2-smoke-test":
        print(lab.candidate_promotion_v2_smoke_test())
    elif args.command == "candidate-promotion-v2":
        print(lab.candidate_promotion_v2(hours=args.hours))
    elif args.command == "shadow-strategy-simulator-smoke-test":
        print(lab.shadow_strategy_simulator_smoke_test())
    elif args.command == "shadow-strategy-simulator":
        print(lab.shadow_strategy_simulator(hours=args.hours))
    elif args.command == "operational-intelligence-audit":
        print(lab.operational_intelligence_audit(hours=args.hours))
    elif args.command == "strategy-research-library-smoke-test":
        print(lab.strategy_research_library_smoke_test())
    elif args.command == "strategy-research-library":
        print(lab.strategy_research_library(hours=args.hours))
    elif args.command == "real-strategy-backtester-smoke-test":
        print(lab.real_strategy_backtester_smoke_test())
    elif args.command == "real-strategy-backtest":
        print(lab.real_strategy_backtest(hours=args.hours))
    elif args.command == "real-strategy-backtest-multi":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        print(lab.real_strategy_backtest_multi(
            hours=args.hours,
            symbols=symbols_arg,
            timeframe=args.timeframe,
        ))
    elif args.command == "real-strategy-backtest-breakdown":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        print(lab.real_strategy_backtest_breakdown(
            hours=args.hours,
            symbols=symbols_arg,
            timeframe=args.timeframe,
            group_by=args.group_by,
            min_trades=args.min_trades,
            top_n=args.top,
        ))
    elif args.command == "final-policy-builder":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        print(lab.final_policy_builder(
            hours=args.hours,
            symbols=symbols_arg,
            timeframe=args.timeframe,
            group_by=args.group_by,
            min_trades=args.min_trades,
            folds=args.folds,
            data_quality_status=args.data_quality_status,
            label_quality_status=args.label_quality_status,
        ))
    elif args.command == "trade-replay-export":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()]
        symbol = symbols_arg[0] if symbols_arg else "BTCUSDT"
        print(lab.trade_replay_export(
            symbol=symbol,
            hours=args.hours,
            timeframe=args.timeframe,
            max_candles=args.max_candles,
            max_trades=args.max_trades,
        ))
    elif args.command == "cost-stress-summary":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        print(lab.cost_stress_summary(
            hours=args.hours, symbols=symbols_arg, timeframe=args.timeframe,
        ))
    elif args.command == "profit-lock-lab":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()]
        symbol = symbols_arg[0] if symbols_arg else "BTCUSDT"
        print(lab.profit_lock_lab(symbol=symbol, hours=args.hours, timeframe=args.timeframe))
    elif args.command == "fast-exit-lab":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()]
        symbol = symbols_arg[0] if symbols_arg else "BTCUSDT"
        print(lab.fast_exit_lab(symbol=symbol, hours=args.hours, timeframe=args.timeframe))
    elif args.command == "time-death-reducer-lab":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()]
        symbol = symbols_arg[0] if symbols_arg else "BTCUSDT"
        print(lab.time_death_reducer_lab(symbol=symbol, hours=args.hours, timeframe=args.timeframe))
    elif args.command == "time-exit-autopsy-v2":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        print(lab.time_exit_autopsy_v2(hours=args.hours, symbols=symbols_arg, timeframe=args.timeframe))
    elif args.command == "dynamic-hold-lab":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        print(lab.dynamic_hold_lab(hours=args.hours, symbols=symbols_arg, timeframe=args.timeframe))
    elif args.command == "entry-exhaustion-lab":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        print(lab.entry_exhaustion_lab(hours=args.hours, symbols=symbols_arg, timeframe=args.timeframe))
    elif args.command == "reversal-candidate-lab":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        print(lab.reversal_candidate_lab(hours=args.hours, symbols=symbols_arg, timeframe=args.timeframe))
    elif args.command == "exit-policy-v2":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        print(lab.exit_policy_v2(hours=args.hours, symbols=symbols_arg, timeframe=args.timeframe))
    elif args.command == "phase8-candidate-validator":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        print(lab.phase8_candidate_validator(
            hours=args.hours,
            symbols=symbols_arg,
            timeframe=args.timeframe,
            min_trades=args.min_trades,
            folds=args.folds,
        ))
    elif args.command == "phase8-cost-stress":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        print(lab.phase8_cost_stress(
            hours=args.hours,
            symbols=symbols_arg,
            timeframe=args.timeframe,
            policy=args.policy,
        ))
    elif args.command == "dot-regime-diagnosis":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        print(lab.dot_regime_diagnosis(
            hours=args.hours,
            symbols=symbols_arg,
            timeframe=args.timeframe,
            folds=args.folds,
        ))
    elif args.command == "dot-regime-filter-lab":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        print(lab.dot_regime_filter_lab(
            hours=args.hours,
            symbols=symbols_arg,
            timeframe=args.timeframe,
            folds=args.folds,
        ))
    elif args.command == "phase9-paper-readiness":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        print(lab.phase9_paper_readiness(
            hours=args.hours,
            symbols=symbols_arg,
            timeframe=args.timeframe,
            min_trades=args.min_trades,
            folds=args.folds,
        ))
    elif args.command == "net-profit-lock-lab":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        print(lab.net_profit_lock_lab(
            hours=args.hours,
            symbols=symbols_arg,
            timeframe=args.timeframe,
        ))
    elif args.command == "fast-signal-shadow":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        print(lab.fast_signal_shadow(
            hours=args.hours,
            symbols=symbols_arg,
            timeframe=args.timeframe,
        ))
    elif args.command == "research-pack":
        print(lab.research_pack(hours=args.hours))
    elif args.command == "research-cockpit":
        print(lab.research_cockpit())
    elif args.command == "ohlcv-freshness-status":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        timeframes_arg = [t.strip() for t in (args.timeframes or "").split(",") if t.strip()] or None
        print(lab.ohlcv_freshness_status(symbols=symbols_arg, timeframes=timeframes_arg))
    elif args.command == "ohlcv-freshness-refresh":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        timeframes_arg = [t.strip() for t in (args.timeframes or "").split(",") if t.strip()] or None
        # --dry-run forces dry. Without --apply we default to dry-run.
        forced_dry = bool(args.dry_run)
        will_apply = bool(args.apply) and not forced_dry
        dry_run = forced_dry or not will_apply
        print(lab.ohlcv_freshness_refresh(
            symbols=symbols_arg,
            timeframes=timeframes_arg,
            hours=args.hours,
            dry_run=dry_run,
            allow_real_writes=bool(args.allow_real_writes),
        ))
    elif args.command == "training-clean-view-audit":
        print(lab.training_clean_view_audit(hours=args.hours))
    elif args.command == "shadow-multi-trade-status":
        print(lab.shadow_multi_trade_status(hours=args.hours))
    elif args.command == "shadow-multi-trade-replay":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        print(lab.shadow_multi_trade_replay(hours=args.hours, symbols=symbols_arg, timeframe=args.timeframe))
    elif args.command == "capital-leverage-sim":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        margins_arg = tuple(float(value) for value in (args.margins or "").split(",") if value.strip()) or (2.0, 5.0, 10.0, 20.0)
        leverages_arg = tuple(int(float(value)) for value in (args.leverages or "").split(",") if value.strip()) or (1, 3, 5, 10, 20, 50)
        print(lab.capital_leverage_sim(
            hours=args.hours,
            symbols=symbols_arg,
            timeframe=args.timeframe,
            capital_total_usdt=float(args.capital),
            margins=margins_arg,
            leverages=leverages_arg,
        ))
    elif args.command == "fee-aware-exit-trainer":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        print(lab.fee_aware_exit_trainer(
            hours=args.hours,
            symbols=symbols_arg,
            timeframe=args.timeframe,
        ))
    elif args.command == "strategy-research-enhancer":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        print(lab.strategy_research_enhancer(
            hours=args.hours,
            symbols=symbols_arg,
            timeframe=args.timeframe,
        ))
    elif args.command == "clean-research-metrics":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        timeframes_arg = [t.strip() for t in (args.timeframes or "").split(",") if t.strip()] or None
        print(lab.clean_research_metrics(
            hours=args.hours,
            symbols=symbols_arg,
            timeframes=timeframes_arg,
        ))
    elif args.command == "data-pipeline-root-cause":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        timeframes_arg = [t.strip() for t in (args.timeframes or "").split(",") if t.strip()] or None
        print(lab.data_pipeline_root_cause(
            hours=args.hours, symbols=symbols_arg, timeframes=timeframes_arg,
        ))
    elif args.command == "clean-strategy-lab":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        print(lab.clean_strategy_lab(
            hours=args.hours, symbols=symbols_arg, timeframe=args.timeframe,
        ))
    elif args.command == "capital-scaling-simulator":
        # Reuse the clean metrics helper for sane defaults.
        from .clean_research_metrics import get_clean_research_metrics
        cm = get_clean_research_metrics(db, hours=args.hours)
        print(lab.capital_scaling_simulator(
            base_clean_net_ev_pct=float(cm.clean_ev_pct),
            base_clean_pf=float(cm.clean_pf),
            trades_per_window=100,
            data_quality_status=cm.data_quality_status,
            ohlcv_actionable=False,
        ))
    elif args.command == "research-pack-v7":
        print(lab.research_pack_v7(hours=args.hours))
    elif args.command == "duplicate-guard-hook-status":
        print(lab.duplicate_guard_hook_status())
    elif args.command == "funding-cost-model":
        print(lab.funding_cost_model(hours=args.hours))
    elif args.command == "liquidation-model-bitget":
        symbols_arg = [s.strip() for s in (args.symbols or "DOTUSDT").split(",") if s.strip()]
        symbol = symbols_arg[0] if symbols_arg else "DOTUSDT"
        print(lab.liquidation_model_bitget(
            symbol=symbol,
            leverage=5,
            capital_usdt=40.0,
            margin_per_trade_usdt=5.0,
        ))
    elif args.command == "walk-forward-v2":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        print(lab.walk_forward_v2(
            hours=args.hours, timeframe=args.timeframe, symbols=symbols_arg,
        ))
    elif args.command == "research-pack-v7-5":
        print(lab.research_pack_v7_5(hours=args.hours))
    elif args.command == "auto-data-enrichment-status":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        print(lab.auto_data_enrichment_status(
            hours=args.hours, timeframe=args.timeframe, symbols=symbols_arg,
        ))
    elif args.command == "exit-intelligence-lab":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        print(lab.exit_intelligence_lab(
            hours=args.hours, timeframe=args.timeframe, symbols=symbols_arg,
        ))
    elif args.command == "strategy-experiment-registry":
        print(lab.strategy_experiment_registry_snapshot())
    elif args.command == "shadow-candidate-lifecycle":
        print(lab.shadow_candidate_lifecycle_status(hours=args.hours))
    elif args.command == "validation-gates-v9":
        print(lab.validation_gates_v9_status(hours=args.hours))
    elif args.command == "event-catalyst-status":
        print(lab.event_catalyst_status())
    elif args.command == "listing-tracker-audit":
        print(lab.listing_tracker_audit(hours=args.hours))
    elif args.command == "unlock-watchlist-audit":
        print(lab.unlock_watchlist_audit(hours=args.hours))
    elif args.command == "perp-availability-audit":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        print(lab.perp_availability_audit(symbols=symbols_arg))
    elif args.command == "shortability-score-audit":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        print(lab.shortability_score_audit(symbols=symbols_arg))
    elif args.command == "event-candidate-registry-status":
        print(lab.event_candidate_registry_status())
    elif args.command == "research-pack-event-v1":
        symbols_arg = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
        print(lab.research_pack_event_v1(symbols=symbols_arg))
    elif args.command == "bidirectional-funnel":
        side_arg = getattr(args, "side", None)
        print(lab.bidirectional_funnel(hours=args.hours, side=side_arg))
    elif args.command == "missed-opportunities":
        side_arg = getattr(args, "side", None) or "SHORT"
        print(lab.missed_opportunities_cli(hours=args.hours, side=side_arg))
    elif args.command == "blocked-counterfactual":
        side_arg = getattr(args, "side", None) or "SHORT"
        print(lab.blocked_counterfactual_cli(hours=args.hours, side=side_arg))
    elif args.command == "failed-executed":
        side_arg = getattr(args, "side", None) or "SHORT"
        print(lab.failed_executed_cli(hours=args.hours, side=side_arg))
    elif args.command == "good-not-monetized":
        side_arg = getattr(args, "side", None) or "SHORT"
        print(lab.good_not_monetized_cli(hours=args.hours, side=side_arg))
    elif args.command == "score-asymmetry-audit":
        print(lab.score_asymmetry_audit_cli(hours=args.hours))
    elif args.command == "score-symmetric-simulation":
        print(lab.score_symmetric_simulation_cli(hours=args.hours))
    elif args.command == "score-atr-softened-simulation":
        print(lab.score_atr_softened_simulation_cli(hours=args.hours))
    elif args.command == "score-high-vol-directional-simulation":
        print(lab.score_high_vol_directional_simulation_cli(hours=args.hours))
    elif args.command == "regime-router-simulation":
        print(lab.regime_router_simulation_cli(hours=args.hours))
    elif args.command == "trend-campaign-sim":
        side_arg = getattr(args, "side", None) or "SHORT"
        max_adds = int(getattr(args, "max_adds", 3) or 3)
        print(lab.trend_campaign_sim_cli(hours=args.hours, side=side_arg, max_adds=max_adds))
    elif args.command == "profit-lock-sim":
        side_arg = getattr(args, "side", None) or "SHORT"
        # V8.2.3 — ``--policy`` is shared with Phase 8 helpers and defaults to
        # ``late_entry_block_plus_dynamic_hold``. For ``profit-lock-sim`` that
        # default is meaningless, so treat it as ``"all"``. Explicit user
        # input (any other string) is respected.
        policy_arg = getattr(args, "policy", None) or "all"
        if policy_arg == "late_entry_block_plus_dynamic_hold":
            policy_arg = "all"
        print(lab.profit_lock_sim_cli(hours=args.hours, side=side_arg, policy=policy_arg))
    elif args.command == "research-pack-bidirectional-v1":
        print(lab.research_pack_bidirectional_v1_cli(hours=args.hours))
    elif args.command == "future-returns-bridge":
        side_arg = getattr(args, "side", None) or None
        top_n = int(getattr(args, "top", 20) or 20)
        print(lab.future_returns_bridge_cli(hours=args.hours, side=side_arg, top_n=top_n))
    elif args.command == "edgeguard-counterfactual":
        top_n = int(getattr(args, "top", 20) or 20)
        print(lab.edgeguard_counterfactual_cli(hours=args.hours, top_n=top_n))
    elif args.command == "counterfactual-training-dataset":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.counterfactual_training_dataset_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "export-counterfactual-training-dataset":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.export_counterfactual_training_dataset_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "training-dataset-summary":
        print(lab.training_dataset_summary_cli(hours=args.hours))
    elif args.command == "counterfactual-dedup-audit":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.counterfactual_dedup_audit_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "short-sign-barrier-audit":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.short_sign_barrier_audit_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "score-calibration-audit":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.score_calibration_audit_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "counterfactual-cost-stress":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.counterfactual_cost_stress_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "export-counterfactual-clean-v2":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.export_counterfactual_clean_v2_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "research-pack-counterfactual-quality-v1":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.research_pack_counterfactual_quality_v1_cli(
            hours=args.hours, limit=limit_arg,
        ))
    elif args.command == "candidate-rule-miner-v826":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.candidate_rule_miner_v826_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "candidate-rule-walkforward-v826":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.candidate_rule_walkforward_v826_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "short-barrier-debug-v826":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.short_barrier_debug_v826_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "score-recalibration-sandbox-v826":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.score_recalibration_sandbox_v826_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "export-research-v826":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.export_research_v826_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "research-pack-v826":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.research_pack_v826_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "strict-oos-rule-selector-v827":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.strict_oos_rule_selector_v827_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "short-barrier-debug-v827":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.short_barrier_debug_v827_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "final-rule-gate-v827":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.final_rule_gate_v827_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "export-research-v827":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.export_research_v827_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "research-pack-v827":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.research_pack_v827_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "dual-side-barrier-audit-v828":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.dual_side_barrier_audit_v828_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "duplicate-root-cause-v828":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.duplicate_root_cause_v828_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "side-aware-score-calibration-v828":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.side_aware_score_calibration_v828_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "rebound-regime-turn-lab-v828":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.rebound_regime_turn_lab_v828_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "export-research-v828":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.export_research_v828_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "research-pack-v828":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.research_pack_v828_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "rebound-long-candidates-v829":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.rebound_long_candidates_v829_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "edgeguard-repeat-dedup-v829":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.edgeguard_repeat_dedup_v829_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "score-gate-sandbox-v829":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.score_gate_sandbox_v829_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "exit-monetization-audit-v829":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.exit_monetization_audit_v829_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "rebound-long-strict-oos-v829":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.rebound_long_strict_oos_v829_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "adversarial-research-audit-v829":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.adversarial_research_audit_v829_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "export-research-v829":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.export_research_v829_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "research-pack-v829":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.research_pack_v829_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "rebound-outcome-reconciliation-v829":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(
            lab.rebound_outcome_reconciliation_v829_cli(
                hours=args.hours, limit=limit_arg,
            )
        )
    elif args.command == "rebound-sign-integrity-v8293":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(
            lab.rebound_sign_integrity_v8293_cli(
                hours=args.hours, limit=limit_arg,
            )
        )
    elif args.command == "canonical-outcome-v8293":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(
            lab.canonical_outcome_v8293_cli(
                hours=args.hours, limit=limit_arg,
            )
        )
    elif args.command == "exit-bar-replay-v8293":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(
            lab.exit_bar_replay_v8293_cli(
                hours=args.hours, limit=limit_arg,
            )
        )
    elif args.command == "rebound-strict-oos-canonical-v8293":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(
            lab.rebound_strict_oos_canonical_v8293_cli(
                hours=args.hours, limit=limit_arg,
            )
        )
    elif args.command == "signal-path-bridge-v8295":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.signal_path_bridge_v8295_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "canonical-real-outcome-v8295":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.canonical_real_outcome_v8295_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "strategy-tournament-real-v8295":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.strategy_tournament_real_v8295_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "export-research-v8295":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.export_research_v8295_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "research-pack-v8295":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.research_pack_v8295_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "signal-path-bridge-v8296":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.signal_path_bridge_v8296_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "canonical-real-outcome-v8296":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.canonical_real_outcome_v8296_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "strategy-tournament-real-v8296":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.strategy_tournament_real_v8296_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "export-research-v8296":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.export_research_v8296_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "research-pack-v8296":
        limit_arg = int(getattr(args, "limit", 50000) or 50000)
        print(lab.research_pack_v8296_cli(hours=args.hours, limit=limit_arg))
    elif args.command == "edge-data-foundation-v10":
        print(lab.edge_data_foundation_v10_cli(
            hours=args.hours, external_data_path=getattr(args, "external_data_path", ""),
        ))
    elif args.command == "funding-oi-liquidation-research-v10":
        print(lab.funding_oi_liquidation_research_v10_cli(
            hours=args.hours, external_data_path=getattr(args, "external_data_path", ""),
        ))
    elif args.command == "token-unlock-post-listing-research-v10":
        print(lab.token_unlock_post_listing_research_v10_cli(
            hours=args.hours, external_data_path=getattr(args, "external_data_path", ""),
        ))
    elif args.command == "intraday-volatility-breakdown-v10":
        print(lab.intraday_volatility_breakdown_v10_cli(
            hours=args.hours,
            symbols=getattr(args, "symbols", ""),
            timeframe=getattr(args, "timeframe", "5m"),
        ))
    elif args.command == "micro-tp-viability-v10":
        print(lab.micro_tp_viability_v10_cli(hours=args.hours))
    elif args.command == "event-catalyst-layer-v10":
        print(lab.event_catalyst_layer_v10_cli(
            hours=args.hours, external_data_path=getattr(args, "external_data_path", ""),
        ))
    elif args.command == "edge-discovery-orchestrator-v10":
        print(lab.edge_discovery_orchestrator_v10_cli(
            hours=args.hours,
            external_data_path=getattr(args, "external_data_path", ""),
            symbols=getattr(args, "symbols", ""),
            timeframe=getattr(args, "timeframe", "5m"),
        ))
    elif args.command == "alpha-ensemble-v10":
        print(lab.alpha_ensemble_v10_cli(
            hours=args.hours,
            symbols=getattr(args, "symbols", ""),
            timeframe=getattr(args, "timeframe", "15m"),
        ))
    elif args.command == "external-edge-ingest-v101":
        print(lab.external_edge_ingest_v101_cli(
            dataset=getattr(args, "dataset", "perp_market_state"),
            input_path=getattr(args, "input", ""),
            input_dir=getattr(args, "input_dir", ""),
        ))
    elif args.command == "external-data-health-v101":
        print(lab.external_data_health_v101_cli())
    elif args.command == "external-event-study-v101":
        print(lab.external_event_study_v101_cli(
            module=getattr(args, "module", "funding_oi_liq"),
            hours=args.hours,
        ))
    elif args.command == "external-funding-oi-diagnostics-v101":
        print(lab.external_funding_oi_diagnostics_v101_cli(hours=args.hours))
    elif args.command == "external-funding-oi-stability-v101":
        print(lab.external_funding_oi_stability_v101_cli(hours=args.hours))
    elif args.command == "external-missing-oi-audit-v102":
        print(lab.external_missing_oi_audit_v102_cli(hours=args.hours))
    elif args.command == "external-long-history-validation-v102":
        print(lab.external_long_history_validation_v102_cli(hours=args.hours))
    elif args.command == "strategy-replay-backtest-v103":
        print(lab.strategy_replay_backtest_v103_cli(hours=args.hours))
    elif args.command == "external-data-source-audit-v103":
        print(lab.external_data_source_audit_v103_cli(hours=args.hours))
    elif args.command == "external-provider-readiness-v103":
        print(lab.external_provider_readiness_v103_cli())
    elif args.command == "external-provider-verification-v104":
        print(lab.external_provider_verification_v104_cli())
    elif args.command == "external-data-acquisition-plan-v104":
        print(lab.external_data_acquisition_plan_v104_cli())
    elif args.command == "external-research-intake-v104":
        print(lab.external_research_intake_v104_cli())
    elif args.command == "edge-hunter-contract-v104":
        print(lab.edge_hunter_contract_v104_cli())
    elif args.command == "trader-dashboard-contract-v104":
        print(lab.trader_dashboard_contract_v104_cli())
    elif args.command == "runtime-health-audit-v104":
        print(lab.runtime_health_audit_v104_cli())
    elif args.command == "learning-edge-diagnostic-v104":
        print(lab.learning_edge_diagnostic_v104_cli(hours=args.hours))
    elif args.command == "runtime-efficiency-diagnostic-v104":
        print(lab.runtime_efficiency_diagnostic_v104_cli())
    elif args.command == "provider-verification-v105":
        print(lab.provider_verification_v105_cli())
    elif args.command == "data-readiness-v105":
        print(lab.data_readiness_v105_cli())
    elif args.command == "trader-dashboard-contract-v105":
        print(lab.trader_dashboard_contract_v105_cli())
    elif args.command == "ohlcv-replay-loader-smoke-test":
        print(lab.ohlcv_replay_loader_smoke_test())
    elif args.command == "ohlcv-replay-loader-audit":
        print(lab.ohlcv_replay_loader_audit(hours=args.hours))
    elif args.command == "duplicate-module-audit-smoke-test":
        print(lab.duplicate_module_audit_smoke_test())
    elif args.command == "duplicate-module-audit":
        print(lab.duplicate_module_audit())
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
    elif args.command == "pre-move-event-labeler":
        print(lab.pre_move_event_labeler(hours=args.hours))
    elif args.command == "pre-move-feature-snapshot":
        print(lab.pre_move_feature_snapshot(hours=args.hours))
    elif args.command == "pre-move-pattern-miner":
        print(lab.pre_move_pattern_miner(hours=args.hours))
    elif args.command == "pre-move-similarity-scanner":
        print(lab.pre_move_similarity_scanner(hours=args.hours))
    elif args.command == "pre-move-smoke-test":
        print(lab.pre_move_smoke_test())
    elif args.command == "dashboard-pro-smoke-test":
        print(lab.dashboard_pro_smoke_test())
    elif args.command == "dashboard-beauty-exit-calibration-smoke-test":
        print(lab.dashboard_beauty_exit_calibration_smoke_test())
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
    elif args.command == "bot-integrity-audit":
        print(lab.bot_integrity_audit(hours=args.hours))
    elif args.command == "security-audit":
        print(lab.security_audit())
    elif args.command == "label-time-audit":
        print(lab.label_time_audit(hours=args.hours))
    elif args.command == "paper-trading-audit":
        print(lab.paper_trading_audit(hours=args.hours))
    elif args.command == "research-modules-audit":
        print(lab.research_modules_audit(hours=args.hours))
    elif args.command == "bot-integrity-audit-smoke-test":
        print(lab.bot_integrity_audit_smoke_test())
    elif args.command == "dashboard-ui-v3-smoke-test":
        print(lab.dashboard_ui_v3_smoke_test())
    elif args.command == "dashboard-report-timeout-smoke-test":
        print(lab.dashboard_report_timeout_smoke_test())


if __name__ == "__main__":
    main()
