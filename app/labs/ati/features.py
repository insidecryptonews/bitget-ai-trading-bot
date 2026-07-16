"""Causal multi-timeframe feature construction for ATI V2."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REQUIRED_PRICE_COLUMNS = ("open", "high", "low", "close", "volume")
TIMEFRAME_MINUTES = {"1m": 1, "15m": 15, "1h": 60, "4h": 240}


class AtiDataError(ValueError):
    """Fail-closed invalid OHLCV input."""


@dataclass(frozen=True)
class DataAudit:
    rows: int
    first_timestamp: str | None
    last_timestamp: str | None
    duplicate_rows: int
    gap_count: int
    invalid_rows: int
    expected_step_ms: int
    status: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _timestamp_column(columns: list[str]) -> str:
    lowered = {str(column).lower(): str(column) for column in columns}
    for alias in ("timestamp", "timestamp_ms", "ts", "open_time", "time"):
        if alias in lowered:
            return lowered[alias]
    raise AtiDataError("ATI_TIMESTAMP_COLUMN_MISSING")


def _to_utc_timestamp(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.notna().all():
        median = float(numeric.median()) if len(numeric) else 0.0
        unit = "ms" if median > 10_000_000_000 else "s"
        return pd.to_datetime(numeric, unit=unit, utc=True, errors="coerce")
    return pd.to_datetime(series, utc=True, errors="coerce")


def canonicalize_ohlcv(frame: pd.DataFrame, *, symbol: str,
                       timeframe: str = "1m") -> tuple[pd.DataFrame, DataAudit]:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise AtiDataError("ATI_OHLCV_EMPTY")
    ts_column = _timestamp_column(list(frame.columns))
    missing = [column for column in REQUIRED_PRICE_COLUMNS if column not in frame.columns]
    if missing:
        raise AtiDataError("ATI_OHLCV_MISSING_COLUMNS:" + ",".join(missing))
    out = pd.DataFrame({"timestamp": _to_utc_timestamp(frame[ts_column])})
    for column in REQUIRED_PRICE_COLUMNS:
        out[column] = pd.to_numeric(frame[column], errors="coerce")
    out["symbol"] = str(symbol).upper()
    out["timeframe"] = timeframe
    finite = np.isfinite(out[list(REQUIRED_PRICE_COLUMNS)].to_numpy(dtype=float)).all(axis=1)
    coherent = (
        (out["open"] > 0)
        & (out["high"] > 0)
        & (out["low"] > 0)
        & (out["close"] > 0)
        & (out["volume"] >= 0)
        & (out["high"] >= out[["open", "close"]].max(axis=1))
        & (out["low"] <= out[["open", "close"]].min(axis=1))
        & (out["high"] >= out["low"])
        & out["timestamp"].notna()
    )
    valid = finite & coherent.to_numpy(dtype=bool)
    invalid_rows = int((~valid).sum())
    out = out.loc[valid].sort_values("timestamp").reset_index(drop=True)
    duplicates = int(out.duplicated(subset=["timestamp"], keep=False).sum())
    step_ms = TIMEFRAME_MINUTES.get(timeframe, 0) * 60_000
    gaps = 0
    if len(out) > 1 and step_ms:
        # ``DatetimeTZDtype`` may preserve seconds/ms/us resolution depending
        # on pandas/input. Timedelta arithmetic is resolution-independent.
        deltas = out["timestamp"].diff().dropna().dt.total_seconds() * 1000.0
        gaps = int((deltas != step_ms).sum())
    status = "OK" if not invalid_rows and not duplicates and not gaps else "INVALID_DATA"
    audit = DataAudit(
        rows=len(out),
        first_timestamp=(out["timestamp"].iloc[0].isoformat() if len(out) else None),
        last_timestamp=(out["timestamp"].iloc[-1].isoformat() if len(out) else None),
        duplicate_rows=duplicates,
        gap_count=gaps,
        invalid_rows=invalid_rows,
        expected_step_ms=step_ms,
        status=status,
    )
    if status != "OK":
        raise AtiDataError(
            f"ATI_RAW_DATA_FAIL:invalid={invalid_rows}:duplicates={duplicates}:gaps={gaps}"
        )
    return out, audit


def read_ohlcv_csv(path: Path | str, *, symbol: str,
                   timeframe: str = "1m") -> tuple[pd.DataFrame, DataAudit]:
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise AtiDataError("ATI_INPUT_FILE_UNSAFE_OR_MISSING")
    try:
        frame = pd.read_csv(target)
    except (OSError, pd.errors.ParserError, UnicodeDecodeError) as exc:
        raise AtiDataError("ATI_INPUT_CSV_UNREADABLE") from exc
    return canonicalize_ohlcv(frame, symbol=symbol, timeframe=timeframe)


def resample_closed(frame: pd.DataFrame, timeframe: str, *,
                    source_timeframe: str, as_of: pd.Timestamp | None = None) -> pd.DataFrame:
    """Aggregate only complete, closed buckets from a continuous source.

    The source timestamp is the bar-open timestamp. A target bucket is exposed
    only after every expected source bar exists and its close is <= ``as_of``.
    """
    if timeframe not in TIMEFRAME_MINUTES or source_timeframe not in TIMEFRAME_MINUTES:
        raise AtiDataError("ATI_UNSUPPORTED_TIMEFRAME")
    target_minutes = TIMEFRAME_MINUTES[timeframe]
    source_minutes = TIMEFRAME_MINUTES[source_timeframe]
    if target_minutes % source_minutes:
        raise AtiDataError("ATI_NON_DIVISIBLE_TIMEFRAME")
    expected = target_minutes // source_minutes
    indexed = frame.set_index("timestamp").sort_index()
    rule = f"{target_minutes}min"
    grouped = indexed.resample(rule, label="left", closed="left", origin="epoch")
    result = grouped.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        source_rows=("close", "count"),
    ).reset_index()
    result = result[result["source_rows"] == expected].copy()
    result["available_at"] = result["timestamp"] + pd.Timedelta(minutes=target_minutes)
    if as_of is None:
        source_last_close = frame["timestamp"].max() + pd.Timedelta(minutes=source_minutes)
        as_of = source_last_close
    as_of = pd.Timestamp(as_of)
    if as_of.tzinfo is None:
        as_of = as_of.tz_localize("UTC")
    else:
        as_of = as_of.tz_convert("UTC")
    result = result[result["available_at"] <= as_of].copy()
    result["symbol"] = str(frame["symbol"].iloc[0])
    result["timeframe"] = timeframe
    return result.reset_index(drop=True)


def add_indicators(frame: pd.DataFrame, *, atr_period: int = 14) -> pd.DataFrame:
    out = frame.copy()
    previous_close = out["close"].shift(1)
    true_range = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - previous_close).abs(),
            (out["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr14"] = true_range.rolling(atr_period, min_periods=atr_period).mean()
    candle_range = (out["high"] - out["low"]).replace(0, np.nan)
    out["body_strength"] = ((out["close"] - out["open"]).abs() / candle_range).fillna(0.0)
    out["ema20"] = out["close"].ewm(span=20, adjust=False, min_periods=20).mean()
    out["ema50"] = out["close"].ewm(span=50, adjust=False, min_periods=50).mean()
    out["ema20_slope"] = out["ema20"].diff(3) / out["atr14"].replace(0, np.nan)
    atr_pct = out["atr14"] / out["close"]
    out["atr_percentile"] = atr_pct.rolling(100, min_periods=30).rank(pct=True)
    up = (out["ema20"] > out["ema50"]) & (out["ema20_slope"] > 0)
    down = (out["ema20"] < out["ema50"]) & (out["ema20_slope"] < 0)
    out["regime"] = np.select([up, down], ["TREND_UP", "TREND_DOWN"], default="RANGE")
    out["volatility_regime"] = np.where(
        out["atr_percentile"] >= 0.8,
        "HIGH_VOL",
        np.where(out["atr_percentile"] <= 0.2, "LOW_VOL", "NORMAL_VOL"),
    )
    return out


def build_feature_frame(raw_1m: pd.DataFrame, *, as_of: pd.Timestamp | None = None) -> pd.DataFrame:
    base = add_indicators(resample_closed(raw_1m, "15m", source_timeframe="1m", as_of=as_of))
    for timeframe, prefix in (("1h", "h1"), ("4h", "h4")):
        higher = add_indicators(resample_closed(raw_1m, timeframe, source_timeframe="1m", as_of=as_of))
        keep = ["available_at", "close", "ema20", "ema50", "ema20_slope", "atr14",
                "atr_percentile", "regime", "volatility_regime"]
        higher = higher[keep].rename(columns={column: f"{prefix}_{column}" for column in keep if column != "available_at"})
        base = pd.merge_asof(
            base.sort_values("available_at"),
            higher.sort_values("available_at"),
            on="available_at",
            direction="backward",
            allow_exact_matches=True,
        )
    base["feature_ready"] = (
        base["atr14"].notna()
        & base["h1_regime"].notna()
        & base["h4_regime"].notna()
        & base["h4_ema50"].notna()
    )
    return base.reset_index(drop=True)


def feature_row_is_finite(row: pd.Series) -> bool:
    for key in ("open", "high", "low", "close", "atr14", "body_strength"):
        try:
            if not math.isfinite(float(row[key])):
                return False
        except (KeyError, TypeError, ValueError):
            return False
    return True
