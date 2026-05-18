from app.config import BotConfig
from app.training_data_integrity import TrainingDataIntegrity, TrainingDataIntegritySmokeTest, _SmokeDb


def test_training_data_integrity_detects_duplicates_orphans_time_and_probe_separation():
    db = _SmokeDb()
    db.initialize()
    payload = TrainingDataIntegrity(BotConfig(), db).build(hours=24)

    assert payload["final_recommendation"] == "NO LIVE"
    assert payload["duplicate_status"] in {"WARNING", "BAD"}
    assert payload["orphan_labels"] > 0
    assert payload["relation_status"] == "BAD"
    assert payload["label_quality_status"] in {"WARNING", "BAD"}
    assert payload["mfe_mae_zero_rate"] > 0
    assert payload["market_probe_never_actionable"] is True


def test_training_data_integrity_smoke_test_passes_without_live():
    text = TrainingDataIntegritySmokeTest(BotConfig()).to_text()

    assert "TRAINING DATA INTEGRITY SMOKE TEST START" in text
    assert "duplicates_detected: true" in text
    assert "orphan_labels_detected: true" in text
    assert "market_probe_separated: true" in text
    assert "LIVE_TRADING=false" in text
    assert "DRY_RUN=true" in text
    assert "PAPER_TRADING=true" in text
    assert "final_recommendation: NO LIVE" in text
    assert "result: PASS" in text
