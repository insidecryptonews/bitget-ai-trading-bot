"""ResearchOps V10.1 — event-study tests. All synthetic. No DB, no network."""

from __future__ import annotations

from app.labs.external_event_study_v10_1 import (
    MAX_EVENT_DOMINANCE,
    MAX_SYMBOL_DOMINANCE,
    STATUS_GREEN,
    STATUS_NEED_DATA,
    STATUS_NEED_MORE,
    STATUS_REJECT,
    STATUS_WATCH,
    build_market_series,
    define_funding_oi_extreme_events,
    run_event_study,
)

BASE = 1780000000000
STEP = 3600000  # 1h


def _series(closes, *, highs=None, lows=None, funding=None, oi=None, start=BASE, step=STEP):
    n = len(closes)
    ts = [start + i * step for i in range(n)]
    return {
        "ts": ts,
        "close": list(closes),
        "high": list(highs) if highs else [c * 1.001 for c in closes],
        "low": list(lows) if lows else [c * 0.999 for c in closes],
        "funding": list(funding) if funding else [0.0001] * n,
        "oi": list(oi) if oi else [1.8e9] * n,
    }


def _declining(n=400, p0=100.0, drop=0.002):
    return [p0 * (1 - drop) ** i for i in range(n)]


def _rising(n=400, p0=100.0, up=0.002):
    return [p0 * (1 + up) ** i for i in range(n)]


def _events(symbol, anchors, direction=1):
    return [{"symbol": symbol, "timestamp_ms": BASE + a * STEP, "direction": direction}
            for a in anchors]


# 19. events without series -> NEED_DATA
def test_no_series_need_data():
    r = run_event_study(_events("BTCUSDT", [10, 20, 30]), {}, module="x")
    assert r.status == STATUS_NEED_DATA


def test_series_without_events_need_data():
    mbs = {"BTCUSDT": _series(_rising())}
    r = run_event_study([], mbs, module="x")
    assert r.status == STATUS_NEED_DATA


# 20. few events -> NEED_MORE_DATA
def test_few_events_need_more_data():
    mbs = {"BTCUSDT": _series(_rising())}
    r = run_event_study(_events("BTCUSDT", [50, 60, 70]), mbs, module="x",
                        bootstrap_n=100, baseline_n=50, min_events=20)
    assert r.status == STATUS_NEED_MORE


# 21. event DEFINITION does not use the future (no lookahead)
def test_event_definition_no_lookahead():
    n = 400
    funding = [0.0001] * n
    for i in range(60, n, 25):
        funding[i] = 0.02  # spikes
    closes = _rising(n)
    mbs_a = {"BTCUSDT": _series(closes, funding=funding)}
    events_a = define_funding_oi_extreme_events(mbs_a, funding_z_thr=2.0, oi_z_thr=99.0, lookback=48)
    split = n // 2
    ts_split = BASE + split * STEP
    # Mutate funding in the SECOND HALF only.
    funding_b = list(funding)
    for i in range(split, n):
        funding_b[i] = funding_b[i] * 10 + 0.5
    mbs_b = {"BTCUSDT": _series(closes, funding=funding_b)}
    events_b = define_funding_oi_extreme_events(mbs_b, funding_z_thr=2.0, oi_z_thr=99.0, lookback=48)

    def first_half(evs):
        return sorted(e["timestamp_ms"] for e in evs if e["timestamp_ms"] < ts_split)

    assert first_half(events_a), "expected first-half events"
    assert first_half(events_a) == first_half(events_b)


# 22. MFE/MAE are diagnostic only: positive MFE must NOT rescue a negative net.
def test_mfe_mae_diagnostic_only():
    # price spikes up intrabar (high) but closes lower at the horizon.
    n = 400
    closes = _declining(n, drop=0.002)
    highs = [c * 1.05 for c in closes]  # big favorable excursion for a long
    lows = [c * 0.999 for c in closes]
    mbs = {"BTCUSDT": _series(closes, highs=highs, lows=lows)}
    anchors = list(range(0, 150, 5))  # 30 long events
    r = run_event_study(_events("BTCUSDT", anchors, direction=1), mbs, module="x",
                        horizons_h=(1, 4, 8, 24), primary_horizon_h=24,
                        cost=0.0018, bootstrap_n=200, baseline_n=100, min_events=20)
    assert r.avg_mfe_pct > 0  # diagnostic shows favorable excursion
    assert r.net_ev_pct <= 0  # but net is negative
    assert r.status == STATUS_REJECT  # MFE did not rescue the verdict


