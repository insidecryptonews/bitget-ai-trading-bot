from app.net_rr import calculate_net_rr


def test_net_rr_separates_gross_and_cost_adjusted_rr():
    result = calculate_net_rr(entry=100, stop_loss=99.4, take_profit_1=100.96, side="LONG", slippage_bps=3)

    assert round(result.gross_rr, 2) == 1.60
    assert 1.02 <= result.net_rr <= 1.12
    assert result.gross_rr > result.net_rr
    assert result.fee_cost_bps == 12
    assert result.slippage_cost_bps == 3
    assert result.rr_warning == "NET_RR_BELOW_MIN"


def test_net_rr_invalid_inputs_are_safe():
    result = calculate_net_rr(entry=0, stop_loss=99, take_profit_1=101, side="LONG")

    assert result.net_rr == 0
    assert result.rr_cost_adjusted is False
