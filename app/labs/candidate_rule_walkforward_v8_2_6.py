"""V8.2.6 — Candidate Rule Walk-forward / OOS validator (research-only).

Validates candidate rules with temporal train/test splits:
- 60% train / 40% test.
- Rolling 3 folds if enough distinct timestamps.
- NEED_MORE_DATA when sample is insufficient.

Hard contract: research-only. No live, no paper filter.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE, STATUS_NEED_DATA, STATUS_OK
from .candidate_rule_miner_v8_2_6 import (
    COST_REALISTIC_PCT,
    EX_ANTE_FEATURES,
    _is_evaluable,
)
from .counterfactual_dedup_audit import dedup_rows
from .counterfactual_training_dataset import build_dataset


WF_PASS = "WF_PASS"
WF_FAIL = "WF_FAIL"
WF_NEED_MORE_DATA = "NEED_MORE_DATA"

MIN_TRAIN_SAMPLES = 12
MIN_TEST_SAMPLES = 8
MIN_FOLDS_FOR_ROLLING = 3
TRAIN_FRACTION = 0.60


@dataclass
class WalkForwardResult:
    rule_id: str
    features: dict[str, Any]
    total_samples: int
    train_samples: int
    test_samples: int
    train_net_ev_pct: float
    test_net_ev_pct: float
    train_pf: float
    test_pf: float
    degradation_net_ev_pct: float
    folds: int
    fold_details: list[dict[str, Any]] = field(default_factory=list)
    decision: str = WF_NEED_MORE_DATA
    reason: str = ""
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WalkForwardReport:
    hours: int
    generated_at: str
    rules_evaluated: int = 0
    by_decision: dict[str, int] = field(default_factory=dict)
    results: list[dict[str, Any]] = field(default_factory=list)
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _matches_rule(row: dict[str, Any], features: dict[str, Any]) -> bool:
    for k, v in features.items():
        if row.get(k) != v:
            return False
    return True


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
        return {"samples": 0, "net_ev": 0.0, "pf": 0.0}
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    loss_sum = abs(sum(losses))
    pf = (sum(wins) / loss_sum) if loss_sum > 0 else 0.0
    realistic_net = (sum(grosses) - len(nets) * COST_REALISTIC_PCT) / len(nets)
    return {
        "samples": len(nets),
        "net_ev": realistic_net,
        "pf": pf,
    }


def _sort_rows_by_time(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def _key(r: dict[str, Any]) -> str:
        return str(r.get("timestamp") or "")
    return sorted(rows, key=_key)


def _decide(train: dict[str, Any], test: dict[str, Any]) -> tuple[str, str]:
    if train["samples"] < MIN_TRAIN_SAMPLES or test["samples"] < MIN_TEST_SAMPLES:
        return WF_NEED_MORE_DATA, "insufficient_train_or_test_samples"
    train_ev = train["net_ev"]
    test_ev = test["net_ev"]
    if test_ev <= 0:
        return WF_FAIL, "test_net_ev_not_positive"
    if test["pf"] < 1.0:
        return WF_FAIL, "test_pf_below_1"
    if train_ev > 0 and test_ev < train_ev * 0.40:
        return WF_FAIL, "test_degrades_more_than_60pct_vs_train"
    return WF_PASS, "test_holds_within_acceptable_degradation"


def _evaluate_rule(rule_features: dict[str, Any], rows: list[dict[str, Any]]) -> WalkForwardResult:
    rule_id = "|".join(f"{k}={rule_features.get(k)}" for k in EX_ANTE_FEATURES)
    matched = [r for r in rows if _matches_rule(r, rule_features)]
    matched = _sort_rows_by_time(matched)
    total = len(matched)
    if total < (MIN_TRAIN_SAMPLES + MIN_TEST_SAMPLES):
        return WalkForwardResult(
            rule_id=rule_id, features=rule_features,
            total_samples=total, train_samples=0, test_samples=0,
            train_net_ev_pct=0.0, test_net_ev_pct=0.0,
            train_pf=0.0, test_pf=0.0,
            degradation_net_ev_pct=0.0, folds=0,
            decision=WF_NEED_MORE_DATA, reason="total_samples_below_min",
        )
    # Primary train/test split.
    cutoff = int(total * TRAIN_FRACTION)
    train_rows = matched[:cutoff]
    test_rows = matched[cutoff:]
    train_m = _metrics(train_rows)
    test_m = _metrics(test_rows)
    decision, reason = _decide(train_m, test_m)
    # Rolling 3 folds.
    fold_details: list[dict[str, Any]] = []
    folds_run = 0
    if total >= MIN_TRAIN_SAMPLES * 2 + MIN_TEST_SAMPLES:
        fold_size = total // MIN_FOLDS_FOR_ROLLING
        for i in range(MIN_FOLDS_FOR_ROLLING):
            start = i * fold_size
            end = start + fold_size
            fold_rows = matched[start:end]
            f_cut = int(len(fold_rows) * TRAIN_FRACTION)
            f_train = fold_rows[:f_cut]
            f_test = fold_rows[f_cut:]
            f_train_m = _metrics(f_train)
            f_test_m = _metrics(f_test)
            f_decision, f_reason = _decide(f_train_m, f_test_m)
            fold_details.append({
                "fold": i + 1,
                "train_samples": f_train_m["samples"],
                "test_samples": f_test_m["samples"],
                "train_net_ev": f_train_m["net_ev"],
                "test_net_ev": f_test_m["net_ev"],
                "decision": f_decision,
                "reason": f_reason,
            })
            folds_run += 1
        # Tighten decision if no fold passes individually.
        passes = sum(1 for f in fold_details if f["decision"] == WF_PASS)
        if decision == WF_PASS and passes < 2:
            decision = WF_FAIL
            reason = "primary_split_passes_but_rolling_folds_inconsistent"
    deg = train_m["net_ev"] - test_m["net_ev"]
    return WalkForwardResult(
        rule_id=rule_id, features=rule_features,
        total_samples=total,
        train_samples=train_m["samples"], test_samples=test_m["samples"],
        train_net_ev_pct=train_m["net_ev"], test_net_ev_pct=test_m["net_ev"],
        train_pf=train_m["pf"], test_pf=test_m["pf"],
        degradation_net_ev_pct=deg, folds=folds_run,
        fold_details=fold_details, decision=decision, reason=reason,
    )


def run_walkforward(
    db: Any = None,
    *,
    hours: int = 168,
    limit: int = 50000,
    rules: Iterable[dict[str, Any]] | None = None,
    rows: Iterable[dict[str, Any]] | None = None,
) -> WalkForwardReport:
    """Validate each rule's features dict against the dedup dataset."""
    report = WalkForwardReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    if rows is None:
        dataset, _ = build_dataset(db, hours=int(hours), limit=int(limit))
    else:
        dataset = list(rows)
    evaluable = [r for r in dataset if _is_evaluable(r)]
    evaluable = dedup_rows(evaluable)
    if not evaluable:
        return report
    rule_list = list(rules) if rules is not None else []
    if not rule_list:
        # No rules supplied → NEED_MORE_DATA without further work.
        report.status = STATUS_NEED_DATA
        return report
    results: list[WalkForwardResult] = []
    for rule in rule_list:
        feats = dict(rule.get("features") or {})
        if not feats:
            continue
        results.append(_evaluate_rule(feats, evaluable))
    report.rules_evaluated = len(results)
    for r in results:
        report.by_decision[r.decision] = report.by_decision.get(r.decision, 0) + 1
    report.results = [r.as_dict() for r in results]
    report.status = STATUS_OK if results else STATUS_NEED_DATA
    return report