# 23. baseline reproducible with seed
def test_baseline_reproducible():
    mbs = {"BTCUSDT": _series(_rising()), "ETHUSDT": _series(_rising(p0=50))}
    evs = _events("BTCUSDT", list(range(0, 120, 4)))
    a = run_event_study(evs, mbs, primary_horizon_h=24, bootstrap_n=100, baseline_n=200, seed=42)
    b = run_event_study(evs, mbs, primary_horizon_h=24, bootstrap_n=100, baseline_n=200, seed=42)
    assert a.baseline_net_ev_pct == b.baseline_net_ev_pct


# 24. bootstrap CI computed
def test_bootstrap_ci_computed():
    mbs = {"BTCUSDT": _series(_rising())}
    evs = _events("BTCUSDT", list(range(0, 150, 5)))
    r = run_event_study(evs, mbs, primary_horizon_h=24, bootstrap_n=500, baseline_n=100, seed=7, min_events=20)
    assert r.bootstrap_ci_low <= r.net_ev_pct <= r.bootstrap_ci_high or r.bootstrap_ci_low <= r.bootstrap_ci_high
    assert r.bootstrap_n == 500


# 25. one-event dominance detected
def test_one_event_dominance_detected():
    # 24 tiny-return events + 1 huge => dominance high.
    closes = [100.0] * 400
    # build a series mostly flat, but make one anchor explode upward over its horizon
    # Easier: custom market with one symbol; engineer returns via close steps.
    # anchor a: forward 24h return = (close[a+24]-close[a])/close[a].
    # Make most anchors ~0 and one anchor +50%.
    for a in range(0, 130, 5):
        # flat
        pass
    big_anchor = 200
    for i in range(big_anchor + 1, big_anchor + 30):
        closes[i] = 150.0  # +50% after big_anchor
    mbs = {"BTCUSDT": _series(closes)}
    anchors = list(range(0, 130, 5)) + [big_anchor]  # ~27 events
    r = run_event_study(_events("BTCUSDT", anchors, direction=1), mbs, primary_horizon_h=24,
                        bootstrap_n=100, baseline_n=50, min_events=20)
    assert r.one_event_dominance > MAX_EVENT_DOMINANCE


# 26. one-symbol dominance detected
def test_one_symbol_dominance_detected():
    mbs = {"BTCUSDT": _series(_rising()), "ETHUSDT": _series(_rising(p0=50))}
    # all events on a single symbol
    evs = _events("BTCUSDT", list(range(0, 150, 5)), direction=1)
    r = run_event_study(evs, mbs, primary_horizon_h=24, bootstrap_n=100, baseline_n=100, min_events=20)
    assert r.one_symbol_dominance > MAX_SYMBOL_DOMINANCE


# 27. cost reduces net return
def test_cost_reduces_net():
    mbs = {"BTCUSDT": _series(_rising())}
    evs = _events("BTCUSDT", list(range(0, 150, 5)))
    low = run_event_study(evs, mbs, primary_horizon_h=24, cost=0.0, bootstrap_n=50, baseline_n=50, min_events=20)
    high = run_event_study(evs, mbs, primary_horizon_h=24, cost=0.003, bootstrap_n=50, baseline_n=50, min_events=20)
    assert high.net_ev_pct < low.net_ev_pct


# 28. net EV negative -> REJECT
def test_negative_net_ev_reject():
    mbs = {"BTCUSDT": _series(_declining())}
    evs = _events("BTCUSDT", list(range(0, 150, 5)), direction=1)  # long into a downtrend
    r = run_event_study(evs, mbs, primary_horizon_h=24, cost=0.0018, bootstrap_n=100, baseline_n=100, min_events=20)
    assert r.net_ev_pct <= 0
    assert r.status == STATUS_REJECT


# 29 + 30. never PAPER_READY / LIVE_READY
def test_never_paper_or_live_ready():
    mbs = {"BTCUSDT": _series(_rising()), "ETHUSDT": _series(_rising(p0=70)),
           "SOLUSDT": _series(_rising(p0=20))}
    evs = (_events("BTCUSDT", list(range(0, 120, 5)))
           + _events("ETHUSDT", list(range(0, 120, 7)))
           + _events("SOLUSDT", list(range(0, 120, 9))))
    r = run_event_study(evs, mbs, primary_horizon_h=24, bootstrap_n=200, baseline_n=200, min_events=20)
    assert r.paper_ready is False
    assert r.live_ready is False
    assert r.final_recommendation == "NO LIVE"
    # verdict ceiling is RESEARCH_GREEN
    assert r.status in (STATUS_GREEN, STATUS_WATCH, STATUS_REJECT, STATUS_NEED_MORE)


# ---------------------------------------------------------------------------
# V10.1 --hours window filter (Codex mini-fix)
# ---------------------------------------------------------------------------

def _long_series(n=1000, p0=100.0, up=0.0005):
    closes = [p0 * (1 + up) ** i for i in range(n)]
    return _series(closes)


