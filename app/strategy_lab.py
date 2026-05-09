from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from .database import Database
from .research_lab import ResearchMetrics, max_drawdown, parse_timestamp, profit_factor_from_returns, return_pct, score_bucket
from .utils import iso_utc, json_dumps, safe_float, safe_int


SAFE_MODE_MAX_ROWS = 20000
SAFE_MODE_BATCH_SIZE = 5000
MIN_CANDIDATE_SAMPLES = 100
MIN_OUT_OF_SAMPLE_PF = 1.2
MAX_TIME_RATIO = 0.80
FUTURE_OUTCOME_FIELDS = {
    "label",
    "first_barrier_hit",
    "realized_return_pct",
    "simulated_pnl",
    "would_have_won",
    "bars_to_outcome",
    "max_favorable_excursion",
    "max_adverse_excursion",
    "path_max_favorable_excursion_pct",
    "path_max_adverse_excursion_pct",
    "path_candles_until_exit",
    "counterfactual_reverse_helped",
    "counterfactual_avoided_loss",
    "counterfactual_closer_tp_helped",
    "counterfactual_wider_stop_helped",
    "virtual_research_positive",
    "in_stop_loss_failure_cluster",
    "in_win_cluster",
}


Predicate = Callable[[dict[str, Any]], bool]


@dataclass(frozen=True)
class StrategyLabWalkForwardSplit:
    window_index: int
    train: list[dict[str, Any]]
    test: list[dict[str, Any]]


@dataclass
class StrategyLabCandidate:
    name: str
    family: str
    params: dict[str, Any]
    predicate: Predicate
    feature_names: set[str]
    uses_future: bool = False

    def matches(self, row: dict[str, Any]) -> bool:
        try:
            return bool(self.predicate(row))
        except Exception:
            return False


@dataclass
class CandidateEvaluation:
    candidate: StrategyLabCandidate
    status: str
    reason: str
    total_samples: int
    train_samples: int = 0
    test_samples: int = 0
    in_sample_profit_factor: float = 0.0
    out_of_sample_profit_factor: float = 0.0
    expectancy: float = 0.0
    decisive_win_rate: float = 0.0
    drawdown_estimated: float = 0.0
    sl_rate: float = 0.0
    tp_rate: float = 0.0
    time_rate: float = 0.0
    stability_score: float = 0.0
    overfit_penalty: float = 0.0
    conservative_score: float = 0.0
    walkforward_rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.status == "ACCEPTED_RESEARCH_ONLY"

    def to_payload(self, run_id: str) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "candidate_name": self.candidate.name,
            "family": self.candidate.family,
            "params_json": json_dumps(self.candidate.params),
            "status": self.status,
            "reason": self.reason,
            "total_samples": self.total_samples,
            "train_samples": self.train_samples,
            "test_samples": self.test_samples,
            "in_sample_profit_factor": self.in_sample_profit_factor,
            "out_of_sample_profit_factor": self.out_of_sample_profit_factor,
            "expectancy": self.expectancy,
            "decisive_win_rate": self.decisive_win_rate,
            "drawdown_estimated": self.drawdown_estimated,
            "sl_rate": self.sl_rate,
            "tp_rate": self.tp_rate,
            "time_rate": self.time_rate,
            "stability_score": self.stability_score,
            "overfit_penalty": self.overfit_penalty,
            "conservative_score": self.conservative_score,
        }


@dataclass
class StrategyLabResult:
    run_id: str
    rows_loaded: int
    safe_mode: bool
    families_tested: int
    candidates_accepted: int
    candidates_rejected: int
    evaluations: list[CandidateEvaluation] = field(default_factory=list)
    recommendations_created: int = 0
    errors: int = 0

    @property
    def best(self) -> list[CandidateEvaluation]:
        return sorted(
            [item for item in self.evaluations if item.accepted],
            key=lambda item: (item.conservative_score, item.out_of_sample_profit_factor, item.expectancy),
            reverse=True,
        )[:8]

    @property
    def worst(self) -> list[CandidateEvaluation]:
        return sorted(
            self.evaluations,
            key=lambda item: (item.out_of_sample_profit_factor, item.expectancy, -item.time_rate),
        )[:8]

    def to_text(self) -> str:
        lines = [
            "STRATEGY LAB START",
            f"rows loaded: {self.rows_loaded}",
            f"safe mode: {self.safe_mode}",
            f"families tested: {self.families_tested}",
            f"candidates accepted: {self.candidates_accepted}",
            f"candidates rejected: {self.candidates_rejected}",
            f"errors: {self.errors}",
            "",
            "best OOS candidates",
        ]
        lines.extend(_evaluation_lines(self.best))
        lines.extend(["", "worst candidates"])
        lines.extend(_evaluation_lines(self.worst))
        lines.extend(["", "recommended research-only filters"])
        if self.best:
            for item in self.best[:5]:
                lines.append(
                    f"- {item.candidate.name}: observar en paper; PF OOS={item.out_of_sample_profit_factor:.2f}, "
                    f"expectancy={item.expectancy:.5f}, estabilidad={item.stability_score:.1%}"
                )
        else:
            lines.append("- sin filtros con evidencia suficiente; continuar solo en paper/research")
        lines.extend(
            [
                "",
                "NO LIVE recommendation",
                "final recommendation: NO LIVE",
                "STRATEGY LAB END",
            ]
        )
        return "\n".join(lines)


