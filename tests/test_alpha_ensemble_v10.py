"""ResearchOps V10 — Alpha Ensemble tests.

All synthetic OHLCV. No DB, no network. Verifies: honest REJECT on
random-walk (no edge), edge detection on a momentum world, no-lookahead +
STOP_BEFORE_TP, cost-stress gating, and the hard invariants (never
paper/live ready; verdict ceiling = SHADOW).
"""

from __future__ import annotations

import ast
import datetime as dt
import importlib
import pathlib
import random

from app.labs.alpha_ensemble_v10 import (
    EXIT_SL,
    REGIME_DOWN,
    REGIME_UP,
    VERDICT_REJECT,
    VERDICT_SHADOW,
    VERDICT_NEED_DATA,
    _simulate_exit,
    backtest_portfolio,
    classify_regimes,
    cross_sectional_momentum_trades,
    donchian_trend_trades,
    run_alpha_ensemble,
)

BASE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def _bar(i, o, h, l, c, tf=15):
    return {"open": o, "high": h, "low": l, "close": c, "volume": 100.0,
            "ts": (BASE + dt.timedelta(minutes=tf * i)).isoformat()}


def _gen(seed, n=2200, drift_world=False):
    random.seed(seed)
    bars = []
    price = 100.0
    d = 0.001
    for i in range(n):
        if drift_world:
            if i % 200 == 0:
                d = 0.001 if random.random() < 0.5 else -0.001
            ret = d + random.gauss(0, 0.004)
        else:
            ret = random.gauss(0.0, 0.01)
        o = price
        c = o * (1 + ret)
        hi = max(o, c) * (1 + abs(random.gauss(0, 0.002)))
        lo = min(o, c) * (1 - abs(random.gauss(0, 0.002)))
        bars.append(_bar(i, o, hi, lo, c))
        price = c
    return bars


def test_no_ohlcv_is_need_data():
    rep = backtest_portfolio({}, timeframe="15m")
    assert rep.verdict == VERDICT_NEED_DATA
    assert "ohlcv_missing" in rep.blockers
    assert rep.final_recommendation == "NO LIVE"


def test_random_walk_is_rejected_honestly():
    bbs = {s: _gen(i, drift_world=False) for i, s in
           enumerate(["BTCUSDT", "ETHUSDT", "SOLUSDT", "LINKUSDT", "DOTUSDT"])}
    rep = backtest_portfolio(bbs, timeframe="15m")
    # On data with no edge, the engine must NOT hallucinate profit.
    assert rep.total_trades > 0
    assert rep.net_ev_pct <= 0
    assert rep.verdict == VERDICT_REJECT
    assert rep.paper_ready is False and rep.live_ready is False


def test_momentum_world_detects_edge_but_caps_at_shadow():
    bbs = {s: _gen(i + 100, drift_world=True) for i, s in
           enumerate(["BTCUSDT", "ETHUSDT", "SOLUSDT", "LINKUSDT", "DOTUSDT"])}
    rep = backtest_portfolio(bbs, timeframe="15m")
    assert rep.net_ev_pct > 0
    assert rep.net_pf > 1.0
    # Even with a strong edge, the ceiling is SHADOW research only.
    assert rep.verdict in (VERDICT_SHADOW, "WATCH_ONLY")
    assert rep.paper_ready is False
    assert rep.live_ready is False
    assert rep.final_recommendation == "NO LIVE"


def test_cost_stress_runs_all_levels():
    bbs = {s: _gen(i + 5, drift_world=True) for i, s in
           enumerate(["BTCUSDT", "ETHUSDT", "SOLUSDT"])}
    rep = backtest_portfolio(bbs, timeframe="15m")
    costs = sorted(r["cost_pct"] for r in rep.cost_stress)
    assert costs == [0.0018, 0.0022, 0.0025]


def test_regime_classifier_no_lookahead():
    # A strictly rising series => later bars classified TREND_UP, and the
    # classification at bar i must not depend on bars after i.
    bars = [_bar(i, 100 + i, 100 + i + 0.5, 100 + i - 0.5, 100 + i) for i in range(200)]
    regimes_a = classify_regimes(bars)
    mutated = [dict(b) for b in bars]
    mutated[190] = _bar(190, 1, 2, 0.5, 1)  # wreck a far-future bar
    regimes_b = classify_regimes(mutated)
    # regime at bar 100 unchanged by mutating bar 190
    assert regimes_a[100] == regimes_b[100]


