from app.exit_policy_v3 import simulate_exit_policy, simulate_exit_policy_bar_by_bar


def test_mfe_summary_only_needs_bar_path():
    row = {"side": "LONG", "market_regime": "TREND_UP", "mfe": 5.0, "mae": 0.1, "first_barrier_hit": "TP"}

    result = simulate_exit_policy(row, "trailing_stop_atr")

    assert result.backtest_status == "NEED_BAR_PATH"
    assert result.simulated_net_ev is None
    assert result.decision == "NEED_BAR_PATH"


def test_bar_path_trailing_is_plausible_and_research_only():
    row = {
        "side": "LONG",
        "market_regime": "TREND_UP",
        "entry": 100.0,
        "bar_path": [
            {"open": 100, "high": 100.5, "low": 99.9, "close": 100.4},
            {"open": 100.4, "high": 101.4, "low": 100.3, "close": 101.1},
        ],
    }

    result = simulate_exit_policy(row, "regime_adaptive_exit")

    assert result.research_only is True
    assert result.backtest_status == "OK_BAR_PATH"
    assert result.simulated_return_pct is not None


def test_same_bar_stop_before_tp_worst_case():
    result = simulate_exit_policy_bar_by_bar(
        entry=100.0,
        side="LONG",
        bars=[{"open": 100, "high": 102.5, "low": 98.5, "close": 101}],
        policy_config={"tp_pct": 2.0, "sl_pct": 1.0},
    )

    assert result["exit_reason"] == "STOP_LOSS"
    assert result["same_bar_stop_tp_rule"] == "STOP_BEFORE_TP"
    assert result["realized_return_pct"] < 0


def test_no_lookahead_future_bars_after_exit_do_not_change_result():
    bars = [{"open": 100, "high": 102.5, "low": 98.5, "close": 101}]
    base = simulate_exit_policy_bar_by_bar(entry=100.0, side="LONG", bars=bars, policy_config={"tp_pct": 2.0, "sl_pct": 1.0})
    with_future = simulate_exit_policy_bar_by_bar(
        entry=100.0,
        side="LONG",
        bars=bars + [{"open": 101, "high": 110, "low": 100, "close": 109}],
        policy_config={"tp_pct": 2.0, "sl_pct": 1.0},
    )

    assert with_future["exit_bar_index"] == base["exit_bar_index"]
    assert with_future["realized_return_pct"] == base["realized_return_pct"]
