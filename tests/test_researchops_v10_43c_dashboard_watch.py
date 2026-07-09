import json
import os
from pathlib import Path

from app.labs import research_dashboard_v10_43c as DASH


def _state() -> dict:
    return {
        "tool_version": "v10.43c",
        "symbol": "BTCUSDT",
        "generated_at": "2026-07-09T00:00:00+00:00",
        "git_head": "test",
        "health": {"status": "HEALTHY", "collector_fresh": True},
        "view": {
            "forward_n_bars": 60,
            "total_n_bars": 60,
            "forward_coverage_ratio": 1.0,
            "forward_max_contiguous_run_bars": 60,
            "forward_verdict": "CONTINUOUS_ENOUGH",
            "fit_for_fine_backtest": False,
            "fit_for_shadow_forward": True,
        },
        "data_quality": {"tournament_result_reliability": "EXPLORATORY"},
        "shadow": None,
        "scoreboard": [],
        "bankroll": None,
        "ws_dataset": {"exists": True, "size_kb": 12.0},
        "persistent_health": {
            "status": "HEALTHY",
            "connected": True,
            "age_seconds": 0.2,
            "health_file_age_seconds": 1.0,
            "dataset_file_age_min": 0.1,
            "messages_count": 10,
            "trades_count": 100,
        },
        "persistent_continuity": {
            "verdict": "WS_READY_FOR_SHADOW_FORWARD",
            "trades": 100,
            "bars": 30,
            "max_contiguous_run": 30,
            "forward_coverage": 1.0,
            "fit_for_shadow_forward": True,
        },
        "source_compare_3way": {
            "recommended_source": "ws_persistent",
            "ready_for_shadow_forward": True,
            "blockers": [],
            "rest": {"bars": 1, "max_contiguous_run": 1, "coverage": 0.01, "verdict": "TOO_GAPPY"},
            "ws": {"bars": 1, "max_contiguous_run": 1, "coverage": 0.01, "verdict": "TOO_GAPPY"},
            "ws_persistent": {"bars": 30, "max_contiguous_run": 30, "coverage": 1.0, "verdict": "OK"},
        },
        "strategy_hardening": {
            "global_verdict": "ALL_NEEDS_MORE_DATA",
            "watchlist_or_better": 0,
            "candidates_generated": 0,
        },
        "ws_persistent_tournament": {"verdict": "NEED_MORE_DATA", "best_strategy": {}},
        "exit_optimization": {
            "verdict": "NEEDS_MORE_DATA",
            "watchlist_or_better": 0,
            "best_variant": {},
        },
        "readiness_v1043c": {
            "primary": "RESEARCH_ONLY_NOT_ACTIONABLE",
            "states": ["RESEARCH_ONLY"],
            "micro_live_ready": False,
        },
        "fast_metrics": {"last_updated_at": "2026-07-09T00:00:00+00:00"},
        "slow_metrics": {
            "strategy_last_updated_at": "2026-07-09T00:00:00+00:00",
            "exit_last_updated_at": "2026-07-09T00:00:00+00:00",
            "strategy_stale": False,
            "exit_stale": False,
        },
        "research_only": True,
        "can_send_real_orders": False,
        "paper_filter_enabled": False,
        "edge_validated": False,
        "final_recommendation": "NO LIVE",
    }


def test_build_dashboard_adds_local_meta_refresh(tmp_path: Path):
    result = DASH.build_dashboard(
        "BTCUSDT", state=_state(), out_dir=tmp_path, auto_refresh_seconds=30
    )
    html = (tmp_path / "index.html").read_text(encoding="utf-8")

    assert result["auto_refresh_seconds"] == 30
    assert '<meta http-equiv="refresh" content="30">' in html
    assert "Dashboard Auto Refresh" in html
    assert "NO LIVE" in html
    assert "SAFE_PAPER_ONLY" in html


def test_watcher_once_writes_status_json_and_log(tmp_path: Path):
    result = DASH.run_dashboard_watch(
        "BTCUSDT",
        interval_seconds=30,
        once=True,
        out_dir=tmp_path,
        state_builder=lambda symbol: _state(),
    )
    status = json.loads((tmp_path / DASH.STATUS_FILE).read_text(encoding="utf-8"))

    assert result["watcher_status"] == "ONCE_COMPLETED"
    assert status["watcher_status"] == "ONCE_COMPLETED"
    assert status["mode"] == "RESEARCH_ONLY"
    assert status["final_recommendation"] == "NO LIVE"
    assert (tmp_path / DASH.LOG_FILE).is_file()
    assert (tmp_path / "dashboard_data_v10_43c.json").is_file()


def test_missing_reports_fast_state_does_not_crash(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(DASH.CE, "_repo_root", lambda: tmp_path)

    state = DASH.gather_state_fast("BTCUSDT")

    assert state["research_only"] is True
    assert state["can_send_real_orders"] is False
    assert state["strategy_hardening"]["stale_or_missing"] is True
    assert state["exit_optimization"]["stale_or_missing"] is True


def test_lock_blocks_duplicate_watcher(tmp_path: Path):
    lock = tmp_path / DASH.LOCK_FILE
    lock.write_text(
        json.dumps({"pid": os.getpid(), "started_at": "now", "interval_seconds": 30}),
        encoding="utf-8",
    )

    result = DASH.run_dashboard_watch(
        "BTCUSDT",
        interval_seconds=30,
        once=True,
        out_dir=tmp_path,
        state_builder=lambda symbol: _state(),
    )

    assert result["watcher_status"] == "WATCHER_ALREADY_RUNNING"
    assert result["mode"] == "RESEARCH_ONLY"
    assert result["final_recommendation"] == "NO LIVE"


def test_html_does_not_contain_fake_or_actionable_claims(tmp_path: Path):
    DASH.build_dashboard("BTCUSDT", state=_state(), out_dir=tmp_path, auto_refresh_seconds=30)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")

    forbidden = ["92%", "76.6%", "$41,291", "LIVE READY", "guaranteed profit"]
    for text in forbidden:
        assert text.lower() not in html.lower()


def test_dashboard_watch_source_has_no_trading_side_effect_calls():
    source = Path(DASH.__file__).read_text(encoding="utf-8")
    forbidden = [
        "place_order",
        "private_get",
        "private_post",
        "set_leverage",
        "set_margin_mode",
        "ExecutionEngine.execute",
        "PaperTrader.open_position",
        "LIVE_TRADING=True",
        "ENABLE_PAPER_POLICY_FILTER=True",
        "can_send_real_orders=True",
    ]
    for token in forbidden:
        assert token not in source
