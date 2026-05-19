from app.execution_safety import evaluate_circuit_breaker_magnitude


def test_micro_losses_do_not_trigger_hard_cooldown():
    result = evaluate_circuit_breaker_magnitude([0.0001, 0.0001, 0.0001])

    assert result["status"] == "MICRO_LOSS_STREAK_WATCH"


def test_large_losses_trigger_cooldown_and_hard_drawdown_triggers_stop():
    cooldown = evaluate_circuit_breaker_magnitude([0.01, 0.012, 0.013])
    hard = evaluate_circuit_breaker_magnitude([0.06])

    assert cooldown["status"] == "LOSS_STREAK_COOLDOWN"
    assert hard["status"] == "DRAWDOWN_HARD_STOP"
