from __future__ import annotations

import json
from pathlib import Path

from app import health_server
from app.health_server import HealthState, _ati_shadow_status_payload
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
        "dataset_source_mode": "verified_fixture",
        "baseline_trades": 4, "history_days": 90,
        "by_setup": [{"setup_id": "SHORT_R1", "trades": 4, "net_ev": -0.1,
                      "secret": "must-not-leak"}],
        "blockers": ["history_below_180_days"],
        "by_symbol": [{"symbol": "BTCUSDT", "trades": 4, "net_ev": -0.1}],
        "by_regime": [{"regime": "TREND_DOWN", "trades": 4, "net_ev": -0.1}],
        "trailing_grid": [{"policy": "baseline", "trades": 4, "net_ev": -0.1}],
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
    assert payload["dataset_source_mode"] == "verified_fixture"
    assert payload["by_symbol"][0]["symbol"] == "BTCUSDT"
    assert payload["by_regime"][0]["regime"] == "TREND_DOWN"
    assert payload["trailing_grid"][0]["policy"] == "baseline"
    assert "must-not-leak" not in encoded
    assert "api_key" not in encoded


def test_dashboard_ui_has_one_ati_panel_and_clean_js_binding() -> None:
    html = (ROOT / "app" / "static" / "dashboard.html").read_text(encoding="utf-8")
    js = (ROOT / "app" / "static" / "dashboard.js").read_text(encoding="utf-8")
    assert html.count('id="ati-shadow"') == 1
    assert html.count('id="atiShadowRefreshBtn"') == 1
    assert html.count('id="atiSymbolRows"') == 1
    assert html.count('id="atiRegimeRows"') == 1
    assert html.count('id="atiTrailingRows"') == 1
    assert js.count("async function loadAtiShadow") == 1
    assert js.count('$("atiShadowRefreshBtn")?.addEventListener') == 1
    assert "/api/research/ati-shadow" in js
    assert "can_send_real_orders=false" in html
    assert "NO LIVE" in html


def test_health_components_are_separate_and_fail_closed_on_stale_heavy_metrics(
    monkeypatch, tmp_path: Path,
) -> None:
    dashboard = tmp_path / "dashboard_data_v10_43c.json"
    dashboard.write_text(json.dumps({
        "health": {"status": "HEALTHY"},
        "persistent_health": {"status": "HEALTHY", "age_seconds": 1},
        "source_compare_3way": {
            "ready_for_shadow_forward": True, "recommended_source": "ws_persistent",
        },
        "dashboard_watch": {
            "watcher_status": "RUNNING", "last_refresh_at": "2099-01-01T00:00:00+00:00",
            "interval_seconds": 30,
        },
        "slow_metrics": {"strategy_stale": True, "exit_stale": True},
    }), encoding="utf-8")
    monkeypatch.setattr(health_server, "_RESEARCH_DASHBOARD_V1043C", dashboard)
    monkeypatch.setattr(health_server, "_ati_shadow_status_payload", lambda: {
        "status": "HEALTHY", "can_send_real_orders": False,
    })
    monkeypatch.setattr("app.labs.ati_paper.api.health_payload", lambda: {
        "status": "WAITING_FOR_SIGNAL", "can_send_real_orders": False,
        "simulation_only": True, "final_recommendation": "NO LIVE",
    })
    payload = health_server._research_components_status_payload(HealthState(mode="paper"))
    assert {
        "mode", "safety", "bot", "collectors", "datasets",
        "dashboard_watcher", "heavy_research", "ati_shadow", "ati_paper_executor",
        "public_rest_data", "public_ws_data", "p11_forward_observer", "storage", "disk",
    } <= set(payload["components"])
    assert {
        "cross_venue", "CROSS_VENUE_BITGET", "CROSS_VENUE_BINANCE",
        "CROSS_VENUE_BYBIT", "CROSS_VENUE_OKX", "CROSS_VENUE_HYPERLIQUID",
        "CROSS_VENUE_NORMALIZER", "CROSS_VENUE_LEADLAG", "CROSS_VENUE_PAPER",
        "CROSS_VENUE_LEVERAGE_LAB",
    } <= set(payload["components"])
    assert payload["components"]["heavy_research"]["status"] == "DEGRADED"
    assert payload["overall_status"] == "DEGRADED"
    assert payload["components"]["safety"]["can_send_real_orders"] is False
    assert payload["reason_codes"]


