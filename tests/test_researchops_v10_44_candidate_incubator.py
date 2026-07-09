from __future__ import annotations

import json
from pathlib import Path

from app.labs import alpha_factory_v10_44 as AF
from app.labs import candidate_incubator_v10_44 as INC


def test_incubator_never_activates_paper_or_live(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(AF.CE, "_repo_root", lambda: tmp_path)
    out = tmp_path / "reports" / "research" / "v10_44_alpha_sprint"
    out.mkdir(parents=True)
    (out / "alpha_factory_v10_44.json").write_text(json.dumps({
        "top_candidates": [{
            "candidate_id": "c1",
            "symbol": "BTCUSDT",
            "strategy_name": "trend_breakout_long",
            "side": "LONG",
            "status": "PAPER_CANDIDATE_RESEARCH_ONLY",
            "score": 88,
            "blockers": [],
            "metrics_test": {"net_EV": 0.002},
        }],
        "final_recommendation": "NO LIVE",
    }), encoding="utf-8")
    (out / "exit_factory_v10_44.json").write_text(json.dumps({
        "best_exit": {"candidate_id": "c1", "status": "EXIT_IMPROVES_RESEARCH_ONLY"},
        "final_recommendation": "NO LIVE",
    }), encoding="utf-8")

    report = INC.run_incubator(symbols="BTCUSDT", data_source="ws_persistent", write_reports=True)
    best = report["best_research_candidate"]

    assert best["incubator_state"] == "PAPER_CANDIDATE_RESEARCH_ONLY"
    assert best["activation"] == "disabled"
    assert best["paper_filter_enabled"] is False
    assert best["can_send_real_orders"] is False
    assert "manual_review_required" in best["blockers"]
    assert report["paper_ready"] is False
    assert report["live_ready"] is False
    assert report["final_recommendation"] == "NO LIVE"


def test_incubator_downgrades_rejected_or_negative_candidate(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(AF.CE, "_repo_root", lambda: tmp_path)
    out = tmp_path / "reports" / "research" / "v10_44_alpha_sprint"
    out.mkdir(parents=True)
    (out / "alpha_factory_v10_44.json").write_text(json.dumps({
        "top_candidates": [{
            "candidate_id": "bad",
            "symbol": "BTCUSDT",
            "strategy_name": "trend_breakout_long",
            "side": "LONG",
            "status": "REJECTED",
            "score": 10,
            "blockers": ["test_sample_too_small", "test_net_ev_not_positive"],
            "metrics_test": {"net_EV": -0.001},
        }],
        "final_recommendation": "NO LIVE",
    }), encoding="utf-8")
    (out / "exit_factory_v10_44.json").write_text(json.dumps({
        "best_exit": None, "final_recommendation": "NO LIVE"}), encoding="utf-8")

    report = INC.run_incubator(symbols="BTCUSDT", data_source="ws_persistent", write_reports=True)

    assert report["best_research_candidate"]["incubator_state"] == "REJECTED"
    assert report["overall_verdict"] == "NO_CANDIDATE_READY"


def test_incubator_cli_renderer_is_not_actionable():
    text = INC.render_cli({
        "overall_verdict": "INCUBATE_RESEARCH_ONLY",
        "state_counts": {"INCUBATE": 1},
        "best_research_candidate": {"candidate_id": "c1", "incubator_state": "INCUBATE", "next_action": "collect_more"},
        "reports_dir": "reports/research/v10_44_alpha_sprint",
    })

    assert "activation: disabled" in text
    assert "paper_filter_enabled: false" in text
    assert "can_send_real_orders: false" in text
    assert "final_recommendation: NO LIVE" in text
