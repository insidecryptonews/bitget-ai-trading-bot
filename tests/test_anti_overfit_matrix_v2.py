from app.anti_overfit_matrix_v2 import evaluate_overfit_group


def test_anti_overfit_market_probe_edge_only_rejected():
    rows = [{"source": "market_probe", "return_pct": 0.5, "first_barrier_hit": "TP"} for _ in range(300)]

    result = evaluate_overfit_group(("ETHUSDT", "SHORT", "TREND_DOWN", "85-89", "market_probe"), rows)

    assert "MARKET_PROBE_EDGE_ONLY" in result["flags"]
    assert result["decision"] == "REJECT_OVERFIT"


def test_anti_overfit_robust_trade_signal_can_shadow():
    rows = [{"source": "trade_signal", "return_pct": 0.5, "first_barrier_hit": "TP"} for _ in range(800)]

    result = evaluate_overfit_group(("ETHUSDT", "SHORT", "TREND_DOWN", "85-89", "trade_signal"), rows)

    assert result["decision"] in {"SHADOW_CANDIDATE", "WATCH_ONLY"}
