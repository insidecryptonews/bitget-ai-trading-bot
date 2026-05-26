from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.phase8_candidate_validator import (
    PAPER_DEMO_READY_MANUAL_REVIEW_ONLY,
    REJECT_COST_STRESS_FAIL,
    REJECT_NEGATIVE_EV,
    REJECT_TOO_FEW_TRADES,
    REJECT_WALK_FORWARD_FAIL,
    Phase8PolicySample,
    validate_phase8_candidate_from_samples,
)


def _samples(symbol: str, values: list[float], *, gross: float = 0.60, policy: str = "p") -> list[Phase8PolicySample]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        Phase8PolicySample(
            symbol=symbol,
            policy_name=policy,
            timestamp=start + timedelta(minutes=5 * i),
            gross_return_pct=gross,
            net_return_pct=value,
            exit_reason="DYNAMIC_HORIZON_CLOSE",
            duration_bars=12,
        )
        for i, value in enumerate(values)
    ]


def test_phase8_candidate_validator_can_emit_manual_review_only_without_activation():
    baseline = _samples("DOTUSDT", [-0.05] * 240, gross=0.10, policy="baseline")
    policy = _samples("DOTUSDT", [0.30] * 240, gross=0.65)
    result = validate_phase8_candidate_from_samples(
        candidate_id="DOT::late_entry",
        symbols=["DOTUSDT"],
        policy_name="late_entry_block_plus_dynamic_hold",
        baseline_samples=baseline,
        policy_samples=policy,
        min_trades=200,
    )
    assert result.final_decision == PAPER_DEMO_READY_MANUAL_REVIEW_ONLY
    assert result.paper_filter_enabled is False
    assert result.can_send_real_orders is False
    assert result.final_recommendation == "NO LIVE"


def test_phase8_candidate_validator_rejects_negative_ev():
    baseline = _samples("LINKUSDT", [-0.05] * 240)
    policy = _samples("LINKUSDT", [-0.02] * 240, gross=0.60)
    result = validate_phase8_candidate_from_samples(
        candidate_id="LINK::late_entry",
        symbols=["LINKUSDT"],
        policy_name="late_entry_block_plus_dynamic_hold",
        baseline_samples=baseline,
        policy_samples=policy,
    )
    assert result.final_decision == REJECT_NEGATIVE_EV


def test_phase8_candidate_validator_rejects_cost_stress_fail_even_if_net_positive():
    baseline = _samples("DOTUSDT", [-0.05] * 240, gross=0.10)
    policy = _samples("DOTUSDT", [0.05] * 240, gross=0.20)
    result = validate_phase8_candidate_from_samples(
        candidate_id="DOT::cost_fail",
        symbols=["DOTUSDT"],
        policy_name="late_entry_block_plus_dynamic_hold",
        baseline_samples=baseline,
        policy_samples=policy,
    )
    assert result.final_decision == REJECT_COST_STRESS_FAIL
    assert result.cost_stress_status == "FAIL"
    assert any("stress_0_22" in reason or "stress_0_25" in reason for reason in result.cost_stress.reasons)


def test_phase8_candidate_validator_rejects_walk_forward_fail():
    baseline = _samples("DOTUSDT", [-0.05] * 240, gross=0.10)
    policy_values = [1.0] * 60 + [-0.10] * 180
    policy = _samples("DOTUSDT", policy_values, gross=0.65)
    result = validate_phase8_candidate_from_samples(
        candidate_id="DOT::wf_fail",
        symbols=["DOTUSDT"],
        policy_name="late_entry_block_plus_dynamic_hold",
        baseline_samples=baseline,
        policy_samples=policy,
    )
    assert result.final_decision == REJECT_WALK_FORWARD_FAIL
    assert result.walk_forward_status == "FAIL"


def test_phase8_candidate_validator_blocks_low_sample():
    baseline = _samples("DOTUSDT", [-0.05] * 50)
    policy = _samples("DOTUSDT", [0.30] * 50, gross=0.65)
    result = validate_phase8_candidate_from_samples(
        candidate_id="DOT::low_sample",
        symbols=["DOTUSDT"],
        policy_name="late_entry_block_plus_dynamic_hold",
        baseline_samples=baseline,
        policy_samples=policy,
        min_trades=200,
    )
    assert result.final_decision == REJECT_TOO_FEW_TRADES
