"""ResearchOps V10.12 - Quality-Gated Intelligent Shadow Scalper tests.

Pure/offline/deterministic. Verifies the quality pre-gate (cost/vol/range/spread/
risk/duplicate + pattern-memory + false-discovery), intraday data readiness, the
real-time shadow runner (no orders, no private exchange, journal path-safe), and
the integrated intelligent scalper - plus the hard invariant that NOTHING is ever
approved for paper/live (shadow only, paper_candidate_future always False).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.labs import high_quality_shadow_v10_12 as H

MODULE_PATH = "app/labs/high_quality_shadow_v10_12.py"
TF_MS = {"4h": 240 * 60_000, "6h": 360 * 60_000}


# ---- synthetic fixtures ----------------------------------------------------

def _bars(n, tf="6h", start=100.0, drift=0.0008, vol=0.006, seed=1):
    import random as _r
    rng = _r.Random(seed)
    out, p, t = [], start, 1700000000000
    bar = TF_MS[tf]
    for i in range(n):
        o = p
        c = p * (1 + drift + rng.uniform(-vol, vol))
        h = max(o, c) * (1 + abs(rng.uniform(0, vol)))
        l = min(o, c) * (1 - abs(rng.uniform(0, vol)))
        out.append({"ts": t + i * bar, "open": o, "high": h, "low": l, "close": c, "volume": 10})
        p = c
    return out


def _write_sample(sample_dir, symbols, tf="6h", n=300, seed_base=0):
    os.makedirs(sample_dir, exist_ok=True)
    for j, s in enumerate(symbols):
        bars = _bars(n, tf, seed=seed_base + j + 1)
        lines = ["timestamp,open,high,low,close,volume"] + \
                [f"{b['ts']},{b['open']},{b['high']},{b['low']},{b['close']},10" for b in bars]
        Path(sample_dir, f"{s}_{tf}_ohlcv.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
        Path(sample_dir, f"{s}_funding.csv").write_text(
            "timestamp,funding_rate\n" + "\n".join(f"{b['ts']},0.0001" for b in bars) + "\n",
            encoding="utf-8")


def _mem(net, count, *, symbols=("BTCUSDT", "ETHUSDT"), months=3, side="LONG",
         tf="6h", strat="micro_breakout", went_green=True, start_day=0):
    cases = []
    for i in range(count):
        sym = symbols[i % len(symbols)]
        cases.append({
            "pattern_id": f"{sym}{start_day}_{i}", "symbol": sym, "timeframe": tf, "side": side,
            "strategy_family": strat, "exit_policy": "micro_profit_take",
            "entry_ts": 1700000000000 + (start_day + i) * H.DAY_MS, "net_result": net,
            "gross_result": net + 0.0022, "closed_green": net > 0,
            "went_green": went_green, "green_to_red_failure": bool(went_green and net <= 0),
            "MFE": abs(net) + 0.001, "MAE": 0.002, "month_bucket": i % months,
            "features": {"side": side, "timeframe": tf, "strategy_family": strat,
                         "recent_return": 0.001, "body_range_ratio": 0.5, "atr_pct": 0.01,
                         "volume_proxy": 1.0, "breakout_distance": 0.5, "pullback_distance": 0.1,
                         "setup_quality": 0.6, "vol_regime": "low_volatility",
                         "trend_regime": "range", "funding_regime": "flat"}})
    return cases


def _qf(side="LONG", tf="6h", strat="micro_breakout"):
    return {"side": side, "timeframe": tf, "strategy_family": strat, "symbol": "BTCUSDT",
            "recent_return": 0.001, "body_range_ratio": 0.5, "atr_pct": 0.01,
            "volume_proxy": 1.0, "breakout_distance": 0.5, "pullback_distance": 0.1,
            "setup_quality": 0.6, "vol_regime": "low_volatility",
            "trend_regime": "range", "funding_regime": "flat"}


# candidate evaluated far AFTER all default _mem cases (so they count as "past")
FUTURE_TS = 1700000000000 + 10_000 * H.DAY_MS


def _strong(**over):
    kw = dict(side="LONG", timeframe="6h", strategy_family="micro_breakout", signal_idx=100,
              tp_pct=0.006, sl_pct=0.005, atr_pct=0.01, range_to_atr=1.5, recent_return=0.02,
              round_trip_fraction=0.0022, spread_fraction=0.0002, setup_quality=0.6,
              candidate_entry_ts=FUTURE_TS)
    kw.update(over)
    return kw


# 1. quality gate blocks when cost > target
def test_quality_gate_blocks_cost_too_high():
    r = H.quality_pre_gate(**_strong(tp_pct=0.002))   # rt 0.0022 / 0.002 = 1.1 >= 0.6
    assert r["quality_gate_decision"] == H.Q_COST
    assert r["shadow_allowed"] is False


# 2. quality gate blocks low volatility
def test_quality_gate_blocks_low_vol():
    r = H.quality_pre_gate(**_strong(atr_pct=0.0005))
    assert r["quality_gate_decision"] == H.Q_VOL
    assert r["shadow_allowed"] is False


# 3. quality gate blocks negative-EV pattern memory
def test_quality_gate_blocks_negative_pattern_memory():
    r = H.quality_pre_gate(**_strong(features=_qf(), memory_cases=_mem(-0.01, 40)))
    assert r["quality_gate_decision"] == H.Q_EV
    assert r["pattern_memory_decision"] == "FAIL_NEGATIVE_EV"
    assert r["shadow_allowed"] is False


# 4. quality gate allows a strong synthetic setup with winning memory
def test_quality_gate_allows_strong_setup():
    r = H.quality_pre_gate(**_strong(features=_qf(), memory_cases=_mem(0.01, 40)))
    assert r["quality_gate_decision"] == H.Q_PASS
    assert r["pattern_memory_decision"] == "PASS_SHADOW_GATE"
    assert r["shadow_allowed"] is True


# 11. false-discovery HIGH is never accepted (even with winning memory)
def test_quality_gate_blocks_false_discovery_high():
    r = H.quality_pre_gate(**_strong(features=_qf(), memory_cases=_mem(0.01, 40),
                                     false_discovery_risk="HIGH"))
    assert r["quality_gate_decision"] == H.Q_FD
    assert r["shadow_allowed"] is False


# extra: duplicate signal too close is blocked
def test_quality_gate_blocks_duplicate_signal():
    r = H.quality_pre_gate(**_strong(features=_qf(), memory_cases=_mem(0.01, 40),
                                     signal_idx=101, dup_last_idx=100))
    assert r["quality_gate_decision"] == H.Q_DUP


# extra: stop too large blocked
def test_quality_gate_blocks_risk_too_high():
    r = H.quality_pre_gate(**_strong(sl_pct=0.05))
    assert r["quality_gate_decision"] == H.Q_RISK


# 8. intraday readiness detects absence of 1m/5m
def test_intraday_readiness_no_intraday(tmp_path):
    sample = tmp_path / "s"
    _write_sample(sample, ["BTCUSDT", "ETHUSDT"], "6h", 120)
    r = H.intraday_data_readiness(str(sample), ["BTCUSDT", "ETHUSDT"])
    assert r["status"] == "NO_INTRADAY_DATA"
    assert r["has_1m"] is False and r["has_5m"] is False
    assert r["scalping_ready"] is False
    assert r["missing_orderbook"] and r["missing_trades"]
    assert r["final_recommendation"] == "NO LIVE"


def test_intraday_readiness_detects_1m(tmp_path):
    sample = tmp_path / "s"
    _write_sample(sample, ["BTCUSDT", "ETHUSDT"], "6h", 120)
    # add 1m files (short coverage -> partial, not scalping ready)
    bars = _bars(200, "6h", seed=9)  # generator reused; written under the _1m_ suffix
    for s in ("BTCUSDT", "ETHUSDT"):
        lines = ["timestamp,open,high,low,close,volume"] + \
                [f"{b['ts']},{b['open']},{b['high']},{b['low']},{b['close']},10" for b in bars]
        Path(sample, f"{s}_1m_ohlcv.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = H.intraday_data_readiness(str(sample), ["BTCUSDT", "ETHUSDT"])
    assert r["has_1m"] is True
    assert r["status"] in ("PARTIAL_INTRADAY_DATA", "INTRADAY_RESEARCH_READY")


def _run(tmp_path, symbols=("BTCUSDT", "ETHUSDT"), n=300, **over):
    sample = tmp_path / "s"
    _write_sample(sample, list(symbols), "6h", n)
    kw = dict(sample_dir=str(sample), symbols=list(symbols), timeframes=["6h"],
              sides=["LONG", "SHORT"], strategy_families=["micro_breakout", "micro_reversal"],
              mode="offline-replay", max_candidates_per_run=300)
    kw.update(over)
    return H.run_intelligent_shadow(**kw)


# 9. intelligent scalper rejects setups without sufficient memory (tiny sample)
def test_scalper_rejects_insufficient_memory(tmp_path):
    rep = _run(tmp_path, symbols=("BTCUSDT",), n=80)
    assert rep["errors"] == []
    assert rep["n_shadow_trades"] == 0
    assert H.Q_NOSIM in rep["rejection_breakdown"]


# 10. intelligent scalper rejects negative-EV setups (no shadow trades survive)
def test_scalper_rejects_negative_ev(tmp_path):
    rep = _run(tmp_path, n=320)
    assert rep["errors"] == []
    assert rep["n_shadow_trades"] == 0
    bd = rep["rejection_breakdown"]
    assert (H.Q_EV in bd) or (H.Q_NOSIM in bd)
    # any setup that DID pass must have had a PASS pattern-memory decision
    for t in rep["shadow_trades"]:
        assert t["pattern_memory_decision"] == "PASS_SHADOW_GATE"


# 5/20/21. runner sends no orders / no order primitives reachable
def test_runner_sends_no_orders(tmp_path):
    rep = _run(tmp_path, mode="offline-replay")
    assert rep["can_send_real_orders"] is False
    assert rep["paper_ready"] is False and rep["live_ready"] is False
    # the run produced trade records but they are SHADOW only
    for t in rep["shadow_trades"]:
        assert t.get("would_enter") is True   # simulated, not real
    blob = json.dumps(rep, default=str)
    for tok in ("place_order", "create_order", "set_leverage", "set_margin_mode"):
        assert tok not in blob


# 6. forward shadow does not use a private exchange
def test_forward_shadow_no_private_exchange(tmp_path):
    rep = _run(tmp_path, mode="forward-shadow")
    assert rep["mode"] == "forward-shadow"
    assert rep["can_send_real_orders"] is False
    assert "INTRADAY_DATA_REQUIRED" in rep["warnings"]   # no 1m/5m -> non-conclusive
    assert rep["scalping_conclusive"] is False
    src = Path(MODULE_PATH).read_text(encoding="utf-8")
    for tok in ("private_get", "private_post", "ACCESS-KEY", "api.bitget.com"):
        assert tok not in src


# 7. journal writes only under reports/research/v10_12
def test_journal_path_safe(tmp_path):
    rep = _run(tmp_path)
    # unsafe output dir must be redirected to the canonical root
    run_dir = H.write_shadow_journal(rep, output_dir="external_data/raw/evil")
    assert run_dir.startswith("reports/research/v10_12")
    assert os.path.isfile(os.path.join(run_dir, "journal.csv"))
    safe = H.write_shadow_journal(rep, output_dir=str(tmp_path / "out"))
    assert os.path.isfile(os.path.join(safe, "journal.csv"))


def test_reports_written_and_path_safe(tmp_path):
    rep = _run(tmp_path)
    run_dir = H.write_v1012_reports(rep, output_dir="backups/x")
    assert run_dir.startswith("reports/research/v10_12")
    for fn in ("intelligent_shadow_summary.json", "quality_gate_decisions.csv",
               "pattern_memory_decisions.csv", "shadow_trades.csv", "rejected_setups.csv",
               "candidate_quality_ranking.csv", "report.md"):
        assert os.path.isfile(os.path.join(run_dir, fn)), fn
    md = Path(run_dir, "report.md").read_text(encoding="utf-8").lower()
    assert "no live" in md and "not signals" in md


# 12/13/14/22. safety flags on the run report
def test_run_report_safety_flags(tmp_path):
    rep = _run(tmp_path)
    assert rep["research_only"] is True and rep["shadow_only"] is True
    assert rep["paper_ready"] is False and rep["live_ready"] is False
    assert rep["can_send_real_orders"] is False
    assert rep["paper_filter_enabled"] is False
    assert rep["paper_candidate_future"] is False
    assert rep["paper_candidate"]["paper_candidate_future"] is False
    assert rep["final_recommendation"] == "NO LIVE"


# 15/16/17/18/19/20/21. source-level safety scan
def test_module_has_no_dangerous_primitives():
    import re
    src = Path(MODULE_PATH).read_text(encoding="utf-8")
    # the 'never' list quotes the forbidden nouns purely to DECLARE them off-limits;
    # strip that block so the behavioral scan does not flag its own declaration.
    scan = re.sub(r'"never":\s*\[.*?\],', '"never": [],', src, flags=re.DOTALL)
    # unambiguous import/access forms must be wholly absent
    for token in ["import torch", "from torch", "import jax", "import tensorflow",
                  "import timesfm", "load_dotenv", "os.environ", "import requests",
                  "import socket", "urllib.request", "db.execute", "INSERT INTO",
                  "ACCESS-KEY", "api.bitget.com"]:
        assert token not in scan, f"{MODULE_PATH} must not contain {token!r}"
    # order/exchange/exec primitives must not appear as CALLS or attribute access
    for name in ["place_order", "create_order", "private_get", "private_post",
                 "set_leverage", "set_margin_mode", "open_position"]:
        assert f"{name}(" not in scan and f".{name}" not in scan, name
    for cls in ["ExecutionEngine", "PaperTrader"]:
        assert f"{cls}(" not in scan and f"import {cls}" not in scan, cls
    # APPROVED_* appears ONLY in the explicit 'never' declaration (positive check)
    assert "APPROVED_FOR_PAPER" in src and "APPROVED_FOR_LIVE" in src
    assert "APPROVED_FOR_PAPER" not in scan and "APPROVED_FOR_LIVE" not in scan


# nothing is ever approved in any emitted report
def test_never_approves(tmp_path):
    rep = _run(tmp_path)
    blob = json.dumps(rep, default=str)
    assert "APPROVED_FOR_PAPER" not in blob and "APPROVED_FOR_LIVE" not in blob


# paper-readiness criteria are defined but never activate paper
def test_paper_readiness_criteria_defined_not_active():
    c = H.paper_readiness_criteria()
    assert c["min_shadow_trades"] == 200
    assert c["paper_candidate_future"] is False
    assert c["final_recommendation"] == "NO LIVE"


# plan flags NO LIVE and lists the forbidden primitives
def test_plan_flags_no_live():
    p = H.intelligent_shadow_plan()
    assert p["research_only"] and p["shadow_only"]
    assert p["paper_ready"] is False and p["live_ready"] is False
    assert p["paper_candidate_future"] is False
    assert "APPROVED_FOR_PAPER" in p["never"] and "set_leverage" in p["never"]
    assert p["final_recommendation"] == "NO LIVE"


# ==========================================================================
# V10.12.1 - no-lookahead pattern memory (prefix-only) hotfix
# ==========================================================================
T0 = 1700000000000


def _ts(day):
    return T0 + day * H.DAY_MS


# 1. FUTURE positive memory cannot approve a PAST setup
def test_future_memory_cannot_approve_past_setup():
    future_winners = _mem(0.01, 40, start_day=200)   # all AFTER the candidate
    r = H.quality_pre_gate(**_strong(features=_qf(), memory_cases=future_winners,
                                     candidate_entry_ts=_ts(100)))
    assert r["memory_cases_future_excluded"] == 40
    assert r["memory_cases_prefix_used"] == 0
    assert r["shadow_allowed"] is False
    assert r["quality_gate_decision"] == H.Q_NOSIM


# 2. PAST positive memory CAN clear the gate in a controlled fixture
def test_past_memory_can_pass_gate():
    past_winners = _mem(0.01, 40, start_day=0)        # all BEFORE the candidate
    r = H.quality_pre_gate(**_strong(features=_qf(), memory_cases=past_winners,
                                     candidate_entry_ts=_ts(100)))
    assert r["no_lookahead_status"] == "OK_PREFIX_ONLY"
    assert r["memory_cases_prefix_used"] == 40
    assert r["memory_cases_future_excluded"] == 0
    assert r["quality_gate_decision"] == H.Q_PASS
    assert r["shadow_allowed"] is True


# 3. mutating FUTURE outcomes does not change the PAST decision
def test_future_mutation_does_not_change_past_decision():
    past = _mem(0.01, 40, start_day=0)
    fut_a = _mem(-0.01, 20, start_day=200)
    fut_b = _mem(99.0, 20, start_day=200)            # absurdly positive future
    d1 = H.quality_pre_gate(**_strong(features=_qf(), memory_cases=past + fut_a,
                                      candidate_entry_ts=_ts(100)))
    d2 = H.quality_pre_gate(**_strong(features=_qf(), memory_cases=past + fut_b,
                                      candidate_entry_ts=_ts(100)))
    assert d1["quality_gate_decision"] == d2["quality_gate_decision"]
    assert d1["memory_cases_prefix_used"] == d2["memory_cases_prefix_used"] == 40
    assert d1["pattern_net_EV"] == d2["pattern_net_EV"]


# 4. same-timestamp case is excluded
def test_same_timestamp_excluded():
    cases = _mem(0.01, 1, start_day=100)              # exactly at candidate ts
    past, meta = H.prefix_memory_cases_for_candidate(cases, _ts(100))
    assert meta["same_timestamp_excluded"] == 1
    assert meta["memory_cases_prefix_used"] == 0
    assert past == []


# 4b. own pattern_id excluded; missing candidate_entry_ts fails closed
def test_prefix_fail_closed_and_self_exclusion():
    cases = _mem(0.01, 3, start_day=0)
    past, meta = H.prefix_memory_cases_for_candidate(
        cases, _ts(100), candidate_pattern_id=cases[0]["pattern_id"])
    assert meta["self_excluded"] == 1 and meta["memory_cases_prefix_used"] == 2
    past2, meta2 = H.prefix_memory_cases_for_candidate(cases, None)
    assert meta2["no_lookahead_status"] == "FAIL_MISSING_CANDIDATE_ENTRY_TS"
    assert past2 == [] and "missing_candidate_entry_ts_no_memory_used" in meta2["warnings"]
    # the gate must also fail closed when candidate_entry_ts is missing
    g = H.quality_pre_gate(**_strong(features=_qf(), memory_cases=_mem(0.01, 40),
                                     candidate_entry_ts=None))
    assert g["no_lookahead_status"] == "FAIL_MISSING_CANDIDATE_ENTRY_TS"
    assert g["quality_gate_decision"] == H.Q_NOSIM


# 5/6. run report carries the no-lookahead audit + the clear funnel metrics
def test_run_report_has_causal_audit_and_clear_metrics(tmp_path):
    rep = _run(tmp_path, n=320)
    assert rep["no_lookahead_status"] == "OK_PREFIX_ONLY"
    for k in ("memory_cases_total", "memory_cases_future_excluded", "same_timestamp_excluded",
              "passed_structural_pre_gate", "failed_structural_pre_gate",
              "passed_pattern_memory_gate", "failed_pattern_memory_gate",
              "passed_full_quality_and_pattern_gate", "n_shadow_trades"):
        assert k in rep, k
    # offline replay over real history must exclude future cases for early setups
    assert rep["memory_cases_future_excluded"] > 0


# 7. full-gate count reconciles with simulated shadow trades
def test_full_gate_reconciles_with_shadow_trades(tmp_path):
    rep = _run(tmp_path, n=320)
    # every full-gate pass becomes a shadow trade unless the sim returns None
    assert rep["n_shadow_trades"] <= rep["passed_full_quality_and_pattern_gate"]
    # with negative-EV synthetic data both are zero (decision machine refuses)
    assert rep["passed_full_quality_and_pattern_gate"] == rep["n_shadow_trades"]


# legacy alias documented (not removed) and equals structural pre-gate
def test_legacy_alias_documented(tmp_path):
    rep = _run(tmp_path, n=320)
    assert rep["passed_quality_gate_legacy_alias"] == rep["passed_structural_pre_gate"]
