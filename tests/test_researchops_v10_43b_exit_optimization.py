"""V10.43B Exit Optimization / Profit Extraction — honest, exits-only, no fake edge.

Covers: MFE/MAE + capture, no-lookahead entry (next_open), DATA_GAP / STALE_EXIT,
break-even protection, small sample -> NEEDS_MORE_DATA / INSUFFICIENT_SAMPLE,
ranking by net_EV_lower_bound (not win rate), slippage sensitivity measured,
valid verdicts, no invented edge on noise, winner/loser stats, reports written,
and the hard red lines (no leverage/sizing change, NO LIVE).
"""

from __future__ import annotations

import random

from app.labs import exit_optimization_v10_43b as EX
from app.labs import continuous_edge_factory_v10_38 as CE

T0 = 1_700_000_000_000
BAR = EX.BAR_MS


def bar(ts, o, h, l, c):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c}


# --------------------------------------------------------------------------
# Deterministic unit tests on the exit-aware simulator
# --------------------------------------------------------------------------

def test_entry_is_next_open_and_mfe_mae_measured():
    # signal at i=0; entry = open of bar[1] = 100. Far TP/SL so neither triggers.
    bars = [
        bar(T0 + 0 * BAR, 100, 100, 100, 100),          # signal bar (i=0)
        bar(T0 + 1 * BAR, 100, 101.0, 100.0, 100.5),    # entry bar, MFE peak here (+1%)
        bar(T0 + 2 * BAR, 100.5, 100.6, 100.2, 100.3),
        bar(T0 + 3 * BAR, 100.3, 100.4, 100.0, 100.1),
    ]
    o = EX.simulate_exit(bars, 0, "long", tp=0.02, sl=0.02, trail=None, be=None, horizon=5)
    assert o["valid"] and o["exit_reason"] == "TIME"
    assert abs(o["mfe"] - 0.01) < 1e-9            # +1% favorable excursion
    assert o["mae"] <= 0.0
    # captured_of_mfe = net / mfe  and net < mfe (gave the move back + costs)
    assert o["captured_of_mfe"] is not None and o["captured_of_mfe"] < 1.0


def test_no_lookahead_past_bar_prices_are_ignored():
    base = [
        bar(T0 + 0 * BAR, 100, 100, 100, 100),
        bar(T0 + 1 * BAR, 100, 100.7, 100.0, 100.6),
        bar(T0 + 2 * BAR, 100.6, 100.8, 100.4, 100.7),
    ]
    o1 = EX.simulate_exit(base, 0, "long", 0.02, 0.02, None, None, 5)
    # mutate ONLY the pre-entry signal bar's prices wildly
    base[0] = bar(T0 + 0 * BAR, 999, 9999, 0.01, 5)
    o2 = EX.simulate_exit(base, 0, "long", 0.02, 0.02, None, None, 5)
    assert o1["net_return"] == o2["net_return"] and o1["mfe"] == o2["mfe"]


def test_data_gap_before_entry_is_invalid():
    bars = [
        bar(T0 + 0 * BAR, 100, 100, 100, 100),
        bar(T0 + 3 * BAR, 100, 101, 100, 100.8),        # >2 bars gap from signal
    ]
    o = EX.simulate_exit(bars, 0, "long", 0.006, 0.006, None, None, 5)
    assert o["valid"] is False and o["exit_reason"] == "DATA_GAP"


def test_stale_exit_on_mid_trade_gap():
    bars = [
        bar(T0 + 0 * BAR, 100, 100, 100, 100),
        bar(T0 + 1 * BAR, 100, 100.2, 99.9, 100.1),
        bar(T0 + 5 * BAR, 100.1, 100.3, 100.0, 100.2),  # jump -> stale
        bar(T0 + 6 * BAR, 100.2, 100.4, 100.1, 100.3),
    ]
    o = EX.simulate_exit(bars, 0, "long", 0.02, 0.02, None, None, 5)
    assert o["valid"] and o["exit_reason"] == "STALE_EXIT"


def test_break_even_protects_vs_no_break_even():
    # goes +0.5% (arms BE at 40bps), then rolls over and would hit a 0.6% stop.
    bars = [
        bar(T0 + 0 * BAR, 100, 100, 100, 100),
        bar(T0 + 1 * BAR, 100, 100.5, 100.05, 100.4),   # arms BE, no stop touch
        bar(T0 + 2 * BAR, 100.4, 100.45, 99.90, 99.95),  # dips below entry
        bar(T0 + 3 * BAR, 99.95, 100.0, 99.30, 99.35),   # deep enough to hit 0.6% SL
    ]
    with_be = EX.simulate_exit(bars, 0, "long", 0.02, 0.006, None, be=0.004, horizon=5)
    no_be = EX.simulate_exit(bars, 0, "long", 0.02, 0.006, None, be=None, horizon=5)
    assert with_be["exit_reason"] == "BE" and abs(with_be["gross"]) < 1e-9
    assert no_be["exit_reason"] == "SL"
    assert with_be["gross"] > no_be["gross"]             # BE saved the tail


def test_sl_wins_ties_and_capture_none_when_no_excursion():
    # flat then straight down to the stop: MFE ~ 0 -> captured_of_mfe is None
    bars = [
        bar(T0 + 0 * BAR, 100, 100, 100, 100),
        bar(T0 + 1 * BAR, 100, 100.0, 99.30, 99.35),    # low hits 0.6% stop
        bar(T0 + 2 * BAR, 99.35, 99.4, 99.0, 99.1),
    ]
    o = EX.simulate_exit(bars, 0, "long", 0.006, 0.006, None, None, 5)
    assert o["exit_reason"] == "SL"
    assert o["mfe"] <= 1e-9 and o["captured_of_mfe"] is None


