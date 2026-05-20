from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

FINAL_RECOMMENDATION = "NO LIVE"

OHLCV_TABLE_CANDIDATES = (
    "ohlcv_candles",
    "candles",
    "market_candles",
    "market_data_candles",
    "historical_candles",
    "ohlcv",
)

CANONICAL_ALIASES: dict[str, tuple[str, ...]] = {
    "timestamp": ("timestamp", "ts", "time", "open_time", "candle_time", "created_at"),
    "symbol": ("symbol", "instrument", "pair"),
    "timeframe": ("timeframe", "interval", "tf"),
    "open": ("open", "open_price", "o"),
    "high": ("high", "high_price", "h"),
    "low": ("low", "low_price", "l"),
    "close": ("close", "close_price", "c"),
    "volume": ("volume", "base_volume", "vol", "v"),
}

REQUIRED_COLUMNS = tuple(CANONICAL_ALIASES.keys())


@dataclass
class OhlcvReplayLoadResult:
    status: str
    table: str = ""
    timeframe: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)
    frames_by_symbol: dict[str, pd.DataFrame] = field(default_factory=dict)
    missing_columns: list[str] = field(default_factory=list)
    duplicate_candles: int = 0
    gap_count: int = 0
    warnings: list[str] = field(default_factory=list)
    final_recommendation: str = FINAL_RECOMMENDATION

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "table": self.table,
            "timeframe": self.timeframe,
            "rows": len(self.rows),
            "symbols": sorted(self.frames_by_symbol),
            "missing_columns": self.missing_columns,
            "duplicate_candles": self.duplicate_candles,
            "gap_count": self.gap_count,
            "warnings": self.warnings,
            "final_recommendation": self.final_recommendation,
        }


class OhlcvReplayLoader:
    """Read-only OHLCV loader for local candle replay backtests."""

    def __init__(self, db: Any, *, table_candidates: tuple[str, ...] = OHLCV_TABLE_CANDIDATES) -> None:
        self.db = db
        self.table_candidates = table_candidates

    def load_ohlcv(
        self,
        *,
        symbols: list[str],
        timeframe: str,
        since: datetime,
        until: datetime | None = None,
        limit: int = 200000,
    ) -> OhlcvReplayLoadResult:
        if self.db is None:
            return OhlcvReplayLoadResult(status="NEED_DATA", timeframe=timeframe, warnings=["database_unavailable"])
        table = self._find_table()
        if not table:
            return OhlcvReplayLoadResult(
                status="NEED_DATA",
                timeframe=timeframe,
                warnings=[f"missing_ohlcv_table; checked={','.join(self.table_candidates)}"],
            )
        columns = self._columns(table)
        mapping = _column_mapping(columns)
        missing = [column for column in REQUIRED_COLUMNS if column not in mapping]
        if missing:
            return OhlcvReplayLoadResult(status="MISSING_COLUMNS", table=table, timeframe=timeframe, missing_columns=missing)

        rows = self._fetch_rows(table, mapping["timestamp"], since, limit=limit)
        canonical = [_canonical_row(row, mapping) for row in rows]
        symbol_set = {symbol.upper() for symbol in symbols if symbol}
        target_timeframe = timeframe.lower()
        filtered = [
            row
            for row in canonical
            if str(row.get("symbol") or "").upper() in symbol_set
            and str(row.get("timeframe") or "").lower() == target_timeframe
            and (until is None or _parse_ts(row.get("timestamp")) <= until)
        ]
        if not filtered:
            return OhlcvReplayLoadResult(status="NEED_DATA", table=table, timeframe=timeframe, warnings=["no_matching_ohlcv_rows"])

        grouped = group_by_symbol(filtered)
        duplicate_count = 0
        gap_count = 0
        warnings: list[str] = []
        for symbol, frame in grouped.items():
            duplicates = frame.duplicated(subset=["timestamp", "symbol", "timeframe"], keep=False)
            duplicate_count += int(duplicates.sum())
            gaps = _count_time_gaps(frame, timeframe)
            gap_count += gaps
            if gaps:
                warnings.append(f"gaps_detected:{symbol}:{gaps}")

        status = "OK"
        if duplicate_count:
            status = "DUPLICATE_CANDLES"
        elif gap_count:
            status = "TOO_MANY_GAPS"
        return OhlcvReplayLoadResult(
            status=status,
            table=table,
            timeframe=timeframe,
            rows=filtered,
            frames_by_symbol=grouped,
            duplicate_candles=duplicate_count,
            gap_count=gap_count,
            warnings=warnings,
        )

    def audit(self, *, config: Any, hours: int = 72) -> OhlcvReplayLoadResult:
        symbols = list(getattr(config, "symbols", []) or [])
        timeframe = str(getattr(config, "main_timeframe", "5m") or "5m").lower()
        since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours or 72)))
        return self.load_ohlcv(symbols=symbols, timeframe=timeframe, since=since)

    def _find_table(self) -> str:
        for table in self.table_candidates:
            try:
                if self.db.table_exists(table):
                    return table
            except Exception:
                continue
        return ""

    def _columns(self, table: str) -> list[str]:
        try:
            return list(self.db.get_table_columns(table))
        except Exception:
            return []

    def _fetch_rows(self, table: str, timestamp_column: str, since: datetime, *, limit: int) -> list[dict[str, Any]]:
        since_iso = since.astimezone(timezone.utc).isoformat()
        try:
            return list(self.db.fetch_table_rows(table, since_iso=since_iso, timestamp_column=timestamp_column, limit=limit))
        except Exception:
            return []


