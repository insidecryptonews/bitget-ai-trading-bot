from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.phase8_candidate_validator import Phase8PolicySample, evaluate_phase8_cost_stress


def _samples(gross: float, n: int = 240) -> list[Phase8PolicySample]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        Phase8PolicySample(
            symbol="DOTUSDT",
            policy_name="late_entry_block_plus_dynamic_hold",
            timestamp=start + timedelta(minutes=i),
            gross_return_pct=gross,
            net_return_pct=gross - 0.18,
        )
        for i in range(n)
    ]


def test_phase8_cost_stress_requires_022_and_025_positive():
    result = evaluate_phase8_cost_stress(_samples(0.24))
    assert result.status == "FAIL"
    assert any("stress_0_25_breaks_edge" in reason for reason in result.reasons)


def test_phase8_cost_stress_passes_when_base_022_025_positive():
    result = evaluate_phase8_cost_stress(_samples(0.40))
    assert result.status == "PASS"


def test_maker_maker_audit_only_never_promotes():
    result = evaluate_phase8_cost_stress(_samples(0.10))
    maker = [scenario for scenario in result.scenarios if scenario.name == "maker_maker_audit_only"][0]
    assert maker.net_ev > 0
    assert maker.promotion_eligible is False
    assert result.status == "FAIL"
    assert "maker_maker_audit_only_never_promotes" in result.reasons
