from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Any

from .config import BotConfig
from .meta_model import MetaModel
from .walkforward import evaluate_walkforward


@dataclass
class ParameterTrial:
    params: dict[str, Any]
    score: float
    metrics: dict[str, float]


PARAMETER_GRID = {
    "min_score_to_trade": [72, 75, 78, 80],
    "min_risk_reward": [1.4, 1.6, 1.8],
    "trade_margin_usdt": [8, 10, 12],
    "max_leverage": [3, 4, 5],
    "min_stop_distance_pct": [0.006, 0.008, 0.01],
    "meta_min_probability": [0.55, 0.58, 0.60, 0.62],
}


def optimize_parameters_walkforward(
    rows: list[dict[str, Any]],
    base_config: BotConfig,
    *,
    train_window: int = 180,
    validation_window: int = 60,
    test_window: int = 60,
    max_trials: int = 200,
) -> list[ParameterTrial]:
    """Small, bounded grid search. It never optimizes on the full history at once."""

    keys = list(PARAMETER_GRID)
    trials: list[ParameterTrial] = []
    for index, values in enumerate(product(*(PARAMETER_GRID[key] for key in keys))):
        if index >= max_trials:
            break
        params = dict(zip(keys, values))
        config = BotConfig(
            min_score_to_trade=params["min_score_to_trade"],
            min_risk_reward=params["min_risk_reward"],
            trade_margin_usdt=str(params["trade_margin_usdt"]),
            max_trade_margin_usdt=str(max(float(base_config.max_trade_margin_usdt), float(params["trade_margin_usdt"]))),
            max_leverage=params["max_leverage"],
            default_leverage=min(base_config.default_leverage, params["max_leverage"]),
            min_stop_distance_pct=params["min_stop_distance_pct"],
            meta_min_probability=params["meta_min_probability"],
            enable_meta_model=True,
            meta_model_mode="filter",
        )
        result = evaluate_walkforward(
            rows,
            lambda cfg=config: MetaModel(cfg),
            train_window=train_window,
            validation_window=validation_window,
            test_window=test_window,
            min_probability_grid=[params["meta_min_probability"]],
        )
        metrics = result.metrics
        score = (
            metrics.get("profit_factor_after_filter", 0.0)
            - metrics.get("max_drawdown_after_filter", 0.0)
            + metrics.get("precision", 0.0)
        )
        trials.append(ParameterTrial(params, score, metrics))
    return sorted(trials, key=lambda trial: trial.score, reverse=True)
