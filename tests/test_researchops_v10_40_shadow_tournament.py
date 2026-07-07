"""V10.40 Shadow Simulation Tournament: no-lookahead, cost-aware, baselines,
20 EUR fake bankroll, trailing, idempotent, never actionable / NO LIVE."""

from __future__ import annotations

import random
from pathlib import Path

from app.labs import shadow_simulation_tournament_v10_40 as SH

T0 = 1_700_000_000_000
BAR = 60_000


def _bar(ts, o, h, l, c):
    return dict(ts=ts, symbol="BTCUSDT", open=o, high=h, low=l, close=c,
                volume=1, buy_volume=1, sell_volume=0, n_trades=1, max_trade=1,
                available_at=ts)


def edge_bars(n, seed=1, every=12, planted=True):
    rng = random.Random(seed)
    price, pump, bars = 100.0, 0, []
    for i in range(n):
        sig = planted and i % every == 0 and i > 0
        ntr = 500 if sig else int(rng.uniform(15, 35) + rng.uniform(15, 35))
        if sig:
            pump = 3
        drift = 0.006 if pump > 0 else rng.uniform(-0.0012, 0.0012)
        pump = max(0, pump - 1)
        new = price * (1 + drift)
        b = _bar(T0 + i * BAR, price, max(price, new) * 1.0005,
                 min(price, new) * 0.9995, new)
        b.update(volume=ntr, buy_volume=ntr * (0.9 if sig else 0.5),
                 sell_volume=ntr * (0.1 if sig else 0.5), n_trades=ntr, max_trade=ntr / 5)
        bars.append(b)
        price = new
    return bars


# ---- trade simulator contracts ----------------------------------------------

def test_simulate_trade_tp_sl_time_and_costs_subtract():
    # long, first future bar hits TP (high>=100.6)
    bars = [_bar(T0, 100, 100, 100, 100),
            _bar(T0 + BAR, 100.2, 100.7, 100.0, 100.6)]
    cheap = SH.simulate_trade(bars, 0, "long", 0.006, 0.02, 30, None,
                              costs={"fee_bps": 0, "slippage_bps": 0, "spread_bps": 0})
    dear = SH.simulate_trade(bars, 0, "long", 0.006, 0.02, 30, None,
                             costs={"fee_bps": 50, "slippage_bps": 50, "spread_bps": 10})
    assert cheap["exit_reason"] == "TP" and cheap["hit_tp"] is True
    assert cheap["gross_return"] == dear["gross_return"]        # gross unchanged
    assert dear["net_return"] < cheap["net_return"]             # costs subtract
    assert cheap["net_return"] == cheap["gross_return"]         # zero-cost case


def test_simulate_trade_sl_and_short_side():
    # short: first bar spikes UP through SL (entry*(1+sl))
    bars = [_bar(T0, 100, 100, 100, 100),
            _bar(T0 + BAR, 100, 100.5, 99.9, 100.4)]
    o = SH.simulate_trade(bars, 0, "short", 0.006, 0.002, 30, None,
                          costs={"fee_bps": 0, "slippage_bps": 0, "spread_bps": 0})
    assert o["exit_reason"] == "SL" and o["hit_sl"] is True
    assert abs(o["gross_return"] - (-0.002)) < 1e-9             # short SL = -sl_pct


def test_data_gap_first_bar_is_invalid_not_a_win():
    # first future bar is already >2 bars away -> cannot step even once
    bars = [_bar(T0, 100, 100, 100, 100),
            _bar(T0 + 7 * BAR, 100, 105, 100, 104)]
    o = SH.simulate_trade(bars, 0, "long", 0.006, 0.02, 30, None)
    assert o["exit_reason"] == "DATA_GAP"
    assert o["valid"] is False and o["net_return"] == 0.0


def test_gap_mid_trade_becomes_stale_exit_valid():
    # one good bar, then a gap -> close at last contiguous price (STALE_EXIT)
    bars = [_bar(T0, 100, 100, 100, 100),
            _bar(T0 + BAR, 100, 100.1, 99.9, 100.05),          # contiguous, no barrier
            _bar(T0 + 7 * BAR, 100, 105, 100, 104)]            # gap here
    o = SH.simulate_trade(bars, 0, "long", 0.02, 0.02, 30, None,
                          costs={"fee_bps": 0, "slippage_bps": 0, "spread_bps": 0})
    assert o["exit_reason"] == "STALE_EXIT" and o["valid"] is True
    assert o["exit_price"] == 100.05 and o["bars_held"] == 1


def test_no_lookahead_outcome_only_uses_future_window():
    bars = [_bar(T0 + i * BAR, 100, 100.05, 99.95, 100.0) for i in range(40)]
    base = SH.simulate_trade(bars, 5, "long", 0.02, 0.02, 10, None)
    # mutating a bar OUTSIDE the [i+1, i+10] window must not change the outcome
    far = [dict(b) for b in bars]
    for b in far[20:]:
        b["high"] *= 5
    assert SH.simulate_trade(far, 5, "long", 0.02, 0.02, 10, None) == base
    # mutating INSIDE the window (bar 6) DOES change it (TP now reachable)
    near = [dict(b) for b in bars]
    near[6]["high"] = 130
    changed = SH.simulate_trade(near, 5, "long", 0.02, 0.02, 10, None)
    assert changed["exit_reason"] == "TP" and changed != base


