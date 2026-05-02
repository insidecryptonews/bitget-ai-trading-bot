import numpy as np
import pandas as pd

from app.indicators import add_indicators
from app.market_data import MarketSnapshot
from app.regime_detector import RegimeDetector


def flat_df(rows=120):
    base = np.full(rows, 100.0) + np.sin(np.linspace(0, 5, rows)) * 0.1
    return add_indicators(
        pd.DataFrame(
            {
                "open": base,
                "high": base + 0.2,
                "low": base - 0.2,
                "close": base,
                "volume": np.full(rows, 1000.0),
                "quote_volume": np.full(rows, 100000.0),
            }
        )
    )


def test_regime_detector_detects_lateral_market():
    snap = MarketSnapshot(symbol="BTCUSDT", candles={"15m": flat_df(), "1h": flat_df()})
    regime = RegimeDetector().detect({"BTCUSDT": snap})
    assert regime.regime in {"CHOPPY_MARKET", "RANGE"}

