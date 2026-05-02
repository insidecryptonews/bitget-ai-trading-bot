import numpy as np
import pandas as pd

from app.indicators import add_indicators


def sample_df(rows=250):
    base = np.linspace(100, 120, rows)
    return pd.DataFrame(
        {
            "open": base,
            "high": base + 1,
            "low": base - 1,
            "close": base + 0.2,
            "volume": np.linspace(1000, 2000, rows),
            "quote_volume": np.linspace(100000, 200000, rows),
        }
    )


def test_indicators_calculate_without_breaking():
    df = add_indicators(sample_df())
    assert "ema_9" in df.columns
    assert "rsi_14" in df.columns
    assert "macd_hist" in df.columns
    assert "atr_14" in df.columns
    assert df["atr_14"].dropna().iloc[-1] > 0

