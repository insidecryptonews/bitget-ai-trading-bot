"""V10.46.6 integrated dashboard + runbook: all required panels render, NO LIVE
is visible, no heavy replay or execution on render, and the runbook exists and
states LIVE_TRADING stays False. Research only, NO LIVE."""

from __future__ import annotations

from pathlib import Path

from app.labs.v10_46 import dashboard as D


def _report():
    return {
        "provenance": {"repo_commit": "abc123", "tree_oid": "def456",
                       "data_generation_id": "gen1",
                       "output_manifest_sha256": "sha1", "seal_match": True},
        "safety": {"mode": "REPLAY/SIM/SHADOW/PAPER RESEARCH ONLY",
                   "can_send_real_orders": False},
        "market": {"regime": "TREND_UP", "trend": "up", "volatility": "low",
                   "move_consumed": 0.4},
        "decision": {"agents_for": 2, "agents_against": 1, "abstention": False,
                     "calibrated_probability": 0.61},
        "position": {"exposure_eur": 5.0, "leverage": 1.0,
                     "planned_max_loss_eur": 0.05, "net_pnl_eur": -0.01},
        "tournament": {"champion": "A_static_abstain",
                       "participants": {
                           "A_static_abstain": {"trades": 40, "net_pnl_eur": -0.2,
                                                "ev_per_trade_eur": -0.005,
                                                "n_eff": 40,
                                                "max_drawdown_eur": -0.5,
                                                "brier": 0.25},
                           "D_no_trade": {"trades": 0, "net_pnl_eur": 0.0,
                                          "ev_per_trade_eur": 0.0, "n_eff": 0,
                                          "max_drawdown_eur": 0.0, "brier": None}},
                       "paired": {"B_vs_A": {"mean_diff_eur": 0.001,
                                             "lower_bound_eur": -0.01}},
                       "promotion_status": "HOLD"},
        "learning": {"last_cause": "TIME_DECAY", "lesson": "raise TP",
                     "mutation": "threshold 0.55->0.6",
                     "mutation_status": "rejected",
                     "memory": "n=120", "challenger_brier": 0.24},
        "verdict": ("No se encontró edge validado en las familias probadas, "
                    "durante esta ventana y bajo este modelo de costes."),
        "reports": {"ledger": "experiment_ledger", "manifest": "output_manifest"},
    }


def test_dashboard_renders_all_panels_and_no_live():
    html = D.render(_report())
    for panel in ("Overview", "Market", "Decision", "Position", "Tournament",
                  "Learning", "Reports"):
        assert f">{panel}</h2>" in html
    assert "NO LIVE" in html
    assert "can_send_real_orders" in html
    assert "A_static_abstain" in html and "D_no_trade" in html
    assert "No se encontró edge validado" in html


def test_dashboard_render_makes_no_execution_calls(monkeypatch):
    # render must be pure: patching the SimOMS/tournament to explode proves
    # the dashboard never runs them
    import app.labs.v10_46.tournament as TT
    import app.labs.v10_46.sim_oms as S

    def boom(*a, **k):
        raise AssertionError("dashboard ran heavy work on render")

    monkeypatch.setattr(TT, "run_tournament", boom)
    monkeypatch.setattr(S, "simulate_trade", boom)
    html = D.render(_report())
    assert len(html) > 500


def test_dashboard_writes_atomically(tmp_path, monkeypatch):
    from app.labs import public_data_backfill_v10_45_1 as BF
    monkeypatch.setattr(BF.CE, "_repo_root", lambda: tmp_path)
    out = tmp_path / "reports" / "research" / "v10_46_final_integrated"
    out.mkdir(parents=True)
    p = D.build_dashboard(_report(), out / "index.html")
    assert Path(p).is_file()
    assert "NO LIVE" in Path(p).read_text(encoding="utf-8")


def test_live_readiness_runbook_exists_and_is_no_live():
    from app.labs import continuous_edge_factory_v10_38 as CE
    root = Path(CE._repo_root())
    rb = root / "docs" / "LIVE_READINESS_RUNBOOK.md"
    assert rb.is_file()
    text = rb.read_text(encoding="utf-8")
    assert "LIVE_TRADING" in text and "False" in text
    assert "NOT AN AUTHORISATION" in text.upper()
    assert "can_send_real_orders" in text
