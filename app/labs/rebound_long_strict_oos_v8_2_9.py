"""V8.2.9 — Rebound LONG Strict OOS Validation (research-only).

Runs a strict 60 / 20 / 20 temporal split over deduplicated LONG
rebound candidates, mines rules with ex-ante features only on train,
prefilters on validation, evaluates once on test. Applies dataset-level
guards: ``duplicate_ratio_after``, ``cluster_ratio``,
``symbol_concentration``, three-level cost stress.

The score is NEVER used as a positive gate when it is anti-calibrated.
Exit monetization diagnostics may be attached to the report but are
not used as detection inputs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE, STATUS_NEED_DATA, STATUS_OK


# Ex-ante features available to the rule miner. ``symbol`` is included
# by default so the per-rule symbol_concentration check is bypassed
# (a symbol-specific rule is symbol-concentrated by construction).
EX_ANTE_FEATURES: tuple[str, ...] = (
    "symbol", "regime_before", "regime_now",
    "volatility_bucket", "trend_recovering_prefix",
)

FORBIDDEN_FEATURES: frozenset[str] = frozenset({
    "ret_15m_pct", "ret_30m_pct", "ret_1h_pct", "ret_4h_pct", "ret_24h_pct",
    "mfe_pct", "mae_pct",
    "first_barrier_hit", "tp_before_sl", "sl_before_tp",
    "baseline_result", "baseline_gross_pnl", "baseline_net_pnl_est",
    "trailing_result", "trailing_net_pnl_est",
    "campaign_result", "campaign_net_pnl_est",
    "training_label",
    # The score is anti-calibrated in production; refuse it as a
    # grouping feature so the rule miner can never construct a gate
    # that depends on it.
    "score", "score_bucket",
})

TRAIN_FRACTION = 0.60
VAL_FRACTION = 0.20

MIN_TRAIN_SAMPLES = 30
MIN_VAL_SAMPLES = 15
MIN_TEST_SAMPLES = 15

COST_NORMAL_PCT = 0.18
COST_REALISTIC_PCT = 0.25
COST_STRESS_PCT = 0.35

MIN_TEST_PF = 1.15
MIN_TEST_WINRATE = 0.55
MAX_DUPLICATE_RATIO_AFTER = 0.30
MAX_CLUSTER_RATIO = 0.30
MAX_SINGLE_SYMBOL_RATIO = 0.50

STATUS_REJECT = "REJECT"
STATUS_NEED_MORE_DATA = "NEED_MORE_DATA"
STATUS_WATCH_ONLY = "WATCH_ONLY"
STATUS_RESEARCH_CANDIDATE = "RESEARCH_CANDIDATE"
STATUS_PAPER_SANDBOX_CANDIDATE = "PAPER_SANDBOX_CANDIDATE"
STATUS_SINGLE_SYMBOL_RESEARCH_ONLY = "SINGLE_SYMBOL_RESEARCH_ONLY"

# V8.2.9.1 — Profit factor sentinel used when there are no losses.
# CSV / JSON / ZIP friendly (avoids ``float("inf")`` serialisation issues).
PF_SENTINEL_NO_LOSSES = 999.0


def _profit_factor(gross_profit: float, gross_loss: float) -> float:
    """Canonical profit-factor calculation.

    - ``gross_loss == 0`` and ``gross_profit > 0`` → ``PF_SENTINEL_NO_LOSSES``.
    - ``gross_loss == 0`` and ``gross_profit <= 0`` → ``0.0``.
    - ``gross_loss != 0`` → ``gross_profit / abs(gross_loss)``.

    ``gross_loss`` may be passed as a negative sum (e.g. ``sum(losses)``);
    only its magnitude is used.
    """
    loss_abs = abs(float(gross_loss))
    if loss_abs == 0.0:
        return PF_SENTINEL_NO_LOSSES if float(gross_profit) > 0 else 0.0
    return float(gross_profit) / loss_abs


@dataclass
class StrictOosRebound:
    rule_id: str
    features: dict[str, Any]
    train_samples: int
    validation_samples: int
    test_samples: int
    train_net_ev_pct: float
    validation_net_ev_pct: float
    test_net_ev_pct: float
    test_net_ev_after_cost_realistic_pct: float
    test_net_ev_after_cost_stress_pct: float
    test_pf: float
    test_winrate: float
    test_cluster_ratio: float
    test_symbol_concentration: float
    duplicate_ratio_after: float
    final_status: str
    reject_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StrictOosReboundReport:
    hours: int
    generated_at: str
    score_anti_calibrated_input: bool = True
    score_used_as_gate: bool = False
    candidates_total: int = 0
    duplicate_ratio_after: float = 0.0
    by_final_status: dict[str, int] = field(default_factory=dict)
    paper_sandbox_candidates: list[dict[str, Any]] = field(default_factory=list)
    research_candidates: list[dict[str, Any]] = field(default_factory=list)
    watch_only: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    need_more_data: list[dict[str, Any]] = field(default_factory=list)
    final_status_top_level: str = STATUS_NEED_MORE_DATA
    exit_monetization_diagnostic: dict[str, Any] = field(default_factory=dict)
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _validate_features(features: Iterable[str]) -> None:
    for f in features:
        if f in FORBIDDEN_FEATURES:
            raise ValueError(
                f"forbidden feature {f!r}: would leak ex-post info or "
                "use anti-calibrated score as a positive gate"
            )


def _split_temporal(rows: list[dict[str, Any]]) -> tuple[list, list, list]:
    if not rows:
        return [], [], []
    ordered = sorted(rows, key=lambda r: str(r.get("timestamp", "")))
    n = len(ordered)
    train_end = int(n * TRAIN_FRACTION)
    val_end = int(n * (TRAIN_FRACTION + VAL_FRACTION))
    return ordered[:train_end], ordered[train_end:val_end], ordered[val_end:]


def _features_key(row: dict[str, Any], features: tuple[str, ...]) -> tuple[Any, ...]:
    return tuple(row.get(f) for f in features)


def _rule_id_str(features: tuple[str, ...], key: tuple[Any, ...]) -> str:
    return "|".join(f"{name}={value}" for name, value in zip(features, key))


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    nets: list[float] = []
    for r in rows:
        v = r.get("net_pnl_est")
        if v is None:
            v = r.get("baseline_net_pnl_est")
        if isinstance(v, (int, float)):
            nets.append(float(v))
    if not nets:
        return {
            "samples": 0, "winrate": 0.0, "net_ev_pct": 0.0, "pf": 0.0,
            "net_ev_after_realistic_pct": 0.0,
            "net_ev_after_stress_pct": 0.0,
        }
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    pf = _profit_factor(sum(wins), sum(losses))
    n = len(nets)
    return {
        "samples": n,
        "winrate": len(wins) / n,
        "net_ev_pct": sum(nets) / n,
        "pf": pf,
        "net_ev_after_realistic_pct": (sum(nets) / n) - COST_REALISTIC_PCT,
        "net_ev_after_stress_pct": (sum(nets) / n) - COST_STRESS_PCT,
    }


def _cluster_ratio(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    buckets: dict[str, int] = {}
    for r in rows:
        ts = str(r.get("timestamp", ""))
        bucket = ts[:13] if len(ts) >= 13 else ts
        buckets[bucket] = buckets.get(bucket, 0) + 1
    return max(buckets.values()) / len(rows)


def _symbol_concentration(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    by: dict[str, int] = {}
    for r in rows:
        s = str(r.get("symbol", "UNKNOWN")).upper()
        by[s] = by.get(s, 0) + 1
    return max(by.values()) / len(rows)


def _duplicate_ratio(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    seen: dict[str, int] = {}
    for r in rows:
        key = "|".join([
            str(r.get("symbol", "")),
            str(r.get("timestamp", ""))[:16],
            str(r.get("regime_now", "") or r.get("regime", "")),
        ])
        seen[key] = seen.get(key, 0) + 1
    dup = sum(c - 1 for c in seen.values() if c > 1)
    return dup / len(rows)


def _decide_final(
    train_m: dict[str, Any],
    val_m: dict[str, Any],
    test_m: dict[str, Any],
    cluster: float,
    sym_conc: float,
    dup_after: float,
    symbol_in_features: bool,
) -> tuple[str, str]:
    if train_m["samples"] < MIN_TRAIN_SAMPLES:
        return STATUS_NEED_MORE_DATA, f"train_samples={train_m['samples']}_below_{MIN_TRAIN_SAMPLES}"
    if val_m["samples"] < MIN_VAL_SAMPLES:
        return STATUS_NEED_MORE_DATA, f"validation_samples={val_m['samples']}_below_{MIN_VAL_SAMPLES}"
    if test_m["samples"] < MIN_TEST_SAMPLES:
        return STATUS_NEED_MORE_DATA, f"test_samples={test_m['samples']}_below_{MIN_TEST_SAMPLES}"
    if dup_after > MAX_DUPLICATE_RATIO_AFTER:
        return STATUS_REJECT, f"duplicate_ratio_after={dup_after:.2f}_above_{MAX_DUPLICATE_RATIO_AFTER}"
    if train_m["net_ev_after_realistic_pct"] <= 0:
        return STATUS_REJECT, "train_net_ev_not_positive_after_realistic_cost"
    if val_m["net_ev_after_realistic_pct"] <= 0:
        return STATUS_REJECT, "validation_net_ev_not_positive_after_realistic_cost"
    if test_m["net_ev_after_realistic_pct"] <= 0:
        return STATUS_REJECT, "test_net_ev_not_positive_after_realistic_cost"
    if test_m["pf"] < MIN_TEST_PF:
        return STATUS_REJECT, f"test_pf={test_m['pf']:.2f}_below_{MIN_TEST_PF}"
    if test_m["winrate"] < MIN_TEST_WINRATE:
        return STATUS_REJECT, f"test_winrate={test_m['winrate']:.2f}_below_{MIN_TEST_WINRATE}"
    if cluster > MAX_CLUSTER_RATIO:
        return STATUS_WATCH_ONLY, f"test_cluster_ratio={cluster:.2f}_above_{MAX_CLUSTER_RATIO}"
    if not symbol_in_features and sym_conc > MAX_SINGLE_SYMBOL_RATIO:
        return STATUS_SINGLE_SYMBOL_RESEARCH_ONLY, (
            f"single_symbol_concentration={sym_conc:.2f}_above_{MAX_SINGLE_SYMBOL_RATIO}"
        )
    # Survives realistic cost. Stress test gates whether the rule earns
    # PAPER_SANDBOX_CANDIDATE or only WATCH_ONLY.
    if test_m["net_ev_after_stress_pct"] <= 0:
        return STATUS_WATCH_ONLY, "survives_realistic_but_not_stress_cost_0_35"
    return STATUS_PAPER_SANDBOX_CANDIDATE, "all_strict_gates_pass_research_label_only"


def run_strict_oos_rebound(
    candidates: Iterable[dict[str, Any]] | None = None,
    *,
    hours: int = 168,
    score_anti_calibrated: bool = True,
    grouping_features: Iterable[str] = EX_ANTE_FEATURES,
    duplicate_ratio_after: float = 0.0,
    exit_monetization_diagnostic: dict[str, Any] | None = None,
) -> StrictOosReboundReport:
    """Strict train-only rule selection + single-shot OOS evaluation
    over LONG rebound candidates."""
    report = StrictOosReboundReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
        score_anti_calibrated_input=bool(score_anti_calibrated),
        duplicate_ratio_after=float(duplicate_ratio_after),
        exit_monetization_diagnostic=dict(exit_monetization_diagnostic or {}),
    )
    features = tuple(grouping_features)
    _validate_features(features)
    rows = list(candidates or [])
    report.candidates_total = len(rows)
    if not rows:
        return report
    train, val, test = _split_temporal(rows)
    train_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for r in train:
        train_groups.setdefault(_features_key(r, features), []).append(r)
    symbol_in_features = "symbol" in features
    for key, train_rs in train_groups.items():
        val_rs = [r for r in val if _features_key(r, features) == key]
        test_rs = [r for r in test if _features_key(r, features) == key]
        train_m = _metrics(train_rs)
        val_m = _metrics(val_rs)
        test_m = _metrics(test_rs)
        cluster = _cluster_ratio(test_rs)
        sym_conc = _symbol_concentration(test_rs)
        dup_after_rule = max(
            _duplicate_ratio(test_rs), float(duplicate_ratio_after)
        )
        status, reason = _decide_final(
            train_m, val_m, test_m, cluster, sym_conc, dup_after_rule,
            symbol_in_features,
        )
        rule = StrictOosRebound(
            rule_id=_rule_id_str(features, key),
            features={f: v for f, v in zip(features, key)},
            train_samples=train_m["samples"],
            validation_samples=val_m["samples"],
            test_samples=test_m["samples"],
            train_net_ev_pct=train_m["net_ev_pct"],
            validation_net_ev_pct=val_m["net_ev_pct"],
            test_net_ev_pct=test_m["net_ev_pct"],
            test_net_ev_after_cost_realistic_pct=test_m["net_ev_after_realistic_pct"],
            test_net_ev_after_cost_stress_pct=test_m["net_ev_after_stress_pct"],
            test_pf=test_m["pf"],
            test_winrate=test_m["winrate"],
            test_cluster_ratio=cluster,
            test_symbol_concentration=sym_conc,
            duplicate_ratio_after=dup_after_rule,
            final_status=status,
            reject_reason=reason,
        )
        d = rule.as_dict()
        report.by_final_status[status] = report.by_final_status.get(status, 0) + 1
        if status == STATUS_PAPER_SANDBOX_CANDIDATE:
            report.paper_sandbox_candidates.append(d)
        elif status == STATUS_RESEARCH_CANDIDATE:
            report.research_candidates.append(d)
        elif status == STATUS_SINGLE_SYMBOL_RESEARCH_ONLY:
            report.research_candidates.append(d)
        elif status == STATUS_WATCH_ONLY:
            report.watch_only.append(d)
        elif status == STATUS_REJECT:
            report.rejected.append(d)
        else:
            report.need_more_data.append(d)
    if report.paper_sandbox_candidates:
        report.final_status_top_level = STATUS_PAPER_SANDBOX_CANDIDATE
    elif report.research_candidates:
        report.final_status_top_level = STATUS_RESEARCH_CANDIDATE
    elif report.watch_only:
        report.final_status_top_level = STATUS_WATCH_ONLY
    elif report.rejected:
        report.final_status_top_level = STATUS_REJECT
    else:
        report.final_status_top_level = STATUS_NEED_MORE_DATA
    report.status = STATUS_OK
    # By construction this report never promotes score as a positive gate.
    report.score_used_as_gate = False
    return report
