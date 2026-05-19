from datetime import datetime, timezone

from app.execution_safety import check_clock_drift


def test_clock_drift_ok_bad_and_unknown():
    local = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)

    assert check_clock_drift(local, datetime(2026, 5, 19, 12, 0, 1, tzinfo=timezone.utc))["clock_drift_status"] == "OK"
    assert check_clock_drift(local, datetime(2026, 5, 19, 12, 0, 10, tzinfo=timezone.utc))["clock_drift_status"] == "BAD"
    assert check_clock_drift(local, None)["clock_drift_status"] == "UNKNOWN"