# --------------------------------------------------------------------------
# Integration on synthetic datasets via monkeypatched _load_bars
# --------------------------------------------------------------------------

def noise_bars(n, seed=1):
    rng = random.Random(seed)
    price, out = 100.0, []
    for i in range(n):
        drift = rng.uniform(-0.0012, 0.0012)
        new = price * (1 + drift)
        ntr = int(rng.uniform(15, 35) + rng.uniform(15, 35))
        out.append({"ts": T0 + i * BAR, "bar_close_ts": T0 + i * BAR,
                    "available_at": T0 + i * BAR, "open": price,
                    "high": max(price, new) * 1.0007, "low": min(price, new) * 0.9993,
                    "close": new, "volume": ntr, "buy_volume": ntr * 0.5,
                    "sell_volume": ntr * 0.5, "n_trades": ntr, "max_trade": ntr / 5,
                    "symbol": "BTCUSDT"})
        price = new
    return out


def test_insufficient_sample_is_honest(monkeypatch, tmp_path):
    monkeypatch.setattr(EX.CE, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(EX.LAB, "_load_bars", lambda s, ds: (noise_bars(50), "ws", {}))
    r = EX.run_exit_optimization("BTCUSDT", data_source="ws", write_reports=True)
    assert r["verdict"] == "INSUFFICIENT_SAMPLE"
    assert r["can_send_real_orders"] is False
    assert r["final_recommendation"] == "NO LIVE"


def test_noise_yields_no_exit_edge(monkeypatch):
    monkeypatch.setattr(EX.LAB, "_load_bars", lambda s, ds: (noise_bars(2200, 3), "ws", {}))
    r = EX.run_exit_optimization("BTCUSDT", data_source="ws", write_reports=False)
    assert r["n_entries"] >= 30                          # enough entries to score
    assert r["watchlist_or_better"] == 0                 # nothing promoted on noise
    assert r["verdict"] == "NO_EXIT_EDGE_ALL_REJECTED"


def test_ranking_by_lower_bound_not_win_rate(monkeypatch):
    monkeypatch.setattr(EX.LAB, "_load_bars", lambda s, ds: (noise_bars(2200, 5), "ws", {}))
    captured = {}
    monkeypatch.setattr(EX, "_write",
                        lambda summary, rows, wl: captured.update(rows=rows))
    EX.run_exit_optimization("BTCUSDT", data_source="ws", write_reports=True)
    rows = captured["rows"]
    lbs = [r["net_EV_lower_bound"] for r in rows if r["net_EV_lower_bound"] is not None]
    assert lbs == sorted(lbs, reverse=True)              # sorted by lower bound desc


def test_slippage_sensitivity_and_valid_verdicts(monkeypatch):
    monkeypatch.setattr(EX.LAB, "_load_bars", lambda s, ds: (noise_bars(2200, 9), "ws", {}))
    captured = {}
    monkeypatch.setattr(EX, "_write",
                        lambda summary, rows, wl: captured.update(rows=rows))
    EX.run_exit_optimization("BTCUSDT", data_source="ws", write_reports=True)
    rows = captured["rows"]
    scored = [r for r in rows if r["net_EV"] is not None]
    assert scored
    # higher costs never improve EV -> cost_sensitivity >= 0 where measured
    assert any(r["cost_sensitivity"] is not None and r["cost_sensitivity"] >= 0 for r in scored)
    for r in rows:
        assert r["verdict"] in EX.VERDICTS


def test_winner_loser_stats_present(monkeypatch):
    monkeypatch.setattr(EX.LAB, "_load_bars", lambda s, ds: (noise_bars(2200, 3), "ws", {}))
    r = EX.run_exit_optimization("BTCUSDT", data_source="ws", write_reports=False)
    wl = r["winner_loser"]
    for k in ("n", "never_went_favorable_pct", "exit_analysis_reliable"):
        assert k in wl


def test_reports_written(monkeypatch, tmp_path):
    monkeypatch.setattr(EX.CE, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(EX.LAB, "_load_bars", lambda s, ds: (noise_bars(2200, 3), "ws", {}))
    EX.run_exit_optimization("BTCUSDT", data_source="ws", write_reports=True)
    d = tmp_path.joinpath(*EX.OUTPUT_SUBDIR)
    assert (d / "exit_optimization_scoreboard_v1043b.csv").is_file()
    assert (d / "exit_optimization_report_v1043b.md").is_file()
    md = (d / "exit_optimization_report_v1043b.md").read_text(encoding="utf-8")
    assert "NO LIVE" in md and "exits only" in md


def test_safety_no_leverage_or_sizing_change(monkeypatch):
    monkeypatch.setattr(EX.LAB, "_load_bars", lambda s, ds: (noise_bars(300, 3), "ws", {}))
    r = EX.run_exit_optimization("BTCUSDT", data_source="ws", write_reports=False)
    assert r["changes_sizing"] is False and r["changes_leverage"] is False
    assert r["aggressiveness_from"] == "exits_only_not_risk_or_sizing"
    assert r["edge_validated"] is False and r["can_send_real_orders"] is False


def test_cli_registered():
    import app.research_lab as RL
    assert "exit-optimization-v1043b" in RL.PUBLIC_RESEARCH_ONLY_COMMANDS
