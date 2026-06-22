"""ResearchOps V10.10 — Micro-Scalp Shadow Tournament tests.

Pure/offline/deterministic. Verifies no-lookahead, same-bar worst-case,
gap-adverse fills, cost application, closed-green / green-to-red metrics,
close-in-green policies, dangerous-compounding flag, leverage-never-real,
multi-window/cost-stress gating, shadow-journal path safety, and the hard
invariant that NOTHING is ever approved for paper/live (shadow only).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.labs import micro_scalp_shadow_v10_10 as M

MODULE_PATH = "app/labs/micro_scalp_shadow_v10_10.py"
COSTS = M.MicroCosts(6.0, 4.0, 2.0, True, 1)
TF_MS = {"4h": 240 * 60_000, "6h": 360 * 60_000}


def _bar(ts, o, h, l, c, v=100.0):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _entry(bars, ei, side="LONG", atr=1.0):
    return {"symbol": "BTCUSDT", "timeframe": "4h", "side": side,
            "strategy_family": "micro_breakout", "signal_idx": ei - 1, "entry_idx": ei,
            "entry_ts": bars[ei]["ts"], "entry_price": bars[ei]["open"],
            "entry_reason": "x", "atr": atr, "setup_quality_score": 0.5,
            "regime_snapshot": "range", "orderbook_real": False,
            "funding_snapshot": 0.0, "no_lookahead": True}


def _mk(net, ts, symbol="BTCUSDT", went_green=False, gross=None):
    return {"symbol": symbol, "timeframe": "6h", "side": "LONG",
            "strategy_family": "micro_breakout", "policy": "micro_profit_take",
            "entry_ts": ts, "entry_price": 100.0, "exit_price": 100 + net * 100,
            "exit_reason": "TP" if net > 0 else "STOP", "gross_pnl": gross if gross is not None else net + 0.0022,
            "net_pnl": net, "net_pnl_bps": net * 10_000.0, "R": net / 0.005,
            "time_in_trade": 3, "mfe": abs(net) + 0.001, "mae": 0.002,
            "profit_capture": 0.5, "giveback": 0.3, "went_green": went_green,
            "green_to_red_failure": bool(went_green and net <= 0),
            "break_even_locked": False, "closed_green": net > 0, "closed_red": net <= 0,
            "same_bar_ambiguity": False, "gap_adverse": False,
            "fee": 0.0012, "slippage": 0.0008, "spread_cost": 0.0002, "funding": 0.0,
            "sl_pct": 0.005, "orderbook_real": False, "regime_snapshot": "range",
            "params": {"tp_pct": 0.004}}


def _trend_bars(n, tf="6h", start=100.0, drift=0.001, vol=0.004, seed=1):
    import random as _r
    rng = _r.Random(seed)
    bars, p, t = [], start, 1700000000000
    bar = TF_MS[tf]
    for i in range(n):
        o = p
        c = p * (1 + drift + rng.uniform(-vol, vol))
        h = max(o, c) * (1 + abs(rng.uniform(0, vol)))
        l = min(o, c) * (1 - abs(rng.uniform(0, vol)))
        bars.append(_bar(t + i * bar, o, h, l, c))
        p = c
    return bars


def _write_sample(sample_dir, symbols, tf="6h", n=300):
    os.makedirs(sample_dir, exist_ok=True)
    for s in symbols:
        bars = _trend_bars(n, tf, seed=hash(s) % 997)
        lines = ["timestamp,open,high,low,close,volume"] + \
                [f"{b['ts']},{b['open']},{b['high']},{b['low']},{b['close']},10" for b in bars]
        Path(sample_dir, f"{s}_{tf}_ohlcv.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
        Path(sample_dir, f"{s}_funding.csv").write_text(
            "timestamp,funding_rate\n" + "\n".join(f"{b['ts']},0.0001" for b in bars) + "\n",
            encoding="utf-8")


# 1. no-lookahead
def test_no_lookahead_entries():
    bars = _trend_bars(120, "6h", drift=0.004)
    ents = M.generate_micro_entries(symbol="BTCUSDT", timeframe="6h", side="LONG",
                                    family="micro_breakout", bars=bars, funding=[])
    assert ents
    for e in ents:
        assert e["no_lookahead"] is True
        assert e["entry_idx"] == e["signal_idx"] + 1
        assert e["entry_price"] == bars[e["entry_idx"]]["open"]
        assert e["orderbook_real"] is False


# 2. same-bar worst case
def test_same_bar_worst_case():
    bars = [_bar(0, 100, 100, 100, 100), _bar(1, 100, 100, 100, 100),
            _bar(2, 100, 106, 94, 100)]
    tr = M.simulate_micro_trade(bars, [1.0, 1.0, 1.0], _entry(bars, 1, "LONG"),
                                policy="max_loss_hard_stop",
                                params={"tp_pct": 0.05, "sl_pct": 0.05, "max_hold": 2},
                                costs=COSTS, funding=[])
    assert tr["exit_reason"] == "SAME_BAR_WORST_CASE"
    assert tr["same_bar_ambiguity"] is True and tr["net_pnl"] < 0


# 3. gap adverse
def test_gap_adverse_fills_at_open():
    bars = [_bar(0, 100, 100, 100, 100), _bar(1, 100, 100, 100, 100),
            _bar(2, 90, 91, 88, 89)]
    tr = M.simulate_micro_trade(bars, [1.0, 1.0, 1.0], _entry(bars, 1, "LONG"),
                                policy="max_loss_hard_stop",
                                params={"tp_pct": 0.2, "sl_pct": 0.05, "max_hold": 2},
                                costs=COSTS, funding=[], gap_policy="adverse_open")
    assert tr["gap_adverse"] is True
    assert tr["exit_price"] == 90


# 4. fees/slippage/spread applied
def test_costs_applied():
    bars = [_bar(0, 100, 100, 100, 100), _bar(1, 100, 100, 100, 100),
            _bar(2, 100, 100.5, 100, 100.45)]
    tr = M.simulate_micro_trade(bars, [1.0, 1.0, 1.0], _entry(bars, 1, "LONG"),
                                policy="micro_profit_take",
                                params={"tp_pct": 0.004, "sl_pct": 0.05, "max_hold": 2},
                                costs=COSTS, funding=[])
    assert tr["net_pnl"] < tr["gross_pnl"]
    assert tr["fee"] > 0 and tr["slippage"] > 0 and tr["spread_cost"] > 0


# 5/6. closed-green + green-to-red metrics
def test_closed_green_and_green_to_red_metrics():
    trades = [_mk(0.01, 1, went_green=True), _mk(-0.01, 2, went_green=True),
              _mk(0.01, 3, went_green=True), _mk(-0.01, 4, went_green=False)]
    m = M.micro_metrics(trades, COSTS)
    assert m["closed_green_rate"] == 0.5
    assert m["green_to_red_rate"] == 0.25   # 1 of 4 went green then closed red


# 7. break-even lock
def test_break_even_lock_protects():
    bars = [_bar(0, 100, 100, 100, 100), _bar(1, 100, 100, 100, 100),
            _bar(2, 100, 100.6, 100, 100.5), _bar(3, 100.2, 100.2, 99.5, 99.6)]
    tr = M.simulate_micro_trade(bars, [1.0] * 4, _entry(bars, 1, "LONG"),
                                policy="instant_green_lock",
                                params={"tp_pct": 0.05, "sl_pct": 0.01,
                                        "green_lock_bps": 8.0, "max_hold": 3},
                                costs=COSTS, funding=[])
    assert tr["break_even_locked"] is True
    assert tr["exit_price"] >= tr["entry_price"]


# 8. kill if not green fast
def test_kill_if_not_green_fast():
    bars = [_bar(i, 100, 100.01, 99.99, 100) for i in range(10)]
    tr = M.simulate_micro_trade(bars, [1.0] * 10, _entry(bars, 1, "LONG"),
                                policy="kill_if_not_green_fast",
                                params={"tp_pct": 0.05, "sl_pct": 0.05, "kill_bars": 4,
                                        "green_lock_bps": 8.0, "max_hold": 9},
                                costs=COSTS, funding=[])
    assert tr["exit_reason"] == "KILL_NOT_GREEN"


# 9. compounding negative EV dangerous
def test_compounding_negative_ev_dangerous():
    trades = [_mk(-0.01, i) for i in range(30)]
    c = M.compounding_sim(trades, initial_capital=100.0, compound_mode="capped_fraction")
    assert c["compounding_status"] == "COMPOUNDING_DANGEROUS_NEGATIVE_EV"
    assert c["paper_ready"] is False and c["final_recommendation"] == "NO LIVE"


# 10. losing streak counted
def test_losing_streak_counted():
    trades = [_mk(-0.01, i) for i in range(12)]
    c = M.compounding_sim(trades, initial_capital=100.0)
    assert c["longest_losing_streak"] == 12


# 11/12. leverage sim never real + 20x dangerous
def test_leverage_never_real_and_20x_dangerous():
    m = M.micro_metrics([_mk(0.01, i) for i in range(20)], COSTS)
    lev = M.leverage_sim(m, sl_pct=0.005, edge_validated=False)
    assert lev["real_leverage_allowed"] is False
    assert lev["leverage_research_status"] == "BLOCKED_NO_VALIDATED_EDGE"
    r20 = [r for r in lev["rows"] if r["leverage"] == 20][0]
    assert r20["dangerous_leverage_flag"] == "DANGEROUS_RESEARCH_ONLY"
    openr = M.leverage_sim(m, sl_pct=0.005, edge_validated=True)
    assert openr["real_leverage_allowed"] is False   # still never real


# 13. one-window-only never SHADOW
def test_one_window_only_not_shadow():
    # positive recent 90d, negative older -> 180d window fails -> only 1 window
    trades = ([_mk(0.012, t) for t in range(1, 41)]
              + [_mk(-0.02, t) for t in range(41, 81)])
    # shift timestamps to days
    for i, t in enumerate(trades):
        t["entry_ts"] = 1700000000000 + i * M.DAY_MS * 2  # spread ~160 days
    ev = M._evaluate(trades, costs=COSTS, min_trades=10, windows=[90, 180], max_tier=M.CAND_WEAK)
    assert ev["tier"] != M.CAND_SHADOW
    assert ev["windows_passed"] <= ev["windows_tested"]


# 14. cost stress fail rejected
def test_cost_stress_failure_rejected():
    trades = [_mk(0.001, 1700000000000 + i * M.DAY_MS) for i in range(40)]  # tiny edge
    ev = M._evaluate(trades, costs=COSTS, min_trades=10, windows=[90, 180], max_tier=M.CAND_WEAK)
    assert ev["tier"] == M.CAND_REJECTED
    assert any("cost_stress" in r for r in ev["rejection_reasons"])


# 15/16. shadow journal + path safety
def test_safe_output_base_blocks_unsafe():
    assert M._safe_output_base("external_data/raw") == M.OUTPUT_ROOT
    assert M._safe_output_base("x/../y") == M.OUTPUT_ROOT
    assert M._safe_output_base("backups/x") == M.OUTPUT_ROOT
    assert M._safe_output_base("reports/research/v10_10") == "reports/research/v10_10"


def test_tournament_writes_journal_under_output(tmp_path):
    sample = tmp_path / "s"
    _write_sample(sample, ["BTCUSDT", "ETHUSDT"], "6h", 200)
    rep = M.run_micro_scalp_tournament(
        sample_dir=str(sample), symbols=["BTCUSDT", "ETHUSDT"], timeframes=["6h"],
        sides=["LONG", "SHORT"], strategy_families=["micro_breakout", "volatility_burst_scalp"],
        min_trades=10, windows=[60, 120], max_grid_combos=60)
    run_dir = M.write_micro_reports(rep, output_dir=str(tmp_path / "out"))
    assert os.path.isfile(os.path.join(run_dir, "shadow_journal", "journal.csv"))
    assert os.path.isfile(os.path.join(run_dir, "micro_scalp_summary.json"))
    assert run_dir.replace("\\", "/").startswith(str(tmp_path).replace("\\", "/"))


# 17-20. safety source scan + flags
def test_module_has_no_dangerous_primitives():
    src = Path(MODULE_PATH).read_text(encoding="utf-8")
    for token in ["place_order", "create_order", "private_get", "private_post",
                  "set_leverage", "set_margin_mode", "ExecutionEngine", "PaperTrader",
                  "import torch", "from torch", "import jax", "import tensorflow",
                  "import timesfm", "load_dotenv", "os.environ", "import requests",
                  "import socket", "urllib.request"]:
        assert token not in src, f"{MODULE_PATH} must not contain {token!r}"


def test_plan_flags_no_live():
    p = M.micro_scalp_plan()
    assert p["research_only"] and p["shadow_only"]
    assert p["paper_ready"] is False and p["live_ready"] is False
    assert p["can_send_real_orders"] is False
    assert p["final_recommendation"] == "NO LIVE"


# 22/24/25. tournament end-to-end + no approvals
def test_tournament_no_approval_and_no_live(tmp_path):
    sample = tmp_path / "s"
    _write_sample(sample, ["BTCUSDT", "ETHUSDT", "SOLUSDT"], "6h", 200)
    rep = M.run_micro_scalp_tournament(
        sample_dir=str(sample), symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"], timeframes=["6h"],
        sides=["LONG", "SHORT"], strategy_families=list(M.STRATEGY_FAMILIES),
        min_trades=10, windows=[60, 120], max_grid_combos=200)
    assert rep["errors"] == []
    assert rep["max_candidate_quality_tier"] == M.CAND_WEAK   # capped, current data
    assert rep["n_shadow_test_candidate"] == 0
    for c in rep["candidates"] + rep["rejected_candidates"]:
        assert c["final_tier"] in (M.CAND_REJECTED, M.CAND_WEAK)
    blob = json.dumps({k: v for k, v in rep.items() if not k.startswith("_")}, default=str)
    assert "APPROVED_FOR_PAPER" not in blob and "APPROVED_FOR_LIVE" not in blob
    assert rep["final_recommendation"] == "NO LIVE"
    assert rep["paper_ready"] is False and rep["live_ready"] is False
    assert rep["shadow_only"] is True


# 23. report summary
def test_report_summary_no_approval():
    s = M.summarize_micro({"data_classification": "INTERMEDIATE_RESEARCH_ONLY",
                           "candidates": [], "n_candidates": 0})
    assert s["approved_for_paper"] is False and s["approved_for_live"] is False
    assert s["final_recommendation"] == "NO LIVE"
    assert s["edge_validated"] is False
