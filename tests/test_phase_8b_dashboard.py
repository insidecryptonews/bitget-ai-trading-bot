from __future__ import annotations

from types import SimpleNamespace

from app.dynamic_hold_lab import run_dynamic_hold_lab
from app.health_server import _phase8_research_endpoint


def test_phase8b_validator_720h_dashboard_endpoint_is_skipped_without_allow_heavy():
    payload = _phase8_research_endpoint(
        None,
        None,
        {
            "hours": ["720"],
            "timeframe": ["5m"],
            "symbols": ["DOTUSDT,LINKUSDT"],
        },
        "phase8_candidate_validator",
    )
    assert payload["status"] == "HEAVY_RESEARCH_SKIPPED"
    assert "phase8-candidate-validator" in payload["cli_command"]
    assert payload["paper_filter_enabled"] is False
    assert payload["can_send_real_orders"] is False
    assert payload["final_recommendation"] == "NO LIVE"


def test_phase8b_cost_stress_720h_dashboard_endpoint_is_skipped_without_allow_heavy():
    payload = _phase8_research_endpoint(
        None,
        None,
        {
            "hours": ["720"],
            "timeframe": ["5m"],
            "symbols": ["DOTUSDT"],
            "policy": ["late_entry_block_plus_dynamic_hold"],
        },
        "phase8_cost_stress",
    )
    assert payload["status"] == "HEAVY_RESEARCH_SKIPPED"
    assert "phase8-cost-stress" in payload["cli_command"]
    assert payload["research_only"] is True


def test_dynamic_hold_720h_multi_symbol_warns_to_use_batches(monkeypatch):
    monkeypatch.setattr(
        "app.dynamic_hold_lab.load_replay_trade_contexts",
        lambda *a, **k: SimpleNamespace(
            contexts=[],
            loader_statuses={},
            warnings=[],
            hours=720,
            timeframe="5m",
            symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"],
        ),
    )
    report = run_dynamic_hold_lab(object(), object(), hours=720, symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"])
    assert "heavy_720h_multi_symbol_run_recommend_cli_batches_or_per_symbol" in report.warnings
