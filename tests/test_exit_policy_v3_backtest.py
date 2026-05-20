from app.exit_policy_v3_backtest import evaluate_group


def test_exit_policy_v3_backtest_low_sample_not_promoted():
    rows = [
        {"symbol": "ETHUSDT", "side": "SHORT", "market_regime": "TREND_DOWN", "score_bucket": "85-89", "strategy": "trend", "source": "trade_signal", "mfe": 2.0, "mae": 0.2, "return_pct": 0.2, "first_barrier_hit": "TP"}
        for _ in range(20)
    ]

    result = evaluate_group(rows)

    assert result["decision"] == "NEED_BAR_PATH"
    assert result["backtest_status"] == "NEED_BAR_PATH"
    assert result["research_only"] is True


def test_exit_policy_v3_backtest_market_probe_not_actionable():
    rows = [
        {"symbol": "ETHUSDT", "side": "SHORT", "market_regime": "TREND_DOWN", "score_bucket": "85-89", "strategy": "trend", "source": "market_probe", "mfe": 2.0, "mae": 0.2, "return_pct": 0.2, "first_barrier_hit": "TP"}
        for _ in range(300)
    ]

    result = evaluate_group(rows)

    assert result["decision"] in {"NEED_BAR_PATH", "NEED_MORE_DATA", "REJECT", "WATCH_ONLY"}
    assert result["research_only"] is True


def test_exit_policy_v3_backtest_runs_only_with_bar_path():
    bars = [
        {"open": 100, "high": 100.5, "low": 99.9, "close": 100.3},
        {"open": 100.3, "high": 101.4, "low": 100.2, "close": 101.1},
    ]
    rows = [
        {"symbol": "ETHUSDT", "side": "LONG", "market_regime": "TREND_UP", "score_bucket": "85-89", "strategy": "trend", "source": "trade_signal", "entry": 100, "bar_path": bars, "return_pct": 0.2, "first_barrier_hit": "TP"}
        for _ in range(300)
    ]

    result = evaluate_group(rows)

    assert result["backtest_status"] == "OK_BAR_PATH"
