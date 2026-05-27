"""ResearchOps V5 — OHLCV Freshness Manager (multi-symbol, multi-timeframe).

Pure research/management module. Two surfaces:

  - `freshness_status(...)` : compute STALE/OK/NEED_DATA/GAP per (symbol, timeframe)
  - `refresh(..., dry_run=...)` : delegate to the existing `app.ohlcv_backfill`
    (public history-candles endpoint only) to fetch missing data

Hard rules:

  - never call private endpoints
  - never place orders, never set leverage / margin mode
  - dry-run does not write to the database
  - the auto-refresh runtime path is disabled by default
    (`config.enable_ohlcv_auto_refresh`). Even when True, only OHLCV rows are
    written by the existing public-only backfill helper.

The dashboard surfaces this module via /api/research/ohlcv-freshness-status and
/api/research/ohlcv-freshness-refresh-dry. The dashboard NEVER calls the live
refresh path; that lives behind the CLI only.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .data_freshness_gate import (
    DEFAULT_STALENESS_MINUTES,
    NON_ACTIONABLE_STATUSES,
    FreshnessVerdict,
    evaluate_freshness,
)


FINAL_RECOMMENDATION = "NO LIVE"

DEFAULT_V5_SYMBOLS: tuple[str, ...] = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "BNBUSDT", "LINKUSDT", "AVAXUSDT", "ADAUSDT", "DOTUSDT",
)
DEFAULT_V5_TIMEFRAMES: tuple[str, ...] = ("5m", "15m", "1h")

# Gap detection — if newer-than-oldest span is much shorter than the expected
# span for the timeframe, we flag it as GAP. Conservative thresholds.
GAP_TOLERANCE = 0.85  # We expect at least 85% of the theoretical row count.


@dataclass
class SymbolTimeframeStatus:
    symbol: str
    timeframe: str
    status: str  # OK / STALE / NEED_DATA / GAP / LOADER_ERROR / HISTORICAL_RESEARCH_ONLY
    oldest_timestamp: str
    newest_timestamp: str
    age_minutes: float
    staleness_budget_minutes: int
    row_count: int
    expected_rows_window: int
    gap_ratio: float
    actionable: bool
    suggested_command: str
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FreshnessMatrixReport:
    symbols: list[str]
    timeframes: list[str]
    rows: list[SymbolTimeframeStatus] = field(default_factory=list)
    overall_actionable: bool = False
    stale_count: int = 0
    need_data_count: int = 0
    gap_count: int = 0
    ok_count: int = 0
    auto_refresh_enabled: bool = False
    activation_disabled_until_manual_vps_validation: bool = True
    research_only: bool = True
    final_recommendation: str = FINAL_RECOMMENDATION

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbols": list(self.symbols),
            "timeframes": list(self.timeframes),
            "rows": [row.as_dict() for row in self.rows],
            "overall_actionable": self.overall_actionable,
            "stale_count": self.stale_count,
            "need_data_count": self.need_data_count,
            "gap_count": self.gap_count,
            "ok_count": self.ok_count,
            "auto_refresh_enabled": self.auto_refresh_enabled,
            "activation_disabled_until_manual_vps_validation": self.activation_disabled_until_manual_vps_validation,
            "research_only": self.research_only,
            "final_recommendation": self.final_recommendation,
        }


@dataclass
class RefreshSymbolResult:
    symbol: str
    timeframe: str
    status: str  # OK / DRY_RUN / API_ERROR / SKIPPED_AUTO_DISABLED / INVALID_TIMEFRAME
    rows_inserted: int
    rows_skipped: int
    rows_rejected: int
    duration_seconds: float
    dry_run: bool
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RefreshReport:
    symbols: list[str]
    timeframes: list[str]
    hours: int
    dry_run: bool
    auto_refresh_enabled: bool
    activation_disabled_until_manual_vps_validation: bool
    results: list[RefreshSymbolResult] = field(default_factory=list)
    total_rows_inserted: int = 0
    total_rows_skipped: int = 0
    total_rows_rejected: int = 0
    research_only: bool = True
    final_recommendation: str = FINAL_RECOMMENDATION

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbols": list(self.symbols),
            "timeframes": list(self.timeframes),
            "hours": self.hours,
            "dry_run": self.dry_run,
            "auto_refresh_enabled": self.auto_refresh_enabled,
            "activation_disabled_until_manual_vps_validation": self.activation_disabled_until_manual_vps_validation,
            "results": [result.as_dict() for result in self.results],
            "total_rows_inserted": self.total_rows_inserted,
            "total_rows_skipped": self.total_rows_skipped,
            "total_rows_rejected": self.total_rows_rejected,
            "research_only": self.research_only,
            "final_recommendation": self.final_recommendation,
        }


def _expected_rows_for_window(timeframe: str, age_minutes_window: int) -> int:
    minutes_per_bar = DEFAULT_STALENESS_MINUTES.get(str(timeframe or "5m").lower(), 60) // 4
    minutes_per_bar = max(1, minutes_per_bar) if timeframe != "5m" else 5
    mapping = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
    minutes_per_bar = mapping.get(str(timeframe or "5m").lower(), 60)
    return max(1, int(age_minutes_window / minutes_per_bar))


def _coerce_symbols(symbols: Iterable[str] | str | None) -> list[str]:
    if symbols is None:
        return list(DEFAULT_V5_SYMBOLS)
    if isinstance(symbols, str):
        values = [part.strip().upper() for part in symbols.split(",") if part.strip()]
    else:
        values = [str(part).strip().upper() for part in symbols if str(part).strip()]
    return values or list(DEFAULT_V5_SYMBOLS)


def _coerce_timeframes(timeframes: Iterable[str] | str | None) -> list[str]:
    if timeframes is None:
        return list(DEFAULT_V5_TIMEFRAMES)
    if isinstance(timeframes, str):
        values = [part.strip().lower() for part in timeframes.split(",") if part.strip()]
    else:
        values = [str(part).strip().lower() for part in timeframes if str(part).strip()]
    return values or list(DEFAULT_V5_TIMEFRAMES)


def _ohlcv_count_and_oldest(db: Any, symbol: str, timeframe: str) -> tuple[int, str]:
    """Return (row_count, oldest_timestamp) for (symbol, timeframe). Best-effort."""
    if not db:
        return 0, ""
    try:
        if hasattr(db, "table_exists") and not db.table_exists("ohlcv_candles"):
            return 0, ""
    except Exception:
        return 0, ""
    sql = (
        "SELECT COUNT(*) AS cnt, MIN(timestamp) AS oldest FROM ohlcv_candles "
        "WHERE symbol = ? AND timeframe = ?"
    )
    if bool(getattr(db, "_use_postgres", False)):
        sql = sql.replace("?", "%s")
    try:
        with db._connect() as conn:
            row = conn.execute(sql, (str(symbol).upper(), str(timeframe).lower())).fetchone()
    except Exception:
        return 0, ""
    if not row:
        return 0, ""
    try:
        cnt = int(db._row_value(row, "cnt", 0, 0) or 0)
        oldest = db._row_value(row, "oldest", 1, "")
    except Exception:
        return 0, ""
    return cnt, str(oldest or "")


def _row_status_from_verdict(
    db: Any,
    verdict: FreshnessVerdict,
    *,
    window_minutes: int = 7200,  # 5 days default
) -> SymbolTimeframeStatus:
    row_count, oldest = _ohlcv_count_and_oldest(db, verdict.symbol, verdict.timeframe)
    expected_rows = _expected_rows_for_window(verdict.timeframe, window_minutes)
    gap_ratio = (row_count / expected_rows) if expected_rows > 0 else 0.0
    status = verdict.status
    reasons = list(verdict.reasons)
    if status == "OK" and row_count > 0 and gap_ratio < GAP_TOLERANCE:
        status = "GAP"
        reasons.append(
            f"gap_ratio={gap_ratio:.3f}_below_tolerance_{GAP_TOLERANCE:.2f}"
        )
    return SymbolTimeframeStatus(
        symbol=verdict.symbol,
        timeframe=verdict.timeframe,
        status=status,
        oldest_timestamp=oldest,
        newest_timestamp=verdict.newest_timestamp,
        age_minutes=verdict.age_minutes,
        staleness_budget_minutes=verdict.staleness_budget_minutes,
        row_count=row_count,
        expected_rows_window=expected_rows,
        gap_ratio=gap_ratio,
        actionable=(status == "OK"),
        suggested_command=verdict.suggested_command,
        reasons=reasons,
    )


def freshness_status(
    db: Any,
    *,
    symbols: Iterable[str] | str | None = None,
    timeframes: Iterable[str] | str | None = None,
    now: datetime | None = None,
    historical: bool = False,
    config: Any | None = None,
) -> FreshnessMatrixReport:
    """Build the symbol x timeframe freshness matrix."""
    symbol_list = _coerce_symbols(symbols)
    timeframe_list = _coerce_timeframes(timeframes)
    rows: list[SymbolTimeframeStatus] = []
    for symbol in symbol_list:
        for timeframe in timeframe_list:
            verdict = evaluate_freshness(
                db, symbol=symbol, timeframe=timeframe, historical=historical, now=now,
            )
            rows.append(_row_status_from_verdict(db, verdict))
    stale = sum(1 for row in rows if row.status == "STALE")
    need_data = sum(1 for row in rows if row.status == "NEED_DATA")
    gap = sum(1 for row in rows if row.status == "GAP")
    ok = sum(1 for row in rows if row.status == "OK")
    overall = ok > 0 and (stale + need_data + gap) == 0
    auto = bool(getattr(config, "enable_ohlcv_auto_refresh", False)) if config is not None else False
    return FreshnessMatrixReport(
        symbols=symbol_list,
        timeframes=timeframe_list,
        rows=rows,
        overall_actionable=overall,
        stale_count=stale,
        need_data_count=need_data,
        gap_count=gap,
        ok_count=ok,
        auto_refresh_enabled=auto,
        activation_disabled_until_manual_vps_validation=True,
    )


def refresh(
    db: Any,
    *,
    config: Any | None = None,
    symbols: Iterable[str] | str | None = None,
    timeframes: Iterable[str] | str | None = None,
    hours: int = 120,
    dry_run: bool = True,
    allow_real_writes: bool = False,
    logger: logging.Logger | None = None,
) -> RefreshReport:
    """Run the public-only OHLCV backfill for the given symbols/timeframes.

    `dry_run=True` (default) simulates the write — no insert hits the DB.

    `allow_real_writes` is the explicit gate the CLI sets when the operator
    asked for a real refresh. Even with `dry_run=False`, if both
    `config.enable_ohlcv_auto_refresh` is False AND `allow_real_writes` is
    False, the manager refuses to write and reports SKIPPED_AUTO_DISABLED.

    No private endpoints. No order placement. No leverage / margin changes.
    """
    log = logger or logging.getLogger("ohlcv_freshness_manager")
    symbol_list = _coerce_symbols(symbols)
    timeframe_list = _coerce_timeframes(timeframes)
    auto_enabled = bool(getattr(config, "enable_ohlcv_auto_refresh", False)) if config is not None else False
    days = max(1, int(round(int(hours or 1) / 24.0))) if hours else 1
    will_write_real = (not dry_run) and (auto_enabled or allow_real_writes)
    results: list[RefreshSymbolResult] = []
    total_inserted = 0
    total_skipped = 0
    total_rejected = 0
    if not dry_run and not will_write_real:
        # Auto-refresh is disabled and the caller did not explicitly opt-in via
        # `allow_real_writes`. Refuse to do real writes; downgrade to dry-run.
        for symbol in symbol_list:
            for timeframe in timeframe_list:
                results.append(RefreshSymbolResult(
                    symbol=symbol,
                    timeframe=timeframe,
                    status="SKIPPED_AUTO_DISABLED",
                    rows_inserted=0,
                    rows_skipped=0,
                    rows_rejected=0,
                    duration_seconds=0.0,
                    dry_run=True,
                    error="enable_ohlcv_auto_refresh_false_and_allow_real_writes_false",
                ))
        return RefreshReport(
            symbols=symbol_list,
            timeframes=timeframe_list,
            hours=int(hours),
            dry_run=True,
            auto_refresh_enabled=auto_enabled,
            activation_disabled_until_manual_vps_validation=True,
            results=results,
        )
    # Build a lightweight client only if we are going to write. dry-run only
    # needs DB reads, so we still call backfill_pair but with dry_run=True.
    try:
        from .ohlcv_backfill import backfill_pair  # local import keeps optional
    except Exception as exc:  # pragma: no cover - defensive guard
        err_results: list[RefreshSymbolResult] = []
        for symbol in symbol_list:
            for timeframe in timeframe_list:
                err_results.append(RefreshSymbolResult(
                    symbol=symbol,
                    timeframe=timeframe,
                    status="API_ERROR",
                    rows_inserted=0, rows_skipped=0, rows_rejected=0,
                    duration_seconds=0.0,
                    dry_run=dry_run,
                    error=f"import_error:{type(exc).__name__}",
                ))
        return RefreshReport(
            symbols=symbol_list,
            timeframes=timeframe_list,
            hours=int(hours),
            dry_run=dry_run,
            auto_refresh_enabled=auto_enabled,
            activation_disabled_until_manual_vps_validation=True,
            results=err_results,
        )
    client = None
    if will_write_real:
        # Public endpoint only — `get_history_candles` is unauthenticated.
        # BitgetClient.__init__ requires (config, logger). We never invoke
        # private methods from this module; if config is missing, load it from
        # disk the same way `app.ohlcv_backfill.run_backfill` does so the call
        # has a valid config object even when this helper is called without
        # one.
        try:
            from .bitget_client import BitgetClient

            if config is None:
                from .config import load_config as _load_config
                cfg_for_client = _load_config()
            else:
                cfg_for_client = config
            client = BitgetClient(cfg_for_client, log)
        except Exception as exc:
            # One row per (symbol, timeframe) so the dashboard renders the
            # grid correctly even when the client cannot be constructed.
            err_results: list[RefreshSymbolResult] = []
            for symbol in symbol_list:
                for timeframe in timeframe_list:
                    err_results.append(RefreshSymbolResult(
                        symbol=symbol,
                        timeframe=timeframe,
                        status="API_ERROR",
                        rows_inserted=0, rows_skipped=0, rows_rejected=0,
                        duration_seconds=0.0,
                        dry_run=dry_run,
                        error=f"client_init_error:{type(exc).__name__}",
                    ))
            return RefreshReport(
                symbols=symbol_list,
                timeframes=timeframe_list,
                hours=int(hours),
                dry_run=dry_run,
                auto_refresh_enabled=auto_enabled,
                activation_disabled_until_manual_vps_validation=True,
                results=err_results,
            )
    for symbol in symbol_list:
        for timeframe in timeframe_list:
            if will_write_real and client is not None:
                try:
                    stats = backfill_pair(
                        client=client,
                        db=db,
                        symbol=symbol,
                        timeframe=timeframe,
                        days=days,
                        dry_run=False,
                        logger=log,
                    )
                    status = stats.status if stats.status in {"OK", "API_ERROR", "INVALID_TIMEFRAME"} else "OK"
                    results.append(RefreshSymbolResult(
                        symbol=symbol,
                        timeframe=timeframe,
                        status=status,
                        rows_inserted=stats.inserted,
                        rows_skipped=stats.skipped,
                        rows_rejected=stats.rejected,
                        duration_seconds=stats.duration_seconds,
                        dry_run=False,
                        error=stats.error,
                    ))
                    total_inserted += stats.inserted
                    total_skipped += stats.skipped
                    total_rejected += stats.rejected
                except Exception as exc:  # pragma: no cover - defensive guard
                    results.append(RefreshSymbolResult(
                        symbol=symbol,
                        timeframe=timeframe,
                        status="API_ERROR",
                        rows_inserted=0, rows_skipped=0, rows_rejected=0,
                        duration_seconds=0.0,
                        dry_run=False,
                        error=f"run_error:{type(exc).__name__}",
                    ))
            else:
                # Dry-run: do NOT contact the exchange and do NOT write. Just
                # describe what the plan would be.
                results.append(RefreshSymbolResult(
                    symbol=symbol,
                    timeframe=timeframe,
                    status="DRY_RUN",
                    rows_inserted=0, rows_skipped=0, rows_rejected=0,
                    duration_seconds=0.0,
                    dry_run=True,
                    error="dry_run_no_api_call",
                ))
    return RefreshReport(
        symbols=symbol_list,
        timeframes=timeframe_list,
        hours=int(hours),
        dry_run=not will_write_real,
        auto_refresh_enabled=auto_enabled,
        activation_disabled_until_manual_vps_validation=True,
        results=results,
        total_rows_inserted=total_inserted,
        total_rows_skipped=total_skipped,
        total_rows_rejected=total_rejected,
    )


def render_freshness_matrix_text(report: FreshnessMatrixReport) -> str:
    lines = [
        "OHLCV FRESHNESS MATRIX START",
        f"symbols: {','.join(report.symbols)}",
        f"timeframes: {','.join(report.timeframes)}",
        f"ok: {report.ok_count}",
        f"stale: {report.stale_count}",
        f"need_data: {report.need_data_count}",
        f"gap: {report.gap_count}",
        f"overall_actionable: {str(report.overall_actionable).lower()}",
        f"auto_refresh_enabled: {str(report.auto_refresh_enabled).lower()}",
        f"activation_disabled_until_manual_vps_validation: {str(report.activation_disabled_until_manual_vps_validation).lower()}",
        "symbol | timeframe | status | age_min | budget | rows | newest | suggested_command",
    ]
    for row in report.rows:
        lines.append(
            f"{row.symbol} | {row.timeframe} | {row.status} | "
            f"{row.age_minutes:.1f} | {row.staleness_budget_minutes} | "
            f"{row.row_count} | {row.newest_timestamp or '-'} | "
            f"{row.suggested_command or '-'}"
        )
    lines.extend([
        "non_actionable_statuses: " + ",".join(NON_ACTIONABLE_STATUSES),
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "final_recommendation: NO LIVE",
        "OHLCV FRESHNESS MATRIX END",
    ])
    return "\n".join(lines)


def render_refresh_report_text(report: RefreshReport) -> str:
    lines = [
        "OHLCV FRESHNESS REFRESH START",
        f"hours: {report.hours}",
        f"dry_run: {str(report.dry_run).lower()}",
        f"auto_refresh_enabled: {str(report.auto_refresh_enabled).lower()}",
        f"activation_disabled_until_manual_vps_validation: {str(report.activation_disabled_until_manual_vps_validation).lower()}",
        f"total_rows_inserted: {report.total_rows_inserted}",
        f"total_rows_skipped: {report.total_rows_skipped}",
        f"total_rows_rejected: {report.total_rows_rejected}",
        "symbol | timeframe | status | inserted | skipped | rejected | duration_s | dry_run | error",
    ]
    for result in report.results:
        lines.append(
            f"{result.symbol} | {result.timeframe} | {result.status} | "
            f"{result.rows_inserted} | {result.rows_skipped} | {result.rows_rejected} | "
            f"{result.duration_seconds:.2f} | {str(result.dry_run).lower()} | "
            f"{result.error or '-'}"
        )
    lines.extend([
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "no_private_endpoints_used: true",
        "no_order_placement: true",
        "final_recommendation: NO LIVE",
        "OHLCV FRESHNESS REFRESH END",
    ])
    return "\n".join(lines)
