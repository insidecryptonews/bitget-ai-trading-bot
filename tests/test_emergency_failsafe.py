from app.execution_safety import place_stop_loss_with_retry


def test_stop_failure_retries_then_critical():
    attempts = {"stop": 0, "close": 0}

    def stop():
        attempts["stop"] += 1
        raise RuntimeError("stop failed")

    def close():
        attempts["close"] += 1
        raise RuntimeError("close failed")

    result = place_stop_loss_with_retry(stop, emergency_close_callback=close, max_attempts=3)

    assert attempts["stop"] == 3
    assert attempts["close"] == 3
    assert "CRITICAL_UNPROTECTED_POSITION" in result["status"]
