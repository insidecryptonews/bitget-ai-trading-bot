"""ResearchOps V10.4 — Edge Hunter Contract + Gates (research-only).

Defines (but does NOT operate) the future V10.5 Edge Hunter: how a candidate
is defined and the anti-overfit gates it must pass. Includes a conservative
gate evaluator whose ceiling is ``SHADOW_ONLY`` — it can NEVER return
``live_ready`` or ``paper_ready`` true. No network, no DB, no execution.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE

# Gate verdicts (ceiling = SHADOW_ONLY).
GATE_NEED_LONG_HISTORY = "NEED_LONG_HISTORY"
GATE_NEED_MORE_SAMPLES = "NEED_MORE_SAMPLES"
GATE_REJECT = "REJECT"
GATE_WATCH = "WATCH_ONLY"
GATE_SHADOW = "SHADOW_ONLY"
GATE_OI_BLOCKED = "MISSING_OI_RISK_BLOCK"

MIN_SAMPLES = 150
MIN_NET_PF = 1.30
MIN_HISTORY_DAYS = 180
MAX_ONE_TRADE_DOMINANCE = 0.25
MAX_TIME_DEATH = 0.55


def build_edge_hunter_contract() -> dict[str, Any]:
    """The V10.5 Edge Hunter contract. Pure data; not operational."""
    return {
        "candidate_definition": ["symbol", "side", "regime", "score_bucket",
                                 "exit_policy", "timeframe"],
        "minimum_samples": MIN_SAMPLES,
        "metrics_required": ["net_EV_after_costs", "net_PF", "gross_PF",
                             "time_death_rate", "tp_sl_time_distribution",
                             "cost_sensitivity_x1_x2_x3", "max_drawdown",
                             "exposure_time", "return_by_month", "return_by_regime",
                             "worst_streak", "top1_top5_trade_dependency"],
        "validation": ["walk_forward_monthly", "walk_forward_rolling",
                       "train_test_split", "regime_split", "oos_validation",
                       "anti_overfit_score", "stability_matrix"],
        "anti_lookahead": ["entry_next_bar_after_signal",
                           "same_bar_sl_tp_worst_case",
                           "trailing_window_features_only",
                           "no_labels_in_decision"],
        "cost_model": ["maker_taker_fees", "slippage_x1_x2_x3", "funding", "spread_proxy"],
        "reject_reasons": ["fewer_than_minimum_samples", "pf_positive_only_pre_cost",
                           "one_trade_dominance", "one_week_or_month_dominance",
                           "oos_fail", "cost_x2_fail", "drawdown_too_high",
                           "same_bar_ambiguity_too_frequent",
                           "missing_oi_dependency_unresolved",
                           "edge_disappears_outside_eth"],
        "promotion_ladder": ["RESEARCH_ONLY", "BACKTEST_CANDIDATE",
                             "WALK_FORWARD_CANDIDATE", "SHADOW_ONLY",
                             "PAPER_ELIGIBLE_FUTURE"],
        "output_ceiling": GATE_SHADOW,
        "never": ["live_readiness", "paper_filter_enable", "auto_promotion",
                  "operate_without_180d_clean_data_and_valid_manifest"],
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }


@dataclass
class EdgeHunterGateResult:
    candidate: str = ""
    samples: int = 0
    clean_days: float = 0.0
    net_ev: float = 0.0
    net_pf: float = 0.0
    gross_pf: float = 0.0
    time_death_rate: float = 0.0
    one_trade_dominance: float = 0.0
    cost_x2_pass: bool = False
    oos_pass: bool = False
    uses_oi: bool = False
    missing_oi_blocked: bool = False
    verdict: str = GATE_NEED_LONG_HISTORY
    blocker: str = ""
    research_only: bool = True
    paper_ready: bool = False
    live_ready: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_edge_hunter_gate(
    *,
    candidate: str = "",
    clean_days: float = 0.0,
    samples: int = 0,
    net_ev: float = 0.0,
    net_pf: float = 0.0,
    gross_pf: float = 0.0,
    time_death_rate: float = 0.0,
    one_trade_dominance: float = 0.0,
    cost_x2_pass: bool = False,
    oos_pass: bool = False,
    uses_oi: bool = False,
    missing_oi_blocked: bool = False,
) -> EdgeHunterGateResult:
    """Conservative gate. Ceiling SHADOW_ONLY; never paper/live ready."""
    r = EdgeHunterGateResult(
        candidate=candidate, samples=samples, clean_days=clean_days, net_ev=net_ev,
        net_pf=net_pf, gross_pf=gross_pf, time_death_rate=time_death_rate,
        one_trade_dominance=one_trade_dominance, cost_x2_pass=cost_x2_pass,
        oos_pass=oos_pass, uses_oi=uses_oi, missing_oi_blocked=missing_oi_blocked)

    if clean_days < MIN_HISTORY_DAYS:
        r.verdict, r.blocker = GATE_NEED_LONG_HISTORY, f"clean_days<{MIN_HISTORY_DAYS}"
    elif uses_oi and missing_oi_blocked:
        r.verdict, r.blocker = GATE_OI_BLOCKED, "oi_strategy_with_blocked_oi"
    elif samples < MIN_SAMPLES:
        r.verdict, r.blocker = GATE_NEED_MORE_SAMPLES, f"samples<{MIN_SAMPLES}"
    elif net_ev <= 0 or net_pf < MIN_NET_PF:
        r.verdict, r.blocker = GATE_REJECT, "net_ev_or_pf_insufficient"
    elif not cost_x2_pass:
        r.verdict, r.blocker = GATE_REJECT, "cost_x2_fail"
    elif not oos_pass:
        r.verdict, r.blocker = GATE_REJECT, "oos_fail"
    elif one_trade_dominance > MAX_ONE_TRADE_DOMINANCE or time_death_rate > MAX_TIME_DEATH:
        r.verdict, r.blocker = GATE_WATCH, "dominance_or_time_death_risk"
    else:
        r.verdict, r.blocker = GATE_SHADOW, "NONE"

    # Hard invariant — never operational from here.
    r.paper_ready = False
    r.live_ready = False
    return r
