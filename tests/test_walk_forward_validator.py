from app.walk_forward_validator import validate_group


def test_walk_forward_rejects_overfit_train_only():
    rows = [
        {"symbol": "SOLUSDT", "side": "LONG", "market_regime": "RANGE", "score_bucket": "90-94", "strategy": "breakout", "source": "trade_signal", "return_pct": 0.5 if i < 450 else -0.6, "first_barrier_hit": "TP" if i < 450 else "SL", "timestamp": f"2026-01-01T{i//60:02d}:{i%60:02d}:00+00:00"}
        for i in range(900)
    ]

    result = validate_group(rows)

    assert result["decision"] in {"OVERFIT_REJECT", "REJECT"}


def test_walk_forward_low_sample_needs_more_data():
    rows = [{"return_pct": 0.5, "first_barrier_hit": "TP", "timestamp": f"2026-01-01T00:{i:02d}:00+00:00"} for i in range(40)]

    assert validate_group(rows)["decision"] == "NEED_MORE_DATA"
