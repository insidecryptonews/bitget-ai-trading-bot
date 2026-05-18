from app.config import BotConfig
from app.score_calibration import ScoreCalibration
from app.score_calibration_smoke_test import _FakeScoreDb, score_calibration_smoke_text


def test_score_calibration_detects_non_monotonic_high_score_failure():
    payload = ScoreCalibration(BotConfig(), _FakeScoreDb()).build(hours=24)

    assert payload["final_recommendation"] == "NO LIVE"
    assert payload["overall_score_quality"] in {"BAD", "MIXED"}
    assert payload["flags"]["score_not_monotonic"] is True
    assert payload["flags"]["high_score_negative_net_EV"] is True
    assert payload["high_score_failures"]
    assert payload["penalty_suggestions"][0]["action"] == "SHADOW_ONLY_DO_NOT_APPLY"


def test_score_calibration_smoke_test_passes_and_stays_no_live():
    text = score_calibration_smoke_text(BotConfig())

    assert "SCORE CALIBRATION SMOKE TEST START" in text
    assert "score_not_monotonic: true" in text
    assert "long_bad_side: true" in text
    assert "final_recommendation: NO LIVE" in text
    assert "paper_filter_enabled: false" in text
    assert "result: PASS" in text
