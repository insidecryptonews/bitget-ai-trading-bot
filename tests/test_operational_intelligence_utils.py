from app.operational_intelligence_utils import edge_metrics, extract_realized_return, return_coverage_report, row_return
from app.strategy_research_library import evaluate_benchmark


def test_row_return_uses_realized_return_only():
    assert row_return({"return_pct": 0.42, "mfe": 9.0}) == 0.42
    assert row_return({"realized_return_pct": -0.2, "mfe": 9.0}) == -0.2
    assert row_return({"net_return_pct": 0.1, "mae": 9.0}) == 0.1


def test_row_return_blocks_mfe_mae_and_hit_type_fallbacks():
    assert extract_realized_return({"mfe": 9.0, "first_barrier_hit": "TP"}) is None
    assert row_return({"mfe": 9.0, "first_barrier_hit": "TP"}) is None
    assert row_return({"mae": 9.0, "first_barrier_hit": "SL"}) is None
    assert row_return({"first_barrier_hit": "TIME"}) is None


def test_edge_metrics_requires_realized_return_coverage():
    rows = [{"mfe": 3.0, "first_barrier_hit": "TP"} for _ in range(10)]

    metrics = edge_metrics(rows)

    assert metrics["edge_metrics_status"] == "NEED_REALIZED_RETURN"
    assert metrics["rows_missing_realized_return"] == 10
    assert metrics["samples"] == 0


def test_simple_breakout_does_not_use_mfe_oracle():
    rows = [{"mfe": 10.0, "return_pct": 1.0, "first_barrier_hit": "TP"} for _ in range(20)]

    benchmark = evaluate_benchmark("simple_breakout_baseline", rows)

    assert benchmark["benchmark_status"] == "NEED_FEATURES"
    assert benchmark["net_EV"] == 0.0


def test_return_coverage_statuses_do_not_count_mfe():
    assert return_coverage_report([])["return_quality_status"] == "NEED_DATA"
    assert return_coverage_report([{"mfe": 1.0}])["return_quality_status"] == "BAD_NO_REALIZED_RETURNS"
    ok_rows = [{"return_pct": 0.1} for _ in range(95)] + [{"mfe": 1.0} for _ in range(5)]
    assert return_coverage_report(ok_rows)["return_quality_status"] == "OK"
