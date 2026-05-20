from pathlib import Path

from app.config import BotConfig


ROOT = Path(__file__).resolve().parents[1]


def test_default_config_remains_paper_safe():
    config = BotConfig()

    assert config.paper_trading is True
    assert config.live_trading is False
    assert config.dry_run is True
    assert config.enable_paper_policy_filter is False
    assert config.margin_mode == "isolated"


def test_research_modules_do_not_place_orders():
    research_modules = [
        "app/strategy_research_library.py",
        "app/exit_policy_v3.py",
        "app/walk_forward_validator.py",
        "app/anti_overfit_matrix_v2.py",
        "app/candidate_promotion_v2.py",
        "app/real_strategy_backtester.py",
    ]
    forbidden = ("place_order(", "ExecutionEngine(", "BitgetClient(")
    for rel in research_modules:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert not any(item in text for item in forbidden), rel


def test_dashboard_has_no_dangerous_post_actions():
    js = (ROOT / "app/static/dashboard.js").read_text(encoding="utf-8")
    html = (ROOT / "app/static/dashboard.html").read_text(encoding="utf-8")

    assert "method: \"POST\"" not in js
    assert "method: 'POST'" not in js
    assert "activate live" not in html.lower()
    assert "place order" not in html.lower()
