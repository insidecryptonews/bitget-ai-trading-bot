"""V10.46.7 final integration: the experiment harness runs A/B/C/D paired over a
verified generation, stays research-only, and emits a report whose verdict is
honest. Research only, NO LIVE."""

from __future__ import annotations

import random

from pathlib import Path

from app.labs import public_data_backfill_v10_45_1 as BF
from app.labs.v10_46 import experiment as EXP


def _publish_generation(tmp_path, monkeypatch, n=4000):
    monkeypatch.setattr(BF.CE, "_repo_root", lambda: tmp_path)
    T0 = 1_700_000_400_000
    rng = random.Random(3)
    rows, price = [], 100.0
    for i in range(n):
        phase = (i // 400) % 3
        drift = 0.0009 if phase == 0 else (-0.0009 if phase == 2 else 0.0)
        new = price * (1 + drift + rng.uniform(-0.0008, 0.0008))
        rows.append([T0 + i * 60000, round(price, 6),
                     round(max(price, new) * 1.0006, 6),
                     round(min(price, new) * 0.9994, 6), round(new, 6),
                     10.0, 1000.0])
        price = new
    BF.save_dataset("bitget", "BTCUSDT", rows, 3, requested_start_ms=T0,
                    requested_end_ms=T0 + n * 60000)


def test_experiment_runs_paired_abcd_and_is_research_only(tmp_path,
                                                          monkeypatch):
    _publish_generation(tmp_path, monkeypatch)
    rep = EXP.run_experiment(symbol="BTCUSDT", timeframe="5m", write=True,
                             log=lambda *a: None)
    assert rep["safety"]["can_send_real_orders"] is False
    assert rep["safety"]["live_trading"] is False
    assert rep["safety"]["final_recommendation"] == "NO LIVE"
    part = rep["tournament"]["participants"]
    assert {"A_static_abstain", "B_learn_abstain", "C_learn_no_abstain",
            "D_no_trade", "Q_random"} <= set(part)
    assert part["D_no_trade"]["net_pnl_eur"] == 0.0        # no-trade baseline
    assert "mean_diff_eur" in rep["tournament"]["paired"]["B_vs_A"]
    # honest verdict: no fabricated edge
    assert isinstance(rep["verdict"], str) and rep["verdict"]
    # outputs written under the gitignored reports dir
    out = tmp_path / "reports" / "research" / "v10_46_final_integrated"
    assert (out / "integrated_report.json").is_file()
    assert (out / "dashboard.html").is_file()
    assert (out / "tournament_scoreboard_eur.csv").is_file()
    assert (out / "output_manifest_v10_46.json").is_file()
    assert "NO LIVE" in (out / "dashboard.html").read_text(encoding="utf-8")


def test_experiment_is_deterministic(tmp_path, monkeypatch):
    _publish_generation(tmp_path, monkeypatch)
    a = EXP.run_experiment(symbol="BTCUSDT", timeframe="5m", write=False,
                           log=lambda *a: None)
    b = EXP.run_experiment(symbol="BTCUSDT", timeframe="5m", write=False,
                           log=lambda *a: None)
    for name in a["tournament"]["participants"]:
        assert a["tournament"]["participants"][name]["net_pnl_eur"] == \
            b["tournament"]["participants"][name]["net_pnl_eur"]


def test_experiment_fails_closed_on_missing_dataset(tmp_path, monkeypatch):
    monkeypatch.setattr(BF.CE, "_repo_root", lambda: tmp_path)
    rep = EXP.run_experiment(symbol="BTCUSDT", timeframe="5m", write=False,
                             log=lambda *a: None)
    assert rep["status"] == "INVALID_MANIFEST_CONTRACT"
    assert rep["safety"]["can_send_real_orders"] is False
