from dataclasses import replace

from app.config import BotConfig
from app.order_manager import InstrumentRules, OrderManager
from app.risk_manager import RiskManager
from app.signal_engine import Signal


def rules():
    return InstrumentRules(
        symbol="BTCUSDT",
        min_trade_num=0.0001,
        min_trade_usdt=5,
        size_multiplier=0.0001,
        volume_place=4,
        price_place=1,
        price_end_step=1,
        min_leverage=1,
        max_leverage=125,
        maker_fee_rate=0.0004,
        taker_fee_rate=0.0006,
        symbol_status="normal",
        max_market_order_qty=1000,
        max_order_qty=1000,
    )


def symbol_rules(symbol, min_trade_num, min_trade_usdt, size_multiplier, volume_place, price_place=4):
    return InstrumentRules(
        symbol=symbol,
        min_trade_num=min_trade_num,
        min_trade_usdt=min_trade_usdt,
        size_multiplier=size_multiplier,
        volume_place=volume_place,
        price_place=price_place,
        price_end_step=1,
        min_leverage=1,
        max_leverage=125,
        maker_fee_rate=0.0004,
        taker_fee_rate=0.0006,
        symbol_status="normal",
        max_market_order_qty=1000000,
        max_order_qty=1000000,
    )


def valid_signal():
    return Signal(
        symbol="BTCUSDT",
        side="LONG",
        strategy_type="BREAKOUT",
        confidence_score=85,
        entry_price=100.0,
        stop_loss=98.0,
        take_profit_1=103.0,
        take_profit_2=105.0,
        trailing_stop_enabled=True,
        trailing_stop_rule="ATR",
        risk_reward_ratio=1.5,
        leverage_recommendation=3,
        position_size=0,
        reason="test",
        confirmations=["a", "b", "c"],
    )


def signal_for(symbol, entry, leverage=3, score=88):
    return Signal(
        symbol=symbol,
        side="LONG",
        strategy_type="BREAKOUT",
        confidence_score=score,
        entry_price=entry,
        stop_loss=entry * 0.99,
        take_profit_1=entry * 1.02,
        take_profit_2=entry * 1.03,
        trailing_stop_enabled=True,
        trailing_stop_rule="ATR",
        risk_reward_ratio=1.5,
        leverage_recommendation=leverage,
        position_size=0,
        reason="test",
        confirmations=["a", "b", "c"],
    )


def test_risk_manager_blocks_trade_without_stop():
    cfg = BotConfig()
    rm = RiskManager(cfg, OrderManager({"BTCUSDT": rules()}))
    decision = rm.validate_signal(replace(valid_signal(), stop_loss=0), balance=40, rules=rules())
    assert not decision.approved
    assert "stop" in decision.reason.lower()


def test_risk_manager_blocks_daily_loss_exceeded():
    cfg = BotConfig()
    rm = RiskManager(cfg, OrderManager({"BTCUSDT": rules()}))
    decision = rm.validate_signal(valid_signal(), balance=40, daily_pnl=-4, rules=rules())
    assert not decision.approved
    assert "diaria" in decision.reason.lower()


def test_position_sizing_respects_risk():
    cfg = BotConfig()
    rm = RiskManager(cfg, OrderManager({"BTCUSDT": rules()}))
    decision = rm.validate_signal(valid_signal(), balance=40, available_balance=40, rules=rules())
    assert decision.approved
    assert decision.real_risk <= decision.risk_amount + 1e-9
    assert decision.signal.position_size > 0


def test_small_account_tight_stop_is_blocked_with_clear_reason():
    cfg = BotConfig()
    rm = RiskManager(cfg, OrderManager({"BTCUSDT": rules()}))
    signal = replace(
        valid_signal(),
        stop_loss=99.8,
        take_profit_1=101.0,
        take_profit_2=102.0,
        risk_reward_ratio=5.0,
        leverage_recommendation=5,
    )
    decision = rm.validate_signal(signal, balance=40, available_balance=40, rules=rules())
    assert not decision.approved
    assert "stop_distance_pct" in decision.reason
    assert decision.stop_distance_pct < cfg.min_stop_distance_pct


