from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from app.ohlcv_replay_loader import OhlcvReplayLoader, ohlcv_replay_loader_smoke_text


class FakeDb:
    def __init__(self, tables):
        self.tables = tables

    def table_exists(self, table: str) -> bool:
        return table in self.tables

    def get_table_columns(self, table: str) -> list[str]:
        rows = self.tables.get(table, [])
        return list(rows[0].keys()) if rows else []

    def fetch_table_rows(self, table: str, *, since_iso: str | None = None, timestamp_column: str | None = None, limit: int = 200000):
        rows = list(self.tables.get(table, []))
        if since_iso and timestamp_column:
            since = pd.Timestamp(since_iso)
            rows = [row for row in rows if pd.Timestamp(row[timestamp_column]) >= since]
        return rows[:limit]


def _rows(*, gap: bool = False, duplicate: bool = False):
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    rows = []
    for index in range(12):
        if gap and index == 6:
            continue
        timestamp = base + timedelta(minutes=5 * index)
        rows.append(
            {
                "timestamp": timestamp.isoformat(),
                "symbol": "BTCUSDT",
                "timeframe": "5m",
                "open": 100 + index,
                "high": 101 + index,
                "low": 99 + index,
                "close": 100.5 + index,
                "volume": 1000 + index,
            }
        )
    if duplicate:
        rows.append(dict(rows[-1]))
    return rows


def test_ohlcv_loader_detects_missing_table():
    result = OhlcvReplayLoader(FakeDb({})).load_ohlcv(
        symbols=["BTCUSDT"],
        timeframe="5m",
        since=datetime.now(timezone.utc) - timedelta(hours=2),
    )

    assert result.status == "NEED_DATA"
    assert "missing_ohlcv_table" in ",".join(result.warnings)


def test_ohlcv_loader_detects_missing_columns():
    result = OhlcvReplayLoader(FakeDb({"candles": [{"timestamp": datetime.now(timezone.utc).isoformat(), "symbol": "BTCUSDT"}]})).load_ohlcv(
        symbols=["BTCUSDT"],
        timeframe="5m",
        since=datetime.now(timezone.utc) - timedelta(hours=2),
    )

    assert result.status == "MISSING_COLUMNS"
    assert "open" in result.missing_columns


def test_ohlcv_loader_orders_by_timestamp():
    rows = list(reversed(_rows()))
    result = OhlcvReplayLoader(FakeDb({"candles": rows})).load_ohlcv(
        symbols=["BTCUSDT"],
        timeframe="5m",
        since=datetime.now(timezone.utc) - timedelta(hours=2),
    )

    frame = result.frames_by_symbol["BTCUSDT"]
    assert result.status == "OK"
    assert frame["timestamp"].is_monotonic_increasing


def test_ohlcv_loader_detects_gaps_and_duplicates():
    gap_result = OhlcvReplayLoader(FakeDb({"candles": _rows(gap=True)})).load_ohlcv(
        symbols=["BTCUSDT"],
        timeframe="5m",
        since=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    dup_result = OhlcvReplayLoader(FakeDb({"candles": _rows(duplicate=True)})).load_ohlcv(
        symbols=["BTCUSDT"],
        timeframe="5m",
        since=datetime.now(timezone.utc) - timedelta(hours=2),
    )

    assert gap_result.status == "TOO_MANY_GAPS"
    assert gap_result.gap_count > 0
    assert dup_result.status == "DUPLICATE_CANDLES"
    assert dup_result.duplicate_candles > 0


def test_ohlcv_replay_loader_smoke_passes():
    assert "result: PASS" in ohlcv_replay_loader_smoke_text()
    assert "NO LIVE" in ohlcv_replay_loader_smoke_text()
