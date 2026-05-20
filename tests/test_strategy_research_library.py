from app.strategy_research_library import evaluate_benchmark, evaluate_hypothesis


def test_strategy_library_low_sample_and_market_probe_not_actionable():
    low = [{"market_regime": "TREND_DOWN", "source": "trade_signal", "return_pct": 0.5, "first_barrier_hit": "TP"} for _ in range(20)]
    probe = [{**row, "source": "market_probe"} for row in low * 20]

    assert evaluate_hypothesis("time_series_momentum", low)["decision"] == "NEED_MORE_DATA"
    assert evaluate_hypothesis("time_series_momentum", probe)["decision"] == "NEED_MORE_DATA_NOT_ACTIONABLE"


def test_strategy_library_benchmark_comparison_exists():
    rows = [{"market_regime": "TREND_DOWN", "source": "trade_signal", "return_pct": 0.5, "first_barrier_hit": "TP"} for _ in range(20)]

    benchmark = evaluate_benchmark("simple_momentum_baseline", rows)

    assert benchmark["benchmark_id"] == "simple_momentum_baseline"
    assert "net_EV" in benchmark
