from app.dynamic_exit_policy import propose_dynamic_tp_sl


def test_trend_uses_wider_shadow_targets():
    result = propose_dynamic_tp_sl(symbol="ETHUSDT", side="SHORT", regime="TREND_DOWN", entry=100, atr=1, score=85)

    assert result.research_only is True
    assert result.dynamic_exit_candidate["tp1_r"] == 2.0
    assert result.dynamic_exit_candidate["tp2_r"] == 3.5
    assert result.trailing_candidate is True
    assert result.dynamic_exit_candidate["apply_automatically"] is False


def test_range_uses_closer_targets_and_choppy_prefers_no_trade():
    range_result = propose_dynamic_tp_sl(symbol="BTCUSDT", side="LONG", regime="RANGE", entry=100, atr=1, score=80)
    choppy = propose_dynamic_tp_sl(symbol="DOGEUSDT", side="LONG", regime="CHOPPY_MARKET", entry=100, atr=1, score=70)

    assert 1.3 <= range_result.dynamic_exit_candidate["tp1_r"] <= 1.5
    assert range_result.trailing_candidate is False
    assert choppy.prefer_no_trade is True
    assert choppy.time_death_risk == "HIGH"
