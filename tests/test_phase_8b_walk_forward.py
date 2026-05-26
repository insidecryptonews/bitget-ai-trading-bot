from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.phase8_candidate_validator import Phase8PolicySample, evaluate_phase8_walk_forward


def _samples(values: list[float], *, symbol: str = "DOTUSDT") -> list[Phase8PolicySample]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        Phase8PolicySample(
            symbol=symbol,
            policy_name="late_entry_block_plus_dynamic_hold",
            timestamp=start + timedelta(minutes=5 * i),
            gross_return_pct=0.60,
            net_return_pct=value,
        )
        for i, value in enumerate(values)
    ]


def test_phase8_walk_forward_passes_stable_positive_folds():
    baseline = _samples([-0.05] * 240)
    policy = _samples([0.20] * 240)
    result = evaluate_phase8_walk_forward(baseline, policy, folds=4)
    assert result.status == "PASS"
    assert all(fold.pass_fold for fold in result.folds)


def test_phase8_walk_forward_fails_when_only_one_fold_saves_result():
    baseline = _samples([-0.05] * 240)
    policy = _samples([1.0] * 60 + [-0.10] * 180)
    result = evaluate_phase8_walk_forward(baseline, policy, folds=4)
    assert result.status == "FAIL"


def test_phase8_walk_forward_needs_data_for_tiny_samples():
    result = evaluate_phase8_walk_forward(_samples([-0.05] * 20), _samples([0.20] * 20), folds=4)
    assert result.status == "NEED_MORE_DATA"
