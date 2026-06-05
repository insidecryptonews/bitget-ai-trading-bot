"""V8.2.6 — Candidate Rule Miner (research-only).

Mines candidate trading rules from the V8.2.5 deduplicated counterfactual
dataset using **ex-ante features only**.

Forbidden as features (would be label leakage):
- ``training_label``
- ``first_barrier_hit``
- ``baseline_net_pnl_est``, ``baseline_gross_pnl``
- any ``ret_*_pct`` / ``mfe_pct`` / ``mae_pct``

Allowed as features (known at the time the signal is generated):
- ``symbol``, ``side``, ``regime``, ``strategy``, ``score_bucket``,
  ``candidate_selected``, ``risk_approved``.

Hard contract: research-only. No production rules. No paper filter.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE, STATUS_NEED_DATA, STATUS_OK
from .counterfactual_dedup_audit import _is_evaluable, dedup_rows
from .counterfactual_training_dataset import build_dataset


# Ex-ante grouping dimensions (label-leakage safe).
EX_ANTE_FEATURES: tuple[str, ...] = (
    "symbol", "side", "regime", "strategy", "score_bucket",
    "candidate_selected", "risk_approved",
)

# Hard-list of fields that MUST NOT be used as features (label leakage).
EX_POST_LABELS: frozenset[str] = frozenset({
    "training_label", "first_barrier_hit",
    "baseline_net_pnl_est", "baseline_gross_pnl",
    "ret_15m_pct", "ret_30m_pct", "ret_1h_pct", "ret_4h_pct", "ret_24h_pct",
    "mfe_pct", "mae_pct",
    "baseline_result", "trailing_result", "campaign_result",
    "would_have_worked_baseline", "would_have_worked_trailing",
    "would_have_worked_campaign",
})


# Default gates.
MIN_SAMPLES_PREFERRED = 30
MIN_SAMPLES_WEAK = 20
MIN_PF = 1.2
MIN_WINRATE = 0.55
MAX_TIMESTAMP_CLUSTER_RATIO = 0.30
COST_NORMAL_PCT = 0.18
COST_REALISTIC_PCT = 0.25
COST_STRESS_PCT = 0.35
COST_STRESS_FLIP_THRESHOLD_PCT = -0.30


STATUS_REJECT = "REJECT"
STATUS_WATCH_ONLY = "WATCH_ONLY"
STATUS_CANDIDATE_RESEARCH = "CANDIDATE_RESEARCH"
STATUS_PAPER_CANDIDATE_ONLY_IF_WF_PASS = "PAPER_CANDIDATE_ONLY_IF_WF_PASS"


@dataclass
class CandidateRule:
    rule_id: str
    features: dict[str, Any]
    samples: int
    winrate: float
    net_ev_avg_pct: float
    pf: float
    max_loss_pct: float
    drawdown_proxy_pct: float
    cost_normal_net_ev_pct: float
    cost_realistic_net_ev_pct: float
    cost_stress_net_ev_pct: float
    timestamp_cluster_max_ratio: float
    rule_status: str
    rule_reason: str
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CandidateRuleMinerReport:
    hours: int
    generated_at: str
    short_verdict: str = ""
    short_excluded: bool = False
    total_rules: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    candidate_rules: list[dict[str, Any]] = field(default_factory=list)
    watch_only_rules: list[dict[str, Any]] = field(default_factory=list)
    rejected_rules: list[dict[str, Any]] = field(default_factory=list)
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _rule_id(features: dict[str, Any]) -> str:
    parts = [f"{k}={features.get(k)}" for k in EX_ANTE_FEATURES]
    return "|".join(parts)


def _timestamp_cluster_ratio(rows: list[dict[str, Any]]) -> float:
    """Return the largest fraction of rows sharing the same 1-hour bucket."""
    if not rows:
        return 0.0
    buckets: dict[str, int] = {}
    for r in rows:
        ts = str(r.get("timestamp", ""))
        bucket = ts[:13] if len(ts) >= 13 else ts
        buckets[bucket] = buckets.get(bucket, 0) + 1
    return max(buckets.values()) / len(rows)


def _group_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Outcome metrics for a group. Uses ``baseline_net_pnl_est`` /
    ``baseline_gross_pnl`` as the **outcome** (not as a feature).
    """
    nets: list[float] = []
    grosses: list[float] = []
    for r in rows:
        try:
            nets.append(float(r.get("baseline_net_pnl_est") or 0))
        except Exception:
            continue
        try:
            grosses.append(float(r.get("baseline_gross_pnl") or 0))
        except Exception:
            grosses.append(0.0)
    if not nets:
        return {
            "samples": 0, "winrate": 0.0, "net_ev_avg_pct": 0.0, "pf": 0.0,
            "max_loss_pct": 0.0, "drawdown_proxy_pct": 0.0,
            "cost_normal_net_ev_pct": 0.0,
            "cost_realistic_net_ev_pct": 0.0,
            "cost_stress_net_ev_pct": 0.0,
        }
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    loss_sum = abs(sum(losses))
    pf = (sum(wins) / loss_sum) if loss_sum > 0 else 0.0
    max_loss = min(nets)
    # Drawdown proxy: cumulative min over the sequence.
    cum = 0.0
    peak = 0.0
    drawdown = 0.0
    for n in nets:
        cum += n
        peak = max(peak, cum)
        drawdown = min(drawdown, cum - peak)
    cost_normal = (sum(grosses) - len(nets) * COST_NORMAL_PCT) / len(nets)
    cost_realistic = (sum(grosses) - len(nets) * COST_REALISTIC_PCT) / len(nets)
    cost_stress = (sum(grosses) - len(nets) * COST_STRESS_PCT) / len(nets)
    return {
        "samples": len(nets),
        "winrate": len(wins) / len(nets),
        "net_ev_avg_pct": sum(nets) / len(nets),
        "pf": pf,
        "max_loss_pct": max_loss,
        "drawdown_proxy_pct": drawdown,
        "cost_normal_net_ev_pct": cost_normal,
        "cost_realistic_net_ev_pct": cost_realistic,
        "cost_stress_net_ev_pct": cost_stress,
    }


