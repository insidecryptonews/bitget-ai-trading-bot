from app.pre_move_intelligence_v2 import _decision, classify_pre_move_event
from app.operational_intelligence_utils import edge_metrics


def test_pre_move_false_positive_rejected():
    rows = [{"side": "LONG", "market_regime": "CHOPPY_MARKET", "mfe": 0.8, "mae": 1.5, "return_pct": -0.5, "first_barrier_hit": "SL"} for _ in range(300)]

    assert classify_pre_move_event(rows[0])["quality"] in {"FAKEOUT", "CHOPPY_NOISE", "DIRTY_MOVE"}
    assert _decision(edge_metrics(rows), 0.8, "trade_signal") == "REJECT"


def test_clean_short_and_long_are_separate():
    long_event = classify_pre_move_event({"side": "LONG", "market_regime": "TREND_UP", "mfe": 1.4, "mae": 0.2})
    short_event = classify_pre_move_event({"side": "SHORT", "market_regime": "TREND_DOWN", "mfe": 1.4, "mae": 0.2})

    assert long_event["event_type"] != short_event["event_type"]
