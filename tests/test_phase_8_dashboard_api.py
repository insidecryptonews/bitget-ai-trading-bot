from __future__ import annotations

from pathlib import Path

from app.health_server import _phase8_research_endpoint


def test_phase8_dashboard_endpoint_error_is_safe_without_db():
    payload = _phase8_research_endpoint(None, None, {"hours": ["72"]}, "time_exit_autopsy_v2")
    assert payload["research_only"] is True
    assert payload["paper_filter_enabled"] is False
    assert payload["can_send_real_orders"] is False
    assert payload["final_recommendation"] == "NO LIVE"


def test_phase8_dashboard_endpoint_skips_heavy_720h_without_allow_heavy():
    payload = _phase8_research_endpoint(
        None,
        None,
        {
            "hours": ["720"],
            "timeframe": ["5m"],
            "symbols": ["BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT"],
        },
        "dynamic_hold_lab",
    )
    assert payload["status"] == "HEAVY_RESEARCH_SKIPPED"
    assert payload["skipped_heavy"] is True
    assert "python -m app.research_lab dynamic-hold-lab" in payload["cli_command"]
    assert payload["research_only"] is True
    assert payload["paper_filter_enabled"] is False
    assert payload["can_send_real_orders"] is False
    assert payload["final_recommendation"] == "NO LIVE"


def test_phase8_productive_modules_do_not_call_private_or_order_paths():
    root = Path(__file__).resolve().parents[1]
    modules = [
        root / "app" / "time_exit_autopsy_v2.py",
        root / "app" / "dynamic_hold_lab.py",
        root / "app" / "entry_exhaustion_lab.py",
        root / "app" / "reversal_candidate_lab.py",
        root / "app" / "exit_policy_v2.py",
    ]
    forbidden = [
        "private_get(",
        "private_post(",
        "place_order(",
        "set_leverage(",
        "set_margin_mode(",
        "ExecutionEngine.execute",
        "PaperTrader.open_position",
        "can_send_real_orders=True",
        "LIVE_TRADING=True",
        "ENABLE_PAPER_POLICY_FILTER=True",
    ]
    for module in modules:
        text = module.read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in text, f"{needle} found in {module}"