def _classify_rule(
    metrics: dict[str, Any],
    cluster_ratio: float,
    *,
    score_calibration_ok: bool,
) -> tuple[str, str]:
    """Apply the V8.2.6 gates and return ``(status, reason)``."""
    samples = int(metrics.get("samples") or 0)
    if samples < MIN_SAMPLES_WEAK:
        return STATUS_REJECT, f"samples={samples}_below_min_{MIN_SAMPLES_WEAK}"
    pf = float(metrics.get("pf") or 0)
    winrate = float(metrics.get("winrate") or 0)
    cost_realistic = float(metrics.get("cost_realistic_net_ev_pct") or 0)
    cost_stress = float(metrics.get("cost_stress_net_ev_pct") or 0)
    if cost_realistic <= 0:
        return STATUS_REJECT, "cost_realistic_net_ev_not_positive"
    if pf < MIN_PF:
        return STATUS_REJECT, f"pf={pf:.2f}_below_min_{MIN_PF}"
    if winrate < MIN_WINRATE:
        return STATUS_REJECT, f"winrate={winrate:.2f}_below_min_{MIN_WINRATE}"
    if cluster_ratio > MAX_TIMESTAMP_CLUSTER_RATIO:
        return STATUS_WATCH_ONLY, f"timestamp_cluster={cluster_ratio:.2f}_above_max_{MAX_TIMESTAMP_CLUSTER_RATIO}"
    if cost_stress < COST_STRESS_FLIP_THRESHOLD_PCT:
        return STATUS_REJECT, f"cost_stress_net_ev={cost_stress:.4f}_flips_strongly_negative"
    if cost_stress < 0:
        return STATUS_WATCH_ONLY, "fragile_under_stress_cost"
    if not score_calibration_ok:
        return STATUS_CANDIDATE_RESEARCH, "score_calibration_unreliable_research_only"
    if samples < MIN_SAMPLES_PREFERRED:
        return STATUS_CANDIDATE_RESEARCH, f"samples={samples}_below_preferred_{MIN_SAMPLES_PREFERRED}"
    return STATUS_PAPER_CANDIDATE_ONLY_IF_WF_PASS, "all_gates_pass_pending_walkforward"