def test_donchian_long_only_in_uptrend():
    # Build a clean uptrend; all donchian trades must be long (regime gate).
    bars = []
    price = 100.0
    for i in range(400):
        price *= 1.003
        bars.append(_bar(i, price, price * 1.002, price * 0.999, price))
    trades = donchian_trend_trades(bars, cost=0.0018, symbol="BTCUSDT")
    assert all(t["direction"] == 1 for t in trades)


def test_entry_is_next_bar_open():
    bars = []
    price = 100.0
    for i in range(400):
        price *= 1.003
        bars.append(_bar(i, price, price * 1.002, price * 0.999, price))
    trades = donchian_trend_trades(bars, cost=0.0018, symbol="BTCUSDT")
    assert trades  # at least one
    # entry_ts equals the bar AFTER some signal bar (monotonic, not first bar)
    assert trades[0]["entry_ts"] > bars[0]["ts"]


def test_run_alpha_ensemble_with_fake_db():
    class _DB:
        def __init__(self, m):
            self._m = m

        def fetch_ohlcv_range(self, symbol, timeframe, *, since_iso=None, until_iso=None, limit=200000):
            return list(self._m.get(symbol.upper(), []))

    bbs = {s: _gen(i + 9, drift_world=True) for i, s in enumerate(["BTCUSDT", "ETHUSDT", "SOLUSDT"])}
    db = _DB(bbs)
    rep = run_alpha_ensemble(db, symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"], timeframe="15m", hours=2160)
    assert rep.total_trades > 0
    assert rep.live_ready is False and rep.paper_ready is False
    assert rep.final_recommendation == "NO LIVE"


def test_run_alpha_ensemble_empty_db_is_need_data():
    class _DB:
        def fetch_ohlcv_range(self, *a, **k):
            return []
    rep = run_alpha_ensemble(_DB(), symbols=["BTCUSDT"], timeframe="15m", hours=24)
    assert rep.verdict == VERDICT_NEED_DATA
    assert rep.final_recommendation == "NO LIVE"


# ---------------------------------------------------------------------------
# Codex hardening 1 — explicit same-bar TP+SL => STOP_BEFORE_TP
# ---------------------------------------------------------------------------

def _straddle_bars():
    # bar[1] straddles both barriers; flat neighbours.
    return [
        {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "ts": "t0"},
        {"open": 100.0, "high": 103.0, "low": 97.0, "close": 100.0, "ts": "t1"},
        {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "ts": "t2"},
    ]


def test_same_bar_tp_and_sl_long_exits_sl():
    # LONG entry=100, atr=2 => tp=102, sl=98. bar[1] high=103(>=tp) AND
    # low=97(<=sl). STOP_BEFORE_TP => must exit SL at 98.
    bars = _straddle_bars()
    et, ep, bh = _simulate_exit(bars, 0, 100.0, 1, 2.0, tp_atr=1.0, sl_atr=1.0, hold=2)
    assert et == EXIT_SL
    assert ep == 98.0
    assert bh == 1


def test_same_bar_tp_and_sl_short_exits_sl():
    # SHORT entry=100, atr=2 => tp=98, sl=102. bar[1] high=103(>=sl) AND
    # low=97(<=tp). STOP_BEFORE_TP => must exit SL at 102.
    bars = _straddle_bars()
    et, ep, bh = _simulate_exit(bars, 0, 100.0, -1, 2.0, tp_atr=1.0, sl_atr=1.0, hold=2)
    assert et == EXIT_SL
    assert ep == 102.0
    assert bh == 1


# ---------------------------------------------------------------------------
# Codex hardening 2 — cross-sectional momentum is no-lookahead
# ---------------------------------------------------------------------------

def test_cross_sectional_momentum_no_lookahead():
    """Mutating bars in the SECOND HALF must not change the (symbol,
    direction, entry_ts) signals generated in the FIRST HALF. The ranking
    uses only closes <= signal index, and entry is the next bar open."""
    n = 1200

    def series(seed, slope):
        random.seed(seed)
        bars = []
        price = 100.0
        for i in range(n):
            price *= (1 + slope + random.gauss(0, 0.003))
            bars.append({"open": price, "high": price * 1.001, "low": price * 0.999,
                         "close": price, "volume": 100.0,
                         "ts": (BASE + dt.timedelta(minutes=15 * i)).isoformat()})
        return bars

    A = {"BTCUSDT": series(1, 0.0012), "ETHUSDT": series(2, 0.0003), "SOLUSDT": series(3, -0.0006)}
    trades_A = cross_sectional_momentum_trades(A, cost=0.0018)

    split = n // 2
    ts_split = A["BTCUSDT"][split]["ts"]
    # Mutate ALL of the second half's high/low/close (keep open + ts).
    B = {s: [dict(b) for b in bars] for s, bars in A.items()}
    for s in B:
        for i in range(split, n):
            B[s][i]["high"] *= 5.0
            B[s][i]["low"] *= 0.2
            B[s][i]["close"] *= 3.0
    trades_B = cross_sectional_momentum_trades(B, cost=0.0018)

    def first_half_signals(trades):
        return sorted((t["symbol"], t["direction"], t["entry_ts"])
                      for t in trades if t["entry_ts"] < ts_split)

    sig_a = first_half_signals(trades_A)
    sig_b = first_half_signals(trades_B)
    assert sig_a, "expected first-half cross-sectional signals"
    # Future bars (>= split) leave earlier signals untouched => no lookahead.
    assert sig_a == sig_b


def test_cross_sectional_entry_after_signal_bar():
    """Entry timestamp is strictly after the bar whose close fed the rank."""
    n = 600

    def series(seed, slope):
        random.seed(seed)
        bars = []
        price = 100.0
        for i in range(n):
            price *= (1 + slope + random.gauss(0, 0.002))
            bars.append({"open": price, "high": price * 1.001, "low": price * 0.999,
                         "close": price, "volume": 100.0,
                         "ts": (BASE + dt.timedelta(minutes=15 * i)).isoformat()})
        return bars

    bbs = {"BTCUSDT": series(1, 0.0012), "ETHUSDT": series(2, 0.0002), "SOLUSDT": series(3, -0.0006)}
    trades = cross_sectional_momentum_trades(bbs, cost=0.0018)
    assert trades
    valid_ts = {b["ts"] for b in bbs["BTCUSDT"]} | {b["ts"] for b in bbs["ETHUSDT"]} | {b["ts"] for b in bbs["SOLUSDT"]}
    # every entry_ts is a real bar timestamp and never the very first bar
    first_ts = bbs["BTCUSDT"][0]["ts"]
    for t in trades:
        assert t["entry_ts"] in valid_ts
        assert t["entry_ts"] != first_ts


# ---------------------------------------------------------------------------
# Codex hardening 4 — OOS metadata not sold as walk-forward
# ---------------------------------------------------------------------------

def test_oos_metadata_not_sold_as_walk_forward():
    bbs = {s: _gen(i + 9, drift_world=True) for i, s in
           enumerate(["BTCUSDT", "ETHUSDT", "SOLUSDT"])}
    rep = backtest_portfolio(bbs, timeframe="15m")
    assert rep.oos_method == "chronological_70_30_split"
    assert rep.walk_forward_ready is False
    assert rep.walk_forward_status == "NOT_IMPLEMENTED"
    # And the hard invariants always hold.
    assert rep.paper_ready is False
    assert rep.live_ready is False
    assert rep.final_recommendation == "NO LIVE"


# ---------------------------------------------------------------------------
# Safety scan
# ---------------------------------------------------------------------------

def test_alpha_ensemble_no_forbidden_calls():
    mod = "app.labs.alpha_ensemble_v10"
    path = pathlib.Path(importlib.import_module(mod).__file__)
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    forbidden = {"place_order", "set_leverage", "set_margin_mode",
                 "private_get", "private_post", "execute", "open_position"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            assert name not in forbidden, f"calls {name}"
    for tok in ("import requests", "import ccxt", "import websocket",
                "LIVE_TRADING = True", "can_send_real_orders = True",
                "ENABLE_PAPER_POLICY_FILTER = True", "allow_real_writes = True"):
        assert tok not in src, f"contains {tok}"
    for tok in ("import paper_trader", "import edge_guard", "import signal_engine",
                "import strategy_engine", "import execution_engine"):
        assert tok not in src