class StrategyLab:
    """Research-only strategy discovery over stored labels and virtual analysis outputs."""

    def __init__(self, db: Database, logger=None) -> None:
        self.db = db
        self.logger = logger

    def run(self, *, limit: int = SAFE_MODE_MAX_ROWS, safe_mode: bool = True) -> StrategyLabResult:
        run_id = f"strategy_lab_{iso_utc().replace(':', '').replace('-', '').replace('.', '')}"
        rows = self._load_rows(limit=limit, safe_mode=safe_mode)
        candidates = build_strategy_lab_candidates(rows, safe_mode=safe_mode)
        evaluations: list[CandidateEvaluation] = []
        errors = 0
        for candidate in candidates:
            try:
                evaluation = evaluate_candidate(candidate, rows)
                evaluations.append(evaluation)
                self._persist_candidate(run_id, evaluation)
            except Exception as exc:
                errors += 1
                self._warn("strategy lab candidate failed %s: %s", candidate.name, exc)
        recommendations = self._persist_recommendations(run_id, evaluations)
        accepted = sum(1 for item in evaluations if item.accepted)
        return StrategyLabResult(
            run_id=run_id,
            rows_loaded=len(rows),
            safe_mode=safe_mode,
            families_tested=len({candidate.family for candidate in candidates}),
            candidates_accepted=accepted,
            candidates_rejected=max(0, len(evaluations) - accepted),
            evaluations=evaluations,
            recommendations_created=recommendations,
            errors=errors,
        )

    def _load_rows(self, *, limit: int, safe_mode: bool) -> list[dict[str, Any]]:
        max_rows = SAFE_MODE_MAX_ROWS if safe_mode else max(0, int(limit or 0))
        target = min(max(0, int(limit or 0)), max_rows) if safe_mode else max(0, int(limit or 0))
        if target <= 0:
            return []
        batch_size = min(SAFE_MODE_BATCH_SIZE, target) if safe_mode else target
        rows: list[dict[str, Any]] = []
        offset = 0
        while len(rows) < target:
            take = min(batch_size, target - len(rows))
            batch = self.db.fetch_strategy_lab_rows(limit=take, offset=offset)
            if not batch:
                break
            rows.extend(normalize_strategy_lab_row(row) for row in batch)
            offset += len(batch)
            if len(batch) < take:
                break
        return sorted(rows, key=lambda row: str(row.get("timestamp") or ""))

    def _persist_candidate(self, run_id: str, evaluation: CandidateEvaluation) -> None:
        self.db.record_strategy_lab_candidate(evaluation.to_payload(run_id))
        for row in evaluation.walkforward_rows:
            payload = dict(row)
            payload["run_id"] = run_id
            payload["candidate_name"] = evaluation.candidate.name
            self.db.record_strategy_lab_walkforward(payload)

    def _persist_recommendations(self, run_id: str, evaluations: list[CandidateEvaluation]) -> int:
        created = 0
        accepted = sorted(
            [item for item in evaluations if item.accepted],
            key=lambda item: item.conservative_score,
            reverse=True,
        )
        if not accepted:
            self.db.record_strategy_lab_recommendation(
                {
                    "run_id": run_id,
                    "recommendation_type": "OBSERVE_ONLY",
                    "candidate_name": "NO_ACCEPTED_CANDIDATE",
                    "condition_json": "{}",
                    "action": "NO_LIVE_CONTINUE_PAPER_RESEARCH",
                    "evidence_score": 0.0,
                    "explanation": "No hay filtros con validacion temporal suficiente. No activar live.",
                }
            )
            return 1
        for item in accepted[:10]:
            self.db.record_strategy_lab_recommendation(
                {
                    "run_id": run_id,
                    "recommendation_type": "OBSERVE_ONLY",
                    "candidate_name": item.candidate.name,
                    "condition_json": json_dumps(item.candidate.params),
                    "action": "PAPER_RESEARCH_ONLY",
                    "evidence_score": item.conservative_score,
                    "explanation": (
                        f"Candidato research-only con PF OOS {item.out_of_sample_profit_factor:.2f}, "
                        f"expectancy {item.expectancy:.5f} y estabilidad {item.stability_score:.1%}. "
                        "No autoriza live."
                    ),
                }
            )
            created += 1
        return created

    def _warn(self, message: str, *args: Any) -> None:
        if self.logger:
            self.logger.warning(message, *args)


