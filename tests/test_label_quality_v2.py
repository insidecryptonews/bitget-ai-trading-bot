from app.config import BotConfig
from app.label_quality_v2 import LabelQualityV2, LabelQualityV2SmokeTest, _LabelQualitySmokeDb


def test_label_quality_v2_detects_missed_tp_sl_and_time_mismatch():
    db = _LabelQualitySmokeDb()
    db.initialize()
    payload = LabelQualityV2(BotConfig(), db).build(hours=24)

    assert payload["final_recommendation"] == "NO LIVE"
    assert payload["missed_tp_labels"] > 0
    assert payload["missed_sl_labels"] > 0
    assert payload["inconsistent_time_labels"] > 0
    assert payload["path_metric_label_mismatch"] > 0
    assert payload["label_quality_status"] in {"WARNING", "BAD"}


def test_label_quality_v2_smoke_test_passes():
    text = LabelQualityV2SmokeTest(BotConfig()).to_text()

    assert "LABEL QUALITY V2 SMOKE TEST START" in text
    assert "missed_tp_detected: true" in text
    assert "missed_sl_detected: true" in text
    assert "time_consistency_checked: true" in text
    assert "final_recommendation: NO LIVE" in text
    assert "result: PASS" in text
