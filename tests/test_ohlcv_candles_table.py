from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import load_config
from app.database import Database


@pytest.fixture()
def db(tmp_path: Path, monkeypatch) -> Database:
    monkeypatch.setattr("app.database.PROJECT_ROOT", tmp_path)
    cfg = load_config()
    instance = Database(cfg, logging.getLogger("test"))
    instance.sqlite_path = tmp_path / "test.db"
    instance.initialize()
    return instance


def _row(symbol: str, timeframe: str, minutes_offset: int, *, base: float = 50000.0) -> dict:
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=minutes_offset)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "timestamp": ts.isoformat(),
        "open": base,
        "high": base + 50,
        "low": base - 50,
        "close": base + 10,
        "volume": 100.0,
        "quote_volume": 100.0 * base,
    }


def test_ohlcv_table_is_created_with_expected_columns(db: Database) -> None:
    assert db.table_exists("ohlcv_candles")
    columns = db.get_table_columns("ohlcv_candles")
    expected = {"symbol", "timeframe", "timestamp", "open", "high", "low", "close", "volume", "quote_volume", "source", "ingested_at"}
    assert expected.issubset(set(columns))


def test_insert_ohlcv_batch_inserts_valid_rows(db: Database) -> None:
    rows = [_row("BTCUSDT", "5m", offset) for offset in (0, 5, 10)]
    result = db.insert_ohlcv_batch(rows)
    assert result == {"inserted": 3, "skipped": 0, "rejected": 0}
    assert db.count_ohlcv_rows("BTCUSDT", "5m") == 3


def test_insert_ohlcv_batch_is_idempotent(db: Database) -> None:
    rows = [_row("BTCUSDT", "5m", offset) for offset in (0, 5)]
    db.insert_ohlcv_batch(rows)
    second = db.insert_ohlcv_batch(rows)
    assert second["inserted"] == 0
    assert second["skipped"] == 2
    assert db.count_ohlcv_rows("BTCUSDT", "5m") == 2


def test_insert_ohlcv_batch_rejects_invalid_rows(db: Database) -> None:
    bad_high = _row("BTCUSDT", "5m", 0)
    bad_high["high"] = bad_high["close"] - 1
    bad_volume = _row("BTCUSDT", "5m", 5)
    bad_volume["volume"] = -1
    missing_symbol = _row("", "5m", 10)
    bad_type = _row("BTCUSDT", "5m", 15)
    bad_type["open"] = "not-a-number"
    result = db.insert_ohlcv_batch([bad_high, bad_volume, missing_symbol, bad_type])
    assert result["inserted"] == 0
    assert result["rejected"] == 4
    assert db.count_ohlcv_rows() == 0


def test_get_latest_ohlcv_timestamp_returns_max_per_pair(db: Database) -> None:
    db.insert_ohlcv_batch([_row("BTCUSDT", "5m", 0), _row("BTCUSDT", "5m", 10)])
    db.insert_ohlcv_batch([_row("ETHUSDT", "5m", 0)])
    latest_btc = db.get_latest_ohlcv_timestamp("BTCUSDT", "5m")
    latest_eth = db.get_latest_ohlcv_timestamp("ETHUSDT", "5m")
    assert latest_btc is not None and latest_btc.endswith("+00:00")
    assert latest_eth is not None
    assert latest_btc > latest_eth


def test_get_latest_ohlcv_timestamp_returns_none_when_empty(db: Database) -> None:
    assert db.get_latest_ohlcv_timestamp("BTCUSDT", "5m") is None


def test_fetch_ohlcv_range_filters_by_symbol_timeframe_and_window(db: Database) -> None:
    db.insert_ohlcv_batch([_row("BTCUSDT", "5m", offset) for offset in (0, 5, 10, 15)])
    db.insert_ohlcv_batch([_row("BTCUSDT", "15m", 0)])
    since_iso = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=5)).isoformat()
    until_iso = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=10)).isoformat()
    rows = db.fetch_ohlcv_range("BTCUSDT", "5m", since_iso=since_iso, until_iso=until_iso)
    assert len(rows) == 2
    timestamps = [row["timestamp"] for row in rows]
    assert timestamps == sorted(timestamps)
    for row in rows:
        assert row["symbol"] == "BTCUSDT"
        assert row["timeframe"] == "5m"


def test_count_ohlcv_rows_supports_filters(db: Database) -> None:
    db.insert_ohlcv_batch([_row("BTCUSDT", "5m", 0), _row("BTCUSDT", "5m", 5)])
    db.insert_ohlcv_batch([_row("ETHUSDT", "5m", 0)])
    db.insert_ohlcv_batch([_row("BTCUSDT", "15m", 0)])
    assert db.count_ohlcv_rows() == 4
    assert db.count_ohlcv_rows("BTCUSDT") == 3
    assert db.count_ohlcv_rows("BTCUSDT", "5m") == 2


def test_replay_loader_finds_persisted_candles(db: Database) -> None:
    from app.ohlcv_replay_loader import OhlcvReplayLoader

    db.insert_ohlcv_batch([_row("BTCUSDT", "5m", offset) for offset in (0, 5, 10)])
    result = OhlcvReplayLoader(db).load_ohlcv(
        symbols=["BTCUSDT"],
        timeframe="5m",
        since=datetime(2025, 12, 31, tzinfo=timezone.utc),
    )
    assert result.status == "OK"
    assert result.table == "ohlcv_candles"
    assert len(result.rows) == 3
    assert "BTCUSDT" in result.frames_by_symbol