def test_small_account_stop_one_percent_uses_margin_cap_and_allows_trade():
    cfg = BotConfig()
    rm = RiskManager(cfg, OrderManager({"BTCUSDT": rules()}))
    signal = replace(
        valid_signal(),
        stop_loss=99.0,
        take_profit_1=101.5,
        take_profit_2=102.5,
        risk_reward_ratio=1.5,
        leverage_recommendation=3,
    )
    decision = rm.validate_signal(signal, balance=40, available_balance=40, rules=rules())
    assert decision.approved
    assert decision.margin_required <= 40 * cfg.max_margin_usage_per_trade + 1e-9
    assert decision.notional <= 40 * cfg.max_margin_usage_per_trade * 3 + 1e-9
    assert decision.real_risk <= decision.risk_amount + 1e-9


def test_exchange_minimum_breaks_margin_or_risk_is_blocked():
    high_min_rules = replace(rules(), min_trade_usdt=80)
    cfg = BotConfig()
    rm = RiskManager(cfg, OrderManager({"BTCUSDT": high_min_rules}))
    signal = replace(
        valid_signal(),
        stop_loss=99.0,
        take_profit_1=101.5,
        take_profit_2=102.5,
        risk_reward_ratio=1.5,
        leverage_recommendation=3,
    )
    decision = rm.validate_signal(signal, balance=40, available_balance=40, rules=high_min_rules)
    assert not decision.approved
    assert "minimo de Bitget" in decision.reason


def test_risk_manager_blocks_margin_mode_not_isolated():
    cfg = BotConfig(margin_mode="crossed", disallow_crossed_margin=False)
    rm = RiskManager(cfg, OrderManager({"BTCUSDT": rules()}))
    decision = rm.validate_signal(valid_signal(), balance=40, available_balance=40, rules=rules())
    assert not decision.approved
    assert decision.block_reason == "margin_mode_not_isolated"


def test_risk_manager_blocks_second_position_small_account():
    cfg = BotConfig()
    rm = RiskManager(cfg, OrderManager({"BTCUSDT": rules()}))
    decision = rm.validate_signal(
        valid_signal(),
        balance=40,
        available_balance=28,
        open_positions=[{"symbol": "ETHUSDT", "margin_used": 12}],
        rules=rules(),
    )
    assert not decision.approved
    assert decision.block_reason == "max_positions"


def test_fixed_trade_margin_balance_40_leverage_3_notional_36():
    cfg = BotConfig()
    rm = RiskManager(cfg, OrderManager({"BTCUSDT": rules()}))
    decision = rm.validate_signal(valid_signal(), balance=40, available_balance=40, rules=rules())
    assert decision.approved
    assert round(decision.selected_margin_usdt, 2) == 12.00
    assert round(decision.notional, 2) == 36.00
    assert decision.leverage == 3


def test_fixed_trade_margin_balance_40_leverage_5_notional_60():
    cfg = BotConfig()
    rm = RiskManager(cfg, OrderManager({"BTCUSDT": rules()}))
    signal = replace(valid_signal(), confidence_score=90, leverage_recommendation=5, stop_loss=99.0, risk_reward_ratio=1.5)
    decision = rm.validate_signal(signal, balance=40, available_balance=40, rules=rules())
    assert decision.approved
    assert round(decision.selected_margin_usdt, 2) == 12.00
    assert round(decision.notional, 2) == 60.00
    assert decision.leverage == 5


def test_risk_manager_blocks_trade_margin_above_max():
    try:
        BotConfig(trade_margin_usdt="20.00", max_trade_margin_usdt="15.00")
    except ValueError:
        return
    raise AssertionError("TRADE_MARGIN_USDT > MAX_TRADE_MARGIN_USDT debe abortar configuracion")


def test_btc_quantity_uses_real_entry_price_not_mock_price():
    btc_rules = symbol_rules("BTCUSDT", 0.000001, 5, 0.000001, 6)
    rm = RiskManager(BotConfig(), OrderManager({"BTCUSDT": btc_rules}))
    decision = rm.validate_signal(signal_for("BTCUSDT", 77000), balance=40, available_balance=40, rules=btc_rules)
    assert decision.approved
    assert round(decision.target_notional, 2) == 36.00
    assert abs(decision.raw_quantity - (36 / 77000)) < 1e-10
    assert abs(decision.rounded_quantity - 0.000467) < 1e-9