def _should_skip_short(side: str, short_verdict: str) -> bool:
    """SHORT is excluded until the short barrier debug verdict is safe."""
    if str(side).upper() != "SHORT":
        return False
    safe_verdicts = {"SHORT_LABELS_TRUSTED", "SHORT_SAFE_TO_USE_FOR_RESEARCH", ""}
    return short_verdict not in safe_verdicts


def mine_candidate_rules(
    db: Any = None,
    *,
    hours: int = 168,
    limit: int = 50000,
    rows: Iterable[dict[str, Any]] | None = None,
    short_verdict: str = "",
    score_calibration_ok: bool = False,
    grouping_features: Iterable[str] = EX_ANTE_FEATURES,
) -> CandidateRuleMinerReport:
    """Mine candidate rules. Returns rejected/watch/candidate buckets.

    ``rows`` allows test injection. ``short_verdict`` from
    ``short_barrier_debug_v8_2_6`` controls SHORT inclusion.
    """
    features = tuple(grouping_features)
    # Hard guard: refuse forbidden feature names if a caller tries to override.
    for f in features:
        if f in EX_POST_LABELS:
            raise ValueError(
                f"forbidden feature {f!r}: would leak the label into the rule miner"
            )
    report = CandidateRuleMinerReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
        short_verdict=short_verdict,
        short_excluded=(short_verdict not in {"SHORT_LABELS_TRUSTED",
                                              "SHORT_SAFE_TO_USE_FOR_RESEARCH", ""}),
    )
    if rows is None:
        dataset, _ = build_dataset(db, hours=int(hours), limit=int(limit))
    else:
        dataset = list(rows)
    evaluable = [r for r in dataset if _is_evaluable(r)]
    evaluable = dedup_rows(evaluable)
    if not evaluable:
        return report
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for r in evaluable:
        if _should_skip_short(r.get("side"), short_verdict):
            continue
        key = tuple(r.get(f) for f in features)
        groups.setdefault(key, []).append(r)
    rules: list[CandidateRule] = []
    for key, rs in groups.items():
        feat_map = {f: v for f, v in zip(features, key)}
        metrics = _group_metrics(rs)
        cluster = _timestamp_cluster_ratio(rs)
        status, reason = _classify_rule(
            metrics, cluster, score_calibration_ok=score_calibration_ok,
        )
        rule = CandidateRule(
            rule_id=_rule_id(feat_map),
            features=feat_map,
            samples=int(metrics["samples"]),
            winrate=float(metrics["winrate"]),
            net_ev_avg_pct=float(metrics["net_ev_avg_pct"]),
            pf=float(metrics["pf"]),
            max_loss_pct=float(metrics["max_loss_pct"]),
            drawdown_proxy_pct=float(metrics["drawdown_proxy_pct"]),
            cost_normal_net_ev_pct=float(metrics["cost_normal_net_ev_pct"]),
            cost_realistic_net_ev_pct=float(metrics["cost_realistic_net_ev_pct"]),
            cost_stress_net_ev_pct=float(metrics["cost_stress_net_ev_pct"]),
            timestamp_cluster_max_ratio=float(cluster),
            rule_status=status,
            rule_reason=reason,
        )
        rules.append(rule)
    report.total_rules = len(rules)
    for r in rules:
        report.by_status[r.rule_status] = report.by_status.get(r.rule_status, 0) + 1
    rules.sort(key=lambda r: r.net_ev_avg_pct, reverse=True)
    report.candidate_rules = [
        r.as_dict() for r in rules
        if r.rule_status in {STATUS_CANDIDATE_RESEARCH, STATUS_PAPER_CANDIDATE_ONLY_IF_WF_PASS}
    ]
    report.watch_only_rules = [
        r.as_dict() for r in rules if r.rule_status == STATUS_WATCH_ONLY
    ]
    report.rejected_rules = [
        r.as_dict() for r in rules if r.rule_status == STATUS_REJECT
    ][:100]
    report.status = STATUS_OK
    return report
