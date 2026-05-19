from app.structural_stop import calculate_structural_stop


def test_atr_fallback_uses_1_4_not_nearest_atr():
    result = calculate_structural_stop(side="LONG", entry=100, atr=1, support=0, regime="TREND_UP")

    assert abs(result.stop_loss - 98.6) < 1e-9
    assert result.stop_quality == "ATR_FALLBACK"


def test_structure_with_buffer_has_priority():
    long_result = calculate_structural_stop(side="LONG", entry=100, atr=1, support=98.5, regime="TREND_UP")
    short_result = calculate_structural_stop(side="SHORT", entry=100, atr=1, resistance=101.5, regime="TREND_DOWN")

    assert long_result.stop_loss < 98.5
    assert short_result.stop_loss > 101.5
    assert long_result.stop_quality == "STRUCTURAL_VALID"
    assert short_result.stop_quality == "STRUCTURAL_VALID"


def test_choppy_raises_whipsaw_risk():
    result = calculate_structural_stop(side="LONG", entry=100, atr=1, support=98.5, regime="CHOPPY_MARKET")

    assert result.stop_quality == "CHOPPY_WHIPSAW_RISK"
    assert result.whipsaw_risk >= 0.85
