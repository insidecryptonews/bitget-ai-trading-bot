from app.config import BotConfig
from app.relation_repair_audit import RelationRepairAudit, RelationRepairAuditSmokeTest, _RelationSmokeDb


def test_relation_repair_audit_classifies_orphans_and_never_takes_unsafe_actions():
    db = _RelationSmokeDb()
    db.initialize()
    payload = RelationRepairAudit(BotConfig(), db).build(hours=24)

    assert payload["final_recommendation"] == "NO LIVE"
    assert payload["orphan_path_metrics_total"] > 0
    assert payload["conflicting_labels"] > 0
    assert payload["relation_health_status"] in {"WARNING", "BAD"}
    assert "no_delete" in payload["unsafe_actions_not_taken"]
    assert payload["safe_fix_recommendations"]


def test_relation_repair_audit_smoke_test_passes():
    text = RelationRepairAuditSmokeTest(BotConfig()).to_text()

    assert "RELATION REPAIR AUDIT SMOKE TEST START" in text
    assert "orphan_path_metric_detected: true" in text
    assert "unsafe_actions_not_taken: true" in text
    assert "final_recommendation: NO LIVE" in text
    assert "result: PASS" in text
