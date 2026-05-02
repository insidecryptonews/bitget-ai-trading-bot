from app.config import BotConfig
from app.paper_trader import PaperTrader
from app.risk_manager import RiskDecision
from app.signal_engine import Signal


class DummyDB:
    def __init__(self):
        self.statuses = []

    def record_trade(self, **kwargs):
        return 1

    def update_trade_status(self, *args, **kwargs):
        self.statuses.append((args, kwargs))


class DummyTelegram:
    def trade_opened(self, signal):
        pass

    def send(self, message):
        pass


class DummyLogger:
    def info(self, *args, **kwargs):
        pass


def make_signal():
    return Signal(
        symbol="BTCUSDT",
        side="LONG",
        strategy_type="BREAKOUT",
        confidence_score=90,
        entry_price=100,
        stop_loss=98,
        take_profit_1=103,
        take_profit_2=105,
        trailing_stop_enabled=True,
        trailing_stop_rule="ATR",
        risk_reward_ratio=1.5,
        leverage_recommendation=3,
        position_size=0.36,
        reason="test",
    )


def test_paper_trader_reserves_and_releases_isolated_margin():
    trader = PaperTrader(BotConfig(), DummyDB(), DummyTelegram(), DummyLogger())
    risk = RiskDecision(True, "ok", selected_margin_usdt=12, notional=36)
    position = trader.open_position(make_signal(), risk_amount=1, risk=risk)
    assert position.margin_mode == "isolated"
    assert trader.reserved_margin == 12
    trader.monitor({"BTCUSDT": 98})
    assert trader.reserved_margin == 0