def normalize_strategy_lab_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    entry = safe_float(normalized.get("entry_price"))
    stop = safe_float(normalized.get("stop_loss"))
    tp1 = safe_float(normalized.get("take_profit_1"))
    tp2 = safe_float(normalized.get("take_profit_2"))
    stop_distance = abs(entry - stop) / entry if entry > 0 and stop > 0 else 0.0
    tp1_distance = abs(tp1 - entry) / entry if entry > 0 and tp1 > 0 else 0.0
    tp2_distance = abs(tp2 - entry) / entry if entry > 0 and tp2 > 0 else 0.0
    normalized["holding_bars"] = normalized.get("bars_to_outcome")
    normalized["stop_distance_pct"] = stop_distance
    normalized["tp1_distance_pct"] = tp1_distance
    normalized["tp2_distance_pct"] = tp2_distance
    normalized["tp1_to_sl_ratio"] = tp1_distance / stop_distance if stop_distance > 0 else 0.0
    normalized["tp2_to_sl_ratio"] = tp2_distance / stop_distance if stop_distance > 0 else 0.0
    normalized["score_bucket"] = normalized.get("score_bucket") or score_bucket(safe_float(normalized.get("confidence_score")))
    normalized["side"] = str(normalized.get("side") or "").upper()
    normalized["strategy_type"] = str(normalized.get("strategy_type") or "NA").upper()
    normalized["symbol"] = str(normalized.get("symbol") or "NA").upper()
    normalized["market_regime"] = str(normalized.get("market_regime") or "NA").upper()
    normalized["btc_regime"] = str(normalized.get("btc_regime") or "NA").upper()
    normalized["timestamp"] = str(normalized.get("timestamp") or normalized.get("label_timestamp") or "")
    return normalized


def evaluate_candidate(candidate: StrategyLabCandidate, rows: list[dict[str, Any]]) -> CandidateEvaluation:
    matched = [row for row in rows if candidate.matches(row)]
    total = len(matched)
    if candidate.uses_future or candidate.feature_names.intersection(FUTURE_OUTCOME_FIELDS):
        return CandidateEvaluation(
            candidate=candidate,
            status="REJECTED_FUTURE_LEAKAGE_RESEARCH_ONLY",
            reason="El candidato usa resultado/counterfactual como selector. Solo diagnostico, no recomendacion.",
            total_samples=total,
        )
    if total < MIN_CANDIDATE_SAMPLES:
        return CandidateEvaluation(
            candidate=candidate,
            status="REJECTED_TOO_FEW_SAMPLES",
            reason=f"muestra insuficiente: {total} < {MIN_CANDIDATE_SAMPLES}",
            total_samples=total,
        )

    splits = make_strategy_lab_walkforward_splits(matched)
    if not splits:
        return CandidateEvaluation(
            candidate=candidate,
            status="REJECTED_NO_WALKFORWARD",
            reason="no hay bloques temporales suficientes para validar fuera de muestra",
            total_samples=total,
        )

    train_rows = splits[-1].train
    test_rows = [row for split in splits for row in split.test]
    train_metrics = strategy_lab_metrics(train_rows)
    test_metrics = strategy_lab_metrics(test_rows)
    window_rows: list[dict[str, Any]] = []
    passed_windows = 0
    for split in splits:
        split_train = strategy_lab_metrics(split.train)
        split_test = strategy_lab_metrics(split.test)
        passed = int(split_test["profit_factor"] >= MIN_OUT_OF_SAMPLE_PF and split_test["expectancy"] > 0)
        passed_windows += passed
        window_rows.append(
            {
                "window_index": split.window_index,
                "train_start": _first_timestamp(split.train),
                "train_end": _last_timestamp(split.train),
                "test_start": _first_timestamp(split.test),
                "test_end": _last_timestamp(split.test),
                "train_samples": len(split.train),
                "test_samples": len(split.test),
                "train_profit_factor": split_train["profit_factor"],
                "test_profit_factor": split_test["profit_factor"],
                "test_expectancy": split_test["expectancy"],
                "test_drawdown": split_test["max_drawdown_estimated"],
                "test_time_rate": split_test["time_rate"],
                "passed": passed,
            }
        )
    stability = passed_windows / max(len(splits), 1)
    overfit_penalty = _overfit_penalty(train_metrics["profit_factor"], test_metrics["profit_factor"], stability)
    conservative = conservative_score(
        total=total,
        oos_profit_factor=test_metrics["profit_factor"],
        expectancy=test_metrics["expectancy"],
        drawdown=test_metrics["max_drawdown_estimated"],
        time_rate=test_metrics["time_rate"],
        stability=stability,
        overfit_penalty=overfit_penalty,
    )
    status, reason = _candidate_status(train_metrics, test_metrics, stability, overfit_penalty)
    return CandidateEvaluation(
        candidate=candidate,
        status=status,
        reason=reason,
        total_samples=total,
        train_samples=len(train_rows),
        test_samples=len(test_rows),
        in_sample_profit_factor=train_metrics["profit_factor"],
        out_of_sample_profit_factor=test_metrics["profit_factor"],
        expectancy=test_metrics["expectancy"],
        decisive_win_rate=test_metrics["decisive_win_rate"],
        drawdown_estimated=test_metrics["max_drawdown_estimated"],
        sl_rate=test_metrics["sl_rate"],
        tp_rate=test_metrics["tp_rate"],
        time_rate=test_metrics["time_rate"],
        stability_score=stability,
        overfit_penalty=overfit_penalty,
        conservative_score=conservative,
        walkforward_rows=window_rows,
    )


