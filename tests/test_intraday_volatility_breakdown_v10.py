"""ResearchOps V10 — Intraday Volatility Breakdown tests.

All synthetic OHLCV. No real exchange data. Verifies the no-lookahead +
STOP_BEFORE_TP invariants and the cost/coverage gates.
"""

from __future__ import annotations

import datetime as dt

from app.labs.intraday_volatility_breakdown_v10 import (
    DECISION_NEED_DATA,
    DECISION_REJECT,
    EXIT_SL,
    MIN_TRADES,
    VolatilityRuleResult,
    _aggregate,
    run_intraday_volatility_breakdown,
    simulate_rule_for_symbol,
)

BASE = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)


def _mk(i, o, h, l, c, *, base=BASE, tf_min=5):
    return {
        "timestamp": (base + dt.timedelta(minutes=tf_min * i)).isoformat(),
        "open": o, "high": h, "low": l, "close": c, "volume": 100.0,
    }


def _compression_breakout_series():
    """50 high-vol bars, then deep compression, a small breakout at bar 70,
    and a straddle bar at 71 (both SL and TP touched)."""
    bars = []
    for i in range(50):
        bars.append(_mk(i, 100, 120, 80, 100))      # high volatility
    for i in range(50, 70):
        bars.append(_mk(i, 100, 100.5, 99.5, 100))  # compression
    bars.append(_mk(70, 100, 100.7, 99.5, 100.7))   # small breakout LONG
    bars.append(_mk(71, 100.7, 102, 99, 100.5))      # straddle: hits SL and TP
    for i in range(72, 80):
        bars.append(_mk(i, 100.5, 100.6, 100.4, 100.5))
    return bars


def test_stop_before_tp_same_bar():
    bars = _compression_breakout_series()
    trades = simulate_rule_for_symbol(bars, hold_bars=5, tp_atr=1.0, sl_atr=1.0, cost_pct=0.2)
    assert len(trades) >= 1
    # The straddle bar touches both SL and TP; SL must win (conservative).
    assert trades[0]["exit_type"] == EXIT_SL


def test_no_lookahead_signal_independent_of_future_bars():
    """Mutating bars strictly AFTER the entry must not change the entry
    decision (signal uses only data <= signal bar)."""
    bars = _compression_breakout_series()
    trades_a = simulate_rule_for_symbol(bars, hold_bars=3, tp_atr=2.0, sl_atr=2.0, cost_pct=0.1)
    mutated = [dict(b) for b in bars]
    # Drastically change a far-future bar (index 78) — must not create or
    # destroy the signal at bar 70.
    mutated[78] = _mk(78, 999, 1500, 1, 999)
    trades_b = simulate_rule_for_symbol(mutated, hold_bars=3, tp_atr=2.0, sl_atr=2.0, cost_pct=0.1)
    assert len(trades_a) == len(trades_b)


def test_entry_is_next_bar_open_not_signal_bar():
    bars = _compression_breakout_series()
    trades = simulate_rule_for_symbol(bars, hold_bars=5, tp_atr=1.0, sl_atr=1.0, cost_pct=0.2)
    # entry == open of bar 71 == 100.7; with SL exit at entry-atr the gross
    # is clearly negative for a LONG, confirming entry side/price.
    assert trades[0]["direction"] == 1
    assert trades[0]["gross_pct"] < 0


class _FakeDB:
    def __init__(self, rows_by_symbol):
        self._rows = rows_by_symbol

    def fetch_ohlcv_range(self, symbol, timeframe, *, since_iso=None, until_iso=None, limit=200000):
        return list(self._rows.get(symbol.upper(), []))


def test_missing_ohlcv_is_need_data():
    db = _FakeDB({})
    r = run_intraday_volatility_breakdown(db, symbols=["BTCUSDT"], timeframe="5m", hours=168)
    assert r.decision == DECISION_NEED_DATA
    assert "ohlcv_missing" in r.blockers
    assert r.final_recommendation == "NO LIVE"


def test_stale_ohlcv_blocks():
    # Bars far in the past => stale => blocked, NEED_DATA.
    old_base = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
    rows = [_mk(i, 100, 101, 99, 100, base=old_base) for i in range(60)]
    db = _FakeDB({"BTCUSDT": rows})
    r = run_intraday_volatility_breakdown(db, symbols=["BTCUSDT"], timeframe="5m", hours=999999)
    assert r.freshness_status == "STALE"
    assert "ohlcv_stale" in r.blockers
    assert r.decision == DECISION_NEED_DATA


def test_aggregate_negative_ev_is_reject():
    # >= MIN_TRADES trades, all net-negative => REJECT.
    trades = [{"exit_type": "SL", "gross_pct": -0.5, "net_pct": -0.7,
               "bars_held": 3, "direction": 1, "mfe_pct": 0.1, "mae_pct": -0.6}
              for _ in range(MIN_TRADES + 5)]
    res = _aggregate({"BTCUSDT": trades}, rule_id="x", timeframe="5m",
                     hold_bars=5, tp_atr=1.0, sl_atr=1.0, cost_pct=0.2)
    assert isinstance(res, VolatilityRuleResult)
    assert res.net_ev_pct <= 0
    assert res.decision == DECISION_REJECT
    assert res.cost_stress_status == "FAIL"


def test_aggregate_low_pf_is_reject():
    # Mix giving positive-ish EV but PF < 1.2 should not be RESEARCH_POCKET.
    trades = []
    for i in range(MIN_TRADES + 10):
        if i % 2 == 0:
            trades.append({"exit_type": "TP", "gross_pct": 0.5, "net_pct": 0.3,
                           "bars_held": 4, "direction": 1, "mfe_pct": 0.6, "mae_pct": -0.1})
        else:
            trades.append({"exit_type": "SL", "gross_pct": -0.5, "net_pct": -0.32,
                           "bars_held": 4, "direction": 1, "mfe_pct": 0.1, "mae_pct": -0.6})
    res = _aggregate({"BTCUSDT": trades}, rule_id="x", timeframe="5m",
                     hold_bars=5, tp_atr=1.0, sl_atr=1.0, cost_pct=0.2)
    # net EV slightly negative here => REJECT; the point is it never reaches
    # a research pocket on weak economics.
    assert res.decision in (DECISION_REJECT, "WATCH_ONLY")


def test_insufficient_trades_is_need_data():
    trades = [{"exit_type": "TP", "gross_pct": 1.0, "net_pct": 0.8,
               "bars_held": 3, "direction": 1, "mfe_pct": 1.1, "mae_pct": -0.1}
              for _ in range(5)]
    res = _aggregate({"BTCUSDT": trades}, rule_id="x", timeframe="5m",
                     hold_bars=5, tp_atr=1.0, sl_atr=1.0, cost_pct=0.2)
    assert res.decision == DECISION_NEED_DATA
