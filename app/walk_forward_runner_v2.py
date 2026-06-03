"""ResearchOps V7.5 — Walk-Forward Runner V2 con rolling windows + Bootstrap CI.

Complementa el Phase 8B walk-forward (folds estáticos). NO lo reemplaza.

Diseño:
  - rolling windows con `train_days`, `test_days`, `step_days` configurables.
  - per-fold metrics: trades, net_EV, net_PF, win_rate, max_drawdown_proxy.
  - bootstrap (con seed fija para reproducibilidad) sobre la lista de net_EV
    por fold → IC 95%.
  - detección de single-fold dominance y regime instability.

Hard rules:
  - research-only
  - sin lookahead (los folds están ordenados por timestamp y no se mezclan).
  - sin endpoints privados.
  - decisión máxima `WF2_PROMISING_LABEL_ONLY`; nunca activa nada.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence


FINAL_RECOMMENDATION = "NO LIVE"

DECISION_NEED_MORE_DATA = "WF2_NEED_MORE_DATA"
DECISION_NEED_MORE_FOLDS = "WF2_NEED_MORE_FOLDS"
DECISION_REJECT = "WF2_REJECT"
DECISION_WATCH_ONLY = "WF2_WATCH_ONLY"
DECISION_PROMISING = "WF2_PROMISING_LABEL_ONLY"


@dataclass
class FoldMetrics:
    fold_index: int
    start: str
    end: str
    train_start: str
    train_end: str
    train_trades: int
    test_trades: int
    test_net_ev_pct: float
    test_net_pf: float
    test_win_rate: float
    test_max_drawdown_proxy: float
    regime_mode: str = "UNKNOWN"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BootstrapCI:
    mean: float
    median: float
    low: float
    high: float
    n_samples: int
    n_bootstrap: int
    seed: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WalkForwardV2Report:
    symbols: list[str]
    timeframe: str
    train_days: int
    test_days: int
    step_days: int
    n_folds: int
    positive_folds: int
    fold_metrics: list[FoldMetrics] = field(default_factory=list)
    bootstrap_net_ev: BootstrapCI | None = None
    bootstrap_net_pf: BootstrapCI | None = None
    single_fold_dominance: bool = False
    regime_instability: bool = False
    decision: str = DECISION_NEED_MORE_DATA
    reasons: list[str] = field(default_factory=list)
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION
    no_lookahead_status: str = "OK_PREFIX_ONLY"

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.bootstrap_net_ev:
            data["bootstrap_net_ev"] = self.bootstrap_net_ev.as_dict()
        if self.bootstrap_net_pf:
            data["bootstrap_net_pf"] = self.bootstrap_net_pf.as_dict()
        return data


# ---------------------------------------------------------------------------


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _avg(values: Iterable[float]) -> float:
    values = list(values)
    return sum(values) / max(len(values), 1)


def _net_pf(values: Sequence[float]) -> float:
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v < 0]
    loss_sum = abs(sum(losses))
    if loss_sum > 0:
        return sum(wins) / loss_sum
    return 999.0 if wins else 0.0


def _max_drawdown_proxy(values: Sequence[float]) -> float:
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for value in values:
        equity += float(value or 0.0)
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return abs(worst)


def _bootstrap_ci(
    values: Sequence[float],
    *,
    statistic: str,
    n_bootstrap: int = 1000,
    confidence: float = 0.95,
    seed: int = 1729,
) -> BootstrapCI | None:
    if len(values) < 3:
        return None
    rng = random.Random(seed)
    n = len(values)
    samples: list[float] = []
    for _ in range(n_bootstrap):
        resample = [values[rng.randrange(n)] for _ in range(n)]
        if statistic == "mean":
            samples.append(sum(resample) / n)
        elif statistic == "pf":
            samples.append(_net_pf(resample))
        else:
            samples.append(sum(resample) / n)
    samples.sort()
    alpha = (1.0 - confidence) / 2.0
    low = samples[int(alpha * n_bootstrap)]
    high = samples[int((1.0 - alpha) * n_bootstrap) - 1]
    return BootstrapCI(
        mean=sum(samples) / n_bootstrap,
        median=samples[n_bootstrap // 2],
        low=low,
        high=high,
        n_samples=n,
        n_bootstrap=n_bootstrap,
        seed=seed,
    )


def _detect_single_fold_dominance(folds: list[FoldMetrics]) -> bool:
    positive = [f for f in folds if f.test_net_ev_pct > 0]
    if not positive:
        return False
    total_positive_ev = sum(f.test_net_ev_pct for f in positive)
    if total_positive_ev <= 0:
        return False
    top = max(f.test_net_ev_pct for f in positive)
    return (top / total_positive_ev) > 0.80


def _detect_regime_instability(folds: list[FoldMetrics]) -> bool:
    if len(folds) < 3:
        return False
    pos = sum(1 for f in folds if f.test_net_ev_pct > 0)
    neg = sum(1 for f in folds if f.test_net_ev_pct < 0)
    return pos > 0 and neg > 0 and abs(pos - neg) <= 1


def _classify_decision(report: WalkForwardV2Report, min_folds_for_promising: int = 4) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if report.n_folds == 0:
        return DECISION_NEED_MORE_DATA, ["no_folds_computed"]
    if report.n_folds < min_folds_for_promising:
        reasons.append(f"n_folds={report.n_folds}_below_min_{min_folds_for_promising}")
        return DECISION_NEED_MORE_FOLDS, reasons
    if not report.bootstrap_net_ev:
        return DECISION_NEED_MORE_DATA, ["bootstrap_ci_unavailable"]
    if report.bootstrap_net_ev.low >= 0 and report.positive_folds >= max(3, int(report.n_folds * 0.75)):
        if report.single_fold_dominance:
            reasons.append("single_fold_dominance_detected")
            return DECISION_WATCH_ONLY, reasons
        if report.regime_instability:
            reasons.append("regime_instability_detected")
            return DECISION_WATCH_ONLY, reasons
        return DECISION_PROMISING, ["bootstrap_lower_bound_non_negative_majority_folds_positive_label_only"]
    if report.bootstrap_net_ev.mean <= 0 and report.positive_folds < (report.n_folds // 2):
        return DECISION_REJECT, ["bootstrap_mean_non_positive_minority_folds_positive"]
    return DECISION_WATCH_ONLY, ["mixed_signal_keep_in_research"]


def _fold_from_trades(
    fold_index: int,
    *,
    train: list[dict[str, Any]],
    test: list[dict[str, Any]],
    start: datetime,
    end: datetime,
    train_start: datetime,
    train_end: datetime,
    regime_mode: str = "UNKNOWN",
) -> FoldMetrics:
    test_returns = [float(t.get("net_return_pct") or 0.0) for t in test]
    wins = [v for v in test_returns if v > 0]
    return FoldMetrics(
        fold_index=fold_index,
        start=start.isoformat(),
        end=end.isoformat(),
        train_start=train_start.isoformat(),
        train_end=train_end.isoformat(),
        train_trades=len(train),
        test_trades=len(test),
        test_net_ev_pct=_avg(test_returns),
        test_net_pf=_net_pf(test_returns),
        test_win_rate=len(wins) / max(len(test_returns), 1),
        test_max_drawdown_proxy=_max_drawdown_proxy(test_returns),
        regime_mode=regime_mode,
    )


def run_walk_forward_v2(
    *,
    trades: Sequence[dict[str, Any]],
    train_days: int = 30,
    test_days: int = 7,
    step_days: int = 7,
    symbols: list[str] | None = None,
    timeframe: str = "5m",
    n_bootstrap: int = 1000,
    seed: int = 1729,
    min_trades_per_fold: int = 20,
) -> WalkForwardV2Report:
    """Rolling walk-forward. Cada trade es un dict con `entry_time` y
    `net_return_pct`. Si la lista está vacía o demasiado corta, devuelve
    `WF2_NEED_MORE_DATA`.
    """
    parsed_trades: list[tuple[datetime, dict[str, Any]]] = []
    for trade in trades:
        dt = _parse_dt(trade.get("entry_time"))
        if dt is None:
            continue
        parsed_trades.append((dt, trade))
    parsed_trades.sort(key=lambda item: item[0])

    if not parsed_trades:
        report = WalkForwardV2Report(
            symbols=list(symbols or []),
            timeframe=str(timeframe or "5m"),
            train_days=int(train_days),
            test_days=int(test_days),
            step_days=int(step_days),
            n_folds=0,
            positive_folds=0,
            reasons=["no_trades_with_entry_time"],
        )
        report.decision = DECISION_NEED_MORE_DATA
        return report

    first_dt = parsed_trades[0][0]
    last_dt = parsed_trades[-1][0]
    folds: list[FoldMetrics] = []
    fold_idx = 0
    cursor = first_dt + timedelta(days=int(train_days))
    while cursor + timedelta(days=int(test_days)) <= last_dt + timedelta(days=int(step_days)):
        train_start = cursor - timedelta(days=int(train_days))
        train_end = cursor
        test_start = cursor
        test_end = cursor + timedelta(days=int(test_days))
        train = [t for dt, t in parsed_trades if train_start <= dt < train_end]
        test = [t for dt, t in parsed_trades if test_start <= dt < test_end]
        if len(test) >= min_trades_per_fold:
            fold_idx += 1
            folds.append(_fold_from_trades(
                fold_idx,
                train=train, test=test,
                start=test_start, end=test_end,
                train_start=train_start, train_end=train_end,
            ))
        cursor = cursor + timedelta(days=int(step_days))

    positive = sum(1 for f in folds if f.test_net_ev_pct > 0)
    bootstrap_ev = _bootstrap_ci(
        [f.test_net_ev_pct for f in folds], statistic="mean",
        n_bootstrap=min(int(n_bootstrap), 1000), seed=int(seed),
    )
    bootstrap_pf = _bootstrap_ci(
        [f.test_net_pf for f in folds], statistic="mean",
        n_bootstrap=min(int(n_bootstrap), 1000), seed=int(seed) + 1,
    )
    report = WalkForwardV2Report(
        symbols=list(symbols or []),
        timeframe=str(timeframe or "5m"),
        train_days=int(train_days),
        test_days=int(test_days),
        step_days=int(step_days),
        n_folds=len(folds),
        positive_folds=positive,
        fold_metrics=folds,
        bootstrap_net_ev=bootstrap_ev,
        bootstrap_net_pf=bootstrap_pf,
        single_fold_dominance=_detect_single_fold_dominance(folds),
        regime_instability=_detect_regime_instability(folds),
    )
    decision, reasons = _classify_decision(report)
    report.decision = decision
    report.reasons = reasons
    return report


def render_walk_forward_v2_text(report: WalkForwardV2Report) -> str:
    lines = [
        "WALK FORWARD V2 START",
        f"symbols: {','.join(report.symbols) if report.symbols else 'ALL'}",
        f"timeframe: {report.timeframe}",
        f"train_days: {report.train_days}",
        f"test_days: {report.test_days}",
        f"step_days: {report.step_days}",
        f"n_folds: {report.n_folds}",
        f"positive_folds: {report.positive_folds}",
        f"single_fold_dominance: {str(report.single_fold_dominance).lower()}",
        f"regime_instability: {str(report.regime_instability).lower()}",
        f"decision: {report.decision}",
    ]
    if report.bootstrap_net_ev:
        ci = report.bootstrap_net_ev
        lines.append(
            f"bootstrap_net_ev: mean={ci.mean:.6f} median={ci.median:.6f} "
            f"low={ci.low:.6f} high={ci.high:.6f} samples={ci.n_samples} "
            f"bootstrap={ci.n_bootstrap} seed={ci.seed}"
        )
    if report.bootstrap_net_pf:
        cp = report.bootstrap_net_pf
        lines.append(
            f"bootstrap_net_pf: mean={cp.mean:.4f} low={cp.low:.4f} high={cp.high:.4f}"
        )
    lines.append("fold | train_n | test_n | net_ev | net_pf | win | dd")
    for fold in report.fold_metrics[:30]:
        lines.append(
            f"{fold.fold_index} | {fold.train_trades} | {fold.test_trades} | "
            f"{fold.test_net_ev_pct:.6f} | {fold.test_net_pf:.4f} | "
            f"{fold.test_win_rate:.3f} | {fold.test_max_drawdown_proxy:.4f}"
        )
    if report.reasons:
        lines.append("reasons:")
        for reason in report.reasons:
            lines.append(f"- {reason}")
    lines.extend([
        "no_lookahead_status: OK_PREFIX_ONLY",
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "final_recommendation: NO LIVE",
        "WALK FORWARD V2 END",
    ])
    return "\n".join(lines)