def strategy_lab_metrics(rows: list[dict[str, Any]]) -> dict[str, float]:
    metrics = ResearchMetrics.calculate(rows)
    total = max(len(rows), 1)
    tp_count = safe_float(metrics.get("tp1_count")) + safe_float(metrics.get("tp2_count"))
    sl_count = safe_float(metrics.get("sl_count"))
    time_count = safe_float(metrics.get("time_count"))
    decisive = tp_count + sl_count
    returns = [return_pct(row) for row in rows]
    metrics["tp_rate"] = tp_count / total
    metrics["sl_rate"] = sl_count / total
    metrics["time_rate"] = time_count / total
    metrics["decisive_win_rate"] = tp_count / max(decisive, 1.0)
    metrics["max_drawdown_estimated"] = max_drawdown(returns)
    metrics["profit_factor"] = profit_factor_from_returns(returns)
    metrics["expectancy"] = sum(returns) / len(returns) if returns else 0.0
    return metrics


def make_strategy_lab_walkforward_splits(
    rows: list[dict[str, Any]],
    *,
    max_windows: int = 6,
    min_block_size: int = 20,
) -> list[StrategyLabWalkForwardSplit]:
    ordered = sorted(rows, key=lambda row: parse_timestamp(row.get("timestamp")))
    if len(ordered) < min_block_size * 3:
        return []
    block_count = min(max_windows + 1, max(3, len(ordered) // min_block_size))
    block_size = max(min_block_size, len(ordered) // block_count)
    blocks = [ordered[index:index + block_size] for index in range(0, len(ordered), block_size)]
    blocks = [block for block in blocks if len(block) >= max(1, min_block_size // 2)]
    if len(blocks) < 3:
        return []
    splits: list[StrategyLabWalkForwardSplit] = []
    for index in range(1, len(blocks)):
        train = [row for block in blocks[:index] for row in block]
        test = blocks[index]
        if train and test:
            splits.append(StrategyLabWalkForwardSplit(window_index=index, train=train, test=test))
    return splits


def build_strategy_lab_candidates(rows: list[dict[str, Any]], *, safe_mode: bool = True) -> list[StrategyLabCandidate]:
    candidates: list[StrategyLabCandidate] = []

    def add(
        name: str,
        family: str,
        params: dict[str, Any],
        predicate: Predicate,
        features: set[str],
        *,
        uses_future: bool = False,
    ) -> None:
        candidates.append(StrategyLabCandidate(name, family, params, predicate, features, uses_future))

    add(
        "NORMAL_ONLY",
        "ensemble",
        {"shadow_strategy": 0},
        lambda row: safe_int(row.get("shadow_strategy")) == 0,
        {"shadow_strategy"},
    )
    add(
        "TREND_EMA_ALIGNED",
        "trend_following",
        {"rule": "side aligned with ema21 and ema50 distance"},
        lambda row: _side_aligned(row, "distance_to_ema_21") and _side_aligned(row, "distance_to_ema_50"),
        {"side", "distance_to_ema_21", "distance_to_ema_50"},
    )
    add(
        "TREND_BTC_ALIGNED",
        "trend_following",
        {"rule": "btc momentum confirms side"},
        _btc_aligned,
        {"side", "btc_momentum_5", "btc_momentum_15"},
    )
    add(
        "TREND_PULLBACK_TO_EMA21",
        "trend_following",
        {"max_abs_distance_to_ema_21": 0.015, "volume_relative_min": 1.0},
        lambda row: abs(safe_float(row.get("distance_to_ema_21"))) <= 0.015 and safe_float(row.get("volume_relative")) >= 1.0,
        {"distance_to_ema_21", "volume_relative"},
    )
    add(
        "TREND_HIGHER_TIMEFRAME_CONFIRMATION",
        "trend_following",
        {"regimes": ["TREND_UP", "TREND_DOWN"], "btc_regime": True},
        lambda row: str(row.get("market_regime")) in {"TREND_UP", "TREND_DOWN"} or str(row.get("btc_regime")) in {"TREND_UP", "TREND_DOWN"},
        {"market_regime", "btc_regime"},
    )
    add(
        "TREND_STRENGTH_PROXY",
        "trend_following",
        {"momentum_abs_min": 0.01},
        lambda row: abs(safe_float(row.get("momentum_15"))) + abs(safe_float(row.get("distance_to_ema_50"))) >= 0.01,
        {"momentum_15", "distance_to_ema_50"},
    )

    add(
        "MOMENTUM_RSI_HEALTHY",
        "momentum",
        {"long_rsi": "55..72", "short_rsi": "28..45"},
        lambda row: _side_rsi(row, long_min=55, long_max=72, short_min=28, short_max=45),
        {"side", "rsi_14"},
    )
    add(
        "MOMENTUM_MACD_CONFIRMED",
        "momentum",
        {"macd_hist_side_aligned": True},
        lambda row: _side_aligned(row, "macd_hist"),
        {"side", "macd_hist"},
    )
    add(
        "MOMENTUM_VOLUME_CONFIRMED",
        "momentum",
        {"volume_relative_min": 1.5},
        lambda row: safe_float(row.get("volume_relative")) >= 1.5 and _side_aligned(row, "momentum_5"),
        {"side", "volume_relative", "momentum_5"},
    )
    add(
        "MOMENTUM_ROC_5_15",
        "momentum",
        {"momentum_5_15_side_aligned": True},
        lambda row: _side_aligned_value(row, safe_float(row.get("momentum_5")) + safe_float(row.get("momentum_15"))),
        {"side", "momentum_5", "momentum_15"},
    )

    add(
        "BREAKOUT_RANGE_WIDTH",
        "breakout",
        {"range_width_pct_min": 0.01},
        lambda row: safe_float(row.get("range_width_pct")) >= 0.01,
        {"range_width_pct"},
    )
    add(
        "BREAKOUT_VOLUME_EXPANSION",
        "breakout",
        {"volume_relative_min": 1.8},
        lambda row: safe_float(row.get("volume_relative")) >= 1.8,
        {"volume_relative"},
    )
    add(
        "BREAKOUT_ATR_EXPANSION",
        "breakout",
        {"normalized_atr_min": 0.01},
        lambda row: safe_float(row.get("normalized_atr")) >= 0.01,
        {"normalized_atr"},
    )
    add(
        "BREAKOUT_COMPRESSION_THEN_VOLUME",
        "breakout",
        {"normalized_atr_max": 0.02, "volume_relative_min": 1.5},
        lambda row: safe_float(row.get("normalized_atr")) <= 0.02 and safe_float(row.get("volume_relative")) >= 1.5,
        {"normalized_atr", "volume_relative"},
    )

    add(
        "MEAN_REVERSION_RSI_EXTREME_LONG",
        "mean_reversion",
        {"side": "LONG", "rsi_max": 35},
        lambda row: row.get("side") == "LONG" and safe_float(row.get("rsi_14")) <= 35,
        {"side", "rsi_14"},
    )
    add(
        "MEAN_REVERSION_RSI_EXTREME_SHORT",
        "mean_reversion",
        {"side": "SHORT", "rsi_min": 65},
        lambda row: row.get("side") == "SHORT" and safe_float(row.get("rsi_14")) >= 65,
        {"side", "rsi_14"},
    )
    add(
        "MEAN_REVERSION_EMA200_DISTANCE_RANGE_ONLY",
        "mean_reversion",
        {"abs_distance_to_ema_200_min": 0.04, "regimes": ["RANGE", "CHOPPY_MARKET"]},
        lambda row: abs(safe_float(row.get("distance_to_ema_200"))) >= 0.04 and row.get("market_regime") in {"RANGE", "CHOPPY_MARKET"},
        {"distance_to_ema_200", "market_regime"},
    )

    add(
        "LIQUID_LOW_SPREAD",
        "volatility_liquidity",
        {"spread_pct_max": 0.001},
        lambda row: safe_float(row.get("spread_pct")) <= 0.001,
        {"spread_pct"},
    )
    add(
        "LIQUID_VOLUME_RELATIVE_GE_1_2",
        "volatility_liquidity",
        {"volume_relative_min": 1.2},
        lambda row: safe_float(row.get("volume_relative")) >= 1.2,
        {"volume_relative"},
    )
    add(
        "AVOID_LOW_VOLUME_RELATIVE",
        "volatility_liquidity",
        {"volume_relative_min": 0.8},
        lambda row: safe_float(row.get("volume_relative")) >= 0.8,
        {"volume_relative"},
    )
    for name, lower, upper in (
        ("ATR_BUCKET_LOW", 0.0, 0.006),
        ("ATR_BUCKET_MEDIUM", 0.006, 0.02),
        ("ATR_BUCKET_HIGH", 0.02, 999.0),
    ):
        add(
            name,
            "volatility_liquidity",
            {"normalized_atr_min": lower, "normalized_atr_max": upper},
            lambda row, lo=lower, hi=upper: lo <= safe_float(row.get("normalized_atr")) < hi,
            {"normalized_atr"},
        )

    for regime in ("TREND_UP", "TREND_DOWN", "RANGE", "CHOPPY_MARKET", "RISK_ON", "RISK_OFF", "BREAKOUT_POSSIBLE"):
        add(
            f"REGIME_{regime}",
            "regime_filter",
            {"market_regime": regime},
            lambda row, value=regime: row.get("market_regime") == value,
            {"market_regime"},
        )

    for threshold in (72, 75, 80, 85, 90):
        add(
            f"SCORE_GE_{threshold}",
            "score_threshold",
            {"confidence_score_min": threshold},
            lambda row, value=threshold: safe_float(row.get("confidence_score")) >= value,
            {"confidence_score"},
        )

    for rr in (1.2, 1.5, 2.0, 2.5):
        add(
            f"RR_GE_{str(rr).replace('.', '_')}",
            "tp_sl_research",
            {"risk_reward_ratio_min": rr},
            lambda row, value=rr: safe_float(row.get("risk_reward_ratio")) >= value or safe_float(row.get("tp1_to_sl_ratio")) >= value,
            {"risk_reward_ratio", "tp1_to_sl_ratio"},
        )
    add(
        "SL_WIDE_ENOUGH",
        "tp_sl_research",
        {"stop_distance_pct_min": 0.006},
        lambda row: safe_float(row.get("stop_distance_pct")) >= 0.006,
        {"stop_distance_pct"},
    )
    add(
        "SL_TIGHT_RESEARCH_BUCKET",
        "tp_sl_research",
        {"stop_distance_pct_max": 0.006},
        lambda row: 0 < safe_float(row.get("stop_distance_pct")) < 0.006,
        {"stop_distance_pct"},
    )
    add(
        "TIME_STOP_RESEARCH_FILTER",
        "tp_sl_research",
        {"avoid_regime": "CHOPPY_MARKET", "volume_relative_min": 1.0},
        lambda row: row.get("market_regime") != "CHOPPY_MARKET" and safe_float(row.get("volume_relative")) >= 1.0,
        {"market_regime", "volume_relative"},
    )
    add(
        "BREAKEVEN_AFTER_TP1_PROXY",
        "tp_sl_research",
        {"low_spread": True, "risk_reward_ratio_min": 1.4},
        lambda row: safe_float(row.get("spread_pct")) <= 0.001 and safe_float(row.get("risk_reward_ratio")) >= 1.4,
        {"spread_pct", "risk_reward_ratio"},
    )

    add(
        "TREND_CONFIRMATION_ONLY",
        "ensemble",
        {"conditions": ["ema_aligned", "btc_aligned"]},
        lambda row: _side_aligned(row, "distance_to_ema_21") and _btc_aligned(row),
        {"side", "distance_to_ema_21", "btc_momentum_5", "btc_momentum_15"},
    )
    add(
        "REGIME_APPROVED_ONLY",
        "ensemble",
        {"exclude": ["CHOPPY_MARKET", "RISK_OFF"]},
        lambda row: row.get("market_regime") not in {"CHOPPY_MARKET", "RISK_OFF"},
        {"market_regime"},
    )
    add(
        "SYMBOL_APPROVED_ONLY",
        "ensemble",
        {"symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"]},
        lambda row: row.get("symbol") in {"BTCUSDT", "ETHUSDT", "SOLUSDT"},
        {"symbol"},
    )
    add(
        "SCORE_AND_VOLUME_FILTERED",
        "ensemble",
        {"confidence_score_min": 80, "volume_relative_min": 1.2},
        lambda row: safe_float(row.get("confidence_score")) >= 80 and safe_float(row.get("volume_relative")) >= 1.2,
        {"confidence_score", "volume_relative"},
    )
    add(
        "MULTI_FILTER_CONSENSUS_2_OF_3",
        "ensemble",
        {"min_votes": 2, "votes": ["btc_aligned", "volume_good", "spread_low"]},
        lambda row: _vote_count(row, ["btc_aligned", "volume_good", "spread_low"]) >= 2,
        {"side", "btc_momentum_5", "btc_momentum_15", "volume_relative", "spread_pct"},
    )
    add(
        "MULTI_FILTER_CONSENSUS_3_OF_5",
        "ensemble",
        {"min_votes": 3, "votes": ["btc_aligned", "volume_good", "spread_low", "score_80", "not_choppy"]},
        lambda row: _vote_count(row, ["btc_aligned", "volume_good", "spread_low", "score_80", "not_choppy"]) >= 3,
        {"side", "btc_momentum_5", "btc_momentum_15", "volume_relative", "spread_pct", "confidence_score", "market_regime"},
    )
    add(
        "COUNTERFACTUAL_SL_AVOIDANCE_FILTER",
        "ensemble",
        {"counterfactual": "diagnostic_only"},
        lambda row: safe_int(row.get("counterfactual_avoided_loss")) == 0,
        {"counterfactual_avoided_loss"},
        uses_future=True,
    )
    add(
        "PHASE2_FAILURE_CLUSTER_AVOIDANCE_DIAGNOSTIC",
        "ensemble",
        {"phase2_cluster": "diagnostic_only", "avoid_failure_cluster": True},
        lambda row: safe_int(row.get("in_stop_loss_failure_cluster")) == 0,
        {"in_stop_loss_failure_cluster"},
        uses_future=True,
    )
    add(
        "PHASE2_WIN_CLUSTER_CONFIRMATION_DIAGNOSTIC",
        "ensemble",
        {"phase2_cluster": "diagnostic_only", "require_win_cluster": True},
        lambda row: safe_int(row.get("in_win_cluster")) == 1,
        {"in_win_cluster"},
        uses_future=True,
    )
    add(
        "VIRTUAL_RESEARCH_POSITIVE_DIAGNOSTIC",
        "ensemble",
        {"virtual_research_trades": "diagnostic_only"},
        lambda row: safe_int(row.get("virtual_research_positive")) == 1,
        {"virtual_research_positive"},
        uses_future=True,
    )

    dynamic_limit = 8 if safe_mode else 30
    for symbol in _top_values(rows, "symbol", dynamic_limit):
        add(
            f"SYMBOL_{symbol}",
            "symbol_filter",
            {"symbol": symbol},
            lambda row, value=symbol: row.get("symbol") == value,
            {"symbol"},
        )
    for strategy in _top_values(rows, "strategy_type", dynamic_limit):
        add(
            f"STRATEGY_{strategy}",
            "symbol_filter",
            {"strategy_type": strategy},
            lambda row, value=strategy: row.get("strategy_type") == value,
            {"strategy_type"},
        )
    for symbol, strategy in _top_pairs(rows, "symbol", "strategy_type", dynamic_limit):
        add(
            f"SYMBOL_STRATEGY_{symbol}_{strategy}",
            "symbol_filter",
            {"symbol": symbol, "strategy_type": strategy},
            lambda row, sym=symbol, strat=strategy: row.get("symbol") == sym and row.get("strategy_type") == strat,
            {"symbol", "strategy_type"},
        )
    for symbol, regime in _top_pairs(rows, "symbol", "market_regime", dynamic_limit):
        add(
            f"SYMBOL_REGIME_{symbol}_{regime}",
            "symbol_filter",
            {"symbol": symbol, "market_regime": regime},
            lambda row, sym=symbol, reg=regime: row.get("symbol") == sym and row.get("market_regime") == reg,
            {"symbol", "market_regime"},
        )
    for threshold in (72, 75, 80, 85, 90):
        for strategy in _top_values(rows, "strategy_type", 5 if safe_mode else 15):
            add(
                f"SCORE_GE_{threshold}_STRATEGY_{strategy}",
                "score_threshold",
                {"confidence_score_min": threshold, "strategy_type": strategy},
                lambda row, value=threshold, strat=strategy: safe_float(row.get("confidence_score")) >= value and row.get("strategy_type") == strat,
                {"confidence_score", "strategy_type"},
            )
        for regime in _top_values(rows, "market_regime", 5 if safe_mode else 15):
            add(
                f"SCORE_GE_{threshold}_REGIME_{regime}",
                "score_threshold",
                {"confidence_score_min": threshold, "market_regime": regime},
                lambda row, value=threshold, reg=regime: safe_float(row.get("confidence_score")) >= value and row.get("market_regime") == reg,
                {"confidence_score", "market_regime"},
            )
    return candidates


def conservative_score(
    *,
    total: int,
    oos_profit_factor: float,
    expectancy: float,
    drawdown: float,
    time_rate: float,
    stability: float,
    overfit_penalty: float,
) -> float:
    sample_score = min(total / 1000.0, 1.0)
    pf_score = min(oos_profit_factor / 2.0, 1.0) if math.isfinite(oos_profit_factor) else 0.0
    expectancy_score = 1.0 if expectancy > 0 else 0.0
    drawdown_penalty = min(drawdown * 8.0, 1.0)
    score = (
        0.30 * pf_score
        + 0.20 * expectancy_score
        + 0.20 * sample_score
        + 0.20 * stability
        - 0.15 * drawdown_penalty
        - 0.15 * min(time_rate, 1.0)
        - 0.20 * overfit_penalty
    )
    return max(0.0, round(score, 6))


def _candidate_status(
    train_metrics: dict[str, float],
    test_metrics: dict[str, float],
    stability: float,
    overfit_penalty: float,
) -> tuple[str, str]:
    train_pf = safe_float(train_metrics.get("profit_factor"))
    oos_pf = safe_float(test_metrics.get("profit_factor"))
    expectancy = safe_float(test_metrics.get("expectancy"))
    time_rate = safe_float(test_metrics.get("time_rate"))
    if train_pf >= MIN_OUT_OF_SAMPLE_PF and (oos_pf < 1.0 or overfit_penalty >= 0.55):
        return "REJECTED_OVERFITTING", "PF in-sample no se sostiene en bloques posteriores"
    if time_rate > MAX_TIME_RATIO:
        return "REJECTED_TOO_MUCH_TIME", "demasiados TIME; la ventaja no se materializa"
    if oos_pf < MIN_OUT_OF_SAMPLE_PF or expectancy <= 0:
        return "REJECTED_NO_EDGE", "PF OOS o expectancy insuficiente"
    if stability < 0.5:
        return "REJECTED_UNSTABLE", "la ventaja no es estable por ventanas temporales"
    return "ACCEPTED_RESEARCH_ONLY", "candidato solo para seguir observando en paper/research"


def _overfit_penalty(train_pf: float, oos_pf: float, stability: float) -> float:
    train = safe_float(train_pf)
    test = safe_float(oos_pf)
    if not math.isfinite(train):
        train = 10.0
    if not math.isfinite(test):
        test = 0.0
    degradation = max(0.0, train - test)
    return min(1.0, degradation / max(train, 1.0) + max(0.0, 0.5 - stability))


def _side_aligned(row: dict[str, Any], key: str) -> bool:
    return _side_aligned_value(row, safe_float(row.get(key)))


def _side_aligned_value(row: dict[str, Any], value: float) -> bool:
    side = str(row.get("side") or "").upper()
    return (side == "LONG" and value >= 0) or (side == "SHORT" and value <= 0)


def _btc_aligned(row: dict[str, Any]) -> bool:
    return _side_aligned_value(row, safe_float(row.get("btc_momentum_5")) + safe_float(row.get("btc_momentum_15")))


def _side_rsi(row: dict[str, Any], *, long_min: float, long_max: float, short_min: float, short_max: float) -> bool:
    rsi = safe_float(row.get("rsi_14"))
    side = str(row.get("side") or "").upper()
    return (side == "LONG" and long_min <= rsi <= long_max) or (side == "SHORT" and short_min <= rsi <= short_max)


def _vote_count(row: dict[str, Any], votes: list[str]) -> int:
    count = 0
    for vote in votes:
        if vote == "btc_aligned" and _btc_aligned(row):
            count += 1
        elif vote == "volume_good" and safe_float(row.get("volume_relative")) >= 1.2:
            count += 1
        elif vote == "spread_low" and safe_float(row.get("spread_pct")) <= 0.001:
            count += 1
        elif vote == "score_80" and safe_float(row.get("confidence_score")) >= 80:
            count += 1
        elif vote == "not_choppy" and row.get("market_regime") != "CHOPPY_MARKET":
            count += 1
    return count


def _top_values(rows: list[dict[str, Any]], key: str, limit: int) -> list[str]:
    counter = Counter(str(row.get(key) or "NA").upper() for row in rows if row.get(key))
    return [value for value, _ in counter.most_common(limit)]


def _top_pairs(rows: list[dict[str, Any]], key_a: str, key_b: str, limit: int) -> list[tuple[str, str]]:
    counter = Counter(
        (str(row.get(key_a) or "NA").upper(), str(row.get(key_b) or "NA").upper())
        for row in rows
        if row.get(key_a) and row.get(key_b)
    )
    return [pair for pair, _ in counter.most_common(limit)]


def _first_timestamp(rows: list[dict[str, Any]]) -> str:
    return str(rows[0].get("timestamp") or "") if rows else ""


def _last_timestamp(rows: list[dict[str, Any]]) -> str:
    return str(rows[-1].get("timestamp") or "") if rows else ""


def _evaluation_lines(items: list[CandidateEvaluation]) -> list[str]:
    if not items:
        return ["- sin evidencia suficiente"]
    return [
        (
            f"- {item.candidate.name} [{item.candidate.family}]: "
            f"status={item.status}, samples={item.total_samples}, PF OOS={item.out_of_sample_profit_factor:.2f}, "
            f"expectancy={item.expectancy:.5f}, TIME={item.time_rate:.1%}, score={item.conservative_score:.3f}"
        )
        for item in items
    ]