def test_eth_quantity_uses_real_entry_price():
    eth_rules = symbol_rules("ETHUSDT", 0.00001, 5, 0.00001, 5)
    rm = RiskManager(BotConfig(), OrderManager({"ETHUSDT": eth_rules}))
    decision = rm.validate_signal(signal_for("ETHUSDT", 2300), balance=40, available_balance=40, rules=eth_rules)
    assert decision.approved
    assert round(decision.target_notional, 2) == 36.00
    assert abs(decision.raw_quantity - (36 / 2300)) < 1e-10
    assert abs(decision.rounded_quantity - 0.01565) < 1e-9


def test_link_quantity_uses_real_entry_price():
    link_rules = symbol_rules("LINKUSDT", 0.01, 5, 0.01, 2)
    rm = RiskManager(BotConfig(), OrderManager({"LINKUSDT": link_rules}))
    decision = rm.validate_signal(signal_for("LINKUSDT", 9.17), balance=40, available_balance=40, rules=link_rules)
    assert decision.approved
    assert round(decision.target_notional, 2) == 36.00
    assert abs(decision.raw_quantity - (36 / 9.17)) < 1e-10
    assert abs(decision.rounded_quantity - 3.92) < 1e-9


def test_blocks_when_rounding_deviates_more_than_five_percent():
    over_rules = symbol_rules("TESTUSDT", 0.0001, 5, 0.3888, 4)
    rm = RiskManager(BotConfig(), OrderManager({"TESTUSDT": over_rules}))
    decision = rm.validate_signal(signal_for("TESTUSDT", 100), balance=40, available_balance=40, rules=over_rules)
    assert not decision.approved
    assert decision.block_reason == "notional_deviation"
    assert decision.notional_deviation_side == "over"
    assert decision.notional_deviation_pct > decision.max_allowed_deviation_for_side


def test_blocks_when_rounded_notional_below_bitget_min_notional():
    link_rules = symbol_rules("LINKUSDT", 0.01, 50, 0.01, 2)
    rm = RiskManager(BotConfig(), OrderManager({"LINKUSDT": link_rules}))
    decision = rm.validate_signal(signal_for("LINKUSDT", 9.17), balance=40, available_balance=40, rules=link_rules)
    assert not decision.approved
    assert decision.block_reason == "notional_below_min"


def test_btc_under_deviation_8_percent_is_allowed():
    btc_rules = symbol_rules("BTCUSDT", 0.0001, 5, 0.0001, 4)
    rm = RiskManager(BotConfig(), OrderManager({"BTCUSDT": btc_rules}))
    signal = signal_for("BTCUSDT", 78258.2, leverage=5, score=90)
    decision = rm.validate_signal(signal, balance=40, available_balance=40, rules=btc_rules)
    assert decision.approved
    assert round(decision.target_notional, 2) == 60.00
    assert abs(decision.rounded_quantity - 0.0007) < 1e-12
    assert round(decision.calculated_notional_after_rounding, 4) == 54.7807
    assert decision.notional_deviation_side == "under"
    assert decision.notional_deviation_pct < 0.20


def test_blocks_when_under_deviation_exceeds_twenty_percent():
    under_rules = symbol_rules("TESTUSDT", 0.0001, 5, 0.25, 4)
    rm = RiskManager(BotConfig(), OrderManager({"TESTUSDT": under_rules}))
    decision = rm.validate_signal(signal_for("TESTUSDT", 100), balance=40, available_balance=40, rules=under_rules)
    assert not decision.approved
    assert decision.block_reason == "notional_deviation"
    assert decision.notional_deviation_side == "under"
    assert decision.notional_deviation_pct > 0.20


def test_over_deviation_four_percent_can_approve_if_safe():
    over_rules = symbol_rules("TESTUSDT", 0.0001, 5, 0.3744, 4)
    rm = RiskManager(BotConfig(), OrderManager({"TESTUSDT": over_rules}))
    decision = rm.validate_signal(signal_for("TESTUSDT", 100), balance=40, available_balance=40, rules=over_rules)
    assert decision.approved
    assert decision.notional_deviation_side == "over"
    assert decision.notional_deviation_pct < 0.05
    assert round(decision.calculated_notional_after_rounding, 2) == 37.44
