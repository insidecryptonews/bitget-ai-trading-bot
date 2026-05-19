from app.execution_safety import reconcile_pending_executions


class FakeDb:
    def fetch_pending_execution_intents(self):
        return [{"client_oid": "oid-1", "status": "PENDING_EXECUTION"}]


def test_pending_execution_blocks_without_reconcile_in_paper_safe_mode():
    result = reconcile_pending_executions(FakeDb(), mode="paper")

    assert result["status"] == "PENDING_REVIEW_REQUIRED"
    assert result["pending_count"] == 1
