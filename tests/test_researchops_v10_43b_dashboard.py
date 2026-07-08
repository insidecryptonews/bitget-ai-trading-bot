"""V10.43B dashboard: WS + Strategy Factory + Incubator, honest, no fake metrics."""

from __future__ import annotations

from app.labs import research_dashboard_v10_43b as DASH

# minimal empty-ish state (no reports) built on top of an A-style base
STATE = {
    "tool_version": "v10.43b", "symbol": "BTCUSDT",
    "generated_at": "2026-07-08T00:00:00+00:00", "git_head": "deadbeef",
    "health": {"status": "DEGRADED", "sub_states": [], "collector_fresh": True,
               "trades_file_age_min": 0.3},
    "view": {"status": "OK", "forward_n_bars": 900, "total_n_bars": 1300,
             "forward_coverage_ratio": 0.20, "forward_max_contiguous_run_bars": 15,
             "forward_verdict": "TOO_GAPPY", "mixed_with_backfill": True,
             "fit_for_fine_backtest": False, "fit_for_shadow_forward": False},
    "data_quality": {"states": ["TOO_GAPPY"], "tournament_result_reliability": "NOT_RELIABLE_GAPS"},
    "shadow": None, "scoreboard": [], "bankroll": None,
    "ws_dataset": {"exists": True, "size_kb": 100.0, "age_min": 0.5},
    "readiness": {"primary": "BLOCKED_BY_DATA_GAP",
                  "states": ["RESEARCH_ONLY", "BLOCKED_BY_DATA_GAP"], "micro_live_ready": False},
    # V10.43B extras
    "ws_view": {"verdict": "TOO_GAPPY", "bars_created": 132, "ws_trades_used": 150000,
                "max_contiguous_run": 110, "forward_coverage": 0.24, "ws_file_age_min": 0.6,
                "reliability": "EXPLORATORY", "ws_stale": False},
    "source_compare": {"recommended_source": "ws",
                       "rest": {"bars": 1366, "max_contiguous_run": 15, "coverage": 0.20},
                       "ws": {"bars": 132, "max_contiguous_run": 110, "coverage": 0.24}},
    "strategy_rejection": {"verdict_counts": {"NEEDS_MORE_DATA": 14},
                           "top_rejection_reasons": []},
    "strategy_watchlist": [], "strategy_scoreboard": [],
    "ws_tournament": {"verdict": "INSUFFICIENT_SAMPLE", "best_strategy": {}},
    "lead_lag": {"verdict": "INTERNAL_REPRICING_MEASURED",
                 "multi_symbol_lead_lag": "WAITING_DATA (only BTCUSDT collected)"},
    "research_only": True, "can_send_real_orders": False, "final_recommendation": "NO LIVE",
}


def test_build_without_reports_does_not_crash(tmp_path):
    r = DASH.build_dashboard("BTCUSDT", state=STATE, out_dir=tmp_path, write=True)
    assert (tmp_path / "index.html").is_file()
    assert (tmp_path / "dashboard_data_v10_43b.json").is_file()
    assert r["mode"] == "RESEARCH_ONLY" and r["final_recommendation"] == "NO LIVE"


def test_html_contains_v1043b_panels(tmp_path):
    DASH.build_dashboard("BTCUSDT", state=STATE, out_dir=tmp_path)
    h = (tmp_path / "index.html").read_text(encoding="utf-8")
    for token in ["WS Data Integration", "Strategy Factory", "Incubator Watchlist",
                  "Probability Lattice", "Relationship Graph", "V10.43B DASHBOARD",
                  "RESEARCH_ONLY", "NO LIVE", "recommended_source"]:
        assert token in h, token


def test_lattice_shows_unreliable_state(tmp_path):
    DASH.build_dashboard("BTCUSDT", state=STATE, out_dir=tmp_path)
    h = (tmp_path / "index.html").read_text(encoding="utf-8")
    # data is TOO_GAPPY/NOT_RELIABLE -> lattice must show DATA_GAP or STALE or INSUFFICIENT
    assert ("DATA_GAP" in h) or ("STALE" in h) or ("INSUFFICIENT_SAMPLE" in h)


def test_incubator_empty_shows_no_candidate(tmp_path):
    DASH.build_dashboard("BTCUSDT", state=STATE, out_dir=tmp_path)
    h = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "No WATCHLIST / INCUBATE candidate yet" in h


def test_no_fake_metrics(tmp_path):
    DASH.build_dashboard("BTCUSDT", state=STATE, out_dir=tmp_path)
    h = (tmp_path / "index.html").read_text(encoding="utf-8")
    for fake in ["92%", "76.6%", "$41,291", "LIVE READY", "LIVE_READY",
                 "can_send_real_orders=true"]:
        assert fake not in h, fake


def test_json_data_safe(tmp_path):
    DASH.build_dashboard("BTCUSDT", state=STATE, out_dir=tmp_path)
    import json
    d = json.loads((tmp_path / "dashboard_data_v10_43b.json").read_text(encoding="utf-8"))
    assert d["can_send_real_orders"] is False and d["final_recommendation"] == "NO LIVE"
    assert d["edge_validated"] is False


def test_cli_registered():
    import app.research_lab as RL
    for cmd in ("ws-forward-dataset-view-v1043b", "dataset-source-compare-v1043b",
                "shadow-simulation-tournament-ws-v1043b", "autonomous-strategy-lab-v1043b",
                "research-dashboard-build-v1043b"):
        assert cmd in RL.PUBLIC_RESEARCH_ONLY_COMMANDS, cmd
