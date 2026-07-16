from __future__ import annotations

import json
from pathlib import Path

from app.health_server import _ati_shadow_status_payload
from app.labs.research_dashboard_v10_43a import build_dashboard


ROOT = Path(__file__).resolve().parents[1]


def test_ati_dashboard_endpoint_is_whitelisted_and_sanitized(tmp_path: Path) -> None:
    (tmp_path / "ati_health.json").write_text(json.dumps({
        "status": "HEALTHY", "last_run_at": "2026-01-01T00:00:00+00:00",
        "signals_total": 12, "open_positions": 1, "closed_shadow_trades": 4,
        "dataset_last_bar_at": "2026-01-01T00:00:00+00:00",
        "dataset_snapshot_sha256": "abc", "api_key": "must-not-leak",
    }), encoding="utf-8")
    (tmp_path / "ati_summary.json").write_text(json.dumps({
        "status": "INSUFFICIENT_DATA_OR_REJECTED",
        "policy": {"policy_version": "ATI_SHADOW_POLICY_V2", "secret": "no"},
        "overall_baseline": {"net_ev": -0.1, "profit_factor": 0.8, "win_rate": 0.4},
        "baseline_trades": 4, "history_days": 90,
        "by_setup": [{"setup_id": "SHORT_R1", "trades": 4, "net_ev": -0.1,
                      "secret": "must-not-leak"}],
        "blockers": ["history_below_180_days"],
    }), encoding="utf-8")
    (tmp_path / "ati_forward_state.json").write_text(json.dumps({
        "closed_outcomes": 0, "open_positions": 0,
    }), encoding="utf-8")
    payload = _ati_shadow_status_payload(tmp_path)
    encoded = json.dumps(payload)
    assert payload["can_send_real_orders"] is False
    assert payload["paper_filter_enabled"] is False
    assert payload["edge_validated"] is False
    assert payload["final_recommendation"] == "NO LIVE"
    assert payload["stale"] is True
    assert payload["historical_trades"] == 4
    assert payload["closed_shadow_trades"] == 0
    assert "must-not-leak" not in encoded
    assert "api_key" not in encoded


def test_dashboard_ui_has_one_ati_panel_and_clean_js_binding() -> None:
    html = (ROOT / "app" / "static" / "dashboard.html").read_text(encoding="utf-8")
    js = (ROOT / "app" / "static" / "dashboard.js").read_text(encoding="utf-8")
    assert html.count('id="ati-shadow"') == 1
    assert html.count('id="atiShadowRefreshBtn"') == 1
    assert js.count("async function loadAtiShadow") == 1
    assert js.count('$("atiShadowRefreshBtn")?.addEventListener') == 1
    assert "/api/research/ati-shadow" in js
    assert "can_send_real_orders=false" in html
    assert "NO LIVE" in html


def test_local_research_dashboard_renders_ati_without_claiming_edge(tmp_path: Path) -> None:
    state = {
        "tool_version": "test", "symbol": "BTCUSDT", "generated_at": "now",
        "git_head": "abc", "health": {}, "view": {}, "data_quality": {},
        "shadow": None, "scoreboard": [], "bankroll": None, "ws_dataset": {},
        "readiness": {"primary": "DATA_NOT_READY", "states": ["DATA_NOT_READY"]},
        "ati": {"health": {"status": "NO_DATA"}, "summary": {}, "forward": {}},
    }
    result = build_dashboard(state=state, out_dir=tmp_path, write=False)
    page = result["html_str"]
    assert "Adrian Trading Intelligence" in page
    assert "can_send_real_orders" in page
    assert "NO LIVE" in page
    assert "edge validated" not in page.lower()
