"""Historical OHLCV backfill from Bitget public endpoints into ohlcv_candles.

Usage:
    python -m app.ohlcv_backfill --symbols BTCUSDT,ETHUSDT --timeframes 5m,15m --days 365
    python -m app.ohlcv_backfill --symbols BTCUSDT --timeframes 5m --hours 72 --dry-run

Public endpoint /api/v2/mix/market/history-candles — no credentials required.
Idempotent: re-running resumes from the latest stored candle per (symbol, timeframe).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .bitget_client import BitgetClient
from .config import load_config
from .database import Database
from .logger import setup_logger
from .utils import safe_float


FINAL_RECOMMENDATION = "NO LIVE"

GRANULARITY_API = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1H",
    "4h": "4H",
    "1d": "1D",
}

GRANULARITY_MINUTES = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
}

DEFAULT_BATCH_LIMIT = 200
MAX_EMPTY_BATCHES = 5


@dataclass
class BackfillStats:
    symbol: str
    timeframe: str
    inserted: int = 0
    skipped: int = 0
    rejected: int = 0
    batches: int = 0
    api_calls: int = 0
    empty_batches: int = 0
    first_timestamp: str = ""
    last_timestamp: str = ""
    duration_seconds: float = 0.0
    status: str = "OK"
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "inserted": self.inserted,
            "skipped": self.skipped,
            "rejected": self.rejected,
            "batches": self.batches,
            "api_calls": self.api_calls,
            "empty_batches": self.empty_batches,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "duration_seconds": round(self.duration_seconds, 2),
            "status": self.status,
            "error": self.error,
        }


@dataclass
class BackfillReport:
    started_at: str
    ended_at: str = ""
    duration_seconds: float = 0.0
    dry_run: bool = False
    days: int = 0
    per_pair: list[BackfillStats] = field(default_factory=list)
    final_recommendation: str = FINAL_RECOMMENDATION

    def as_dict(self) -> dict[str, Any]:
        totals = {
            "inserted": sum(item.inserted for item in self.per_pair),
            "skipped": sum(item.skipped for item in self.per_pair),
            "rejected": sum(item.rejected for item in self.per_pair),
            "batches": sum(item.batches for item in self.per_pair),
            "api_calls": sum(item.api_calls for item in self.per_pair),
            "duration_seconds": round(self.duration_seconds, 2),
        }
        return {
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": round(self.duration_seconds, 2),
            "dry_run": self.dry_run,
            "days": self.days,
            "totals": totals,
            "per_pair": [item.as_dict() for item in self.per_pair],
            "final_recommendation": self.final_recommendation,
        }


def _parse_timestamp_ms(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()


def _candle_to_row(symbol: str, timeframe: str, raw: list[str]) -> dict[str, Any] | None:
    if not raw or len(raw) < 6:
        return None
    ts_ms = _parse_timestamp_ms(raw[0])
    if ts_ms is None:
        return None
    return {
        "symbol": symbol.upper(),
        "timeframe": timeframe.lower(),
        "timestamp": _ms_to_iso(ts_ms),
        "open": safe_float(raw[1]),
        "high": safe_float(raw[2]),
        "low": safe_float(raw[3]),
        "close": safe_float(raw[4]),
        "volume": safe_float(raw[5]),
        "quote_volume": safe_float(raw[6]) if len(raw) > 6 else 0.0,
        "source": "bitget_rest_v2",
    }


def _parse_iso_to_ms(iso_text: str | None) -> int | None:
    if not iso_text:
        return None
    try:
        dt = datetime.fromisoformat(iso_text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _resume_start_ms(db: Database, symbol: str, timeframe: str, default_start_ms: int, step_minutes: int) -> int:
    latest_ms = _parse_iso_to_ms(db.get_latest_ohlcv_timestamp(symbol, timeframe))
    if latest_ms is None:
        return default_start_ms
    next_ms = latest_ms + step_minutes * 60_000
    return max(default_start_ms, next_ms)


def _get_oldest_ohlcv_ms(db: Database, symbol: str, timeframe: str) -> int | None:
    sql = "SELECT MIN(timestamp) AS ts FROM ohlcv_candles WHERE symbol = ? AND timeframe = ?"
    if db._use_postgres:  # noqa: SLF001 — internal helper accepted for now
        sql = sql.replace("?", "%s")
    with db._connect() as conn:  # noqa: SLF001
        row = conn.execute(sql, (symbol.upper(), timeframe.lower())).fetchone()
        value = db._row_value(row, "ts", 0, None) if row is not None else None  # noqa: SLF001
    return _parse_iso_to_ms(str(value)) if value else None


def backfill_pair(
    *,
    client: BitgetClient,
    db: Database,
    symbol: str,
    timeframe: str,
    days: int,
    dry_run: bool,
    logger: logging.Logger,
    batch_limit: int = DEFAULT_BATCH_LIMIT,
    sleep_between_batches: float = 0.0,
) -> BackfillStats:
    timeframe = timeframe.lower()
    api_granularity = GRANULARITY_API.get(timeframe)
    step_minutes = GRANULARITY_MINUTES.get(timeframe)
    stats = BackfillStats(symbol=symbol.upper(), timeframe=timeframe)
    if api_granularity is None or step_minutes is None:
        stats.status = "INVALID_TIMEFRAME"
        stats.error = f"timeframe '{timeframe}' not supported"
        return stats

    target_start_ms = int((datetime.now(timezone.utc) - timedelta(days=max(1, int(days or 1)))).timestamp() * 1000)
    target_end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    started = time.monotonic()
    batch_minutes = step_minutes * batch_limit

    # Plan ranges to fetch:
    #   - backward range: [target_start_ms, oldest_in_db) if oldest_in_db > target_start_ms
    #   - forward range:  (latest_in_db, target_end_ms] if latest_in_db exists, else [target_start_ms, target_end_ms]
    oldest_in_db_ms = _get_oldest_ohlcv_ms(db, symbol, timeframe)
    latest_in_db_ms = _parse_iso_to_ms(db.get_latest_ohlcv_timestamp(symbol, timeframe))

    ranges_to_fetch: list[tuple[int, int]] = []
    if oldest_in_db_ms is not None and oldest_in_db_ms > target_start_ms:
        ranges_to_fetch.append((target_start_ms, oldest_in_db_ms))
    if latest_in_db_ms is None:
        if not ranges_to_fetch:
            ranges_to_fetch.append((target_start_ms, target_end_ms))
    else:
        forward_start = latest_in_db_ms + step_minutes * 60_000
        if forward_start < target_end_ms:
            ranges_to_fetch.append((forward_start, target_end_ms))

    if not ranges_to_fetch:
        stats.duration_seconds = time.monotonic() - started
        stats.status = "OK"
        return stats

    for range_start_ms, range_end_ms in ranges_to_fetch:
        cursor_ms = range_start_ms
        stats.empty_batches = 0
        while cursor_ms < range_end_ms:
            batch_end_ms = min(cursor_ms + batch_minutes * 60_000, range_end_ms)
            try:
                raw_batch = client.get_history_candles(
                    symbol.upper(),
                    api_granularity,
                    start_ms=cursor_ms,
                    end_ms=batch_end_ms,
                    limit=batch_limit,
                )
            except Exception as exc:
                stats.status = "API_ERROR"
                stats.error = str(exc)
                logger.warning("Backfill API error %s %s: %s", symbol, timeframe, exc)
                break
            stats.api_calls += 1
            stats.batches += 1

            if not raw_batch:
                stats.empty_batches += 1
                if stats.empty_batches >= MAX_EMPTY_BATCHES:
                    logger.info("Backfill %s %s: %d empty batches, advancing range", symbol, timeframe, stats.empty_batches)
                    break
                cursor_ms = batch_end_ms
                continue
            stats.empty_batches = 0

            rows: list[dict[str, Any]] = []
            max_ts_ms = cursor_ms
            for raw in raw_batch:
                row = _candle_to_row(symbol, timeframe, raw)
                if row is None:
                    stats.rejected += 1
                    continue
                rows.append(row)
                ts_ms = _parse_timestamp_ms(raw[0]) or 0
                if ts_ms > max_ts_ms:
                    max_ts_ms = ts_ms

            if rows:
                if not stats.first_timestamp or rows[0]["timestamp"] < stats.first_timestamp:
                    stats.first_timestamp = rows[0]["timestamp"]
                if rows[-1]["timestamp"] > stats.last_timestamp:
                    stats.last_timestamp = rows[-1]["timestamp"]

            if not dry_run and rows:
                result = db.insert_ohlcv_batch(rows)
                stats.inserted += result["inserted"]
                stats.skipped += result["skipped"]
                stats.rejected += result["rejected"]
            elif dry_run:
                stats.inserted += len(rows)

            if max_ts_ms <= cursor_ms:
                cursor_ms = batch_end_ms
            else:
                cursor_ms = max_ts_ms + step_minutes * 60_000

            if sleep_between_batches > 0:
                time.sleep(sleep_between_batches)

        if stats.status == "API_ERROR":
            break

    stats.duration_seconds = time.monotonic() - started
    return stats


def run_backfill(
    *,
    symbols: list[str],
    timeframes: list[str],
    days: int,
    dry_run: bool = False,
    logger: logging.Logger | None = None,
) -> BackfillReport:
    log = logger or setup_logger()
    cfg = load_config()
    db = Database(cfg, log)
    db.initialize()
    client = BitgetClient(cfg, log)
    report = BackfillReport(started_at=datetime.now(timezone.utc).isoformat(), dry_run=dry_run, days=days)
    started = time.monotonic()

    for symbol in symbols:
        for timeframe in timeframes:
            log.info("Backfill start: %s %s days=%d dry_run=%s", symbol, timeframe, days, dry_run)
            stats = backfill_pair(
                client=client,
                db=db,
                symbol=symbol,
                timeframe=timeframe,
                days=days,
                dry_run=dry_run,
                logger=log,
            )
            report.per_pair.append(stats)
            log.info(
                "Backfill done: %s %s inserted=%d skipped=%d rejected=%d batches=%d duration=%.2fs status=%s",
                symbol,
                timeframe,
                stats.inserted,
                stats.skipped,
                stats.rejected,
                stats.batches,
                stats.duration_seconds,
                stats.status,
            )

    report.duration_seconds = time.monotonic() - started
    report.ended_at = datetime.now(timezone.utc).isoformat()
    return report


def render_report_text(report: BackfillReport) -> str:
    data = report.as_dict()
    lines = ["OHLCV BACKFILL START"]
    lines.append(f"started_at: {data['started_at']}")
    lines.append(f"ended_at: {data['ended_at']}")
    lines.append(f"duration_seconds: {data['duration_seconds']}")
    lines.append(f"dry_run: {str(data['dry_run']).lower()}")
    lines.append(f"days: {data['days']}")
    totals = data["totals"]
    lines.append(
        "totals: "
        + ", ".join(f"{key}={value}" for key, value in totals.items())
    )
    for entry in data["per_pair"]:
        lines.append(
            f"- {entry['symbol']} {entry['timeframe']}: "
            f"inserted={entry['inserted']} skipped={entry['skipped']} rejected={entry['rejected']} "
            f"batches={entry['batches']} status={entry['status']} "
            f"first={entry['first_timestamp'] or 'none'} last={entry['last_timestamp'] or 'none'} "
            f"duration={entry['duration_seconds']}s"
            + (f" error={entry['error']}" if entry["error"] else "")
        )
    lines.append("websocket_active: false")
    lines.append("private_endpoints_touched: false")
    lines.append(f"final_recommendation: {data['final_recommendation']}")
    lines.append("OHLCV BACKFILL END")
    return "\n".join(lines)


def _parse_csv(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill historical OHLCV from Bitget into ohlcv_candles.")
    parser.add_argument(
        "--symbols",
        type=str,
        default="BTCUSDT,ETHUSDT,SOLUSDT",
        help="Comma-separated symbol list (e.g. BTCUSDT,ETHUSDT).",
    )
    parser.add_argument(
        "--timeframes",
        type=str,
        default="5m,15m,1h",
        help="Comma-separated timeframes (5m, 15m, 1h).",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--days", type=int, default=365, help="Backfill window in days (default 365).")
    group.add_argument("--hours", type=int, help="Backfill window in hours (overrides --days).")
    parser.add_argument("--dry-run", action="store_true", help="Fetch from API and validate but do not write to DB.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    days = max(1, int(args.hours) // 24 + (1 if args.hours and args.hours % 24 else 0)) if args.hours else args.days
    symbols = _parse_csv(args.symbols)
    timeframes = _parse_csv(args.timeframes)
    if not symbols or not timeframes:
        print("error: --symbols and --timeframes are required", file=sys.stderr)
        return 2
    report = run_backfill(symbols=symbols, timeframes=timeframes, days=days, dry_run=args.dry_run)
    print(render_report_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
