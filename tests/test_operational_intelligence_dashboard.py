from pathlib import Path

from app.config import PROJECT_ROOT


def test_dashboard_contains_operational_intelligence_panel_and_safe_buttons():
    html = (Path(PROJECT_ROOT) / "app" / "static" / "dashboard.html").read_text(encoding="utf-8")
    js = (Path(PROJECT_ROOT) / "app" / "static" / "dashboard.js").read_text(encoding="utf-8")

    assert 'id="operational-intelligence"' in html
    assert "Operational Intelligence / Fase 7" in html
    assert "operational-intelligence-audit" in js
    assert "exit-policy-v3-backtest" in js
    assert "candidate-promotion-v2" in js
    assert "shadow-strategy-simulator" in js
    assert "strategy-research-library" in js
    assert "data-restore" not in js.split("const MAIN_ANALYSIS_STEPS", 1)[-1].split("];", 1)[0]
    assert "LIVE_TRADING=true" not in js