def test_static_dashboard_renders_existing_ati_paper_ledger_truthfully(tmp_path: Path) -> None:
    from app.labs import research_dashboard_v10_43c as dashboard
    state = {
        "tool_version": "test", "symbol": "BTCUSDT", "generated_at": "now",
        "git_head": "abc", "health": {}, "view": {}, "data_quality": {},
        "shadow": None, "scoreboard": [], "bankroll": None, "ws_dataset": {},
        "persistent_health": {}, "persistent_continuity": {}, "source_compare_3way": {},
        "strategy_hardening": {}, "ws_persistent_tournament": {}, "exit_optimization": {},
        "readiness_v1043c": {"primary": "DATA_NOT_READY", "states": ["DATA_NOT_READY"]},
        "ati_paper": {
            "account": {"account": {"account_id": "ATI_PAPER_50"}},
            "positions": {"positions": []},
            "trades": {"trades": [{"trade_id": "truthful_trade", "symbol": "BTCUSDT", "direction": "SHORT", "net_pnl": 0.01}]},
            "events": {"events": [{"timestamp": "now", "event_type": "TRADE_CLOSED", "reason": "TP"}]},
            "health": {"status": "HEALTHY", "commit_hash": "abc"},
            "performance": {"total_trades": 1},
        },
        "cross_venue": {},
    }
    page = dashboard.build_dashboard("BTCUSDT", state=state, out_dir=tmp_path, write=False)["html_str"]
    assert "truthful_trade" in page
    assert "TRADE_CLOSED" in page
    assert "Process start commit" in page
    assert "No forward trades" not in page


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


def test_health_server_handles_local_favicon_without_external_fetch() -> None:
    source = (ROOT / "app" / "health_server.py").read_text(encoding="utf-8")
    assert 'if path == "/favicon.ico":' in source
    assert "self.send_response(204)" in source


def test_health_heavy_status_uses_current_v1044_artifacts_not_legacy_cache(
    monkeypatch, tmp_path: Path,
) -> None:
    dashboard_dir = tmp_path / "reports" / "research" / "dashboard_v10_43c"
    dashboard_dir.mkdir(parents=True)
    dashboard = dashboard_dir / "dashboard_data_v10_43c.json"
    dashboard.write_text(json.dumps({
        "health": {"status": "HEALTHY"},
        "persistent_health": {"status": "HEALTHY", "age_seconds": 1},
        "source_compare_3way": {"ready_for_shadow_forward": True},
        "dashboard_watch": {"watcher_status": "RUNNING", "last_refresh_at": "2099-01-01T00:00:00+00:00"},
        "slow_metrics": {"strategy_stale": True, "exit_stale": True},
    }), encoding="utf-8")
    heavy_dir = tmp_path / "reports" / "research" / "v10_44_alpha_sprint"
    heavy_dir.mkdir(parents=True)
    (heavy_dir / "alpha_factory_v10_44.json").write_text("{}", encoding="utf-8")
    (heavy_dir / "exit_factory_v10_44.json").write_text("{}", encoding="utf-8")
    scheduler = tmp_path / "scheduler_status.json"
    scheduler.write_text(json.dumps({"status": "COMPLETED", "exit_code": 0}), encoding="utf-8")
    monkeypatch.setattr(health_server, "_RESEARCH_DASHBOARD_V1043C", dashboard)
    monkeypatch.setattr(health_server, "_HEAVY_SCHEDULER_STATUS", scheduler)
    monkeypatch.setattr(health_server, "_ati_shadow_status_payload", lambda: {
        "status": "HEALTHY", "can_send_real_orders": False,
    })
    monkeypatch.setattr("app.labs.ati_paper.api.health_payload", lambda: {
        "status": "WAITING_FOR_SIGNAL", "can_send_real_orders": False,
    })
    component = health_server._research_components_status_payload(
        HealthState(mode="paper")
    )["components"]["heavy_research"]
    assert component["status"] == "HEALTHY"
    assert component["artifacts_v1044_stale"] is False
    assert component["legacy_strategy_stale"] is True
    assert component["legacy_exit_stale"] is True
