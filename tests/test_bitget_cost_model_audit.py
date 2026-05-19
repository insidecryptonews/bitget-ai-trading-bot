from app.bitget_cost_model_audit import (
    BITGET_USDTM_VIP0_MAKER_BPS,
    BITGET_USDTM_VIP0_TAKER_BPS,
    BitgetCostModelAudit,
    BitgetCostModelSmokeTest,
    _CostSmokeDb,
    _funding_rate_to_pct,
    _scenario_costs,
)
from app.config import BotConfig
from app.edge_hardening_utils import cost_config


def test_bitget_cost_model_audit_uses_usdt_m_futures_fees_not_spot():
    db = _CostSmokeDb()
    db.initialize()
    payload = BitgetCostModelAudit(BotConfig(), db).build(hours=24)
    inventory = BitgetCostModelAudit(BotConfig(), db).inventory()

    assert payload["final_recommendation"] == "NO LIVE"
    assert payload["maker_fee"] == BITGET_USDTM_VIP0_MAKER_BPS == 2.0
    assert payload["taker_fee"] == BITGET_USDTM_VIP0_TAKER_BPS == 6.0
    assert payload["product_type"] == "USDT-M Futures perpetual"
    assert "spot" not in str(payload["fee_source_status"]).lower()
    assert inventory["applies_to_market_probe"] is True
    assert payload["cost_sensitivity_summary"]["groups"] > 0


def test_funding_can_be_income_or_cost_and_only_applies_when_crossed():
    costs = cost_config(BotConfig())
    short_rows = [{"side": "SHORT", "funding_rate": 0.0001, "bars": 100, "return_pct": 0.1}]
    long_rows = [{"side": "LONG", "funding_rate": 0.0001, "bars": 100, "return_pct": 0.1}]

    assert _funding_rate_to_pct(0.0001) == 0.01
    assert _scenario_costs(short_rows, costs)["dynamic_funding_by_symbol_if_available"] < _scenario_costs(long_rows, costs)["dynamic_funding_by_symbol_if_available"]
    no_cross = [{"side": "LONG", "funding_rate": 0.0001, "bars": 10, "return_pct": 0.1}]
    expected_no_funding = (2 * costs.taker_fee_bps + 2 * costs.slippage_bps) / 100.0
    assert abs(_scenario_costs(no_cross, costs)["zero_funding_if_no_timestamp_cross"] - expected_no_funding) < 0.000001


def test_bitget_cost_model_smoke_test_passes():
    text = BitgetCostModelSmokeTest(BotConfig()).to_text()

    assert "BITGET COST MODEL SMOKE TEST START" in text
    assert "usdt_m_vip0_maker_fee_ok: true" in text
    assert "spot_fee_not_used_for_futures: true" in text
    assert "final_recommendation: NO LIVE" in text
    assert "result: PASS" in text