def group_by_symbol(rows: list[dict[str, Any]]) -> dict[str, pd.DataFrame]:
    grouped: dict[str, pd.DataFrame] = {}
    if not rows:
        return grouped
    frame = pd.DataFrame(rows)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    frame = frame.dropna(subset=["timestamp"]).sort_values(["symbol", "timestamp"]).reset_index(drop=True)
    for symbol, symbol_frame in frame.groupby("symbol"):
        grouped[str(symbol)] = symbol_frame.reset_index(drop=True)
    return grouped


def ohlcv_replay_loader_audit_text(config: Any, db: Any, *, hours: int = 72) -> str:
    result = OhlcvReplayLoader(db).audit(config=config, hours=hours)
    data = result.to_dict()
    lines = [
        "OHLCV REPLAY LOADER AUDIT START",
        f"hours: {hours}",
        f"status: {data['status']}",
        f"table: {data['table'] or 'none'}",
        f"timeframe: {data['timeframe']}",
        f"rows: {data['rows']}",
        f"symbols: {', '.join(data['symbols']) if data['symbols'] else 'none'}",
        f"missing_columns: {', '.join(data['missing_columns']) if data['missing_columns'] else 'none'}",
        f"duplicate_candles: {data['duplicate_candles']}",
        f"gap_count: {data['gap_count']}",
        f"warnings: {', '.join(data['warnings']) if data['warnings'] else 'none'}",
        "no_mfe_mae_as_ohlcv: true",
        "websocket_active: false",
        "final_recommendation: NO LIVE",
        "OHLCV REPLAY LOADER AUDIT END",
    ]
    return "\n".join(lines)


def ohlcv_replay_loader_smoke_text() -> str:
    class MissingDb:
        def table_exists(self, table: str) -> bool:
            return False

    result = OhlcvReplayLoader(MissingDb()).load_ohlcv(
        symbols=["BTCUSDT"],
        timeframe="5m",
        since=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    checks = {
        "missing_table_returns_need_data": result.status == "NEED_DATA",
        "does_not_invent_ohlcv": not result.rows,
        "final_recommendation_no_live": result.final_recommendation == FINAL_RECOMMENDATION,
    }
    lines = ["OHLCV REPLAY LOADER SMOKE TEST START"]
    lines.extend(f"{key}: {str(value).lower()}" for key, value in checks.items())
    lines.extend(
        [
            "LIVE_TRADING=false",
            "DRY_RUN=true",
            "PAPER_TRADING=true",
            "ENABLE_PAPER_POLICY_FILTER=false",
            "final_recommendation: NO LIVE",
            f"result: {'PASS' if all(checks.values()) else 'FAIL'}",
            "OHLCV REPLAY LOADER SMOKE TEST END",
        ]
    )
    return "\n".join(lines)


def _column_mapping(columns: list[str]) -> dict[str, str]:
    lowered = {column.lower(): column for column in columns}
    mapping: dict[str, str] = {}
    for canonical, aliases in CANONICAL_ALIASES.items():
        for alias in aliases:
            if alias.lower() in lowered:
                mapping[canonical] = lowered[alias.lower()]
                break
    return mapping


def _canonical_row(row: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    return {canonical: row.get(source) for canonical, source in mapping.items()}


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _count_time_gaps(frame: pd.DataFrame, timeframe: str) -> int:
    if len(frame) < 2:
        return 0
    expected = _timeframe_delta(timeframe)
    if expected <= pd.Timedelta(0):
        return 0
    deltas = frame["timestamp"].diff().dropna()
    return int((deltas > expected * 1.5).sum())


def _timeframe_delta(timeframe: str) -> pd.Timedelta:
    value = str(timeframe or "").lower().strip()
    try:
        if value.endswith("m"):
            return pd.Timedelta(minutes=int(value[:-1]))
        if value.endswith("h"):
            return pd.Timedelta(hours=int(value[:-1]))
        if value.endswith("d"):
            return pd.Timedelta(days=int(value[:-1]))
    except ValueError:
        return pd.Timedelta(0)
    return pd.Timedelta(0)
