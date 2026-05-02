from app.config import BotConfig
from app.market_data import MarketSnapshot
from app.regime_detector import MarketRegime
from app.signal_engine import SignalEngine


def test_signal_engine_returns_no_trade_with_incomplete_data():
    signal = SignalEngine(BotConfig()).generate_signal(
        "BTCUSDT",
        MarketSnapshot(symbol="BTCUSDT"),
        MarketRegime("RANGE"),
    )
    assert signal.side == "NO_TRADE"
    assert "datos" in signal.reason.lower()

