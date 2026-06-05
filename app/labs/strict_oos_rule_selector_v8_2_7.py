"""V8.2.7 — Strict train-only rule selection + true OOS evaluation.

Fixes the V8.2.6 weakness where rules were mined over the **full** dataset
and then "validated" with walk-forward on the same data. Here:

- 60% train / 20% validation / 20% test (temporal split).
- Rule mining (group discovery + selection) reads ONLY ``train`` rows.
- ``validation`` filters out rules that don't generalise to the held-out
  middle slice.
- ``test`` is evaluated **once** at the end. Never used for selection or
  tuning.

Hard contract: research-only. Forbidden as features (label leakage):
``training_label``, ``first_barrier_hit``, ``baseline_net_pnl_est``,
``baseline_gross_pnl``, every ``ret_*_pct``, ``mfe_pct``, ``mae_pct``,
``baseline_result``, ``trailing_result``, ``campaign_result``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE, STATUS_NEED_DATA, STATUS_OK
from .candidate_rule_miner_v8_2_6 import EX_POST_LABELS
from .counterfactual_dedup_audit import _is_evaluable, dedup_rows
from .counterfactual_training_dataset import build_dataset


# Ex-ante feature whitelist (mirrors V8.2.6 with a clearer comment).
EX_ANTE_FEATURES: tuple[str, ...] = (
    "symbol", "side", "regime", "strategy", "score_bucket",
    "candidate_selected", "risk_approved",
)


# Splits.
TRAIN_FRACTION = 0.60
VAL_FRACTION = 0.20
# test fraction = 1 - TRAIN - VAL

# Sample minima per split.
MIN_TRAIN_SAMPLES = 30
MIN_VAL_SAMPLES = 15
MIN_TEST_SAMPLES = 15

# Outcome gates.
COST_NORMAL_PCT = 0.18
COST_REALISTIC_PCT = 0.25
COST_STRESS_PCT = 0.35
MIN_TEST_PF = 1.15
MIN_TEST_WINRATE = 0.55
MAX_DEGRADATION_TRAIN_TO_TEST = 0.60   # 60%
MAX_TIMESTAMP_CLUSTER_RATIO = 0.30


# Final gate constants.
FINAL_REJECT = "REJECT"
FINAL_WATCH_ONLY = "WATCH_ONLY"
FINAL_RESEARCH_CANDIDATE = "RESEARCH_CANDIDATE"
FINAL_PAPER_SANDBOX_CANDIDATE = "PAPER_SANDBOX_CANDIDATE"
FINAL_NEED_MORE_DATA = "NEED_MORE_DATA"

VALID_FINAL_GATES: tuple[str, ...] = (
    FINAL_REJECT, FINAL_WATCH_ONLY, FINAL_RESEARCH_CANDIDATE,
    FINAL_PAPER_SANDBOX_CANDIDATE, FINAL_NEED_MORE_DATA,
)


@dataclass
class StrictRuleResult:
    rule_id: str
    features: dict[str, Any]
    train_samples: int
    validation_samples: int
    test_samples: int
    train_net_ev_pct: float
    validation_net_ev_pct: float
    test_net_ev_pct: float
    train_pf: float
    validation_pf: float
    test_pf: float
    train_winrate: float
    validation_winrate: float
    test_winrate: float
    degradation_train_to_test_pct: float
    test_cost_normal_net_ev_pct: float
    test_cost_realistic_net_ev_pct: float
    test_cost_stress_net_ev_pct: float
    train_cluster_ratio: float
    test_cluster_ratio: float
    test_symbol_concentration_ratio: float
    final_gate: str
    reject_reason: str = ""
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StrictOosSelectorReport:
    hours: int
    generated_at: str
    short_verdict: str = ""
    short_excluded: bool = False
    score_calibration_ok: bool = False
    total_dataset_rows: int = 0
    evaluable_rows: int = 0
    train_size: int = 0
    validation_size: int = 0
    test_size: int = 0
    total_rules_evaluated: int = 0
    by_final_gate: dict[str, int] = field(default_factory=dict)
    paper_sandbox_candidates: list[dict[str, Any]] = field(default_factory=list)
    research_candidates: list[dict[str, Any]] = field(default_factory=list)
    watch_only_rules: list[dict[str, Any]] = field(default_factory=list)
    rejected_rules: list[dict[str, Any]] = field(default_factory=list)
    need_more_data_rules: list[dict[str, Any]] = field(default_factory=list)
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---- Helpers --------------------------------------------------------------

def _validate_features_whitelist(features: Iterable[str]) -> None:
    for f in features:
        if f in EX_POST_LABELS:
            raise ValueError(
                f"forbidden feature {f!r}: would leak the label into the rule selector"
            )


def _features_key(row: dict[str, Any], features: tuple[str, ...]) -> tuple[Any, ...]:
    return tuple(row.get(f) for f in features)


def _rule_id_str(features: tuple[str, ...], key: tuple[Any, ...]) -> str:
    return "|".join(f"{name}={value}" for name, value in zip(features, key))


def _timestamp_cluster_ratio(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    buckets: dict[str, int] = {}
    for r in rows:
        ts = str(r.get("timestamp", ""))
        bucket = ts[:13] if len(ts) >= 13 else ts
        buckets[bucket] = buckets.get(bucket, 0) + 1
    return max(buckets.values()) / len(rows)


def _symbol_concentration_ratio(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    by_symbol: dict[str, int] = {}
    for r in rows:
        symbol = str(r.get("symbol", "UNKNOWN")).upper()
        by_symbol[symbol] = by_symbol.get(symbol, 0) + 1
    return max(by_symbol.values()) / len(rows)


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    nets: list[float] = []
    grosses: list[float] = []
    for r in rows:
        try:
            nets.append(float(r.get("baseline_net_pnl_est") or 0))
            grosses.append(float(r.get("baseline_gross_pnl") or 0))
        except Exception:
            continue
    if not nets:
        return {
            "samples": 0, "winrate": 0.0, "net_ev_pct": 0.0, "pf": 0.0,
            "cost_normal_net_ev_pct": 0.0,
            "cost_realistic_net_ev_pct": 0.0,
            "cost_stress_net_ev_pct": 0.0,
        }
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    loss_sum = abs(sum(losses))
    pf = (sum(wins) / loss_sum) if loss_sum > 0 else 0.0
    n = len(nets)
    return {
        "samples": n,
        "winrate": len(wins) / n,
        "net_ev_pct": sum(nets) / n,
        "pf": pf,
        "cost_normal_net_ev_pct": (sum(grosses) - n * COST_NORMAL_PCT) / n,
        "cost_realistic_net_ev_pct": (sum(grosses) - n * COST_REALISTIC_PCT) / n,
        "cost_stress_net_ev_pct": (sum(grosses) - n * COST_STRESS_PCT) / n,
    }


def _should_skip_short(side: Any, short_verdict: str) -> bool:
    if str(side).upper() != "SHORT":
        return False
    safe_verdicts = {"SHORT_LABELS_TRUSTED", "SHORT_SAFE_TO_USE_FOR_RESEARCH", ""}
    return short_verdict not in safe_verdicts


def _decide_final_gate(
    train_m: dict[str, Any],
    val_m: dict[str, Any],
    test_m: dict[str, Any],
    train_cluster: float,
    test_cluster: float,
    test_symbol_conc: float,
    score_calibration_ok: bool,
    symbol_in_features: bool = True,
) -> tuple[str, str]:
    """Apply the V8.2.7 strict gates."""
    if train_m["samples"] < MIN_TRAIN_SAMPLES:
        return FINAL_NEED_MORE_DATA, f"train_samples={train_m['samples']}_below_{MIN_TRAIN_SAMPLES}"
    if val_m["samples"] < MIN_VAL_SAMPLES:
        return FINAL_NEED_MORE_DATA, f"validation_samples={val_m['samples']}_below_{MIN_VAL_SAMPLES}"
    if test_m["samples"] < MIN_TEST_SAMPLES:
        return FINAL_NEED_MORE_DATA, f"test_samples={test_m['samples']}_below_{MIN_TEST_SAMPLES}"
    # Train gate — realistic cost.
    if train_m["cost_realistic_net_ev_pct"] <= 0:
        return FINAL_REJECT, "train_net_ev_not_positive_after_realistic_cost"
    # Validation gate.
    if val_m["cost_realistic_net_ev_pct"] <= 0:
        return FINAL_REJECT, "validation_net_ev_not_positive_after_realistic_cost"
    # Test gates.
    if test_m["cost_realistic_net_ev_pct"] <= 0:
        return FINAL_REJECT, "test_net_ev_not_positive_after_realistic_cost"
    if test_m["pf"] < MIN_TEST_PF:
        return FINAL_REJECT, f"test_pf={test_m['pf']:.2f}_below_{MIN_TEST_PF}"
    if test_m["winrate"] < MIN_TEST_WINRATE:
        return FINAL_REJECT, f"test_winrate={test_m['winrate']:.2f}_below_{MIN_TEST_WINRATE}"
    # Degradation gate.
    train_ev = train_m["cost_realistic_net_ev_pct"]
    test_ev = test_m["cost_realistic_net_ev_pct"]
    if train_ev > 0 and test_ev < train_ev * (1.0 - MAX_DEGRADATION_TRAIN_TO_TEST):
        return FINAL_REJECT, "degradation_train_to_test_exceeds_60pct"
    # Cluster gates.
    if train_cluster > MAX_TIMESTAMP_CLUSTER_RATIO or test_cluster > MAX_TIMESTAMP_CLUSTER_RATIO:
        return FINAL_WATCH_ONLY, (
            f"timestamp_cluster_train={train_cluster:.2f}_test={test_cluster:.2f}"
            f"_above_max_{MAX_TIMESTAMP_CLUSTER_RATIO}"
        )
    # Test positive only because one symbol dominates → WATCH_ONLY.
    # Skip the check when ``symbol`` is itself a grouping feature (the rule
    # is symbol-specific by design; concentration will trivially be 1.0).
    if not symbol_in_features and test_symbol_conc > 0.80:
        return FINAL_WATCH_ONLY, f"test_dominated_by_single_symbol_{test_symbol_conc:.2f}"
    if not score_calibration_ok:
        return FINAL_RESEARCH_CANDIDATE, "score_calibration_fail_research_only"
    return FINAL_PAPER_SANDBOX_CANDIDATE, "all_strict_gates_pass_research_label_only"


def _split_temporal(rows: list[dict[str, Any]]) -> tuple[list, list, list]:
    if not rows:
        return [], [], []
    ordered = sorted(rows, key=lambda r: str(r.get("timestamp", "")))
    n = len(ordered)
    train_end = int(n * TRAIN_FRACTION)
    val_end = int(n * (TRAIN_FRACTION + VAL_FRACTION))
    train = ordered[:train_end]
    validation = ordered[train_end:val_end]
    test = ordered[val_end:]
    return train, validation, test


def select_rules_strict_oos(
    db: Any = None,
    *,
    hours: int = 168,
    limit: int = 50000,
    rows: Iterable[dict[str, Any]] | None = None,
    short_verdict: str = "",
    score_calibration_ok: bool = False,
    grouping_features: Iterable[str] = EX_ANTE_FEATURES,
) -> StrictOosSelectorReport:
    """Strict train-only selection + single-shot OOS evaluation.

    Selection process:
    1. Sort dataset by timestamp.
    2. Temporal split 60% train / 20% validation / 20% test.
    3. Enumerate candidate rules **using only train rows**.
    4. Each candidate's metrics on validation and test are computed for
       reporting / gating; the **rule selection criterion** itself sees
       only train. No iteration / "trying again" after looking at test.
    """
    features = tuple(grouping_features)
    _validate_features_whitelist(features)

    report = StrictOosSelectorReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
        short_verdict=short_verdict,
        short_excluded=(short_verdict not in {"SHORT_LABELS_TRUSTED",
                                              "SHORT_SAFE_TO_USE_FOR_RESEARCH", ""}),
        score_calibration_ok=score_calibration_ok,
    )
    if rows is None:
        dataset, _ = build_dataset(db, hours=int(hours), limit=int(limit))
    else:
        dataset = list(rows)
    report.total_dataset_rows = len(dataset)
    evaluable = [r for r in dataset if _is_evaluable(r)]
    evaluable = dedup_rows(evaluable)
    # SHORT exclusion is global: removed from every split.
    if report.short_excluded:
        evaluable = [r for r in evaluable if not _should_skip_short(r.get("side"), short_verdict)]
    report.evaluable_rows = len(evaluable)
    if not evaluable:
        return report

    train, validation, test = _split_temporal(evaluable)
    report.train_size = len(train)
    report.validation_size = len(validation)
    report.test_size = len(test)

    # Mine candidate groups using ONLY train rows. No peeking at val/test.
    train_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for r in train:
        key = _features_key(r, features)
        train_groups.setdefault(key, []).append(r)

    val_index: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for r in validation:
        val_index.setdefault(_features_key(r, features), []).append(r)
    test_index: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for r in test:
        test_index.setdefault(_features_key(r, features), []).append(r)

    results: list[StrictRuleResult] = []
    for key, train_rows in train_groups.items():
        # Look up val/test rows for the same feature key (deterministic; no
        # search over alternatives).
        val_rows = val_index.get(key, [])
        test_rows = test_index.get(key, [])
        train_m = _metrics(train_rows)
        val_m = _metrics(val_rows)
        test_m = _metrics(test_rows)
        train_cluster = _timestamp_cluster_ratio(train_rows)
        test_cluster = _timestamp_cluster_ratio(test_rows)
        test_sym_conc = _symbol_concentration_ratio(test_rows)
        final_gate, reason = _decide_final_gate(
            train_m, val_m, test_m,
            train_cluster, test_cluster, test_sym_conc,
            score_calibration_ok,
            symbol_in_features=("symbol" in features),
        )
        feat_map = {name: value for name, value in zip(features, key)}
        degradation = 0.0
        if train_m["cost_realistic_net_ev_pct"] > 0:
            degradation = (
                train_m["cost_realistic_net_ev_pct"]
                - test_m["cost_realistic_net_ev_pct"]
            ) / max(train_m["cost_realistic_net_ev_pct"], 1e-9)
        rule = StrictRuleResult(
            rule_id=_rule_id_str(features, key),
            features=feat_map,
            train_samples=int(train_m["samples"]),
            validation_samples=int(val_m["samples"]),
            test_samples=int(test_m["samples"]),
            train_net_ev_pct=float(train_m["net_ev_pct"]),
            validation_net_ev_pct=float(val_m["net_ev_pct"]),
            test_net_ev_pct=float(test_m["net_ev_pct"]),
            train_pf=float(train_m["pf"]),
            validation_pf=float(val_m["pf"]),
            test_pf=float(test_m["pf"]),
            train_winrate=float(train_m["winrate"]),
            validation_winrate=float(val_m["winrate"]),
            test_winrate=float(test_m["winrate"]),
            degradation_train_to_test_pct=float(degradation),
            test_cost_normal_net_ev_pct=float(test_m["cost_normal_net_ev_pct"]),
            test_cost_realistic_net_ev_pct=float(test_m["cost_realistic_net_ev_pct"]),
            test_cost_stress_net_ev_pct=float(test_m["cost_stress_net_ev_pct"]),
            train_cluster_ratio=float(train_cluster),
            test_cluster_ratio=float(test_cluster),
            test_symbol_concentration_ratio=float(test_sym_conc),
            final_gate=final_gate,
            reject_reason=reason,
        )
        results.append(rule)

    report.total_rules_evaluated = len(results)
    for r in results:
        report.by_final_gate[r.final_gate] = report.by_final_gate.get(r.final_gate, 0) + 1
    # Sort by test_cost_realistic_net_ev desc within each bucket.
    results.sort(key=lambda r: r.test_cost_realistic_net_ev_pct, reverse=True)
    report.paper_sandbox_candidates = [
        r.as_dict() for r in results if r.final_gate == FINAL_PAPER_SANDBOX_CANDIDATE
    ]
    report.research_candidates = [
        r.as_dict() for r in results if r.final_gate == FINAL_RESEARCH_CANDIDATE
    ]
    report.watch_only_rules = [
        r.as_dict() for r in results if r.final_gate == FINAL_WATCH_ONLY
    ][:100]
    report.rejected_rules = [
        r.as_dict() for r in results if r.final_gate == FINAL_REJECT
    ][:100]
    report.need_more_data_rules = [
        r.as_dict() for r in results if r.final_gate == FINAL_NEED_MORE_DATA
    ][:100]
    report.status = STATUS_OK if results else STATUS_NEED_DATA
    return report
