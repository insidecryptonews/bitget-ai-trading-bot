from app.config import BotConfig
from app.execution_engine import ExecutionEngine
from app.order_manager import InstrumentRules
from app.risk_manager import RiskDecision
from app.signal_engine import Signal


class DummyDB:
    def record_trade(self, **kwargs):
        return 1


class DummyTelegram:
    def trade_opened(self, signal):
        raise AssertionError("No debería abrir en dry-run")

    def critical(self, message):
        pass

    def send(self, message):
        pass


class DummyLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class DummyClient:
    def __init__(self):
        self.orders = []
        self.closes = []
        self.ensure_ok = True

    def place_order(self, **kwargs):
        self.orders.append(kwargs)
        return {"orderId": "open"}

    def ensure_isolated_margin(self, symbol, side):
        return {"isolatedVerified": self.ensure_ok, "marginMode": "isolated" if self.ensure_ok else "crossed"}

    def set_leverage(self, *args, **kwargs):
        pass

    def close_position_market(self, symbol, side, size, client_oid):
        self.closes.append({"symbol": symbol, "side": side, "size": size, "client_oid": client_oid})
        return {"orderId": "close"}


def signal():
    return Signal(
        symbol="BTCUSDT",
        side="LONG",
        strategy_type="BREAKOUT",
        confidence_score=85,
        entry_price=100,
        stop_loss=98,
        take_profit_1=103,
        take_profit_2=105,
        trailing_stop_enabled=True,
        trailing_stop_rule="ATR",
        risk_reward_ratio=1.5,
        leverage_recommendation=3,
        position_size=0.1,
        reason="test",
    )


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


def test_execution_engine_does_not_send_real_orders_in_dry_run():
    cfg = BotConfig(paper_trading=False, live_trading=True, dry_run=True)
    client = DummyClient()
    engine = ExecutionEngine(cfg, client, DummyDB(), DummyTelegram(), DummyLogger())
    result = engine.execute(signal(), RiskDecision(True, "ok", signal=signal()))
    assert result.mode == "dry_run"
    assert client.orders == []


def test_execution_engine_blocks_when_ensure_isolated_fails():
    cfg = BotConfig(
        paper_trading=False,
        live_trading=True,
        dry_run=False,
        bitget_api_key="key",
        bitget_api_secret="secret",
        bitget_passphrase="pass",
    )
    client = DummyClient()
    client.ensure_ok = False
    engine = ExecutionEngine(cfg, client, DummyDB(), DummyTelegram(), DummyLogger())
    risk = RiskDecision(True, "ok", signal=signal(), selected_margin_usdt=12, notional=36)
    result = engine.execute(signal(), risk, rules())
    assert not result.executed
    assert "isolated" in result.reason
    assert client.orders == []


class StopFailExecutionEngine(ExecutionEngine):
    def _place_stop(self, signal, rules, size, hold_side):
        return False

    def _place_take_profits(self, signal, rules, size, hold_side):
        return True


def test_execution_engine_stop_failure_closes_with_original_position_side():
    cfg = BotConfig(
        paper_trading=False,
        live_trading=True,
        dry_run=False,
        bitget_api_key="key",
        bitget_api_secret="secret",
        bitget_passphrase="pass",
    )
    client = DummyClient()
    engine = StopFailExecutionEngine(cfg, client, DummyDB(), DummyTelegram(), DummyLogger())
    sig = signal()
    risk = RiskDecision(True, "ok", signal=sig, selected_margin_usdt=12, notional=36)
    result = engine.execute(sig, risk, rules())
    assert not result.executed
    assert client.closes == [{"symbol": "BTCUSDT", "side": "LONG", "size": "0.1", "client_oid": client.closes[0]["client_oid"]}]
