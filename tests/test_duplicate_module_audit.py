from app.duplicate_module_audit import build_duplicate_module_audit


def test_duplicate_module_audit_documents_without_moving():
    payload = build_duplicate_module_audit()

    assert "backtester" in payload["groups"]
    assert payload["files_moved"] is False
    assert payload["historical_data_modified"] is False
    assert payload["final_recommendation"] == "NO LIVE"