def test_trailing_stop_activates_and_exits():
    bars = [_bar(T0, 100, 100, 100, 100),
            _bar(T0 + BAR, 100, 103.0, 101.5, 102.8),          # peak 103, activates
            _bar(T0 + 2 * BAR, 102.8, 103.2, 100.5, 100.8)]    # retrace -> TRAIL exit
    o = SH.simulate_trade(bars, 0, "long", 0.10, 0.05, 30, 0.02)
    assert o["trail_activated"] is True and o["trail_exit"] is True
    assert o["exit_reason"] == "TRAIL"


# ---- bankroll ---------------------------------------------------------------

def test_bankroll_20eur_computes_and_never_defensible():
    bk = SH.bankroll_sim([0.01, -0.005, 0.008, -0.02, 0.003])
    assert bk["start_eur"] == 20.0
    for prof, v in bk["profiles"].items():
        assert v["final_eur"] >= 0
        assert v["n_trades"] == 5
        assert v["statistically_defensible"] is False
    # bigger position fraction moves equity more from start than the tiny one
    assert abs(bk["profiles"]["ultra_gamble"]["final_eur"] - 20) >= \
        abs(bk["profiles"]["conservative"]["final_eur"] - 20)


# ---- tournament orchestration ----------------------------------------------

def test_tournament_includes_baselines_and_ranks_by_lower_bound():
    rep = SH.run_tournament("SYN", bars=edge_bars(700, seed=3), write_reports=False)
    names = {m["policy"] for m in rep["scoreboard_top"]}
    board = rep["scoreboard_top"]
    # baselines must exist among all policies (scoreboard_top may truncate) -> check total
    assert rep["policies_total"] >= 14
    # ranking is by net_EV_lower_bound descending (None sorts last)
    lbs = [m["net_EV_lower_bound"] for m in board if m["net_EV_lower_bound"] is not None]
    assert lbs == sorted(lbs, reverse=True)
    assert rep["ranking_key"].startswith("net_EV_lower_bound")


def test_small_sample_never_promoted_to_shadow_forward():
    rep = SH.run_tournament("SYN", bars=edge_bars(300, seed=5), write_reports=False)
    for m in rep["scoreboard_top"]:
        if not m.get("sample_sufficient"):
            assert m["verdict"] != "SHADOW_FORWARD"


def test_tournament_is_never_actionable_and_sends_nothing():
    rep = SH.run_tournament("SYN", bars=edge_bars(600, seed=7), write_reports=False)
    assert rep["micro_live_ready"] is False
    assert rep["can_send_real_orders"] is False
    assert rep["sends_orders"] is False
    assert rep["final_recommendation"] == "NO LIVE"
    for banned in SH.FORBIDDEN_OUTPUTS:
        assert banned not in str(rep)


def test_execution_rehearsal_sends_nothing():
    r = SH.execution_rehearsal()
    assert r["real_executor_exists"] is False and r["sends_orders"] is False
    assert "no_bitget_credentials" in r["blockers_before_any_micro_live"]


def test_duplicate_run_is_deterministic():
    bars = edge_bars(600, seed=9)
    a = SH.run_tournament("SYN", bars=bars, write_reports=False)
    b = SH.run_tournament("SYN", bars=bars, write_reports=False)
    assert [m["policy"] for m in a["scoreboard_top"]] == [m["policy"] for m in b["scoreboard_top"]]
    assert [m["n_signals"] for m in a["scoreboard_top"]] == [m["n_signals"] for m in b["scoreboard_top"]]


def test_tournament_writes_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(SH.CE, "_repo_root", lambda: tmp_path)
    SH.run_tournament("SYN", bars=edge_bars(600, seed=3), write_reports=True)
    out = tmp_path.joinpath(*SH.OUTPUT_SUBDIR)
    for name in ("shadow_summary_v1040.json", "shadow_scoreboard_v1040.csv",
                 "shadow_signals_v1040.csv", "shadow_outcomes_v1040.csv",
                 "shadow_bankroll_20eur_v1040.json", "shadow_research_memo_v1040.md",
                 "execution_rehearsal_report_v1040.md",
                 "micro_live_readiness_report_v1040.md"):
        assert (out / name).is_file(), name


def test_module_has_no_order_or_key_primitives():
    src = Path(SH.__file__).read_text(encoding="utf-8")
    # order / key primitives must be absent entirely
    for tok in ["place_order", "create_order", "private_get", "private_post",
                "set_leverage", "set_margin_mode", "load_dotenv", "os.environ",
                "api_key", "BitgetClient", ".execute("]:
        assert tok not in src, tok
    # network libs must not be IMPORTED (the word may appear in prose notes)
    for imp in ["import requests", "import urllib", "import websocket",
                "import ccxt", "from urllib", "import socket"]:
        assert imp not in src, imp
