"""Candidate Incubator V2 — setup-key-aware aggregation + promotion gates.

This is a SEPARATE module from the legacy `candidate_incubator.py` to avoid
touching its existing consumers. The new module groups by the full setup_key
(symbol + side + regime + score_bucket + timeframe + strategy + exit_policy + source)
and evaluates promotion gates as DIAGNOSTIC ONLY — never activates anything.

Promotion gates produce a recommendation in:
    REJECT / WATCH / SHADOW_CANDIDATE / PAPER_CANDIDATE_BLOCKED

PAPER_CANDIDATE_BLOCKED means "this passes the math but we still block it
because there is no human/automated decision to flip paper filter ON". The
filter flip is always manual.

NO RUNTIME HOOK. NO order placement. NO paper filter mutation.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .setup_key import build_setup_key, SetupKey
from .utils import safe_float, safe_int


FINAL_RECOMMENDATION = "NO LIVE"

REC_REJECT = "REJECT"
REC_WATCH = "WATCH"
REC_SHADOW_CANDIDATE = "SHADOW_CANDIDATE"
REC_PAPER_CANDIDATE_BLOCKED = "PAPER_CANDIDATE_BLOCKED"
REC_NOT_ACTIONABLE_RESEARCH_ONLY = "NOT_ACTIONABLE_RESEARCH_ONLY"
MARKET_PROBE_SOURCE = "market_probe"


@dataclass
class GatesConfig:
    min_samples_watch: int = 50
    min_samples_shadow: int = 200
    min_samples_paper: int = 800
    min_net_ev_pct: float = 0.0
    min_net_pf: float = 1.2
    min_cost_stress_022_net_ev: float = -0.05
    min_cost_stress_025_net_ev: float = -0.10
    max_max_drawdown_pct: float = 20.0
    min_monthly_positive_ratio: float = 0.5
    max_single_month_carry_ratio: float = 0.6  # if 1 month >60% of cumulative, fragile
    paper_filter_never_auto_activate: bool = True   # ALWAYS True; safety invariant


@dataclass
class SetupMetrics:
    setup_key: str
    symbol: str
    side: str
    regime: str
    score_bucket: str
    timeframe: str
    strategy: str
    exit_policy: str
    source: str
    sample_size: int
    tp_pct: float
    sl_pct: float
    time_pct: float
    gross_ev_pct: float
    net_ev_pct: float
    net_pf: float
    win_rate: float
    max_drawdown_pct: float
    monthly_pos: int
    monthly_neg: int
    monthly_positive_ratio: float
    single_month_carry_ratio: float
    cost_sensitivity_022_net_ev_pct: float
    cost_sensitivity_025_net_ev_pct: float
    avg_hold_bars: float
    frequency_per_day: float
    expected_move_to_cost_ratio: float
    recommendation: str
    rejection_reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IncubatorResult:
    config: GatesConfig
    setups: list[SetupMetrics] = field(default_factory=list)
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION

    def as_dict(self) -> dict[str, Any]:
        return {
            "config": asdict(self.config),
            "setups": [s.as_dict() for s in self.setups],
            "research_only": self.research_only,
            "paper_filter_enabled": self.paper_filter_enabled,
            "can_send_real_orders": self.can_send_real_orders,
            "final_recommendation": self.final_recommendation,
        }


def _resolve_setup_key(obs: dict[str, Any], label: dict[str, Any] | None) -> SetupKey:
    return build_setup_key(
        symbol=obs.get("symbol"),
        side=obs.get("side"),
        regime=obs.get("market_regime") or obs.get("regime"),
        score=obs.get("confidence_score"),
        timeframe=obs.get("timeframe") or "5m",
        strategy=obs.get("strategy_type") or obs.get("strategy"),
        exit_policy=obs.get("exit_policy") or "current_exit",
        source=obs.get("source") or "trade_signal",
    )


def _month_key(timestamp: Any) -> str:
    try:
        dt = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m")
    except Exception:
        return "unknown"


def _aggregate(
    pairs: list[tuple[dict[str, Any], dict[str, Any] | None]],
    *,
    cost_pct_default: float,
) -> tuple[dict[str, float], dict[str, int], dict[str, float]]:
    """Compute aggregates over a list of (observation, label) pairs."""
    pf_returns: list[float] = []
    months: dict[str, float] = defaultdict(float)
    months_count: dict[str, int] = defaultdict(int)
    cumulative_net: list[float] = []
    bars_to_outcome: list[int] = []
    tp_count = sl_count = tm_count = 0
    expected_moves: list[float] = []

    for obs, label in pairs:
        side = str(obs.get("side") or "").upper()
        entry = safe_float(obs.get("entry_price"))
        tp1 = safe_float(obs.get("take_profit_1"))
        if entry > 0 and tp1 > 0 and side in {"LONG", "SHORT"}:
            em_pct = ((tp1 - entry) / entry * 100.0) if side == "LONG" else ((entry - tp1) / entry * 100.0)
            expected_moves.append(em_pct)
        if not label:
            continue
        realized_pct = safe_float(label.get("realized_return_pct")) * 100.0
        net_pct = realized_pct - cost_pct_default
        pf_returns.append(net_pct)
        cumulative_net.append(net_pct)
        bars_to_outcome.append(safe_int(label.get("bars_to_outcome")))
        barrier = str(label.get("first_barrier_hit") or "").upper()
        if barrier in {"TP1", "TP2"}:
            tp_count += 1
        elif barrier == "SL":
            sl_count += 1
        elif barrier == "TIME":
            tm_count += 1
        month = _month_key(label.get("timestamp") or obs.get("timestamp"))
        months[month] += net_pct
        months_count[month] += 1

    n = max(len(pf_returns), 1)
    wins = [v for v in pf_returns if v > 0]
    losses = [v for v in pf_returns if v < 0]
    # drawdown on chronological cumulative
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for v in cumulative_net:
        equity += v
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)

    monthly_pos = sum(1 for v in months.values() if v > 0)
    monthly_neg = sum(1 for v in months.values() if v < 0)
    total_months = max(len(months), 1)
    monthly_pos_ratio = monthly_pos / total_months if months else 0.0

    cumulative = sum(pf_returns)
    if months and cumulative > 0:
        best_month = max(months.values())
        single_month_carry = best_month / cumulative if cumulative > 0 else 0.0
    else:
        single_month_carry = 0.0

    metrics = {
        "sample_size": float(len(pf_returns)),
        "tp_pct": tp_count / n,
        "sl_pct": sl_count / n,
        "time_pct": tm_count / n,
        "gross_ev_pct": sum(pf_returns + [cost_pct_default for _ in pf_returns]) / n,  # rough back-out
        "net_ev_pct": sum(pf_returns) / n,
        "net_pf": (sum(wins) / abs(sum(losses))) if losses else (999.0 if wins else 0.0),
        "win_rate": len(wins) / n,
        "max_drawdown_pct": abs(max_dd),
        "monthly_pos": float(monthly_pos),
        "monthly_neg": float(monthly_neg),
        "monthly_positive_ratio": monthly_pos_ratio,
        "single_month_carry_ratio": single_month_carry,
        "avg_hold_bars": sum(bars_to_outcome) / n,
        "expected_move_to_cost_ratio": (
            sum(expected_moves) / len(expected_moves) / cost_pct_default
            if expected_moves and cost_pct_default > 0
            else 0.0
        ),
    }
    counts = {
        "samples": len(pf_returns),
        "tp_count": tp_count,
        "sl_count": sl_count,
        "time_count": tm_count,
        "monthly_pos": monthly_pos,
        "monthly_neg": monthly_neg,
    }
    monthly_ev = {month: months[month] for month in months}
    return metrics, counts, monthly_ev


def _cost_stress(pairs: list[tuple[dict[str, Any], dict[str, Any] | None]], cost_pct: float) -> float:
    net_returns: list[float] = []
    for obs, label in pairs:
        if not label:
            continue
        realized = safe_float(label.get("realized_return_pct")) * 100.0
        net_returns.append(realized - cost_pct)
    return sum(net_returns) / max(len(net_returns), 1) if net_returns else 0.0


def _evaluate_gates(metrics: SetupMetrics, gates: GatesConfig) -> tuple[str, list[str]]:
    reasons: list[str] = []
    # Hard block: market_probe is research-only and can NEVER become an actionable candidate,
    # regardless of how good its metrics look. Bypasses every other gate by design.
    if str(metrics.source or "").lower() == MARKET_PROBE_SOURCE:
        return REC_NOT_ACTIONABLE_RESEARCH_ONLY, ["market_probe_research_only_never_actionable"]
    if metrics.sample_size < gates.min_samples_watch:
        return REC_REJECT, ["sample_size_below_watch"]
    if metrics.net_ev_pct <= gates.min_net_ev_pct:
        reasons.append("net_ev_not_positive")
    if metrics.net_pf < gates.min_net_pf:
        reasons.append("net_pf_below_floor")
    if metrics.cost_sensitivity_022_net_ev_pct < gates.min_cost_stress_022_net_ev:
        reasons.append("cost_stress_022_failed")
    if metrics.cost_sensitivity_025_net_ev_pct < gates.min_cost_stress_025_net_ev:
        reasons.append("cost_stress_025_failed")
    if metrics.max_drawdown_pct > gates.max_max_drawdown_pct:
        reasons.append("drawdown_above_cap")
    if metrics.monthly_positive_ratio < gates.min_monthly_positive_ratio:
        reasons.append("monthly_positive_ratio_below_floor")
    if metrics.single_month_carry_ratio > gates.max_single_month_carry_ratio:
        reasons.append("single_month_carries_too_much")

    if reasons:
        if metrics.sample_size >= gates.min_samples_paper:
            # Even with enough samples, if reasons exist, downgrade
            return REC_WATCH, reasons
        if metrics.sample_size >= gates.min_samples_shadow:
            return REC_WATCH, reasons
        return REC_WATCH if metrics.sample_size >= gates.min_samples_watch else REC_REJECT, reasons

    # All gates passed
    if metrics.sample_size >= gates.min_samples_paper:
        # Sample sufficient + clean — but we still BLOCK auto-activation.
        return REC_PAPER_CANDIDATE_BLOCKED, ["all_gates_passed_but_manual_review_required"]
    if metrics.sample_size >= gates.min_samples_shadow:
        return REC_SHADOW_CANDIDATE, ["passes_gates_but_sample_only_shadow_grade"]
    return REC_WATCH, ["passes_gates_but_sample_too_small_for_shadow"]


def build_incubator_result(
    observations: Iterable[dict[str, Any]],
    labels_by_observation: dict[int, dict[str, Any]] | None = None,
    *,
    gates: GatesConfig | None = None,
    cost_pct_default: float = 0.18,
    bar_minutes: int = 5,
) -> IncubatorResult:
    """Group observations+labels by setup_key, compute metrics, run gates."""
    gates = gates or GatesConfig()
    labels_by_observation = labels_by_observation or {}
    grouped: dict[str, list[tuple[dict[str, Any], dict[str, Any] | None]]] = defaultdict(list)
    keys_by_setup: dict[str, SetupKey] = {}
    for obs in observations:
        oid = safe_int(obs.get("id") or obs.get("observation_id"))
        label = labels_by_observation.get(oid)
        key = _resolve_setup_key(obs, label)
        sk = key.as_string()
        grouped[sk].append((obs, label))
        keys_by_setup.setdefault(sk, key)

    setups: list[SetupMetrics] = []
    for sk, pairs in grouped.items():
        metrics_d, counts, monthly_ev = _aggregate(pairs, cost_pct_default=cost_pct_default)
        cost_022 = _cost_stress(pairs, 0.22)
        cost_025 = _cost_stress(pairs, 0.25)
        # frequency per day
        timestamps = []
        for obs, label in pairs:
            ts = (label or {}).get("timestamp") or obs.get("timestamp")
            if ts:
                try:
                    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    timestamps.append(dt)
                except Exception:
                    pass
        if len(timestamps) >= 2:
            span = max((max(timestamps) - min(timestamps)).total_seconds(), 60.0)
            days = span / 86400.0
            freq = len(timestamps) / max(days, 1.0 / 24.0)
        else:
            freq = 0.0

        key = keys_by_setup[sk]
        partial = SetupMetrics(
            setup_key=sk,
            symbol=key.symbol,
            side=key.side,
            regime=key.regime,
            score_bucket=key.score_bucket,
            timeframe=key.timeframe,
            strategy=key.strategy,
            exit_policy=key.exit_policy,
            source=key.source,
            sample_size=int(metrics_d["sample_size"]),
            tp_pct=metrics_d["tp_pct"],
            sl_pct=metrics_d["sl_pct"],
            time_pct=metrics_d["time_pct"],
            gross_ev_pct=metrics_d["gross_ev_pct"],
            net_ev_pct=metrics_d["net_ev_pct"],
            net_pf=metrics_d["net_pf"],
            win_rate=metrics_d["win_rate"],
            max_drawdown_pct=metrics_d["max_drawdown_pct"],
            monthly_pos=int(metrics_d["monthly_pos"]),
            monthly_neg=int(metrics_d["monthly_neg"]),
            monthly_positive_ratio=metrics_d["monthly_positive_ratio"],
            single_month_carry_ratio=metrics_d["single_month_carry_ratio"],
            cost_sensitivity_022_net_ev_pct=cost_022,
            cost_sensitivity_025_net_ev_pct=cost_025,
            avg_hold_bars=metrics_d["avg_hold_bars"],
            frequency_per_day=freq,
            expected_move_to_cost_ratio=metrics_d["expected_move_to_cost_ratio"],
            recommendation=REC_REJECT,
            rejection_reasons=[],
        )
        rec, reasons = _evaluate_gates(partial, gates)
        partial.recommendation = rec
        partial.rejection_reasons = reasons
        setups.append(partial)

    setups.sort(key=lambda s: (
        s.recommendation != REC_PAPER_CANDIDATE_BLOCKED,
        s.recommendation != REC_SHADOW_CANDIDATE,
        s.recommendation == REC_NOT_ACTIONABLE_RESEARCH_ONLY,
        -s.sample_size,
    ))
    return IncubatorResult(
        config=gates,
        setups=setups,
        research_only=True,
        paper_filter_enabled=False,
        can_send_real_orders=False,
        final_recommendation=FINAL_RECOMMENDATION,
    )


def render_result_text(result: IncubatorResult, *, top_n: int = 25) -> str:
    lines = ["CANDIDATE INCUBATOR V2 START"]
    lines.append(f"total_setups: {len(result.setups)}")
    lines.append("recommendation_counts:")
    counts: dict[str, int] = {}
    for s in result.setups:
        counts[s.recommendation] = counts.get(s.recommendation, 0) + 1
    for rec, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"- {rec}: {n}")
    lines.append("top_setups:")
    for s in result.setups[:top_n]:
        lines.append(
            f"- {s.setup_key} | samples={s.sample_size} net_ev={s.net_ev_pct:.4f}% "
            f"PF={s.net_pf:.2f} dd={s.max_drawdown_pct:.2f} months+={s.monthly_pos} "
            f"months-={s.monthly_neg} carry={s.single_month_carry_ratio:.2f} "
            f"cost022={s.cost_sensitivity_022_net_ev_pct:.4f}% "
            f"freq/day={s.frequency_per_day:.2f} "
            f"rec={s.recommendation} reasons={','.join(s.rejection_reasons) if s.rejection_reasons else 'none'}"
        )
    lines.append("paper_filter_enabled: false")
    lines.append("can_send_real_orders: false")
    lines.append("research_only: true")
    lines.append("paper_filter_never_auto_activate: true")
    lines.append(f"final_recommendation: {result.final_recommendation}")
    lines.append("CANDIDATE INCUBATOR V2 END")
    return "\n".join(lines)