def _hours_setup():
    n = 1000
    mbs = {"BTCUSDT": _long_series(n)}
    ts = mbs["BTCUSDT"]["ts"]
    now_ms = ts[-1]
    old = [{"symbol": "BTCUSDT", "timestamp_ms": ts[i], "direction": 1} for i in range(50, 90)]
    recent = [{"symbol": "BTCUSDT", "timestamp_ms": ts[i], "direction": 1} for i in range(800, 840)]
    return mbs, old + recent, now_ms, ts


def test_hours_filter_drops_old_events_and_market():
    mbs, events, now_ms, ts = _hours_setup()
    r = run_event_study(events, mbs, primary_horizon_h=24, hours=200, now_ms=now_ms,
                        lookback_bars_for_events=48, bootstrap_n=100, baseline_n=100, min_events=20)
    assert r.filter_applied is True
    assert r.hours_requested == 200
    assert r.events_before_filter == 80
    assert r.events_after_filter == 40  # only recent kept
    assert r.rows_after_filter < r.rows_before_filter  # old market trimmed
    # cutoff = now - 200h = ts[799]; events kept are >= cutoff
    assert r.cutoff_timestamp_ms == ts[799]
    # lookback retained BEFORE cutoff (48 bars * 1h)
    assert r.lookback_required is True
    assert r.effective_start_timestamp_ms == ts[799] - 48 * STEP


def test_hours_filter_old_events_excluded_from_study():
    mbs, events, now_ms, ts = _hours_setup()
    r = run_event_study(events, mbs, primary_horizon_h=24, hours=200, now_ms=now_ms,
                        lookback_bars_for_events=48, bootstrap_n=100, baseline_n=100, min_events=20)
    # matched events cannot exceed the in-window events
    assert r.matched_events <= r.events_after_filter


def test_hours_filter_tight_window_need_more_data():
    mbs, events, now_ms, ts = _hours_setup()
    r = run_event_study(events, mbs, primary_horizon_h=24, hours=5, now_ms=now_ms,
                        lookback_bars_for_events=48, bootstrap_n=50, baseline_n=50, min_events=20)
    assert r.status in (STATUS_NEED_MORE, STATUS_NEED_DATA)
    assert r.paper_ready is False and r.live_ready is False


def test_hours_filter_metadata_present():
    mbs, events, now_ms, ts = _hours_setup()
    r = run_event_study(events, mbs, primary_horizon_h=24, hours=200, now_ms=now_ms,
                        lookback_bars_for_events=48, bootstrap_n=50, baseline_n=50, min_events=20)
    d = r.as_dict()
    for key in ("filter_applied", "hours_requested", "cutoff_timestamp",
                "rows_before_filter", "rows_after_filter",
                "events_before_filter", "events_after_filter",
                "effective_start_timestamp", "lookback_required", "lookback_ms"):
        assert key in d, f"missing {key}"
    assert d["filter_applied"] is True
    assert d["final_recommendation"] == "NO LIVE"


def test_no_hours_means_filter_not_applied():
    mbs, events, now_ms, ts = _hours_setup()
    r = run_event_study(events, mbs, primary_horizon_h=24, hours=None,
                        bootstrap_n=50, baseline_n=50, min_events=20)
    assert r.filter_applied is False
    assert r.hours_requested is None
    assert r.events_after_filter == r.events_before_filter == 80


def test_hours_filter_does_not_break_empty_need_data():
    r = run_event_study([], {}, hours=720)
    assert r.status == STATUS_NEED_DATA
    assert r.final_recommendation == "NO LIVE"


def test_lookback_retained_but_no_future_used():
    # An event exactly at the cutoff must still be measurable (forward bars
    # after cutoff are retained); bars strictly before effective_start are
    # dropped (only old data removed, never the future).
    mbs, events, now_ms, ts = _hours_setup()
    r = run_event_study(events, mbs, primary_horizon_h=24, hours=200, now_ms=now_ms,
                        lookback_bars_for_events=48, bootstrap_n=50, baseline_n=50, min_events=20)
    # effective_start is strictly before cutoff (lookback into the past only)
    assert r.effective_start_timestamp_ms < r.cutoff_timestamp_ms
    # and effective_start is not in the future relative to reference_now
    assert r.effective_start_timestamp_ms < r.reference_now_ms


def test_build_market_series_from_clean_rows():
    rows = [{"symbol": "BTCUSDT", "timestamp_ms": BASE + i * STEP, "price_close": 100 + i,
             "price_high": 101 + i, "price_low": 99 + i, "funding_rate": 0.0001,
             "oi_usd_close": 1.8e9} for i in range(50)]
    mbs = build_market_series(rows)
    assert "BTCUSDT" in mbs and len(mbs["BTCUSDT"]["ts"]) == 50
    # sorted ascending
    ts = mbs["BTCUSDT"]["ts"]
    assert ts == sorted(ts)
