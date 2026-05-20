import math

from app.quant_metrics import annualization_factor, bps_to_fraction, profit_factor_with_status, sample_reliability


def test_bps_to_fraction():
    assert bps_to_fraction(12) == 0.0012


def test_sharpe_annualization_for_5m_crypto():
    result = annualization_factor("5m")

    assert result["factor"] == math.sqrt(365 * 24 * 12)
    assert result["sharpe_status"] == "OK_CRYPTO_5M"


def test_unknown_timeframe_does_not_sell_sharpe():
    assert annualization_factor("weird")["sharpe_status"] == "UNKNOWN_TIMEFRAME"


def test_pf_inf_low_sample_not_strong_edge():
    result = profit_factor_with_status([0.5] * 5)

    assert math.isinf(result["profit_factor"])
    assert result["pf_status"] == "INSUFFICIENT_LOSSES"
    assert result["metric_reliability"] == "LOW_SAMPLE"


def test_sample_thresholds():
    assert sample_reliability(50) == "LOW_SAMPLE"
    assert sample_reliability(300) == "NO_LIVE_READINESS"
