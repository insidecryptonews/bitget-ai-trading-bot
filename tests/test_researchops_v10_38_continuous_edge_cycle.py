"""V10.38 Continuous edge cycle + future micro-live scaffold (both blocked)."""

from __future__ import annotations

import random

import pytest

from app.labs import continuous_edge_factory_v10_38 as CE

T0 = 1_700_000_000_000
BAR = 60_000


def edge_bars(n, seed=1, every=5, planted=True):
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


def test_cycle_needs_more_data_when_too_few_bars():
    rep = CE.run_cycle("BTCUSDT", bars=edge_bars(20, planted=False),
                       write_reports=False)
    assert rep["verdict"] == "NEEDS_MORE_DATA"
    assert rep["can_send_real_orders"] is False


def test_full_cycle_is_research_only_and_blocked():
    rep = CE.run_cycle("BTCUSDT", bars=edge_bars(900, seed=3), write_reports=False)
    assert rep["verdict"] == "CANDIDATES_UNDER_RESEARCH"
    assert rep["candidates_total"] > 0
    assert "promising" in rep and "rejected" in rep
    assert rep["paper_gate"].startswith("BLOCKED")
    assert rep["future_micro_live"] == "FUTURE_MICRO_LIVE_BLOCKED"
    assert "human_approval_required" in rep["blockers"]
    # every top candidate carries an honest, non-actionable verdict
    for c in rep["top_candidates"]:
        assert c["verdict"] in CE.CANDIDATE_VERDICTS
        assert c["status"] in CE.CANDIDATE_STATES
    # safety contract + no actionable tokens anywhere in the summary
    assert rep["research_only"] and rep["shadow_only"]
    assert rep["edge_validated"] is False and rep["paper_filter_enabled"] is False
    assert rep["final_recommendation"] == "NO LIVE"
    for banned in ("BUY_NOW", "SELL_NOW", "OPEN_POSITION", "LIVE_SIGNAL"):
        assert banned not in str(rep)


def test_cycle_writes_reports(tmp_path, monkeypatch):
    # redirect repo root so the cycle writes its reports under a temp dir
    monkeypatch.setattr(CE, "_repo_root", lambda: tmp_path)
    CE.run_cycle("BTCUSDT", bars=edge_bars(900, seed=4), write_reports=True)
    out = tmp_path.joinpath(*CE.OUTPUT_SUBDIR)
    for name in ("continuous_edge_summary_v1038.json",
                 "walk_forward_report_v1038.json", "drift_report_v1038.json",
                 "promotion_gate_report_v1038.json",
                 "candidate_rankings_v1038.csv",
                 "shadow_policy_metrics_v1038.csv"):
        assert (out / name).is_file(), name


def test_future_micro_live_scaffold_always_blocked():
    sc = CE.future_micro_live_scaffold()
    assert sc["scaffold"] == "FUTURE_MICRO_LIVE_BLOCKED"
    assert sc["actual_live_ready"] is False
    assert sc["can_send_real_orders"] is False
    assert sc["human_promotion_required"] is True
    assert sc["shadow_learner_separate_from_executor"] is True
    assert all(s["implemented"] is False for s in sc["safeguards"].values())
    assert set(sc["safeguards"]) == set(CE.FUTURE_MICRO_LIVE_SAFEGUARDS)


def test_scaffold_rejects_forbidden_policy_state():
    with pytest.raises(ValueError):
        CE.future_micro_live_scaffold({"policy_id": "x", "version": 1,
                                       "status": "LIVE_READY"})
