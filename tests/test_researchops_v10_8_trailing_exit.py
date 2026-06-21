"""ResearchOps V10.8 — tests for the adaptive trailing exit research lab.

Pure/offline/deterministic. Verifies no-lookahead, worst-case same-bar,
monotonic trailing stops, break-even/ladder/time-death mechanics, metrics,
walk-forward + anti-overfit gating, leverage-sim safety, and the hard
invariant that NOTHING is ever approved for paper/live.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.labs import adaptive_trailing_exit_v10_8 as L

MODULE_PATH = "app/labs/adaptive_trailing_exit_v10_8.py"
COSTS = L.Costs(cost_bps=6.0, slippage_bps=4.0, funding_mode=True)


def _bar(ts, o, h, l, c, v=100.0):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _entry(bars, idx, side="LONG", atr=1.0):
    return {"symbol": "BTCUSDT", "timeframe": "4h", "side": side,
            "family": "breakout_momentum", "signal_idx": idx - 1, "entry_idx": idx,
            "entry_ts": bars[idx]["ts"], "entry_price": bars[idx]["open"],
            "entry_reason": "x", "atr": atr, "regimes": ["trend_up"],
            "funding_snapshot": 0.0, "no_lookahead": True}


def _mk_trade(net, ts, symbol="BTCUSDT", exit_reason="TP"):
    return {"symbol": symbol, "timeframe": "4h", "side": "LONG",
            "family": "breakout_momentum", "policy": "atr_trailing", "entry_ts": ts,
            "entry_price": 100.0, "exit_price": 100.0 + net * 100, "exit_reason": exit_reason,
            "gross_ret": net + 0.002, "net_ret": net, "R": net / 0.02, "held_bars": 5,
            "mfe": abs(net) + 0.01, "mae": 0.005, "profit_capture": 0.5, "giveback": 0.3,
            "be_activated": True, "trail_activated": True, "time_to_lock": 2,
            "same_bar_ambiguous": False, "fee_frac": 0.0012, "slippage_frac": 0.0008,
            "funding_frac": 0.0, "regimes": ["trend_up"], "sl_pct": 0.02}


def _trend_bars(n=120, start=100.0, drift=0.012, vol=0.01, seed=1):
    import random as _r
    rng = _r.Random(seed)
    bars = []
    p = start
    ts = 1700000000000
    for i in range(n):
        o = p
        c = p * (1 + drift + rng.uniform(-vol, vol))
        h = max(o, c) * (1 + abs(rng.uniform(0, vol)))
        l = min(o, c) * (1 - abs(rng.uniform(0, vol)))
        bars.append(_bar(ts + i * 4 * 3600 * 1000, o, h, l, c))
        p = c
    return bars


# --------------------------------------------------------------------------
# 1. No lookahead
# --------------------------------------------------------------------------

def test_entries_are_no_lookahead():
    bars = _trend_bars(120)
    ents = L.generate_entries(symbol="BTCUSDT", timeframe="4h", side="LONG",
                              family="breakout_momentum", bars=bars, funding=[])
    assert ents, "expected some entries on a trending series"
    for e in ents:
        assert e["no_lookahead"] is True
        assert e["entry_idx"] == e["signal_idx"] + 1   # fill on NEXT bar
        assert e["entry_idx"] < len(bars)
        assert e["entry_price"] == bars[e["entry_idx"]]["open"]
        assert e["signal_idx"] >= L._WARMUP


# --------------------------------------------------------------------------
# 2. Same-bar worst-case
# --------------------------------------------------------------------------

def test_same_bar_hits_both_uses_worst_case():
    bars = [_bar(1, 100, 100, 100, 100), _bar(2, 100, 108, 92, 100),
            _bar(3, 100, 100, 100, 100)]
    e = _entry(bars, 1, "LONG", atr=2.0)
    tr = L.simulate_trade(bars, [None, None, None], e, policy="fixed_tp_sl_time",
                          params={"sl_pct": 0.05, "tp_pct": 0.05, "max_hold": 2},
                          costs=COSTS, funding=[])
    assert tr["exit_reason"] == "SAME_BAR_AMBIGUITY_WORST_CASE"
    assert tr["same_bar_ambiguous"] is True
    assert tr["net_ret"] < 0  # worst case = the loss


# --------------------------------------------------------------------------
# 3. Break-even lock — never gives a winner back to a real loss
# --------------------------------------------------------------------------

def test_break_even_lock_protects_to_be():
    bars = [_bar(0, 100, 100, 100, 100), _bar(1, 100, 100, 100, 100),
            _bar(2, 100, 103, 100, 102), _bar(3, 101, 101, 99, 99.5),
            _bar(4, 99, 99, 98, 98)]
    e = _entry(bars, 1, "LONG", atr=1.0)
    tr = L.simulate_trade(bars, [1.0] * len(bars), e, policy="break_even_lock",
                          params={"sl_pct": 0.02, "tp_pct": 0.10, "be_trigger_R": 1.0,
                                  "max_hold": 4}, costs=COSTS, funding=[])
    assert tr["be_activated"] is True
    # exit at/above entry (BE), not at the original 98 stop
    assert tr["exit_price"] >= e["entry_price"]
    assert tr["exit_reason"] in ("BREAK_EVEN", "TRAILING")


# --------------------------------------------------------------------------
# 4. ATR trailing — LONG stop only rises (exit above initial stop)
# --------------------------------------------------------------------------

def test_atr_trailing_stop_only_rises_long():
    bars = [_bar(0, 100, 100, 100, 100), _bar(1, 100, 100, 100, 100),
            _bar(2, 100, 105, 100, 104), _bar(3, 104, 110, 104, 109),
            _bar(4, 109, 109, 105, 105.5)]
    e = _entry(bars, 1, "LONG", atr=1.0)
    tr = L.simulate_trade(bars, [1.0] * len(bars), e, policy="atr_trailing",
                          params={"sl_pct": 0.02, "atr_mult": 2.5, "max_hold": 4},
                          costs=COSTS, funding=[])
    assert tr["trail_activated"] is True
    # stop trailed up well above the initial stop (entry*0.98 = 98)
    assert tr["exit_price"] > e["entry_price"]


def test_atr_trailing_symmetric_short_only_falls():
    bars = [_bar(0, 100, 100, 100, 100), _bar(1, 100, 100, 100, 100),
            _bar(2, 100, 100, 95, 96), _bar(3, 96, 96, 90, 91),
            _bar(4, 91, 95, 91, 94.5)]
    e = _entry(bars, 1, "SHORT", atr=1.0)
    tr = L.simulate_trade(bars, [1.0] * len(bars), e, policy="atr_trailing",
                          params={"sl_pct": 0.02, "atr_mult": 2.5, "max_hold": 4},
                          costs=COSTS, funding=[])
    assert tr["trail_activated"] is True
    assert tr["exit_price"] < e["entry_price"]  # profit on the short


# --------------------------------------------------------------------------
# 5. Percent trailing respects favorable extreme, never loosens
# --------------------------------------------------------------------------

def test_percent_trailing_locks_from_high():
    bars = [_bar(0, 100, 100, 100, 100), _bar(1, 100, 100, 100, 100),
            _bar(2, 100, 110, 100, 109), _bar(3, 109, 109, 104, 104.5)]
    e = _entry(bars, 1, "LONG", atr=1.0)
    tr = L.simulate_trade(bars, [1.0] * len(bars), e, policy="percent_trailing",
                          params={"sl_pct": 0.02, "pct_trail": 0.03, "max_hold": 3},
                          costs=COSTS, funding=[])
    assert tr["trail_activated"] is True
    # trailing from high 110 with 3% → ~106.7; exit must be well above entry
    assert tr["exit_price"] > e["entry_price"]


# --------------------------------------------------------------------------
# 6/7. Structure + hybrid produce valid exits
# --------------------------------------------------------------------------

def test_structure_trailing_valid_exit():
    bars = _trend_bars(60, drift=0.01)
    e = _entry(bars, 40, "LONG", atr=1.0)
    tr = L.simulate_trade(bars, L.atr_series(bars), e, policy="structure_trailing",
                          params={"sl_pct": 0.02, "struct_n": 5, "max_hold": 15},
                          costs=COSTS, funding=[])
    assert tr["exit_reason"] in L.EXIT_REASONS


def test_hybrid_combines_be_and_time_death():
    # flat path → hybrid actively MANAGES the exit (structure/atr trail or
    # time-death), never just rides to END_OF_DATA on a stagnant trade.
    bars = [_bar(i, 100, 100.2, 99.8, 100) for i in range(20)]
    e = _entry(bars, 1, "LONG", atr=1.0)
    tr = L.simulate_trade(bars, [1.0] * len(bars), e, policy="hybrid_trailing",
                          params={"sl_pct": 0.02, "time_death_bars": 6,
                                  "time_death_min_mfe_R": 0.5, "max_hold": 18},
                          costs=COSTS, funding=[])
    assert tr["exit_reason"] in ("TIME_DEATH", "TRAILING", "BREAK_EVEN", "STOP")
    assert tr["exit_reason"] != "END_OF_DATA"


# --------------------------------------------------------------------------
# 8. Profit protection ladder
# --------------------------------------------------------------------------

def test_profit_protection_ladder_locks_profit():
    bars = [_bar(0, 100, 100, 100, 100), _bar(1, 100, 100, 100, 100),
            _bar(2, 100, 105, 100, 104.5), _bar(3, 104, 104, 101, 101.5)]
    e = _entry(bars, 1, "LONG", atr=1.0)
    tr = L.simulate_trade(bars, [1.0] * len(bars), e, policy="profit_protection_ladder",
                          params={"sl_pct": 0.02, "atr_mult": 2.5, "max_hold": 3},
                          costs=COSTS, funding=[])
    # reached >= +2R (104 = +2R on sl_pct 2%) → lock to >= +1R; exit above entry
    assert tr["be_activated"] is True
    assert tr["exit_price"] >= e["entry_price"]


# --------------------------------------------------------------------------
# 9. Time death closes stagnant trades
# --------------------------------------------------------------------------

def test_time_death_closes_stagnant_trade():
    bars = [_bar(i, 100, 100.1, 99.9, 100) for i in range(16)]
    e = _entry(bars, 1, "LONG", atr=1.0)
    tr = L.simulate_trade(bars, [1.0] * len(bars), e, policy="time_death_exit",
                          params={"sl_pct": 0.02, "time_death_bars": 6,
                                  "time_death_min_mfe_R": 0.5, "max_hold": 14},
                          costs=COSTS, funding=[])
    assert tr["exit_reason"] == "TIME_DEATH"


# --------------------------------------------------------------------------
# 10/11. Costs + funding
# --------------------------------------------------------------------------

def test_costs_reduce_net_and_stress_reduces_further():
    trades = [_mk_trade(0.03, 1 + i) for i in range(20)]
    m = L.compute_metrics(trades, COSTS)
    assert m["net_EV"] < m["gross_EV"]              # costs subtracted
    assert m["cost_stress_x2"] < m["net_EV"]        # x2 worse
    assert m["cost_stress_x3"] < m["cost_stress_x2"]


def test_funding_mode_toggle_changes_result():
    bars = _trend_bars(40, drift=0.0, vol=0.005, seed=3)
    funding = [{"ts": bars[i]["ts"], "rate": 0.001} for i in range(len(bars))]
    e = _entry(bars, 10, "LONG", atr=1.0)
    on = L.simulate_trade(bars, L.atr_series(bars), e, policy="fixed_tp_sl_time",
                          params={"sl_pct": 0.05, "tp_pct": 0.20, "max_hold": 20},
                          costs=L.Costs(6, 4, True), funding=funding)
    off = L.simulate_trade(bars, L.atr_series(bars), e, policy="fixed_tp_sl_time",
                           params={"sl_pct": 0.05, "tp_pct": 0.20, "max_hold": 20},
                           costs=L.Costs(6, 4, False), funding=funding)
    assert off["funding_frac"] == 0.0
    # LONG pays positive funding → funding hurts the funded run
    assert on["funding_frac"] <= 0.0
    assert on["net_ret"] <= off["net_ret"] + 1e-9


# --------------------------------------------------------------------------
# 12. Metrics presence
# --------------------------------------------------------------------------

def test_metrics_contain_required_fields():
    trades = [_mk_trade(0.02 if i % 2 else -0.01, 1 + i) for i in range(30)]
    m = L.compute_metrics(trades, COSTS)
    for key in ("profit_capture_ratio", "giveback_ratio", "MFE_MAE_ratio",
                "profit_factor_net", "max_drawdown", "exit_reason_distribution",
                "avg_R", "median_R", "p10_R", "p90_R", "net_PF_after_cost_stress"):
        assert key in m


# --------------------------------------------------------------------------
# 13. Walk-forward split is chronological, non-overlapping
# --------------------------------------------------------------------------

def test_walk_forward_split_is_temporal():
    trades = [_mk_trade(0.01, ts) for ts in range(100, 0, -1)]  # unsorted ts
    train, oos = L.chronological_oos_single_split(trades, 0.6)
    assert len(train) + len(oos) == len(trades)
    assert max(t["entry_ts"] for t in train) <= min(t["entry_ts"] for t in oos)


# --------------------------------------------------------------------------
# 14. Anti-overfit: good train but bad OOS is rejected
# --------------------------------------------------------------------------

def test_anti_overfit_rejects_good_train_bad_oos():
    trades = ([_mk_trade(0.02, ts) for ts in range(1, 25)]      # train: positive
              + [_mk_trade(-0.015, ts) for ts in range(25, 41)])  # oos: negative
    # chronological_split path exposes the single-split OOS gate explicitly
    ev = L.evaluate_candidate(trades, costs=COSTS, min_trades=10, train_ratio=0.6,
                              wf_mode=L.WF_SPLIT, data_classification=L.CLS_INTERMEDIATE)
    assert ev["candidate_status"] == L.CAND_REJECTED
    assert "oos_net_EV<=0" in ev["rejection_reasons"]
    assert ev["approved_for_paper"] is False and ev["approved_for_live"] is False


# --------------------------------------------------------------------------
# 15. Leverage simulation safety
# --------------------------------------------------------------------------

def test_leverage_sim_flags_danger_and_forbids_real_leverage():
    m = L.compute_metrics([_mk_trade(0.02, 1 + i) for i in range(20)], COSTS)
    lev = L.leverage_simulation(m, sl_pct=0.02)
    assert lev["real_leverage_allowed"] is False
    assert lev["leverage_recommendation"] == "NO_REAL_LEVERAGE"
    row20 = [r for r in lev["rows"] if r["leverage"] == 20][0]
    assert row20["dangerous_leverage_flag"] == "DANGEROUS_RESEARCH_ONLY"
    assert lev["final_recommendation"] == "NO LIVE"


# --------------------------------------------------------------------------
# 16. Safety — static source scan
# --------------------------------------------------------------------------

def test_module_has_no_dangerous_primitives():
    src = Path(MODULE_PATH).read_text(encoding="utf-8")
    for token in ["place_order", "create_order", "private_get", "private_post",
                  "set_leverage", "set_margin_mode", "ExecutionEngine",
                  "PaperTrader", "import torch", "from torch", "import jax",
                  "import tensorflow", "import timesfm", "load_dotenv",
                  "os.environ", "import httpx", "import requests", "import socket",
                  "urllib.request"]:
        assert token not in src, f"{MODULE_PATH} must not contain {token!r}"


# --------------------------------------------------------------------------
# 17/19. Plan + report summary safety
# --------------------------------------------------------------------------

def test_plan_is_research_only_no_live():
    p = L.trailing_exit_plan()
    assert p["research_only"] is True and p["paper_ready"] is False
    assert p["live_ready"] is False and p["can_send_real_orders"] is False
    assert p["leverage_recommendation"] == "NO_REAL_LEVERAGE"
    assert p["final_recommendation"] == "NO LIVE"


def test_summary_never_approves():
    s = L.summarize_run({"data_classification": L.CLS_INTERMEDIATE,
                         "trades_simulated": 10, "research_candidates": [],
                         "rejected_candidates": []})
    assert s["approved_for_paper"] is False and s["approved_for_live"] is False
    assert s["final_recommendation"] == "NO LIVE"


# --------------------------------------------------------------------------
# 18/20. End-to-end lab on a fixture + classification cap
# --------------------------------------------------------------------------

def _write_fixture(sample_dir: Path, symbols, drift_map):
    sample_dir.mkdir(parents=True, exist_ok=True)
    for sym in symbols:
        bars = _trend_bars(160, drift=drift_map.get(sym, 0.01), seed=hash(sym) % 999)
        rows = ["timestamp,open,high,low,close,volume"]
        for b in bars:
            rows.append(f"{b['ts']},{b['open']},{b['high']},{b['low']},{b['close']},{b['volume']}")
        (sample_dir / f"{sym}_4h_ohlcv.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
        (sample_dir / f"{sym}_funding.csv").write_text(
            "timestamp,funding_rate\n" + "\n".join(f"{b['ts']},0.0001" for b in bars) + "\n",
            encoding="utf-8")


def test_lab_end_to_end_writes_reports_and_never_approves(tmp_path):
    sample = tmp_path / "sample"
    _write_fixture(sample, ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
                   {"BTCUSDT": 0.012, "ETHUSDT": 0.010, "SOLUSDT": 0.014})
    rep = L.run_trailing_exit_lab(
        sample_dir=str(sample), symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        timeframes=["4h"], sides=["LONG", "SHORT"],
        entry_families=["breakout_momentum", "volatility_expansion"],
        exit_policies=["fixed_tp_sl_time", "atr_trailing", "break_even_lock"],
        cost_bps=6, slippage_bps=4, funding_mode=True, min_trades=10,
        train_ratio=0.6, walk_forward_mode="rolling", max_grid_combos=200, seed=7,
        data_classification=L.CLS_INTERMEDIATE)
    assert rep["errors"] == []
    assert rep["trades_simulated"] > 0
    # NO candidate is ever approved for paper/live (tiers only)
    for c in rep["research_candidates"] + rep["rejected_candidates"]:
        assert c["candidate_status"] in (L.CAND_REJECTED, L.CAND_WEAK, L.CAND_RESEARCH_ONLY)
    assert rep["final_recommendation"] == "NO LIVE"
    assert rep["strategy_ready"] is False

    out = tmp_path / "out"
    run_dir = L.write_reports(rep, output_dir=str(out))
    for fn in ("summary.json", "trades.csv", "policy_metrics.csv",
               "candidate_ranking.csv", "stability_matrix.csv",
               "rejected_candidates.csv", "report.md"):
        assert os.path.isfile(os.path.join(run_dir, fn)), fn
    summ = json.loads(Path(run_dir, "summary.json").read_text(encoding="utf-8"))
    assert summ["final_recommendation"] == "NO LIVE"
    assert summ["paper_ready"] is False and summ["live_ready"] is False


def test_intermediate_classification_caps_at_research_candidate(tmp_path):
    sample = tmp_path / "sample"
    _write_fixture(sample, ["BTCUSDT", "ETHUSDT"], {"BTCUSDT": 0.02, "ETHUSDT": 0.02})
    rep = L.run_trailing_exit_lab(
        sample_dir=str(sample), symbols=["BTCUSDT", "ETHUSDT"], timeframes=["4h"],
        sides=["LONG"], entry_families=["breakout_momentum"],
        exit_policies=["atr_trailing"], min_trades=5, max_grid_combos=50,
        data_classification=L.CLS_INTERMEDIATE)
    statuses = {c["candidate_status"] for c in rep["research_candidates"] + rep["rejected_candidates"]}
    # INTERMEDIATE data caps the top tier to WEAK at best — never the full
    # RESEARCH_CANDIDATE_ONLY tier, and never APPROVED.
    assert statuses <= {L.CAND_REJECTED, L.CAND_WEAK}
    assert L.CAND_RESEARCH_ONLY not in statuses
    assert "APPROVED_FOR_PAPER" not in statuses
    assert "APPROVED_FOR_LIVE" not in statuses


def test_output_dir_refuses_raw_falls_back_to_research(tmp_path):
    # a raw/unsafe output dir must fall back to the canonical research root
    run_dir = L._safe_output_dir(str(tmp_path / "external_data" / "raw"))
    assert "external_data/raw" not in run_dir.replace("\\", "/")
    assert L.OUTPUT_ROOT.split("/")[-1] in run_dir.replace("\\", "/")


# ==========================================================================
# V10.8.1 — gap-adverse execution, rolling walk-forward, hardened reporting
# ==========================================================================

def test_long_gap_adverse_stop_fills_at_open_not_stop():
    # LONG stop ~95; next bar opens at 90 (gaps below) -> exit at 90, not 95.
    bars = [_bar(0, 100, 100, 100, 100), _bar(1, 100, 100, 100, 100),
            _bar(2, 90, 91, 88, 89)]
    e = _entry(bars, 1, "LONG", atr=1.0)
    tr = L.simulate_trade(bars, [1.0, 1.0, 1.0], e, policy="fixed_tp_sl_time",
                          params={"sl_pct": 0.05, "tp_pct": 0.20, "max_hold": 2},
                          costs=COSTS, funding=[], gap_policy="adverse_open")
    assert tr["exit_price"] == 90          # worse open, not the 95 stop
    assert tr["gap_adverse"] is True
    assert tr["gap_slippage_bps"] > 0


def test_short_gap_adverse_stop_fills_at_open_not_stop():
    # SHORT stop ~105; next bar opens at 110 (gaps above) -> exit at 110.
    bars = [_bar(0, 100, 100, 100, 100), _bar(1, 100, 100, 100, 100),
            _bar(2, 110, 112, 109, 111)]
    e = _entry(bars, 1, "SHORT", atr=1.0)
    tr = L.simulate_trade(bars, [1.0, 1.0, 1.0], e, policy="fixed_tp_sl_time",
                          params={"sl_pct": 0.05, "tp_pct": 0.20, "max_hold": 2},
                          costs=COSTS, funding=[], gap_policy="adverse_open")
    assert tr["exit_price"] == 110
    assert tr["gap_adverse"] is True


def test_gap_favorable_does_not_over_improve_base_tp():
    # LONG TP at 106; next bar gaps far above to 110 -> base fills at TP 106.
    bars = [_bar(0, 100, 100, 100, 100), _bar(1, 100, 100, 100, 100),
            _bar(2, 110, 112, 109, 111)]
    e = _entry(bars, 1, "LONG", atr=1.0)
    tr = L.simulate_trade(bars, [1.0, 1.0, 1.0], e, policy="fixed_tp_sl_time",
                          params={"sl_pct": 0.05, "tp_pct": 0.06, "max_hold": 2},
                          costs=COSTS, funding=[], gap_policy="adverse_open")
    assert tr["exit_reason"] == "TP"
    assert tr["exit_price"] == pytest.approx(106.0)   # not 110
    assert tr["gap_adverse"] is False


def test_gap_adverse_count_reported():
    bars = [_bar(0, 100, 100, 100, 100), _bar(1, 100, 100, 100, 100),
            _bar(2, 90, 91, 88, 89)]
    e = _entry(bars, 1, "LONG", atr=1.0)
    trades = [L.simulate_trade(bars, [1.0, 1.0, 1.0], e, policy="fixed_tp_sl_time",
                               params={"sl_pct": 0.05, "tp_pct": 0.2, "max_hold": 2},
                               costs=COSTS, funding=[]) for _ in range(3)]
    m = L.compute_metrics(trades, COSTS)
    assert m["gap_adverse_count"] == 3
    assert m["gap_adverse_slippage_bps_estimate"] > 0


def test_chronological_split_not_called_rolling_walk_forward():
    # the single split is explicitly named and NOT a rolling walk-forward
    assert hasattr(L, "chronological_oos_single_split")
    ev = L.evaluate_candidate([_mk_trade(0.01, ts) for ts in range(40)], costs=COSTS,
                              min_trades=10, wf_mode=L.WF_SPLIT,
                              data_classification=L.CLS_INTERMEDIATE)
    assert "walk_forward_not_rolling_single_split_only" in ev["warnings"]
    assert ev["walk_forward"]["wf_is_rolling"] is False
    # a single split can never reach the top research tier
    assert ev["candidate_status"] in (L.CAND_REJECTED, L.CAND_WEAK)


def test_rolling_walk_forward_creates_multiple_forward_folds():
    trades = [_mk_trade(0.01, ts) for ts in range(80)]
    wf = L.rolling_walk_forward(trades, costs=COSTS, train_frac=0.5, test_frac=0.2,
                                step_frac=0.15, min_folds=3)
    assert wf["wf_is_rolling"] is True
    assert wf["wf_folds_total"] >= 3
    assert wf["walk_forward_status"] == L.WF_STATUS_OK


def test_rolling_walk_forward_never_uses_future_in_train():
    trades = [_mk_trade(0.01, ts) for ts in range(80)]
    wf = L.rolling_walk_forward(trades, costs=COSTS, train_frac=0.5, test_frac=0.2,
                                step_frac=0.15, min_folds=3)
    for f in wf["folds"]:
        assert f["train_end"] <= f["test_start"]       # test strictly after train
        assert f["train_start"] < f["train_end"]


def test_candidate_rejected_when_train_good_but_rolling_folds_fail():
    # strong positive cluster early (good train) but every later test window
    # is negative -> rolling pass_rate collapses -> REJECTED.
    trades = ([_mk_trade(0.03, ts) for ts in range(0, 40)]
              + [_mk_trade(-0.005, ts) for ts in range(40, 80)])
    ev = L.evaluate_candidate(trades, costs=COSTS, min_trades=10, wf_mode=L.WF_ROLLING,
                              wf_train_frac=0.5, wf_test_frac=0.2, wf_step_frac=0.15,
                              wf_min_folds=3, data_classification=L.CLS_INTERMEDIATE)
    assert ev["candidate_status"] == L.CAND_REJECTED
    assert any("rolling_wf_pass_rate_too_low" in r for r in ev["rejection_reasons"])


def test_summary_flags_comparison_not_portfolio(tmp_path):
    sample = tmp_path / "s"
    _write_fixture(sample, ["BTCUSDT", "ETHUSDT"], {"BTCUSDT": 0.012, "ETHUSDT": 0.01})
    rep = L.run_trailing_exit_lab(
        sample_dir=str(sample), symbols=["BTCUSDT", "ETHUSDT"], timeframes=["4h"],
        sides=["LONG", "SHORT"], entry_families=["breakout_momentum"],
        exit_policies=["atr_trailing"], min_trades=10, max_grid_combos=50,
        walk_forward_mode="rolling", data_classification=L.CLS_INTERMEDIATE)
    assert rep["comparison_not_portfolio"] is True
    assert rep["entries_are_baseline_not_validated_edge"] is True
    assert rep["candidates_are_hypotheses_not_signals"] is True
    assert rep["edge_validated"] is False
    assert "global_policy_comparison_metrics" in rep
    run_dir = L.write_reports(rep, output_dir=str(tmp_path / "out"))
    summ = json.loads(Path(run_dir, "summary.json").read_text(encoding="utf-8"))
    assert summ["comparison_not_portfolio"] is True
    assert summ["paper_ready"] is False and summ["live_ready"] is False


def test_report_flags_candidates_not_signals(tmp_path):
    sample = tmp_path / "s"
    _write_fixture(sample, ["BTCUSDT", "ETHUSDT"], {"BTCUSDT": 0.012, "ETHUSDT": 0.01})
    rep = L.run_trailing_exit_lab(
        sample_dir=str(sample), symbols=["BTCUSDT", "ETHUSDT"], timeframes=["4h"],
        sides=["LONG", "SHORT"], entry_families=["breakout_momentum"],
        exit_policies=["atr_trailing"], min_trades=10, max_grid_combos=50,
        data_classification=L.CLS_INTERMEDIATE)
    run_dir = L.write_reports(rep, output_dir=str(tmp_path / "out"))
    md = Path(run_dir, "report.md").read_text(encoding="utf-8").lower()
    assert "not signals" in md
    assert "no live" in md


def test_multiple_comparisons_warning_present(tmp_path):
    sample = tmp_path / "s"
    _write_fixture(sample, ["BTCUSDT", "ETHUSDT"], {"BTCUSDT": 0.012, "ETHUSDT": 0.01})
    rep = L.run_trailing_exit_lab(
        sample_dir=str(sample), symbols=["BTCUSDT", "ETHUSDT"], timeframes=["4h"],
        sides=["LONG", "SHORT"],
        entry_families=["breakout_momentum", "volatility_expansion"],
        exit_policies=["atr_trailing", "break_even_lock", "fixed_tp_sl_time"],
        min_trades=10, max_grid_combos=200, data_classification=L.CLS_INTERMEDIATE)
    assert rep["multiple_comparisons_warning"] is True
    assert rep["n_combos_tested"] >= 20
    assert rep["false_discovery_risk"] in ("LOW", "MODERATE", "HIGH")


def test_short_only_candidates_raise_concentration_warning(tmp_path):
    # strong downtrend + SHORT-only -> accepted candidates are all SHORT.
    sample = tmp_path / "s"
    _write_fixture(sample, ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
                   {"BTCUSDT": -0.02, "ETHUSDT": -0.02, "SOLUSDT": -0.02})
    rep = L.run_trailing_exit_lab(
        sample_dir=str(sample), symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        timeframes=["4h"], sides=["SHORT"],
        entry_families=["breakout_momentum", "volatility_expansion"],
        exit_policies=["atr_trailing", "fixed_tp_sl_time"], min_trades=5,
        max_grid_combos=50, walk_forward_mode="none",
        data_classification=L.CLS_INTERMEDIATE)
    if rep["n_research_candidates"] >= 1:
        assert rep["side_concentration_warning"] == "SHORT_ONLY"


def test_leverage_blocked_without_validated_edge():
    m = L.compute_metrics([_mk_trade(0.02, 1 + i) for i in range(20)], COSTS)
    blocked = L.leverage_simulation(m, sl_pct=0.02, edge_validated=False)
    assert blocked["leverage_research_status"] == "BLOCKED_NO_VALIDATED_EDGE"
    assert blocked["real_leverage_allowed"] is False
    # even if an edge were 'validated', real leverage stays forbidden
    openr = L.leverage_simulation(m, sl_pct=0.02, edge_validated=True)
    assert openr["real_leverage_allowed"] is False
    row20 = [r for r in blocked["rows"] if r["leverage"] == 20][0]
    assert row20["dangerous_leverage_flag"] == "DANGEROUS_RESEARCH_ONLY"


def test_no_approved_for_paper_or_live_anywhere(tmp_path):
    sample = tmp_path / "s"
    _write_fixture(sample, ["BTCUSDT", "ETHUSDT"], {"BTCUSDT": 0.012, "ETHUSDT": 0.01})
    rep = L.run_trailing_exit_lab(
        sample_dir=str(sample), symbols=["BTCUSDT", "ETHUSDT"], timeframes=["4h"],
        sides=["LONG", "SHORT"], entry_families=["breakout_momentum"],
        exit_policies=["atr_trailing"], min_trades=10, max_grid_combos=50,
        data_classification=L.CLS_INTERMEDIATE)
    blob = json.dumps({k: v for k, v in rep.items() if k != "_all_trades"}, default=str)
    assert "APPROVED_FOR_PAPER" not in blob
    assert "APPROVED_FOR_LIVE" not in blob
    for c in rep["research_candidates"] + rep["rejected_candidates"]:
        assert c["candidate_status"] in (L.CAND_REJECTED, L.CAND_WEAK, L.CAND_RESEARCH_ONLY)
