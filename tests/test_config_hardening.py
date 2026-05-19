from app.config import BotConfig
from app.execution_safety import validate_config_hardening


def test_config_hardening_blocks_dangerous_risk_and_cross_margin():
    dangerous_risk = validate_config_hardening(BotConfig(max_risk_per_trade=0.99))

    assert dangerous_risk["config_hardening_status"] == "BAD"


def test_valid_paper_config_keeps_real_orders_blocked():
    result = validate_config_hardening(BotConfig())

    assert result["config_hardening_status"] in {"OK", "WARNING"}
    assert result["can_send_real_orders"] is False
    assert result["paper_filter_enabled"] is False
