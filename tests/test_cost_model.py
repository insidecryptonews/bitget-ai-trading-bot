from app.cost_model import (
    compute_net_ev,
    explain_cost_breakdown,
    get_bitget_usdt_m_vip0_fee_model,
    round_trip_fee_bps,
)


def test_bitget_usdt_m_vip0_round_trips():
    model = get_bitget_usdt_m_vip0_fee_model()

    assert model.product_type == "USDT-M Futures perpetual"
    assert model.maker_fee_bps == 2.0
    assert model.taker_fee_bps == 6.0
    assert round_trip_fee_bps("taker", "taker") == 12.0
    assert round_trip_fee_bps("maker", "taker") == 8.0
    assert round_trip_fee_bps("maker", "maker") == 4.0


def test_market_probe_and_time_no_trade_do_not_receive_false_costs():
    probe = explain_cost_breakdown(source="market_probe")
    time_no_trade = explain_cost_breakdown(outcome="TIME", time_exit_assumption="no_trade")

    assert probe.total_cost_bps == 0.0
    assert probe.actionability == "NOT_ACTIONABLE_MARKET_PROBE"
    assert time_no_trade.total_cost_bps == 0.0


def test_compute_net_ev_keeps_components_separate():
    assert compute_net_ev(0.30, fee_bps=12.0, slippage_bps=6.0, funding_bps=0.0) == 0.12
