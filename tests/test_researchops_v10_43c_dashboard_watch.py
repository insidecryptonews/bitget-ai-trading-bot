import ast
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


def test_fast_watcher_is_artifact_only_when_source_cache_is_stale(
        tmp_path: Path, monkeypatch):
    monkeypatch.setattr(DASH.CE, "_repo_root", lambda: tmp_path)
    out = DASH._output_dir()
    out.mkdir(parents=True)
    previous = _state()
    previous["base_metrics"] = {
        "source": "EXPLICIT_HEAVY_BUILD",
        "refreshed_at": "2020-01-01T00:00:00+00:00",
    }
    (out / "dashboard_data_v10_43c.json").write_text(
        json.dumps(previous), encoding="utf-8")
    (out / DASH.CACHE_FILE).write_text(json.dumps({
        "symbol": "BTCUSDT",
        "updated_ts": 1,
        "source_signature": {"symbol": "BTCUSDT", "size": 1, "mtime": 1},
        "continuity": previous["persistent_continuity"],
        "compare": previous["source_compare_3way"],
    }), encoding="utf-8")
    monkeypatch.setattr(DASH, "_source_signature",
                        lambda symbol: {"symbol": symbol, "size": 2, "mtime": 2})
    monkeypatch.setattr(DASH.A, "_git_head", lambda: "new-render-head")
    monkeypatch.setattr(DASH.PWS, "ws_persistent_health",
                        lambda symbol: {"status": "HEALTHY"})

    def forbidden(*args, **kwargs):
        raise AssertionError("fast watcher attempted heavy dataset analysis")

    monkeypatch.setattr(DASH.A, "gather_state", forbidden)
    monkeypatch.setattr(DASH.PWS, "load_persistent_bars", forbidden)
    monkeypatch.setattr(DASH.PWS, "ws_continuity_audit", forbidden)
    monkeypatch.setattr(DASH.PWS, "dataset_source_compare_3way", forbidden)

    state = DASH.gather_state_fast("BTCUSDT")

    assert state["fast_metrics"]["heavy_analysis_executed"] is False
    assert state["slow_metrics"]["source_metrics_cache"] == "STALE_HIT"
    assert state["slow_metrics"]["source_metrics_stale"] is True
    assert state["git_head"] == "new-render-head"
    assert "SOURCE_METRICS_STALE_EXPLICIT_REFRESH_REQUIRED" in (
        state["readiness_v1043c"]["blockers"])
    assert state["can_send_real_orders"] is False


