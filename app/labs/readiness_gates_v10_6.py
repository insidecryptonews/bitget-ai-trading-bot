"""ResearchOps V10.6 — Readiness Gates & Contracts (research-only).

Pure, conservative readiness evaluators for: edge hunter (F), walk-forward/
OOS/anti-overfit (G), meta-model (H), forecast lab (I), paper (J), live (K)
and the future micro-live risk framework (L). Every evaluator is fail-closed:
with the current reality (no verified data, no demonstrated edge) they all
report NOT_READY with explicit blockers. NOTHING here flips paper_ready or
live_ready true, ever. No network, no DB, no .env, no runtime mutation.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE

# Thresholds (documented, conservative).
MIN_CLEAN_DAYS = 180
STRONG_CLEAN_DAYS = 365
MIN_SAMPLES = 150
MIN_NET_PF = 1.2
MAX_TIME_DEATH = 0.80
META_MIN_SAMPLES = 300
META_MIN_POS = 50
META_MIN_NEG = 50


def _fin(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _base(extra: dict[str, Any]) -> dict[str, Any]:
    return {**extra, "research_only": True, "paper_ready": False,
            "live_ready": False, "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


# --- F. Edge Hunter readiness + candidate incubator contract ----------------

def edge_hunter_readiness(evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    """What is missing to hunt for serious edge. Conservative gates."""
    e = dict(evidence or {})
    blockers: list[str] = []
    clean = _fin(e.get("clean_days"))
    if clean is None or clean < MIN_CLEAN_DAYS:
        blockers.append(f"need_clean_days>={MIN_CLEAN_DAYS}")
    samples = _fin(e.get("samples"))
    if samples is None or samples < MIN_SAMPLES:
        blockers.append(f"need_samples>={MIN_SAMPLES}")
    net_ev = _fin(e.get("net_ev"))
    if net_ev is None or net_ev <= 0:
        blockers.append("need_net_ev_positive")
    net_pf = _fin(e.get("net_pf"))
    if net_pf is None or net_pf < MIN_NET_PF:
        blockers.append(f"need_net_pf>={MIN_NET_PF}")
    time_death = _fin(e.get("time_death"))
    if time_death is None or time_death >= MAX_TIME_DEATH:
        blockers.append("need_time_death<0.80")
    if not e.get("fees_x2_stress_pass"):
        blockers.append("need_fees_x2_stress_pass")
    if not e.get("slippage_stress_pass"):
        blockers.append("need_slippage_stress_pass")
    if not e.get("oos_pass"):
        blockers.append("need_oos_pass")
    if not e.get("walk_forward_stable"):
        blockers.append("need_walk_forward_stable")
    if not e.get("anti_overfit_pass"):
        blockers.append("need_anti_overfit_pass")
    if e.get("missing_oi_clustered"):
        blockers.append("oi_missing_clustered")
    status = "EDGE_RESEARCH_READY" if not blockers else "EDGE_NOT_READY"
    return _base({
        "status": status,
        "candidate_incubator_contract": {
            "required_fields": ["candidate_id", "strategy_type", "symbol", "side",
                                "regime", "feature_buckets", "sample_count",
                                "net_ev", "net_pf", "max_drawdown", "win_rate",
                                "tp_sl_time_distribution", "cost_stress",
                                "oos_status", "wf_status", "blockers"],
            "promotion_rule": "no promotion if ANY gate fails; never on a single "
                              "pretty PF; strong candidate_id required",
            "thresholds": {"min_samples": MIN_SAMPLES, "min_net_pf": MIN_NET_PF,
                           "max_time_death": MAX_TIME_DEATH,
                           "min_clean_days": MIN_CLEAN_DAYS},
        },
        "blockers": blockers,
    })


# --- G. Walk-forward / OOS / anti-overfit contract --------------------------

def walk_forward_readiness(evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    e = dict(evidence or {})
    blockers: list[str] = []
    if not e.get("dataset_validated"):
        blockers.append("need_validated_dataset")
    labels = _fin(e.get("labels"))
    if labels is None or labels < MIN_SAMPLES:
        blockers.append(f"need_min_labels>={MIN_SAMPLES}")
    return _base({
        "status": "WALK_FORWARD_NOT_READY" if blockers else "WALK_FORWARD_CONTRACT_OK",
        "design": {
            "train_test_split": "rolling, chronological, no shuffle",
            "rolling_windows": "monthly + rolling N-window",
            "no_leakage": "features use trailing windows only; embargo/purge "
                          "around test boundaries",
            "minimum_labels": MIN_SAMPLES,
            "minimum_positives_negatives": "both > 0 and balanced enough",
            "stability_matrix": "PF/net_EV per window must not collapse OOS",
        },
        "reject_if": [
            "train_pf_high_but_test_pf_poor",
            "too_few_samples",
            "edge_concentrated_in_one_tiny_period",
            "works_only_on_one_symbol_by_accident",
            "sensitive_to_tiny_threshold_changes",
        ],
        "blockers": blockers,
    })


# --- H. Meta-model readiness (no activation) --------------------------------

def meta_model_readiness(stats: dict[str, Any] | None = None) -> dict[str, Any]:
    s = dict(stats or {})
    blockers: list[str] = []
    samples = _fin(s.get("samples"))
    pos = _fin(s.get("positives"))
    neg = _fin(s.get("negatives"))
    if samples is None or samples < META_MIN_SAMPLES:
        blockers.append(f"need_samples>={META_MIN_SAMPLES}")
    if pos is None or pos < META_MIN_POS:
        blockers.append(f"need_positives>={META_MIN_POS}")
    if neg is None or neg < META_MIN_NEG:
        blockers.append(f"need_negatives>={META_MIN_NEG}")
    if not s.get("leakage_checked"):
        blockers.append("need_leakage_checks")
    if not s.get("calibrated"):
        blockers.append("need_calibration")
    if not s.get("oos_improvement"):
        blockers.append("need_oos_improvement")
    if not s.get("net_ev_improvement_after_filter"):
        blockers.append("need_net_ev_improvement (fewer trades, better net EV)")
    return _base({
        "status": "META_MODEL_NOT_READY" if blockers else "META_MODEL_RESEARCH_READY",
        "activation": {"ENABLE_META_MODEL": False, "runtime_filter": False,
                       "env_change": False},
        "thresholds": {"min_samples": META_MIN_SAMPLES, "min_positives": META_MIN_POS,
                       "min_negatives": META_MIN_NEG},
        "blockers": blockers,
    })


# --- I. Forecast lab readiness (TimesFM etc. — future, no deps) -------------

def forecast_lab_readiness() -> dict[str, Any]:
    return _base({
        "status": "FORECAST_LAB_FUTURE_OFFLINE_ONLY",
        "policy": ["offline/shadow only", "never a direct signal",
                   "no new heavy deps (no torch/jax/tensorflow/timesfm in runtime)"],
        "possible_uses": ["expected_range", "volatility", "volume",
                          "oi_forecast", "funding_forecast", "uncertainty_gate",
                          "no_trade_gate"],
        "must_beat_baselines": ["ATR", "EWMA", "naive", "seasonal_naive"],
        "gate": "only earns a place if it beats baselines OOS AND improves net "
                "EV after realistic costs",
        "implemented": False,
    })


# --- J. Paper readiness gate ------------------------------------------------

def paper_readiness(state: dict[str, Any] | None = None) -> dict[str, Any]:
    s = dict(state or {})
    blockers: list[str] = []
    clean = _fin(s.get("clean_days"))
    if clean is None or clean < MIN_CLEAN_DAYS:
        blockers.append("need_180_365d_clean")
    if not s.get("content_validation_pass"):
        blockers.append("need_content_validation")
    if not s.get("backtester_ready"):
        blockers.append("need_backtester_readiness")
    if not s.get("oos_pass"):
        blockers.append("need_oos")
    if not s.get("walk_forward_pass"):
        blockers.append("need_walk_forward")
    if not s.get("has_edge_candidates"):
        blockers.append("need_edge_candidates")
    if s.get("missing_oi_clustered"):
        blockers.append("oi_missing_clustered")
    net_ev = _fin(s.get("net_ev"))
    if net_ev is None or net_ev <= 0:
        blockers.append("need_net_ev_positive")
    if s.get("paper_policy_disabled", True):
        blockers.append("paper_policy_disabled")
    # Hard invariant: paper is NEVER ready from this gate in the current phase.
    return _base({
        "status": "PAPER_NOT_READY",
        "recommendation": "PAPER_NOT_READY",
        "blockers": blockers or ["phase_gate:paper_not_enabled"],
    })


# --- K. Live readiness audit (brutally conservative) ------------------------

def live_readiness(state: dict[str, Any] | None = None) -> dict[str, Any]:
    s = dict(state or {})
    blockers: list[str] = []
    requirements = [
        ("paper_profitable_sustained", "need_sustained_profitable_paper"),
        ("min_paper_days_met", "need_minimum_paper_days"),
        ("min_paper_trades_met", "need_minimum_paper_trades"),
        ("paper_net_ev_positive", "need_paper_net_ev_positive"),
        ("drawdown_within_limits", "need_drawdown_within_limits"),
        ("no_duplicate_worker", "need_single_worker_lock"),
        ("kill_switches_present", "need_kill_switches"),
        ("manual_approval", "need_manual_human_approval"),
        ("micro_live_risk_rules", "need_micro_live_risk_rules"),
        ("exchange_key_permissions_audited", "need_exchange_key_permission_audit"),
        ("rollback_plan", "need_rollback_plan"),
        ("monitoring_alerts", "need_monitoring_and_alerts"),
    ]
    for key, blocker in requirements:
        if not s.get(key):
            blockers.append(blocker)
    # SAFE_PAPER_ONLY is not enough; LIVE_AUDIT_READY is never auto-true here.
    return _base({
        "status": "LIVE_NOT_READY",
        "live_audit_ready": False,
        "can_send_real_orders": False,
        "security_status_note": "SAFE_PAPER_ONLY is necessary but NOT sufficient",
        "blockers": blockers,
    })


# --- L. Risk framework contract for future micro-live (read-only) -----------

def risk_framework_contract() -> dict[str, Any]:
    return _base({
        "status": "RISK_FRAMEWORK_CONTRACT_ONLY_NOT_ACTIVE",
        "limits": {
            "max_daily_loss": "configurable, conservative; not active",
            "max_weekly_loss": "configurable; not active",
            "max_position_risk": "small fixed %; not active",
            "max_leverage": "low cap; not active",
            "max_open_positions": "low cap; not active",
            "stop_after_n_losses": "circuit breaker; not active",
        },
        "no_trade_conditions": [
            "data_stale", "duplicate_worker", "spread_or_slippage_high",
            "funding_extreme", "oi_missing", "circuit_breaker_open",
        ],
        "emergency_halt": "STOP_REQUESTED loop stop + RiskManager preflight block "
                          "(already present in runtime; unchanged)",
        "activation": "requires explicit human approval + live readiness pass; "
                      "no operative risk_manager change in this phase",
    })


@dataclass
class _Unused:  # keep dataclass import used / future structured results
    placeholder: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
