from app.sudden_move_detector import detect_sudden_move


def test_clean_breakout_up_detects_long_research():
    result = detect_sudden_move({"side": "LONG", "market_regime": "TREND_UP", "mfe": 2.4, "mae": 0.2, "return_pct": 1.0, "volume_change": 2.0, "volatility": 0.02})

    assert result["direction"] == "LONG"
    assert result["research_only"] is True


def test_choppy_fakeout_penalized_and_volume_only_not_promoted():
    choppy = detect_sudden_move({"side": "LONG", "market_regime": "CHOPPY_MARKET", "mfe": 0.7, "mae": 0.6, "return_pct": 0.0, "volume_change": 2.0})
    volume_only = detect_sudden_move({"side": "LONG", "market_regime": "RANGE", "mfe": 0.1, "mae": 0.1, "return_pct": 0.0, "volume_change": 3.0})

    assert "fakeout" in choppy["not_actionable_reason"]
    assert volume_only["direction"] == "NONE" or volume_only["confidence"] == "LOW"


def test_market_probe_sudden_move_not_actionable():
    result = detect_sudden_move({"source": "market_probe", "side": "LONG", "market_regime": "TREND_UP", "mfe": 2.0, "mae": 0.1, "return_pct": 1.0, "volume_change": 2.0})

    assert "market_probe_not_actionable" in result["not_actionable_reason"]
