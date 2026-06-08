"""ResearchOps V10 — Funding / OI / Liquidation research tests.

All synthetic. No DB. No network.
"""

from __future__ import annotations

import datetime as dt

from app.labs.funding_oi_liquidation_research_v10 import (
    DECISION_IMPLEMENT_FIRST,
    DECISION_NEED_DATA,
    DECISION_WATCH_ONLY,
    analyze_funding_oi_liquidation,
    run_funding_oi_liquidation_research,
)

BASE = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)


def _funding(i, rate, sym="BTCUSDT"):
    return {
        "data_type": "funding", "symbol": sym,
        "timestamp": (BASE + dt.timedelta(hours=i)).isoformat(),
        "source": "csv", "source_reliability": 0.9, "funding_rate": rate,
    }


def test_no_data_is_need_data():
    r = run_funding_oi_liquidation_research(hours=24, external_data_path=None)
    assert r.decision == DECISION_NEED_DATA
    assert r.final_recommendation == "NO LIVE"
    assert r.can_send_real_orders is False
    assert r.paper_filter_enabled is False
    assert "funding_rate" in r.required_data_missing


def test_funding_extreme_detected_but_few_events_watch_only():
    rows = [_funding(i, 0.0001) for i in range(12)] + [_funding(20, 0.02)]
    r = analyze_funding_oi_liquidation(rows, hours=24, source_label="csv:test")
    assert r.funding_extreme_events >= 1
    assert r.crowded_long_flush_events >= 1
    # one event is below the study threshold => WATCH_ONLY (not actionable)
    assert r.decision == DECISION_WATCH_ONLY
    assert r.final_recommendation == "NO LIVE"


def test_many_extreme_events_implement_first_research():
    rows = []
    # Five symbols, each a flat baseline plus ONE strong funding spike. A
    # single large outlier per symbol yields one extreme event each (adding
    # more spikes inflates the std and suppresses the z-score), so five
    # symbols => >= 5 events => event-study viable.
    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "LINKUSDT", "DOTUSDT"):
        rows += [_funding(i, 0.0001, sym=s) for i in range(12)]
        rows += [_funding(20, 0.05, sym=s)]
    r = analyze_funding_oi_liquidation(rows, hours=24, source_label="csv:test")
    assert r.event_count >= 5
    assert r.event_study_ready is True
    assert r.decision == DECISION_IMPLEMENT_FIRST
    # research scaffold never claims it can backtest yet (honest)
    assert r.backtest_ready is False
    assert r.final_recommendation == "NO LIVE"


def test_nan_rows_excluded_from_analysis():
    rows = [_funding(i, 0.0001) for i in range(10)]
    rows.append({"data_type": "funding", "symbol": "BTCUSDT",
                 "timestamp": (BASE + dt.timedelta(hours=30)).isoformat(),
                 "source": "csv", "source_reliability": 0.9,
                 "funding_rate": float("inf")})
    r = analyze_funding_oi_liquidation(rows, hours=24)
    # inf row excluded => only 10 valid funding points
    assert r.funding_points == 10
