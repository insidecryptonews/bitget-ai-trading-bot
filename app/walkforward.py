from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .meta_model import MetaModel
from .utils import safe_float


@dataclass
class WalkForwardSplit:
    train: list[dict[str, Any]]
    validation: list[dict[str, Any]]
    test: list[dict[str, Any]]


@dataclass
class WalkForwardResult:
    windows: int
    metrics: dict[str, float] = field(default_factory=dict)
    passed_live_activation_rules: bool = False
    reason: str = ""


def make_walkforward_splits(
    rows: list[dict[str, Any]],
    *,
    train_window: int,
    validation_window: int,
    test_window: int,
    step: int | None = None,
) -> list[WalkForwardSplit]:
    ordered = sorted(rows, key=lambda row: str(row.get("timestamp", "")))
    total = train_window + validation_window + test_window
    if len(ordered) < total:
        return []
    step = step or test_window
    splits: list[WalkForwardSplit] = []
    for start in range(0, len(ordered) - total + 1, step):
        train_end = start + train_window
        validation_end = train_end + validation_window
        test_end = validation_end + test_window
        splits.append(
            WalkForwardSplit(
                train=ordered[start:train_end],
                validation=ordered[train_end:validation_end],
                test=ordered[validation_end:test_end],
            )
        )
    return splits


def evaluate_walkforward(
    rows: list[dict[str, Any]],
    model_factory: Callable[[], MetaModel],
    *,
    train_window: int,
    validation_window: int,
    test_window: int,
    min_probability_grid: list[float] | None = None,
) -> WalkForwardResult:
    splits = make_walkforward_splits(
        rows,
        train_window=train_window,
        validation_window=validation_window,
        test_window=test_window,
    )
    if not splits:
        return WalkForwardResult(0, reason="historico insuficiente para walk-forward")

    grid = min_probability_grid or [0.55, 0.58, 0.60, 0.62]
    window_metrics: list[dict[str, float]] = []
    for split in splits:
        model = model_factory()
        model.train(split.train)
        model.mark_validated("validacion walk-forward temporal")
        threshold = _best_threshold(model, split.validation, grid)
        predictions = _score_rows(model, split.test, threshold)
        window_metrics.append(_metrics(predictions))

    aggregate = _average_metrics(window_metrics)
    passed = (
        len(splits) >= 5
        and len(rows) >= 300
        and aggregate.get("profit_factor_after_filter", 0.0) > aggregate.get("profit_factor_base", 0.0)
        and aggregate.get("max_drawdown_after_filter", 1.0) < aggregate.get("max_drawdown_base", 1.0)
        and aggregate.get("precision", 0.0) >= 0.55
        and aggregate.get("number_of_trades_kept", 0.0) >= max(20.0, aggregate.get("number_of_trades_base", 0.0) * 0.25)
    )
    reason = "walk-forward mejora out-of-sample" if passed else "walk-forward no cumple reglas de activacion live"
    return WalkForwardResult(len(splits), aggregate, passed, reason)


def _best_threshold(model: MetaModel, rows: list[dict[str, Any]], grid: list[float]) -> float:
    best_threshold = grid[0]
    best_pf = -1.0
    for threshold in grid:
        metrics = _metrics(_score_rows(model, rows, threshold))
        pf = metrics.get("profit_factor_after_filter", 0.0)
        if pf > best_pf:
            best_pf = pf
            best_threshold = threshold
    return best_threshold


def _score_rows(model: MetaModel, rows: list[dict[str, Any]], threshold: float) -> list[dict[str, Any]]:
    scored: list[dict[str, Any]] = []
    for row in rows:
        probability = model.predict_probability(row)
        keep = probability is not None and probability >= threshold
        out = dict(row)
        out["meta_probability"] = probability if probability is not None else 0.0
        out["kept"] = int(keep)
        scored.append(out)
    return scored


def _metrics(rows: list[dict[str, Any]]) -> dict[str, float]:
    if not rows:
        return {}
    kept = [row for row in rows if int(row.get("kept", 0)) == 1]
    filtered = [row for row in rows if int(row.get("kept", 0)) == 0]
    positives = [row for row in kept if int(row.get("label", 0)) == 1]
    false_positives = [row for row in kept if int(row.get("label", 0)) != 1]
    false_negatives = [row for row in filtered if int(row.get("label", 0)) == 1]
    true_positives = len(positives)
    labels_positive = sum(1 for row in rows if int(row.get("label", 0)) == 1)

    return {
        "accuracy": _accuracy(rows),
        "precision": true_positives / max(len(kept), 1),
        "recall": true_positives / max(labels_positive, 1),
        "win_rate_after_filter": _win_rate(kept),
        "profit_factor_after_filter": _profit_factor(kept),
        "profit_factor_base": _profit_factor(rows),
        "max_drawdown_after_filter": _max_drawdown(kept),
        "max_drawdown_base": _max_drawdown(rows),
        "number_of_trades_kept": float(len(kept)),
        "number_of_trades_filtered": float(len(filtered)),
        "number_of_trades_base": float(len(rows)),
        "average_return_kept": _avg_return(kept),
        "average_return_filtered": _avg_return(filtered),
        "false_positive_rate": len(false_positives) / max(len(kept), 1),
        "false_negative_rate": len(false_negatives) / max(len(filtered), 1),
    }


def _accuracy(rows: list[dict[str, Any]]) -> float:
    correct = 0
    for row in rows:
        pred = int(row.get("kept", 0)) == 1
        actual = int(row.get("label", 0)) == 1
        correct += pred == actual
    return correct / max(len(rows), 1)


def _win_rate(rows: list[dict[str, Any]]) -> float:
    return sum(1 for row in rows if int(row.get("label", 0)) == 1) / max(len(rows), 1)


def _profit_factor(rows: list[dict[str, Any]]) -> float:
    gains = sum(max(safe_float(row.get("realized_return_pct")), 0.0) for row in rows)
    losses = abs(sum(min(safe_float(row.get("realized_return_pct")), 0.0) for row in rows))
    return gains / losses if losses > 0 else gains if gains > 0 else 0.0


def _avg_return(rows: list[dict[str, Any]]) -> float:
    return sum(safe_float(row.get("realized_return_pct")) for row in rows) / max(len(rows), 1)


def _max_drawdown(rows: list[dict[str, Any]]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for row in rows:
        equity += safe_float(row.get("realized_return_pct"))
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return abs(max_dd)


def _average_metrics(metrics: list[dict[str, float]]) -> dict[str, float]:
    keys = {key for item in metrics for key in item}
    return {key: sum(item.get(key, 0.0) for item in metrics) / max(len(metrics), 1) for key in keys}