def test_fast_watcher_missing_artifacts_fails_closed_without_heavy_fallback(
        tmp_path: Path, monkeypatch):
    monkeypatch.setattr(DASH.CE, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(DASH.PWS, "ws_persistent_health",
                        lambda symbol: {"status": "NO_DATA"})

    def forbidden(*args, **kwargs):
        raise AssertionError("fast watcher attempted heavy fallback")

    monkeypatch.setattr(DASH.A, "gather_state", forbidden)
    monkeypatch.setattr(DASH.PWS, "load_persistent_bars", forbidden)
    monkeypatch.setattr(DASH.PWS, "ws_continuity_audit", forbidden)
    monkeypatch.setattr(DASH.PWS, "dataset_source_compare_3way", forbidden)

    state = DASH.gather_state_fast("BTCUSDT")

    assert state["persistent_continuity"]["verdict"] == "NO_CACHED_SOURCE_METRICS"
    assert state["source_compare_3way"]["ready_for_shadow_forward"] is False
    assert state["slow_metrics"]["source_metrics_cache"] == "MISS_NO_ARTIFACT"
    assert state["readiness_v1043c"]["paper_ready"] is False
    assert state["readiness_v1043c"]["live_ready"] is False


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


def test_missing_p11_snapshot_is_explicitly_unavailable_not_zero(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(DASH.CE, "_repo_root", lambda: tmp_path)

    snapshot = DASH._load_p11_observer_status()
    state = _state()
    state["p11_short_forward_observer"] = snapshot
    html = DASH.render_html(state)

    assert snapshot["observer_status"] == "OBSERVER_STATUS_UNAVAILABLE"
    assert snapshot["_snapshot_available"] is False
    assert "P11_SHORT FORWARD OBSERVER" in html
    assert '<span class="k">Forward n_eff</span><span class="v">N/A</span>' in html
    assert '<span class="k">Profit factor</span><span class="v">N/A</span>' in html
    assert "N/A — not published" in html


def test_p11_snapshot_panel_and_local_exports_are_visible(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(DASH.CE, "_repo_root", lambda: tmp_path)
    observer_dir = tmp_path.joinpath(*DASH.P11_OBSERVER_OUTPUT_SUBDIR)
    observer_dir.mkdir(parents=True)
    payload = {
        "observer_status": "RUNNING",
        "identity": {
            "symbol": "BTCUSDT",
            "venue": "Bitget",
            "timeframe": "15m",
            "hypothesis": "P11_SHORT",
            "mode": "forward_shadow",
        },
        "boundary": {"forward_start_timestamp": "2026-07-15T18:30:00+00:00"},
        "checkpoint": {"last_closed_bar": "2026-07-15T18:30:00+00:00"},
        "metrics": {
            "forward_opportunities": 1,
            "forward_signals": 0,
            "forward_rejections": 1,
            "forward_entries": 0,
            "forward_open_positions": 0,
            "forward_closed_outcomes": 0,
            "forward_finalized_labels": 0,
            "forward_n_raw": 0,
            "forward_n_eff": 0.0,
            "gross_pnl": 0.0,
            "net_pnl": 0.0,
            "profit_factor": 0.0,
            "duplicate_count": 0,
            "orphan_count": 0,
            "observer_heartbeat": "2026-07-15T18:31:00+00:00",
            "observer_lag_seconds": 4.2,
        },
        "reconciliation": {"status": "PASS"},
        "errors": [],
        "provenance": {
            "schema_version": "p11-forward-observer.v1",
            "code_head": "a" * 40,
            "code_tree": "b" * 40,
            "policy_fingerprint": "c" * 64,
            "config_hash": "d" * 64,
        },
    }
    status_path = observer_dir / DASH.P11_OBSERVER_STATUS_FILE
    status_path.write_text(json.dumps(payload), encoding="utf-8")
    for filename in DASH.P11_OBSERVER_EXPORT_FILES.values():
        (observer_dir / filename).write_text("fixture\n", encoding="utf-8")

    snapshot = DASH._load_p11_observer_status()
    state = _state()
    state["p11_short_forward_observer"] = snapshot
    output = tmp_path / "dashboard"
    DASH.build_dashboard("BTCUSDT", state=state, out_dir=output)
    html = (output / "index.html").read_text(encoding="utf-8")

    assert snapshot["_snapshot_available"] is True
    assert "P11_SHORT FORWARD OBSERVER" in html
    assert "Reports &amp; Exports — P11_SHORT" in html
    assert "BTCUSDT" in html and "Bitget" in html and "P11_SHORT" in html
    assert '<span class="k">Closed outcomes</span><span class="v">0</span>' in html
    assert '<span class="k">Forward n_eff</span><span class="v">N/A</span>' in html
    assert '<span class="k">Gross PnL</span><span class="v">N/A</span>' in html
    assert '<span class="k">Profit factor</span><span class="v">N/A</span>' in html
    assert "file:///" in html
    assert 'download="lifecycle_ledger.jsonl"' in html
    assert 'download="outcomes.csv"' in html
    assert 'download="labels.csv"' in html
    assert 'download="reconciliation_report.json"' in html
    assert 'download="summary.txt"' in html
    assert "overflow-wrap:anywhere" in html
    assert ".card{grid-column:span 4" in html and "min-width:0" in html
    assert ".gate .big" in html and "word-break:break-word" in html
    assert "can_send_real_orders=true" not in html


def test_dashboard_html_publish_is_atomic(tmp_path: Path, monkeypatch):
    target = tmp_path / "index.html"
    target.write_text("old-complete-dashboard", encoding="utf-8")
    real_replace = DASH.os.replace
    html_replacements: list[tuple[Path, Path]] = []

    def checked_replace(src, dst):
        src_path, dst_path = Path(src), Path(dst)
        if dst_path.name == "index.html":
            assert dst_path.read_text(encoding="utf-8") == "old-complete-dashboard"
            assert src_path.name == "index.html.tmp"
            assert src_path.read_text(encoding="utf-8").startswith("<!doctype html>")
            html_replacements.append((src_path, dst_path))
        real_replace(src, dst)

    monkeypatch.setattr(DASH.os, "replace", checked_replace)
    DASH.build_dashboard("BTCUSDT", state=_state(), out_dir=tmp_path)

    assert len(html_replacements) == 1
    assert target.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert not (tmp_path / "index.html.tmp").exists()


def test_p11_dashboard_consumer_does_not_import_observer_runtime():
    source = Path(DASH.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")

    assert not any("p11_short_forward_observer" in module for module in imported)
