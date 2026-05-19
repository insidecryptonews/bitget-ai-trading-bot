from app.execution_safety import build_effective_balance_for_risk


def test_fresh_balance_reduced_before_risk_validation():
    result = build_effective_balance_for_risk(
        balance=100,
        available_balance=80,
        used_margin=10,
        reduce_risk=True,
        source="fresh_live_balance",
        balance_timestamp="2026-05-19T12:00:00+00:00",
    )

    assert result.balance == 50
    assert result.available_balance == 50
    assert result.used_margin == 10
    assert result.source == "fresh_live_balance"
