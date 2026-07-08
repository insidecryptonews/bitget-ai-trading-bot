"""V10.43A Trading Research Command Center dashboard builder: honest, no fake
metrics, never crashes without reports, research-only."""

from __future__ import annotations

from pathlib import Path

from app.labs import research_dashboard_v10_43a as DASH

# a minimal, empty-ish state (simulates 'no reports yet')
EMPTY_STATE = {
    "tool_version": "v10.43a", "symbol": "BTCUSDT",
    "generated_at": "2026-07-07T00:00:00+00:00", "git_head": "deadbeef",
    "health": {"status": "COLLECTOR_DOWN", "sub_states": [], "collector_fresh": False,
               "trades_file_age_min": None},
    "view": {"status": "NO_DATA", "forward_n_bars": 0, "total_n_bars": 0,
             "forward_coverage_ratio": None, "forward_max_contiguous_run_bars": None,
             "forward_verdict": "NO_DATA", "mixed_with_backfill": False,
             "fit_for_fine_backtest": False, "fit_for_shadow_forward": False},
    "data_quality": {"states": ["INSUFFICIENT_FORWARD_DATA"],
                     "tournament_result_reliability": "NOT_RELIABLE_SAMPLE"},
    "shadow": None, "scoreboard": [], "bankroll": None,
    "ws_dataset": {"exists": False},
    "readiness": {"primary": "DATA_NOT_READY",
                  "states": ["RESEARCH_ONLY", "DATA_NOT_READY"], "micro_live_ready": False},
    "research_only": True, "can_send_real_orders": False,
    "final_recommendation": "NO LIVE",
}


def test_builder_does_not_crash_without_reports(tmp_path):
    r = DASH.build_dashboard("BTCUSDT", state=EMPTY_STATE, out_dir=tmp_path, write=True)
    assert (tmp_path / "index.html").is_file()
    assert (tmp_path / "dashboard_data_v10_43a.json").is_file()
    assert r["mode"] == "RESEARCH_ONLY" and r["final_recommendation"] == "NO LIVE"


def test_html_contains_required_panels(tmp_path):
    DASH.build_dashboard("BTCUSDT", state=EMPTY_STATE, out_dir=tmp_path)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    for token in ["Bitget AI Trading Research Bot", "RESEARCH_ONLY", "NO LIVE",
                  "Data Quality", "Strategy Tournament", "Probability Lattice",
                  "Relationship Graph", "Readiness Gate", "System Status",
                  "Market Snapshot", "20 EUR Shadow Bankroll"]:
        assert token in html, token


def test_missing_reports_show_waiting_states(tmp_path):
    DASH.build_dashboard("BTCUSDT", state=EMPTY_STATE, out_dir=tmp_path)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "NO RELIABLE TOURNAMENT YET" in html
    assert "WAITING_FOR_SHADOW_BANKROLL_REPORT" in html
    assert "WAITING_DATA" in html
    assert "DATA_NOT_READY" in html


def test_no_fake_hardcoded_metrics(tmp_path):
    DASH.build_dashboard("BTCUSDT", state=EMPTY_STATE, out_dir=tmp_path)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    for fake in ["92%", "76.6%", "76.42%", "$41,291", "$41,337", "41,291.24",
                 "LIVE READY", "LIVE_READY", "+$1,654.79", "Confidence Level"]:
        assert fake not in html, fake
    # and never any actionable/live token
    for banned in ["BUY_NOW", "SELL_NOW", "OPEN_POSITION", "can_send_real_orders=true",
                   "LIVE READY"]:
        assert banned not in html, banned


def test_json_data_is_safe_and_honest(tmp_path):
    DASH.build_dashboard("BTCUSDT", state=EMPTY_STATE, out_dir=tmp_path)
    import json
    d = json.loads((tmp_path / "dashboard_data_v10_43a.json").read_text(encoding="utf-8"))
    assert d["can_send_real_orders"] is False
    assert d["edge_validated"] is False
    assert d["final_recommendation"] == "NO LIVE"
    assert d["readiness"]["primary"] == "DATA_NOT_READY"


def test_readiness_reflects_data_gap():
    view = {"forward_n_bars": 800, "forward_verdict": "TOO_GAPPY", "status": "OK"}
    dq = {"tournament_result_reliability": "NOT_RELIABLE_GAPS"}
    rd = DASH._readiness(view, dq, {"any_strategy_beats_baseline_and_costs": False})
    assert rd["primary"] == "BLOCKED_BY_DATA_GAP"
    assert "RESEARCH_ONLY" in rd["states"]
    assert rd["micro_live_ready"] is False


def test_cli_registered_and_public():
    import app.research_lab as RL
    assert "research-dashboard-build-v1043a" in RL.PUBLIC_RESEARCH_ONLY_COMMANDS
    assert hasattr(RL.ResearchLab, "research_dashboard_build_v1043a_cli")


def test_module_no_order_or_key_primitives():
    src = Path(DASH.__file__).read_text(encoding="utf-8")
    for tok in ["place_order", "create_order", "private_get", "set_leverage",
                "load_dotenv", "os.environ", "requests", "urllib", "import websocket"]:
        assert tok not in src, tok
