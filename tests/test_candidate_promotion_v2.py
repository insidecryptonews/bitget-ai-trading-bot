from app.candidate_promotion_v2 import promote_group


def test_candidate_promotion_market_probe_never_actionable():
    rows = [{"source": "market_probe", "return_pct": 0.5, "first_barrier_hit": "TP"} for _ in range(300)]

    result = promote_group(("DOGEUSDT", "SHORT", "TREND_DOWN", "85-89", "market_probe"), rows)

    assert result["state"] == "NEED_MORE_DATA_NOT_ACTIONABLE"
    assert result["paper_filter_enabled"] is False
    assert result["live_allowed"] is False


def test_candidate_promotion_low_sample_needs_more_data():
    rows = [{"source": "trade_signal", "return_pct": 0.5, "first_barrier_hit": "TP"} for _ in range(40)]

    result = promote_group(("DOGEUSDT", "SHORT", "TREND_DOWN", "85-89", "trade_signal"), rows)

    assert result["state"] == "NEED_MORE_DATA"


def test_candidate_promotion_bad_edge_rejected():
    rows = [{"source": "trade_signal", "return_pct": -0.5, "first_barrier_hit": "SL"} for _ in range(300)]

    result = promote_group(("SOLUSDT", "LONG", "RANGE", "90-94", "trade_signal"), rows)

    assert result["state"] == "REJECT_BAD_EDGE"


def test_candidate_promotion_blocks_missing_realized_returns():
    rows = [{"source": "trade_signal", "mfe": 5.0, "first_barrier_hit": "TP"} for _ in range(300)]

    result = promote_group(("SOLUSDT", "SHORT", "TREND_DOWN", "85-89", "trade_signal"), rows)

    assert result["state"] == "REJECT_INVALID_METRICS"
    assert result["paper_filter_enabled"] is False
