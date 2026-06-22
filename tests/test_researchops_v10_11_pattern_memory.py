"""ResearchOps V10.11 — Pattern Memory & Similarity Decision Gate tests.

Pure/offline/deterministic. Verifies causal feature vectors, similarity search,
the fail-closed shadow gate (insufficient cases / negative EV / cost stress /
window+symbol concentration), closed-green/green-to-red metrics, and the hard
invariant that NOTHING is ever approved for paper/live (shadow only).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.labs import pattern_memory_v10_11 as P

MODULE_PATH = "app/labs/pattern_memory_v10_11.py"
TF_MS = {"4h": 240 * 60_000, "6h": 360 * 60_000}


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
        bars.append({"ts": t + i * bar, "open": o, "high": h, "low": l, "close": c, "volume": 10})
        p = c
    return bars


def _write_sample(sample_dir, symbols, tf="6h", n=300, seed_base=0):
    os.makedirs(sample_dir, exist_ok=True)
    for j, s in enumerate(symbols):
        bars = _trend_bars(n, tf, seed=seed_base + j + 1)
        lines = ["timestamp,open,high,low,close,volume"] + \
                [f"{b['ts']},{b['open']},{b['high']},{b['low']},{b['close']},10" for b in bars]
        Path(sample_dir, f"{s}_{tf}_ohlcv.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
        Path(sample_dir, f"{s}_funding.csv").write_text(
            "timestamp,funding_rate\n" + "\n".join(f"{b['ts']},0.0001" for b in bars) + "\n",
            encoding="utf-8")


def _build(tmp_path, symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT"), tf="6h", n=260):
    sample = tmp_path / "s"
    _write_sample(sample, list(symbols), tf, n)
    return P.build_pattern_memory(
        sample_dir=str(sample), symbols=list(symbols), timeframes=[tf],
        sides=["LONG", "SHORT"], strategy_families=["micro_breakout", "volatility_burst_scalp"],
        exit_policies=["micro_profit_take", "instant_green_lock"])


def _mk_case(net, ts, symbol="BTCUSDT", month=0, went_green=False, side="LONG",
             tf="6h", strat="micro_breakout"):
    return {"pattern_id": f"{symbol}{ts}", "symbol": symbol, "timeframe": tf, "side": side,
            "strategy_family": strat, "exit_policy": "micro_profit_take",
            "entry_ts": ts, "net_result": net, "gross_result": net + 0.0022,
            "closed_green": net > 0, "went_green": went_green,
            "green_to_red_failure": bool(went_green and net <= 0),
            "MFE": abs(net) + 0.001, "MAE": 0.002, "month_bucket": month,
            "features": {"side": side, "timeframe": tf, "strategy_family": strat,
                         "recent_return": 0.001, "body_range_ratio": 0.5, "atr_pct": 0.01,
                         "volume_proxy": 1.0, "breakout_distance": 0.5, "pullback_distance": 0.1,
                         "setup_quality": 0.6, "vol_regime": "low_volatility",
                         "trend_regime": "range", "funding_regime": "flat"}}


# 1. feature vector no-lookahead
def test_feature_vector_no_lookahead(tmp_path):
    mem = _build(tmp_path)
    assert mem["errors"] == [] and mem["n_cases"] > 0
    for c in mem["cases"][:50]:
        assert c["features"]["no_lookahead"] is True
        assert c["entry_ts"] >= 1700000000000


# 2/8/9. similarity search + closed-green/green-to-red metrics
def test_similarity_search_and_metrics():
    cases = ([_mk_case(0.01, 1700000000000 + i * P.DAY_MS, "BTCUSDT", month=i % 3, went_green=True) for i in range(40)]
             + [_mk_case(-0.01, 1700000000000 + i * P.DAY_MS, "ETHUSDT", month=i % 3, went_green=True) for i in range(40)])
    q = P.query_similar(cases, _mk_case(0.0, 0)["features"], min_similar=30)
    assert q["similar_cases_count"] == 80
    assert 0.0 <= q["closed_green_rate"] <= 1.0
    assert q["green_to_red_rate"] > 0   # the -0.01 went-green cases failed
    assert q["symbols_covered"] == 2


# 3. too few cases fails
def test_query_few_cases_fails():
    cases = [_mk_case(0.05, 1700000000000 + i * P.DAY_MS) for i in range(5)]
    q = P.query_similar(cases, _mk_case(0.0, 0)["features"], min_similar=30)
    assert q["decision"] == P.F_FEW


# 4. negative EV fails
def test_query_negative_ev_fails():
    cases = [_mk_case(-0.01, 1700000000000 + i * P.DAY_MS,
                      symbol=("BTCUSDT" if i % 2 else "ETHUSDT"), month=i % 3) for i in range(40)]
    q = P.query_similar(cases, _mk_case(0.0, 0)["features"], min_similar=30)
    assert q["decision"] == P.F_EV


# 5. cost stress fail
def test_query_cost_stress_fails():
    # tiny positive net that x2 cost turns negative
    cases = [_mk_case(0.001, 1700000000000 + i * P.DAY_MS,
                      symbol=("BTCUSDT" if i % 2 else "ETHUSDT"), month=i % 3) for i in range(40)]
    q = P.query_similar(cases, _mk_case(0.0, 0)["features"], min_similar=30)
    assert q["decision"] in (P.F_COST, P.F_PF, P.F_GREEN)  # killed by costs/derived gates


# 6. single window fails
def test_query_single_window_fails():
    cases = [_mk_case(0.02, 1700000000000 + i * P.DAY_MS,
                      symbol=("BTCUSDT" if i % 2 else "ETHUSDT"), month=0) for i in range(40)]
    q = P.query_similar(cases, _mk_case(0.0, 0)["features"], min_similar=30)
    assert q["windows_covered"] == 1
    assert q["decision"] == P.F_WIN


# 7. single symbol degrades/fails
def test_query_single_symbol_fails():
    cases = [_mk_case(0.02, 1700000000000 + i * P.DAY_MS, symbol="BTCUSDT", month=i % 3) for i in range(40)]
    q = P.query_similar(cases, _mk_case(0.0, 0)["features"], min_similar=30)
    assert q["symbols_covered"] == 1
    assert q["decision"] in (P.F_SYM, P.F_WIN)  # never PASS on a single coin


# 10/11/12/13/14. safety source scan
def test_module_has_no_dangerous_primitives():
    src = Path(MODULE_PATH).read_text(encoding="utf-8")
    for token in ["place_order", "create_order", "private_get", "private_post",
                  "set_leverage", "set_margin_mode", "ExecutionEngine", "PaperTrader",
                  "import torch", "from torch", "import jax", "import tensorflow",
                  "import timesfm", "load_dotenv", "os.environ", "import requests",
                  "import socket", "urllib.request"]:
        assert token not in src, f"{MODULE_PATH} must not contain {token!r}"
    # the only mention of APPROVED_* is the explicit "never" declaration
    assert 'never": [' in src and "APPROVED_FOR_PAPER" in src  # documented as forbidden


# 15/16. path safety (no raw/.env writes)
def test_safe_output_base_blocks_unsafe():
    assert P._safe_output_base("external_data/raw") == P.OUTPUT_ROOT
    assert P._safe_output_base("x/../y") == P.OUTPUT_ROOT
    assert P._safe_output_base("backups/x") == P.OUTPUT_ROOT
    assert P._safe_output_base("reports/research/v10_11") == "reports/research/v10_11"


# 17/18/19. build + gate end-to-end on fixture
def test_build_and_gate_end_to_end(tmp_path):
    mem = _build(tmp_path, n=240)
    gate = P.shadow_gate(mem)
    assert gate["n_queries"] >= 1
    # nothing should ever be approved for paper/live
    blob = json.dumps(gate, default=str)
    assert "APPROVED_FOR_PAPER" not in blob and "APPROVED_FOR_LIVE" not in blob
    assert gate["edge_validated"] is False
    assert gate["final_recommendation"] == "NO LIVE"
    run_dir = P.write_pattern_reports(mem, gate, output_dir=str(tmp_path / "out"))
    for fn in ("pattern_memory_summary.json", "pattern_cases.csv",
               "similarity_queries.csv", "shadow_gate_decisions.csv",
               "pattern_candidate_ranking.csv", "rejected_patterns.csv", "report.md"):
        assert os.path.isfile(os.path.join(run_dir, fn)), fn
    md = Path(run_dir, "report.md").read_text(encoding="utf-8").lower()
    assert "not signals" in md and "no live" in md


# 20. plan flags NO LIVE
def test_plan_flags_no_live():
    p = P.pattern_memory_plan()
    assert p["research_only"] and p["shadow_only"]
    assert p["paper_ready"] is False and p["live_ready"] is False
    assert p["edge_validated"] is False
    assert "APPROVED_FOR_PAPER" in p["never"] and "APPROVED_FOR_LIVE" in p["never"]
    assert p["final_recommendation"] == "NO LIVE"


# extra: every query decision in the gate is shadow-only (never approval)
def test_gate_decisions_never_approve(tmp_path):
    mem = _build(tmp_path, n=240)
    gate = P.shadow_gate(mem)
    valid = {P.PASS, P.F_FEW, P.F_EV, P.F_PF, P.F_GREEN, P.F_AVGLOSS, P.F_COST,
             P.F_WIN, P.F_SYM, P.F_DD, P.F_FD}
    for d in gate["decisions"].values():
        assert d in valid
        assert "APPROVED" not in d


def test_build_refuses_missing_sample():
    mem = P.build_pattern_memory(sample_dir="does/not/exist", symbols=["BTCUSDT"],
                                 timeframes=["6h"], sides=["LONG"],
                                 strategy_families=["micro_breakout"])
    assert "sample_dir_not_found" in mem["errors"]
    assert mem["final_recommendation"] == "NO LIVE"
