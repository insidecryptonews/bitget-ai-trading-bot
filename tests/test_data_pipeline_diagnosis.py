from app.config import BotConfig
from app.data_pipeline_diagnosis import DataPipelineDiagnosis, DataPipelineDiagnosisSmokeTest, _PipelineSmokeDb


def test_data_pipeline_diagnosis_separates_real_benign_and_false_positive_duplicates():
    db = _PipelineSmokeDb()
    db.initialize()
    payload = DataPipelineDiagnosis(BotConfig(), db).build(hours=24)

    assert payload["final_recommendation"] == "NO LIVE"
    assert payload["dangerous_duplicate_status"] in {"WARNING", "BAD"}
    assert payload["conflicting_labels"] > 0
    assert payload["benign_minute_bucket_density"] > 0
    assert payload["audit_false_positive_status"] in {"OK", "WARNING"}
    assert payload["recommended_action"] in {"FIX_DATA_PIPELINE", "REFINE_AUDIT_BUCKETS", "REVIEW_DATA_PIPELINE", "KEEP_RESEARCH"}


def test_data_pipeline_diagnosis_smoke_test_passes():
    text = DataPipelineDiagnosisSmokeTest(BotConfig()).to_text()

    assert "DATA PIPELINE DIAGNOSIS SMOKE TEST START" in text
    assert "real_duplicate_detected: true" in text
    assert "benign_density_detected: true" in text
    assert "conflicting_labels_detected: true" in text
    assert "final_recommendation: NO LIVE" in text
    assert "result: PASS" in text
