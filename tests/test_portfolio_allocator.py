from app.config import BotConfig
from app.portfolio_allocator import PortfolioAllocator
from app.signal_engine import Signal


def make_signal(symbol, score, side="LONG"):
    return Signal(
        symbol=symbol,
        side=side,
        strategy_type="BREAKOUT",
        confidence_score=score,
        entry_price=100,
        stop_loss=98,
        take_profit_1=103,
        take_profit_2=105,
        trailing_stop_enabled=True,
        trailing_stop_rule="ATR",
        risk_reward_ratio=1.5,
        leverage_recommendation=3,
        position_size=0,
        reason="test",
    )


def test_allocator_does_not_open_too_many_positions_small_balance():
    allocator = PortfolioAllocator(BotConfig())
    result = allocator.allocate(
        [make_signal("BTCUSDT", 86), make_signal("ETHUSDT", 87), make_signal("XRPUSDT", 74)],
        balance=40,
        open_positions=[],
    )
    assert len(result.selected_signals) <= 2


def test_allocator_prefers_one_position_under_30_usdt():
    allocator = PortfolioAllocator(BotConfig())
    result = allocator.allocate([make_signal("BTCUSDT", 86), make_signal("XRPUSDT", 88)], balance=25)
    assert len(result.selected_signals) == 1

