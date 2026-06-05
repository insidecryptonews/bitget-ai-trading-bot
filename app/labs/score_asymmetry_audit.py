"""V8.2 — Score Asymmetry Audit (research-only).

Compares LONG (in RISK_ON / TREND_UP) vs SHORT (in RISK_OFF / TREND_DOWN)
score distributions and simulates three remediations WITHOUT touching
``signal_engine.py`` or ``regime_detector.py``:

1. ``simulate_symmetric_regime``: what if RISK_OFF used ``score_adjustment=+5``
   (same as RISK_ON) instead of ``-10``?
2. ``simulate_atr_softening``: what if the ATR penalty was scaled and
   exempted when ``side`` coincides with the regime's directional bias?
3. ``simulate_high_vol_directional``: what if HIGH_VOLATILITY routed by
   momentum sign instead of forcing ``allowed_direction=NONE``?

All three are PURE FUNCTIONS that recompute scores from observed rows; they
do not patch production.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from statistics import mean, median
from typing import Any, Iterable

from . import (
    FINAL_RECOMMENDATION_NO_LIVE,
    REGIME_HIGH_VOLATILITY,
    REGIME_RISK_OFF,
    REGIME_RISK_ON,
    REGIME_TREND_DOWN,
    REGIME_TREND_UP,
    SIDE_LONG,
    SIDE_SHORT,
    STATUS_NEED_DATA,
    STATUS_OK,
)


# Production scoring constants (mirror — never mutate the originals).
RISK_OFF_PENALTY = -10
RISK_ON_BONUS = 5
ATR_PENALTY_HARD = 25
SPREAD_PENALTY = 20
MIN_SCORE_TO_TRADE_DEFAULT = 72


def _safe_call(db: Any, method: str, *args, **kwargs) -> tuple[bool, Any]:
    if db is None:
        return False, None
    fn = getattr(db, method, None)
    if fn is None or not callable(fn):
        return False, None
    try:
        return True, fn(*args, **kwargs)
    except Exception:
        return False, None


def _fetch_rows(
    db: Any,
    hours: int,
    rows: Iterable[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    if rows is not None:
        return list(rows), []
    ok, value = _safe_call(db, "fetch_signal_observations", hours=int(hours))
    if not ok or value is None:
        return [], ["signal_observations_method_missing_or_empty"]
    return list(value), []


def _stats(scores: list[float]) -> dict[str, float]:
    if not scores:
        return {"count": 0, "mean": 0.0, "median": 0.0, "p25": 0.0, "p75": 0.0,
                "min": 0.0, "max": 0.0}
    s = sorted(scores)
    n = len(s)
    return {
        "count": n,
        "mean": mean(s),
        "median": median(s),
        "p25": s[max(0, n // 4)],
        "p75": s[min(n - 1, (3 * n) // 4)],
        "min": s[0],
        "max": s[-1],
    }


def _pct_above(scores: list[float], threshold: int) -> float:
    if not scores:
        return 0.0
    return sum(1 for s in scores if s >= threshold) / len(scores)


# ---- Audit -----------------------------------------------------------------

@dataclass
class AsymmetryReport:
    hours: int
    long_in_bull: dict[str, float] = field(default_factory=dict)
    short_in_bear: dict[str, float] = field(default_factory=dict)
    pct_long_pass_min_score: float = 0.0
    pct_short_pass_min_score: float = 0.0
    median_long: float = 0.0
    median_short: float = 0.0
    gap_long_minus_short: float = 0.0
    min_score_to_trade: int = MIN_SCORE_TO_TRADE_DEFAULT
    status: str = STATUS_NEED_DATA
    need_data_reasons: list[str] = field(default_factory=list)
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def audit(
    db: Any,
    *,
    hours: int = 168,
    min_score: int = MIN_SCORE_TO_TRADE_DEFAULT,
    rows: Iterable[dict[str, Any]] | None = None,
) -> AsymmetryReport:
    data, need = _fetch_rows(db, hours, rows)
    report = AsymmetryReport(
        hours=int(hours),
        min_score_to_trade=int(min_score),
        need_data_reasons=list(need),
    )
    if not data:
        return report
    long_scores: list[float] = []
    short_scores: list[float] = []
    for r in data:
        side = str(r.get("proposed_side") or r.get("side") or "").upper()
        regime = str(r.get("market_regime") or r.get("regime") or "").upper()
        score = r.get("confidence_score") if isinstance(r.get("confidence_score"), (int, float)) else r.get("score")
        if not isinstance(score, (int, float)):
            continue
        if side == SIDE_LONG and regime in {REGIME_RISK_ON, REGIME_TREND_UP}:
            long_scores.append(float(score))
        elif side == SIDE_SHORT and regime in {REGIME_RISK_OFF, REGIME_TREND_DOWN}:
            short_scores.append(float(score))
    report.long_in_bull = _stats(long_scores)
    report.short_in_bear = _stats(short_scores)
    report.pct_long_pass_min_score = _pct_above(long_scores, int(min_score))
    report.pct_short_pass_min_score = _pct_above(short_scores, int(min_score))
    report.median_long = report.long_in_bull["median"]
    report.median_short = report.short_in_bear["median"]
    report.gap_long_minus_short = report.median_long - report.median_short
    report.status = STATUS_OK if (long_scores or short_scores) else STATUS_NEED_DATA
    return report


# ---- Simulations -----------------------------------------------------------

@dataclass
class SimulationReport:
    hours: int
    name: str
    delta_long_pass: int = 0
    delta_short_pass: int = 0
    new_long_pass_pct: float = 0.0
    new_short_pass_pct: float = 0.0
    baseline_long_pass_pct: float = 0.0
    baseline_short_pass_pct: float = 0.0
    samples_long: int = 0
    samples_short: int = 0
    status: str = STATUS_NEED_DATA
    need_data_reasons: list[str] = field(default_factory=list)
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _recompute_score_symmetric_regime(row: dict[str, Any]) -> float:
    """Replay the score with RISK_OFF→+5 (symmetric with RISK_ON)."""
    base = float(row.get("confidence_score") or row.get("score") or 0)
    regime = str(row.get("market_regime") or row.get("regime") or "").upper()
    side = str(row.get("proposed_side") or row.get("side") or "").upper()
    if regime == REGIME_RISK_OFF and side == SIDE_SHORT:
        # Production currently subtracts 10; the simulated symmetric path
        # would add 5 instead. Net delta on the score = +15.
        return base + (RISK_ON_BONUS - RISK_OFF_PENALTY)
    return base


def _recompute_score_atr_softened(row: dict[str, Any]) -> float:
    """Replay the score with ATR penalty scaled + exempted when side coincides
    with the regime's directional bias.
    """
    base = float(row.get("confidence_score") or row.get("score") or 0)
    atr_norm = row.get("normalized_atr")
    if not isinstance(atr_norm, (int, float)):
        return base
    side = str(row.get("proposed_side") or row.get("side") or "").upper()
    regime = str(row.get("market_regime") or row.get("regime") or "").upper()
    side_matches_regime = (
        (side == SIDE_LONG and regime in {REGIME_RISK_ON, REGIME_TREND_UP})
        or (side == SIDE_SHORT and regime in {REGIME_RISK_OFF, REGIME_TREND_DOWN})
    )
    # Production: -25 if atr_norm > 0.025.
    production_penalty = ATR_PENALTY_HARD if atr_norm > 0.025 else 0
    # Softened: scaled (max -25 at atr_norm = 0.075), or 0 if side matches regime.
    if side_matches_regime:
        softened_penalty = 0
    else:
        softened_penalty = min(ATR_PENALTY_HARD, max(0.0, (atr_norm - 0.025) * 500))
    return base + (production_penalty - softened_penalty)


def _recompute_score_high_vol_directional(row: dict[str, Any]) -> float:
    """Replay the score under HIGH_VOLATILITY routed by momentum sign.

    Production: HIGH_VOLATILITY with |momentum_15| > 0.06 forces NONE → no signal.
    Simulated: HIGH_VOLATILITY routes to SHORT if momentum<0 or LONG if >0,
    with score_adjustment=-5 instead of -25.
    """
    base = float(row.get("confidence_score") or row.get("score") or 0)
    regime = str(row.get("market_regime") or row.get("regime") or "").upper()
    if regime != REGIME_HIGH_VOLATILITY:
        return base
    side = str(row.get("proposed_side") or r_get_side_intended(row) or "").upper()
    momentum = row.get("momentum_15")
    if not isinstance(momentum, (int, float)):
        return base
    aligned = (
        (side == SIDE_LONG and momentum > 0)
        or (side == SIDE_SHORT and momentum < 0)
    )
    if not aligned:
        return base
    # Production penalty in HIGH_VOL: -25.
    # Simulated: -5 → delta +20.
    return base + 20


def r_get_side_intended(row: dict[str, Any]) -> str | None:
    return row.get("side_intended") or row.get("proposed_side")


def _simulate(
    db: Any,
    hours: int,
    min_score: int,
    name: str,
    recompute_fn,
    rows: Iterable[dict[str, Any]] | None,
) -> SimulationReport:
    data, need = _fetch_rows(db, hours, rows)
    report = SimulationReport(hours=int(hours), name=name, need_data_reasons=list(need))
    if not data:
        return report
    baseline_long_pass = 0
    baseline_short_pass = 0
    new_long_pass = 0
    new_short_pass = 0
    samples_long = 0
    samples_short = 0
    for r in data:
        side = str(r.get("proposed_side") or r.get("side") or "").upper()
        base_score = r.get("confidence_score") or r.get("score") or 0
        if not isinstance(base_score, (int, float)):
            continue
        if side == SIDE_LONG:
            samples_long += 1
            if base_score >= min_score:
                baseline_long_pass += 1
            new_score = recompute_fn(r)
            if new_score >= min_score:
                new_long_pass += 1
        elif side == SIDE_SHORT:
            samples_short += 1
            if base_score >= min_score:
                baseline_short_pass += 1
            new_score = recompute_fn(r)
            if new_score >= min_score:
                new_short_pass += 1
    report.samples_long = samples_long
    report.samples_short = samples_short
    report.baseline_long_pass_pct = baseline_long_pass / max(samples_long, 1)
    report.baseline_short_pass_pct = baseline_short_pass / max(samples_short, 1)
    report.new_long_pass_pct = new_long_pass / max(samples_long, 1)
    report.new_short_pass_pct = new_short_pass / max(samples_short, 1)
    report.delta_long_pass = new_long_pass - baseline_long_pass
    report.delta_short_pass = new_short_pass - baseline_short_pass
    report.status = STATUS_OK if (samples_long or samples_short) else STATUS_NEED_DATA
    return report


def simulate_symmetric_regime(
    db: Any, *, hours: int = 168, min_score: int = MIN_SCORE_TO_TRADE_DEFAULT,
    rows: Iterable[dict[str, Any]] | None = None,
) -> SimulationReport:
    return _simulate(db, hours, min_score, "symmetric_regime",
                     _recompute_score_symmetric_regime, rows)


def simulate_atr_softening(
    db: Any, *, hours: int = 168, min_score: int = MIN_SCORE_TO_TRADE_DEFAULT,
    rows: Iterable[dict[str, Any]] | None = None,
) -> SimulationReport:
    return _simulate(db, hours, min_score, "atr_softening",
                     _recompute_score_atr_softened, rows)


def simulate_high_vol_directional(
    db: Any, *, hours: int = 168, min_score: int = MIN_SCORE_TO_TRADE_DEFAULT,
    rows: Iterable[dict[str, Any]] | None = None,
) -> SimulationReport:
    return _simulate(db, hours, min_score, "high_vol_directional",
                     _recompute_score_high_vol_directional, rows)
