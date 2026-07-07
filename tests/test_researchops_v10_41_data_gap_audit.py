"""V10.41 Data Gap Audit: measures dataset continuity, research-only."""

from __future__ import annotations

from app.labs import data_gap_audit_v10_41 as DGA

T0 = 1_700_000_000_000
BAR = 60_000


def _bar(ts):
    return {"ts": ts, "close": 100.0, "open": 100.0, "high": 100.0, "low": 100.0}


def test_contiguous_dataset_is_full_coverage():
    bars = [_bar(T0 + i * BAR) for i in range(200)]        # perfectly contiguous
    r = DGA.audit("SYN", bars=bars)
    assert r["coverage_ratio"] == 1.0
    assert r["n_gaps"] == 0
    assert r["max_contiguous_run_bars"] == 200
    assert r["fit_for_fine_backtest"] is True
    assert r["verdict"] == "CONTINUOUS_ENOUGH"
    assert r["final_recommendation"] == "NO LIVE"


def test_gappy_dataset_is_flagged():
    # 150 contiguous, then 40 bars each exactly 5 min apart -> low coverage
    bars = [_bar(T0 + i * BAR) for i in range(150)]
    last = T0 + 149 * BAR
    for k in range(1, 41):
        bars.append(_bar(last + k * 5 * BAR))
    r = DGA.audit("SYN", bars=bars)
    assert r["n_gaps"] == 40
    assert r["max_gap_min"] == 5
    assert r["coverage_ratio"] < 0.95
    assert r["gap_cause_estimate"]["rest_cadence_like_le10min"] == 40
    assert r["max_contiguous_run_bars"] == 150


def test_pc_off_like_large_gap_classified():
    bars = [_bar(T0 + i * BAR) for i in range(100)]
    bars.append(_bar(T0 + 100 * BAR + 120 * BAR))          # +2h gap = PC off-like
    r = DGA.audit("SYN", bars=bars)
    assert r["gap_cause_estimate"]["pc_off_like_ge60min"] == 1
    assert r["max_gap_min"] >= 60


def test_no_data_is_honest():
    assert DGA.audit("SYN", bars=[])["verdict"] == "NO_DATA"
    assert DGA.audit("SYN", bars=[_bar(T0)])["verdict"] == "NO_DATA"
