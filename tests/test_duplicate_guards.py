from app.data_guards import exact_duplicate_observation_detector, stable_observation_fingerprint


def test_exact_observation_fingerprint_is_stable():
    row = {"timestamp": "2026-05-19T00:00:00+00:00", "symbol": "btcusdt", "side": "short", "score": 90, "source": "trade_signal", "market_regime": "risk_off"}

    assert stable_observation_fingerprint(row) == stable_observation_fingerprint(dict(row))


def test_duplicate_guard_detects_exact_duplicate_but_not_other_symbol():
    rows = [
        {"timestamp": "2026-05-19T00:00:00+00:00", "symbol": "BTCUSDT", "side": "SHORT", "score": 90, "source": "trade_signal", "market_regime": "RISK_OFF"},
        {"timestamp": "2026-05-19T00:00:00+00:00", "symbol": "BTCUSDT", "side": "SHORT", "score": 90, "source": "trade_signal", "market_regime": "RISK_OFF"},
        {"timestamp": "2026-05-19T00:00:00+00:00", "symbol": "ETHUSDT", "side": "SHORT", "score": 90, "source": "trade_signal", "market_regime": "RISK_OFF"},
    ]

    audit = exact_duplicate_observation_detector(rows)

    assert audit["exact_duplicate_count"] == 1
    assert audit["duplicate_guard_status"] == "WARNING"
