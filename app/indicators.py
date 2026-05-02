from __future__ import annotations

import numpy as np
import pandas as pd


REQUIRED_COLUMNS = {"open", "high", "low", "close", "volume"}


def ensure_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"Faltan columnas OHLCV: {sorted(missing)}")
    out = df.copy()
    for col in ["open", "high", "low", "close", "volume", "quote_volume"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["open", "high", "low", "close"])
    return out


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    avg_gain = gains.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = losses.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return true_range.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def macd(series: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    macd_line = ema(series, 12) - ema(series, 26)
    signal = ema(macd_line, 9)
    histogram = macd_line - signal
    return macd_line, signal, histogram


def bollinger(series: pd.Series, period: int = 20, std_mult: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    middle = series.rolling(period).mean()
    deviation = series.rolling(period).std(ddof=0)
    upper = middle + std_mult * deviation
    lower = middle - std_mult * deviation
    return upper, middle, lower


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = ensure_ohlcv(df)
    if len(out) < 5:
        return out

    close = out["close"]
    out["ema_9"] = ema(close, 9)
    out["ema_21"] = ema(close, 21)
    out["ema_50"] = ema(close, 50)
    out["ema_100"] = ema(close, 100)
    out["ema_200"] = ema(close, 200)
    out["rsi_14"] = rsi(close, 14)
    out["macd"], out["macd_signal"], out["macd_hist"] = macd(close)
    out["atr_14"] = atr(out, 14)
    out["bb_upper"], out["bb_middle"], out["bb_lower"] = bollinger(close)

    typical_price = (out["high"] + out["low"] + out["close"]) / 3
    volume = out["volume"].replace(0, np.nan)
    out["vwap"] = (typical_price * volume).cumsum() / volume.cumsum()

    out["volume_ma_20"] = out["volume"].rolling(20, min_periods=1).mean()
    out["volume_relative"] = out["volume"] / out["volume_ma_20"].replace(0, np.nan)
    out["support_recent"] = out["low"].rolling(20, min_periods=5).min()
    out["resistance_recent"] = out["high"].rolling(20, min_periods=5).max()
    out["range_high_30"] = out["high"].rolling(30, min_periods=10).max()
    out["range_low_30"] = out["low"].rolling(30, min_periods=10).min()
    out["range_width_pct"] = (out["range_high_30"] - out["range_low_30"]) / out["close"].replace(0, np.nan)
    out["distance_to_ema_200"] = (out["close"] - out["ema_200"]) / out["close"].replace(0, np.nan)
    out["normalized_atr"] = out["atr_14"] / out["close"].replace(0, np.nan)
    out["atr_ma_50"] = out["atr_14"].rolling(50, min_periods=10).mean()
    out["volatility_compression"] = out["atr_14"] < (out["atr_ma_50"] * 0.75)
    out["volatility_expansion"] = out["atr_14"] > (out["atr_ma_50"] * 1.35)
    out["momentum_5"] = close.pct_change(5)
    out["momentum_15"] = close.pct_change(15)
    out["higher_high"] = out["high"] > out["high"].shift(1).rolling(4, min_periods=1).max()
    out["higher_low"] = out["low"] > out["low"].shift(1).rolling(4, min_periods=1).min()
    out["lower_high"] = out["high"] < out["high"].shift(1).rolling(4, min_periods=1).max()
    out["lower_low"] = out["low"] < out["low"].shift(1).rolling(4, min_periods=1).min()
    out["body_pct"] = (out["close"] - out["open"]).abs() / out["close"].replace(0, np.nan)
    out["upper_wick_pct"] = (out["high"] - out[["open", "close"]].max(axis=1)) / out["close"].replace(0, np.nan)
    out["lower_wick_pct"] = (out[["open", "close"]].min(axis=1) - out["low"]) / out["close"].replace(0, np.nan)
    out["bullish_rejection"] = (out["lower_wick_pct"] > out["body_pct"] * 1.5) & (out["close"] > out["open"])
    out["bearish_rejection"] = (out["upper_wick_pct"] > out["body_pct"] * 1.5) & (out["close"] < out["open"])
    return out.replace([np.inf, -np.inf], np.nan)


def has_enough_data(df: pd.DataFrame | None, min_rows: int = 60) -> bool:
    return df is not None and len(df.dropna(subset=["close"])) >= min_rows


def latest_row(df: pd.DataFrame) -> pd.Series:
    if df.empty:
        raise ValueError("DataFrame vacío")
    return df.iloc[-1]


def trend_bias(df: pd.DataFrame) -> str:
    if df is None or df.empty or "ema_50" not in df.columns:
        return "neutral"
    row = latest_row(df)
    if row["close"] > row["ema_21"] > row["ema_50"] and row["macd_hist"] > 0:
        return "bullish"
    if row["close"] < row["ema_21"] < row["ema_50"] and row["macd_hist"] < 0:
        return "bearish"
    return "neutral"

