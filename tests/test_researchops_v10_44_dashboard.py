from __future__ import annotations

import json
from pathlib import Path

from app.labs import research_dashboard_v10_43c as DASH


def _state() -> dict:
    return {
        "tool_version": "v10.43c",
        "symbol": "BTCUSDT",
        "generated_at": "2026-07-09T00:00:00+00:00",
        "git_head": "test",
        "health": {"status": "HEALTHY", "collector_fresh": True},
        "view": {"forward_n_bars": 30, "total_n_bars": 30, "forward_coverage_ratio": 1.0,
                 "forward_max_contiguous_run_bars": 30, "forward_verdict": "CONTINUOUS_ENOUGH",
                 "fit_for_fine_backtest": False, "fit_for_shadow_forward": False},
        "data_quality": {"tournament_result_reliability": "EXPLORATORY"},
        "shadow": None,
        "scoreboard": [],
        "bankroll": None,
        "ws_dataset": {"exists": True},
        "persistent_health": {"status": "HEALTHY", "connected": True},
        "persistent_continuity": {"verdict": "WS_USABLE_FOR_EXPLORATORY_RESEARCH", "trades": 100,
                                  "bars": 30, "max_contiguous_run": 30, "forward_coverage": 0.5,
                                  "fit_for_shadow_forward": False},
        "source_compare_3way": {"recommended_source": "ws_persistent", "ready_for_shadow_forward": False,
                                "blockers": ["WS_TOO_GAPPY_FOR_SHADOW_FORWARD"],
                                "rest": {}, "ws": {}, "ws_persistent": {}},
        "strategy_hardening": {"global_verdict": "ALL_NEEDS_MORE_DATA", "watchlist_or_better": 0},
        "ws_persistent_tournament": {"verdict": "NEED_MORE_DATA", "best_strategy": {}},
        "exit_optimization": {"verdict": "NEEDS_MORE_DATA", "watchlist_or_better": 0},
        "readiness_v1043c": {"primary": "BLOCKED_BY_DATA_GAP", "states": ["RESEARCH_ONLY"]},
        "slow_metrics": {"source_metrics_cache": "HIT", "source_metrics_age_seconds": 12,
                         "source_dataset_changed_since_cache": True},
        "fast_metrics": {"last_updated_at": "2026-07-09T00:00:00+00:00"},
        "research_only": True,
        "can_send_real_orders": False,
        "paper_filter_enabled": False,
        "edge_validated": False,
        "final_recommendation": "NO LIVE",
    }


def test_dashboard_includes_v1044_alpha_factory_panel(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(DASH.CE, "_repo_root", lambda: tmp_path)
    out = tmp_path / "reports" / "research" / "v10_44_alpha_sprint"
    out.mkdir(parents=True)
    (out / "alpha_factory_v10_44.json").write_text(json.dumps({
        "overall_verdict": "WATCH_ONLY",
        "strategies_tested": 12,
        "candidate_status_counts": {"WATCH_ONLY": 1},
        "best_candidate": {"candidate_id": "alpha1", "status": "WATCH_ONLY"},
    }), encoding="utf-8")
    (out / "candidate_incubator_v10_44.json").write_text(json.dumps({
        "overall_verdict": "WATCH_ONLY",
        "state_counts": {"WATCH_ONLY": 1},
        "best_research_candidate": {"candidate_id": "alpha1", "incubator_state": "WATCH_ONLY"},
    }), encoding="utf-8")

    DASH.build_dashboard("BTCUSDT", state=_state(), out_dir=tmp_path)
    html = (tmp_path / "index.html").read_text(encoding="utf-8")

    assert "Alpha Factory V10.44" in html
    assert "alpha1" in html
    assert "Candidate labels are NOT executable signals" in html
    assert "NO LIVE" in html
    assert "http://" not in html
    assert "https://" not in html
    assert "get('/api/ati-paper/account')" in html


def test_fast_source_metrics_cache_reuses_recent_heavy_result(monkeypatch, tmp_path: Path):
    calls = {"continuity": 0, "compare": 0}
    monkeypatch.setattr(DASH, "_source_signature", lambda symbol: {"symbol": symbol, "size": 1, "mtime": 1})
    (tmp_path / DASH.CACHE_FILE).write_text(json.dumps({
        "symbol": "BTCUSDT",
        "updated_ts": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).timestamp(),
        "source_signature": {"symbol": "BTCUSDT", "size": 0, "mtime": 0},
        "continuity": {"verdict": "WS_TOO_GAPPY", "max_contiguous_run": 10},
        "compare": {"recommended_source": "ws_persistent", "blockers": ["B"], "rest": {}, "ws": {}, "ws_persistent": {}},
    }), encoding="utf-8")

    def fail_cont(symbol):
        calls["continuity"] += 1
        raise AssertionError("should use cache")

    monkeypatch.setattr(DASH.PWS, "ws_continuity_audit", fail_cont)
    c, cmp_, meta = DASH._cached_source_metrics("BTCUSDT", tmp_path)

    assert calls["continuity"] == 0
    assert c["verdict"] == "WS_TOO_GAPPY"
    assert cmp_["recommended_source"] == "ws_persistent"
    assert meta["source_metrics_cache"] == "STALE_HIT"
    assert meta["source_metrics_stale"] is True
    assert meta["source_dataset_changed_since_cache"] is True
