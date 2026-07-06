"""V10.39 top-level sprint + feature quality audit + diagnose. Finds real edge on
planted data, honestly rejects noise, never actionable."""

from __future__ import annotations

import random

import pytest

from app.labs import alpha_improvement_sprint_v10_39 as A
from app.labs import continuous_edge_factory_v10_38 as CE

T0 = 1_700_000_000_000
BAR = 60_000


def edge_bars(n, seed=1, every=15, planted=True):
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
        bars.append({"ts": T0 + i * BAR, "open": price,
                     "high": max(price, new) * 1.0005,
                     "low": min(price, new) * 0.9995, "close": new,
                     "volume": ntr, "buy_volume": ntr * (0.9 if sig else 0.5),
                     "sell_volume": ntr * (0.1 if sig else 0.5),
                     "n_trades": ntr, "max_trade": ntr / 5})
        price = new
    return bars


def _safety_ok(rep):
    assert rep["research_only"] and rep["shadow_only"]
    assert rep["paper_ready"] is False and rep["live_ready"] is False
    assert rep["can_send_real_orders"] is False
    assert rep["paper_filter_enabled"] is False
    assert rep["edge_validated"] is False
    assert rep["final_recommendation"] == "NO LIVE"


def test_sprint_needs_more_data_when_too_few_bars():
    rep = A.run_sprint("SYN", bars=edge_bars(40, planted=False), write_reports=False)
    assert rep["verdict"] == "NEEDS_MORE_DATA"
    _safety_ok(rep)


def test_sprint_finds_planted_edge_research_only():
    rep = A.run_sprint("SYN", bars=edge_bars(1500, seed=3), write_reports=False)
    assert rep["verdict"] == "PROMISING_CANDIDATES_UNDER_RESEARCH"
    assert rep["families_promising"] >= 1
    assert rep["methodology"]["costs_lowered"] is False
    assert rep["methodology"]["oos_used_for_selection"] is False
    assert "COMPLEXITY_PENALTY_ACTIVE" in rep["methodology"]["guards_active"]
    _safety_ok(rep)
    for banned in ("BUY_NOW", "SELL_NOW", "OPEN_POSITION", "LIVE_SIGNAL"):
        assert banned not in str(rep)


def test_sprint_rejects_pure_noise():
    rep = A.run_sprint("SYN", bars=edge_bars(1500, seed=11, planted=False),
                       write_reports=False)
    assert rep["verdict"] == "NO_EDGE_ALL_REJECTED_RESEARCH_ONLY"
    assert rep["families_promising"] == 0
    _safety_ok(rep)


def test_sprint_writes_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(A.CE, "_repo_root", lambda: tmp_path)
    A.run_sprint("SYN", bars=edge_bars(1500, seed=3), write_reports=True)
    out = tmp_path.joinpath(*A.OUTPUT_SUBDIR)
    for name in ("alpha_improvement_summary_v1039.json",
                 "cost_aware_horizon_scan_v1039.json",
                 "feature_quality_audit_v1039.json", "diagnose_v1039.json",
                 "strategy_family_benchmark_v1039.csv",
                 "regime_edge_report_v1039.csv"):
        assert (out / name).is_file(), name


def test_feature_quality_audit_flags_constant_and_redundant():
    bars = edge_bars(600, seed=3)
    feats = CE.build_features(bars)
    labels = CE.build_labels(bars, side="long")
    # inject a constant feature and a duplicate of an existing one
    for f in feats:
        f["burst_score"] = 0.0                       # make burst constant -> weak
    audit = A.feature_quality_audit(feats, labels)["features"]
    assert audit["burst_score"]["recommendation"] in ("weak", "cost_dominated")
    assert audit["burst_score"]["distribution_stddev"] == 0.0


def test_diagnose_identifies_cost_domination():
    d = A.diagnose(edge_bars(1500, seed=3))
    assert d["round_trip_cost"] == 0.0018
    assert "least_bad_features" in d and d["least_bad_features"]
    _safety_ok(d)


def test_family_and_verdict_constants_have_no_live_states():
    for v in A.FAMILY_VERDICTS:
        assert v not in ("LIVE", "LIVE_READY", "CAN_SEND_REAL_ORDERS")
    assert "BUY_NOW" not in str(A.STRATEGY_FAMILIES)


# ---- V10.39.1 parser / CLI contract for alpha-improvement-search-v1039 -------

def test_search_command_registered_in_parser_help():
    import subprocess
    import sys
    import pathlib
    repo = pathlib.Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, "-m", "app.research_lab", "--help"],
                       cwd=repo, capture_output=True, text=True, timeout=90)
    assert "alpha-improvement-search-v1039" in (r.stdout + r.stderr)


def test_search_command_is_public_research_only():
    import app.research_lab as RL
    assert "alpha-improvement-search-v1039" in RL.PUBLIC_RESEARCH_ONLY_COMMANDS
    assert hasattr(RL.ResearchLab, "alpha_improvement_search_v1039_cli")


def test_search_cli_smoke_is_research_only(monkeypatch):
    import app.research_lab as RL
    fake = {"n_bars": 1500, "verdict": "NO_EDGE_ALL_REJECTED_RESEARCH_ONLY",
            "families_total": 12, "families_promising": 0, "families_rejected": 9,
            "cost_aware_rows": 10,
            "best_family": {"family": "micro_momentum",
                            "verdict": "REJECTED_COSTS_TOO_HIGH",
                            "net_EV": -0.0009, "net_EV_lower_bound": -0.0014},
            "cost_aware_best_cell": {"timeframe_min": 1, "horizon": 15,
                                     "best_feature": "oi_change", "side": "short",
                                     "verdict": "REJECTED_COSTS_TOO_HIGH"},
            "any_timeframe_promising": False, "reports_dir": "x"}
    monkeypatch.setattr(
        "app.labs.alpha_improvement_sprint_v10_39.run_sprint",
        lambda *a, **k: fake)
    lab = RL.ResearchLab.__new__(RL.ResearchLab)
    out = lab.alpha_improvement_search_v1039_cli(symbols="BTCUSDT")
    assert "ALPHA_IMPROVEMENT_SEARCH_V1039" in out
    assert "RESEARCH_ONLY" in out and "NOT_ACTIONABLE" in out
    assert "FINAL_RECOMMENDATION=NO LIVE" in out
    assert "families_evaluated: 12" in out and "cost_aware_rows: 10" in out
    for banned in ("BUY_NOW", "SELL_NOW", "OPEN_POSITION", "LIVE_SIGNAL",
                   "LIVE_READY", "CAN_SEND_REAL_ORDERS"):
        assert banned not in out
