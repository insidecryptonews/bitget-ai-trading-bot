from app.shadow_strategy_simulator import simulate_strategy


def test_shadow_strategy_without_edge_not_promoted():
    rows = [{"symbol": "BTCUSDT", "side": "LONG", "market_regime": "RANGE", "source": "trade_signal", "mfe": 0.2, "mae": 0.8, "return_pct": -0.4, "first_barrier_hit": "SL"} for _ in range(300)]

    result = simulate_strategy(rows, "regime_adaptive_exit")

    assert result["recommendation"] == "REJECT"
    assert result["research_only"] is True


def test_shadow_strategy_drawdown_blocks_promotion():
    rows = [{"symbol": "SOLUSDT", "side": "LONG", "market_regime": "RANGE", "source": "trade_signal", "mfe": 2.0, "mae": 3.0, "return_pct": -3.0 if i % 4 == 0 else 0.5, "first_barrier_hit": "SL" if i % 4 == 0 else "TP"} for i in range(300)]

    result = simulate_strategy(rows, "trailing_stop_atr")

    assert result["recommendation"] in {"REJECT", "REJECT_DRAWDOWN"}
