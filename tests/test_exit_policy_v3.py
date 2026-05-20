from app.exit_policy_v3 import simulate_exit_policy


def test_trailing_leaves_trend_more_room_research_only():
    row = {"side": "SHORT", "market_regime": "TREND_DOWN", "mfe": 2.0, "mae": 0.2, "return_pct": 0.2, "first_barrier_hit": "TP"}

    result = simulate_exit_policy(row, "regime_adaptive_exit")

    assert result.research_only is True
    assert result.expected_capture_ratio >= 0.65
    assert result.simulated_exit_reason == "REGIME_ADAPTIVE_TREND"


def test_range_uses_shorter_targets_and_choppy_no_trade():
    range_row = {"side": "LONG", "market_regime": "RANGE", "mfe": 0.8, "mae": 0.3, "return_pct": 0.0, "first_barrier_hit": "TIME"}
    choppy_row = {"side": "LONG", "market_regime": "CHOPPY_MARKET", "mfe": 0.3, "mae": 0.7, "return_pct": -0.4, "first_barrier_hit": "SL"}

    range_result = simulate_exit_policy(range_row, "regime_adaptive_exit")
    choppy_result = simulate_exit_policy(choppy_row, "regime_adaptive_exit")

    assert range_result.dynamic_tp1 <= range_result.fixed_tp
    assert choppy_result.simulated_exit_reason == "CHOPPY_RESEARCH_NO_TRADE"


def test_break_even_and_profit_lock_do_not_apply_real_exits():
    row = {"side": "LONG", "market_regime": "TREND_UP", "mfe": 1.5, "mae": 0.2, "return_pct": -0.2, "first_barrier_hit": "SL"}

    be = simulate_exit_policy(row, "break_even_after_mfe")
    lock = simulate_exit_policy({**row, "return_pct": 0.1}, "profit_lock_after_mfe")

    assert be.simulated_return_pct >= 0
    assert lock.simulated_return_pct >= 0.25
    assert be.research_only and lock.research_only
