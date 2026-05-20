import pandas as pd

from app.config import BotConfig
from app.market_data import MarketSnapshot
from app.real_strategy_backtester import RealStrategyBacktester
from app.regime_detector import MarketRegime
from app.signal_engine import Signal


class SpyEngine:
    def __init__(self, *, side="LONG", stop=99.0, tp=102.0):
        self.calls = 0
        self.slice_lengths = []
        self.side = side
        self.stop = stop
        self.tp = tp

    def generate_signal(self, symbol: str, snapshot: MarketSnapshot, market_regime: MarketRegime) -> Signal:
        del market_regime
        self.calls += 1
        self.slice_lengths.append(len(snapshot.candles["5m"]))
        return Signal(
            symbol=symbol,
            side=self.side,
            strategy_type="test",
            confidence_score=90,
            entry_price=100.0,
            stop_loss=self.stop,
            take_profit_1=self.tp,
            take_profit_2=self.tp * 1.01,
            trailing_stop_enabled=False,
            trailing_stop_rule="",
            risk_reward_ratio=2.0,
            leverage_recommendation=1,
            position_size=0.0,
            reason="test",
        )


def candles() -> pd.DataFrame:
    base = pd.Timestamp("2026-01-01T00:00:00Z")
    rows = []
    for i in range(80):
        open_price = 100.0 + i * 0.01
        rows.append({
            "timestamp": base + pd.Timedelta(minutes=5 * i),
            "open": open_price,
            "high": open_price + (2.5 if i == 61 else 0.2),
            "low": open_price - (2.0 if i == 61 else 0.1),
            "close": open_price + 0.05,
            "volume": 1000,
            "quote_volume": 100000,
            "funding_rate": 0.0001,
        })
    return pd.DataFrame(rows)


def test_backtester_uses_signal_engine_and_entry_next_open():
    engine = SpyEngine()
    data = candles()

    result = RealStrategyBacktester(BotConfig(), signal_engine=engine).run("BTCUSDT", data, min_order_value_usdt=5, notional_usdt=10, max_holding_bars=3)

    assert result.uses_signal_engine is True
    assert engine.calls > 0
    assert result.trades[0].entry_price == data.iloc[61]["open"]
    assert result.entry_model == "signal_close_i_entry_next_open_i+1"


def test_backtester_no_lookahead_future_changes_do_not_change_first_signal_slice():
    engine_a = SpyEngine()
    engine_b = SpyEngine()
    data_a = candles()
    data_b = candles()
    data_b.loc[70:, "high"] = 999

    RealStrategyBacktester(BotConfig(), signal_engine=engine_a).run("BTCUSDT", data_a, min_order_value_usdt=5, notional_usdt=10)
    RealStrategyBacktester(BotConfig(), signal_engine=engine_b).run("BTCUSDT", data_b, min_order_value_usdt=5, notional_usdt=10)

    assert engine_a.slice_lengths[:5] == engine_b.slice_lengths[:5]


def test_backtester_same_bar_stop_before_tp():
    result = RealStrategyBacktester(BotConfig(), signal_engine=SpyEngine(stop=99.0, tp=101.0)).run("BTCUSDT", candles(), min_order_value_usdt=5, notional_usdt=10, max_holding_bars=3)

    assert result.trades[0].exit_reason == "STOP_LOSS"
    assert result.trades[0].same_bar_worst_case_applied is True


def test_backtester_min_bitget_order_blocks_without_rounding_up():
    result = RealStrategyBacktester(BotConfig(), signal_engine=SpyEngine()).run("BTCUSDT", candles(), min_order_value_usdt=50, notional_usdt=10)

    assert result.blocked_min_notional > 0
    assert result.trades == []


def test_backtester_applies_both_way_fees_and_never_sends_orders():
    result = RealStrategyBacktester(BotConfig(), signal_engine=SpyEngine()).run("BTCUSDT", candles(), min_order_value_usdt=5, notional_usdt=10)

    assert result.trades[0].fee_cost_bps == 12.0
    assert result.summary()["final_recommendation"] == "NO LIVE"
