"""V10.43C dashboard: persistent WS panels, static/local, no fake readiness."""

from __future__ import annotations

import json

from app.labs import research_dashboard_v10_43c as DASH


STATE = {
    "tool_version": "v10.43c", "symbol": "BTCUSDT",
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
    "persistent_health": {"status": "HEALTHY", "connected": True, "messages_count": 10,
                          "trades_count": 20, "dataset_file_age_min": 0.1},
    "persistent_continuity": {"verdict": "WS_TOO_GAPPY", "trades": 20, "bars": 12,
                              "forward_bars": 12, "max_contiguous_run": 12,
                              "forward_coverage": 0.2, "fit_for_shadow_forward": False},
    "source_compare_3way": {
        "recommended_source": "ws_persistent", "ready_for_shadow_forward": False,
        "blockers": ["WS_TOO_GAPPY_FOR_SHADOW_FORWARD"],
        "rest": {"bars": 100, "max_contiguous_run": 10, "coverage": 0.2, "verdict": "TOO_GAPPY"},
        "ws": {"bars": 90, "max_contiguous_run": 20, "coverage": 0.3, "verdict": "TOO_GAPPY"},
        "ws_persistent": {"bars": 120, "max_contiguous_run": 40, "coverage": 0.4, "verdict": "TOO_GAPPY"}},
    "strategy_hardening": {"global_verdict": "ALL_NEEDS_MORE_DATA", "candidates_generated": 14,
                           "verdict_counts": {"NEEDS_MORE_DATA": 14},
                           "rejection_category_counts": {"INSUFFICIENT_SAMPLE": 14},
                           "watchlist_or_better": 0,
                           "best": {"strategy_name": "micro_burst", "verdict": "NEEDS_MORE_DATA"}},
    "ws_persistent_tournament": {"verdict": "INSUFFICIENT_SAMPLE", "best_strategy": {},
                                 "micro_live_ready": False},
    "exit_optimization": {"verdict": "NO_EXIT_EDGE_ALL_REJECTED", "n_entries": 2,
                          "variants_x_horizons": 45, "watchlist_or_better": 0,
                          "best_variant": {"exit_variant": "fixed_baseline", "horizon": 5,
                                           "verdict": "NEEDS_MORE_DATA",
                                           "partial_tp_model": None},
                          "winner_loser": {"avg_pct_of_MFE_captured": None}},
    "readiness_v1043c": {"primary": "BLOCKED_BY_DATA_GAP",
                         "states": ["RESEARCH_ONLY", "BLOCKED_BY_DATA_GAP"],
                         "blockers": ["WS_TOO_GAPPY_FOR_SHADOW_FORWARD"],
                         "micro_live_ready": False},
    "research_only": True, "can_send_real_orders": False, "edge_validated": False,
    "final_recommendation": "NO LIVE",
}


def test_dashboard_builds_static_html_and_json(tmp_path):
    r = DASH.build_dashboard("BTCUSDT", state=STATE, out_dir=tmp_path)
    assert (tmp_path / "index.html").is_file()
    assert (tmp_path / "dashboard_data_v10_43c.json").is_file()
    assert r["mode"] == "RESEARCH_ONLY"
    data = json.loads((tmp_path / "dashboard_data_v10_43c.json").read_text(encoding="utf-8"))
    assert data["can_send_real_orders"] is False
    assert data["final_recommendation"] == "NO LIVE"


def test_dashboard_contains_v1043c_panels_and_blockers(tmp_path):
    DASH.build_dashboard("BTCUSDT", state=STATE, out_dir=tmp_path)
    h = (tmp_path / "index.html").read_text(encoding="utf-8")
    for token in ("Persistent WS Panel", "REST vs WS vs WS Persistent",
                  "Strategy Lab Hardened", "Exit Optimization Panel",
                  "Probability Lattice", "WS_TOO_GAPPY_FOR_SHADOW_FORWARD",
                  "BLOCKED_BY_DATA_GAP", "NO LIVE"):
        assert token in h


def test_dashboard_no_external_fetch_or_fake_metrics(tmp_path):
    DASH.build_dashboard("BTCUSDT", state=STATE, out_dir=tmp_path)
    h = (tmp_path / "index.html").read_text(encoding="utf-8")
    for forbidden in ("fetch(", "http://", "https://", "92%", "76.6%",
                      "$41,291", "LIVE READY", "guaranteed profit",
                      "outcome distribution of the best shadow policy",
                      '<div class="cell-v">0%</div>'):
        assert forbidden not in h


def test_cli_registered():
    import app.research_lab as RL
    assert "research-dashboard-build-v1043c" in RL.PUBLIC_RESEARCH_ONLY_COMMANDS
