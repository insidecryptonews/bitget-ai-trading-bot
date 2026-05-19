from app.bitget_cost_model_audit import (
    BITGET_USDTM_VIP0_MAKER_BPS,
    BITGET_USDTM_VIP0_TAKER_BPS,
    BitgetCostModelAudit,
    BitgetCostModelSmokeTest,
    _CostSmokeDb,
)
from app.config import BotConfig
from app.cost_model import explain_cost_breakdown, normalize_funding_rate_to_bps, should_apply_funding


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
    assert inventory["applies_to_market_probe"] is False
    assert payload["cost_sensitivity_summary"]["groups"] > 0


def test_funding_can_be_income_or_cost_and_only_applies_when_crossed():
    assert normalize_funding_rate_to_bps(0.0001) == 1.0
    assert should_apply_funding("2026-05-19T07:55:00+00:00", "2026-05-19T08:05:00+00:00") is True
    assert should_apply_funding("2026-05-19T01:00:00+00:00", "2026-05-19T02:00:00+00:00") is False
    short = explain_cost_breakdown(side="SHORT", entry_time="2026-05-19T07:55:00+00:00", exit_time="2026-05-19T08:05:00+00:00", funding_rate=0.0001)
    long = explain_cost_breakdown(side="LONG", entry_time="2026-05-19T07:55:00+00:00", exit_time="2026-05-19T08:05:00+00:00", funding_rate=0.0001)
    no_cross = explain_cost_breakdown(side="LONG", entry_time="2026-05-19T01:00:00+00:00", exit_time="2026-05-19T02:00:00+00:00", funding_rate=0.0001)
    assert short.funding_component_bps < 0
    assert long.funding_component_bps > 0
    assert no_cross.funding_component_bps == 0


def test_bitget_cost_model_smoke_test_passes():
    text = BitgetCostModelSmokeTest(BotConfig()).to_text()

    assert "BITGET COST MODEL SMOKE TEST START" in text
    assert "usdt_m_vip0_maker_fee_ok: true" in text
    assert "spot_fee_not_used_for_futures: true" in text
    assert "final_recommendation: NO LIVE" in text
    assert "result: PASS" in text
