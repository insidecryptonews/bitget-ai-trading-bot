from app.config import BotConfig
from app.worker_health_audit import WorkerHealthAuditSmokeTest, _classify_simulated, _classify_stale


def test_worker_health_classifies_duplicate_and_stale_without_actions():
    # Single real Python app.main + lock owned → OK
    assert _classify_simulated(
        processes=["1234 python -m app.main"],
        lock={"lock_status": "owned", "acquired": True},
    ) == "OK"
    # Two distinct PIDs + blocked_duplicate → BAD (real conflict)
    assert _classify_simulated(
        processes=["1234 python -m app.main", "5678 python -m app.main"],
        lock={"lock_status": "blocked_duplicate", "acquired": False},
    ) == "BAD"
    # tmux/bash wrappers must be filtered out — single real worker = OK
    assert _classify_simulated(
        processes=[
            "1234 tmux new-session -d 'python -m app.main'",
            "1235 bash -c 'python -m app.main'",
            "1236 .venv/bin/python -m app.main",
        ],
        lock={"lock_status": "owned", "acquired": True},
    ) == "OK"
    assert _classify_stale(1800) == "WARNING"


def test_worker_health_audit_smoke_test_passes_and_stays_no_live():
    text = WorkerHealthAuditSmokeTest(BotConfig()).to_text()

    assert "WORKER HEALTH AUDIT SMOKE TEST START" in text
    assert "duplicate_worker_detected: true" in text
    assert "stale_last_scan_detected: true" in text
    assert "LIVE_TRADING=false" in text
    assert "DRY_RUN=true" in text
    assert "PAPER_TRADING=true" in text
    assert "final_recommendation: NO LIVE" in text
    assert "result: PASS" in text
