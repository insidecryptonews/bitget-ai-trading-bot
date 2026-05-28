"""ResearchOps V6 — Clean research metrics enforcement layer.

Central helper that every research decision must consume instead of raw counts:

  - Strategy Research Enhancer rankings
  - Phase 9 paper readiness validator
  - Research Pack V5/V6 export
  - Dashboard cockpit cards

The helper computes BOTH `raw_*` and `clean_*` metrics from the existing tables
(no DB writes, no destructive ops) and labels the result with a
`data_quality_status` (OK / WARNING / BAD / UNKNOWN). When the status is BAD or
clean sample count is too low, callers must refuse to promote anything to
paper/demo and must surface the gap to the operator.

Hard rules:
  - never opens orders
  - never modifies any table
  - never calls private endpoints
  - never converts shadow to paper
  - leverage / margin / sizing / slots untouched
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .training_data_clean_view import (
    BAD_DUPLICATE_RATE,
    STATUS_BAD,
    STATUS_OK,
    STATUS_UNKNOWN,
    STATUS_WARNING,
    WARN_DUPLICATE_RATE,
    run_training_data_clean_view,
)


FINAL_RECOMMENDATION = "NO LIVE"

# Confidence thresholds — clean samples needed before we trust a ranking.
MIN_CLEAN_SAMPLES_HIGH_CONFIDENCE = 200
MIN_CLEAN_SAMPLES_MEDIUM_CONFIDENCE = 60
MIN_CLEAN_SAMPLES_LOW_CONFIDENCE = 15


@dataclass
class CleanResearchMetrics:
    hours: int
    symbols: list[str]
    timeframes: list[str]
    # Sample counts
    raw_sample_count: int
    clean_sample_count: int
    duplicate_count: int
    duplicate_rate: float
    dedupe_ratio: float
    # EV / PF / win rate
    raw_ev_pct: float
    clean_ev_pct: float
    raw_pf: float
    clean_pf: float
    raw_win_rate: float
    clean_win_rate: float
    # Exit reasons
    raw_tp_rate: float
    clean_tp_rate: float
    raw_sl_rate: float
    clean_sl_rate: float
    raw_time_rate: float
    clean_time_rate: float
    # Diagnostics
    duplicate_impact_pct: float  # |raw_ev - clean_ev|
    confidence: str               # HIGH / MEDIUM / LOW
    data_quality_status: str      # OK / WARNING / BAD / UNKNOWN
    blocked_gate: str = ""        # populated when promotion must be blocked
    reasons: list[str] = field(default_factory=list)
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _since_iso(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours or 1)))).isoformat()


def _table_exists(db: Any, name: str) -> bool:
    if not db:
        return False
    try:
        return bool(db.table_exists(name))
    except Exception:
        return False


def _row_value(db: Any, row: Any, key: str, idx: int, default: Any) -> Any:
    try:
        return db._row_value(row, key, idx, default)
    except Exception:
        return default


def _safe_query(db: Any, sql: str, params: tuple) -> list[Any]:
    """Execute a query and return a list of rows. Best-effort."""
    if not db:
        return []
    try:
        local_sql = sql
        if bool(getattr(db, "_use_postgres", False)):
            local_sql = local_sql.replace("?", "%s")
        with db._connect() as conn:
            return list(conn.execute(local_sql, params).fetchall())
    except Exception:
        return []


def _raw_trade_metrics(db: Any, since_iso: str, symbols: list[str] | None) -> dict[str, float]:
    """RAW metrics for paper trades (no dedup)."""
    if not _table_exists(db, "trades"):
        return _empty_trade_metrics()
    symbol_filter = ""
    params: list[Any] = [since_iso]
    if symbols:
        placeholders = ",".join("?" for _ in symbols)
        symbol_filter = f" AND UPPER(symbol) IN ({placeholders})"
        params.extend(str(s).upper() for s in symbols)
    sql = (
        "SELECT symbol, side, timestamp, outcome, gross_pnl_pct, net_pnl_pct, exit_reason "
        "FROM trades WHERE timestamp >= ? AND LOWER(mode) = 'paper'" + symbol_filter
    )
    rows = _safe_query(db, sql, tuple(params))
    return _metrics_from_rows(db, rows, exit_key="exit_reason")


def _clean_trade_metrics(db: Any, since_iso: str, symbols: list[str] | None) -> dict[str, float]:
    """CLEAN metrics: keep the first row per (symbol, side, timestamp_minute, setup_key?)."""
    if not _table_exists(db, "trades"):
        return _empty_trade_metrics()
    symbol_filter = ""
    params: list[Any] = [since_iso]
    if symbols:
        placeholders = ",".join("?" for _ in symbols)
        symbol_filter = f" AND UPPER(symbol) IN ({placeholders})"
        params.extend(str(s).upper() for s in symbols)
    sql = (
        "SELECT symbol, side, timestamp, outcome, gross_pnl_pct, net_pnl_pct, exit_reason "
        "FROM trades AS t "
        "WHERE timestamp >= ? AND LOWER(mode) = 'paper'" + symbol_filter +
        " AND rowid = (SELECT MIN(rowid) FROM trades AS u "
        "  WHERE u.symbol = t.symbol AND u.side = t.side "
        "  AND substr(u.timestamp, 1, 16) = substr(t.timestamp, 1, 16) "
        "  AND LOWER(u.mode) = 'paper')"
    )
    rows = _safe_query(db, sql, tuple(params))
    return _metrics_from_rows(db, rows, exit_key="exit_reason")


def _empty_trade_metrics() -> dict[str, float]:
    return {
        "count": 0.0,
        "ev_pct": 0.0,
        "pf": 0.0,
        "win_rate": 0.0,
        "tp_rate": 0.0,
        "sl_rate": 0.0,
        "time_rate": 0.0,
    }


def _metrics_from_rows(db: Any, rows: list[Any], *, exit_key: str = "exit_reason") -> dict[str, float]:
    if not rows:
        return _empty_trade_metrics()
    net_returns: list[float] = []
    tp = sl = tm = 0
    for row in rows:
        try:
            net = float(_row_value(db, row, "net_pnl_pct", 5, 0.0) or 0.0)
        except Exception:
            net = 0.0
        net_returns.append(net)
        exit_reason = str(_row_value(db, row, exit_key, 6, "") or "").upper()
        if exit_reason == "TAKE_PROFIT" or exit_reason.startswith("TP"):
            tp += 1
        elif exit_reason == "STOP_LOSS" or exit_reason == "STOP":
            sl += 1
        elif exit_reason in {"TIME", "HORIZON_CLOSE", "TIME_REDUCED"}:
            tm += 1
    n = len(net_returns)
    wins = [v for v in net_returns if v > 0]
    losses = [v for v in net_returns if v < 0]
    loss_sum = abs(sum(losses))
    gain_sum = sum(wins)
    pf = (gain_sum / loss_sum) if loss_sum > 0 else (999.0 if gain_sum > 0 else 0.0)
    return {
        "count": float(n),
        "ev_pct": sum(net_returns) / n,
        "pf": pf,
        "win_rate": len(wins) / n,
        "tp_rate": tp / n,
        "sl_rate": sl / n,
        "time_rate": tm / n,
    }


def _confidence(clean_count: int, status: str) -> str:
    if status == STATUS_BAD:
        return "LOW"
    if clean_count >= MIN_CLEAN_SAMPLES_HIGH_CONFIDENCE:
        return "HIGH"
    if clean_count >= MIN_CLEAN_SAMPLES_MEDIUM_CONFIDENCE:
        return "MEDIUM"
    if clean_count >= MIN_CLEAN_SAMPLES_LOW_CONFIDENCE:
        return "LOW"
    return "LOW"


def _blocked_gate(status: str, clean_count: int) -> str:
    if status == STATUS_BAD:
        return "data_quality_bad_duplicate_rate"
    if clean_count < MIN_CLEAN_SAMPLES_LOW_CONFIDENCE:
        return "clean_sample_count_too_low"
    return ""


def _coerce_symbols(symbols: Iterable[str] | str | None) -> list[str] | None:
    if symbols is None:
        return None
    if isinstance(symbols, str):
        values = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    else:
        values = [str(s).strip().upper() for s in symbols if str(s).strip()]
    return values or None


def _coerce_timeframes(timeframes: Iterable[str] | str | None) -> list[str]:
    if timeframes is None:
        return ["5m"]
    if isinstance(timeframes, str):
        values = [t.strip().lower() for t in timeframes.split(",") if t.strip()]
    else:
        values = [str(t).strip().lower() for t in timeframes if str(t).strip()]
    return values or ["5m"]


def get_clean_research_metrics(
    db: Any,
    *,
    hours: int = 24,
    symbols: Iterable[str] | str | None = None,
    timeframes: Iterable[str] | str | None = None,
) -> CleanResearchMetrics:
    """Single entry point — every research decision should call this.

    Steps:
      1. Run the V5 training-data clean view to obtain duplicate_rate +
         data_quality_status.
      2. Compute RAW vs CLEAN net EV / PF / win / TP / SL / TIME from the
         paper trades table (research surface only, no exchange calls).
      3. Wrap everything in a `CleanResearchMetrics` payload with a
         `blocked_gate` field populated when promotion must be refused.
    """
    symbol_list = _coerce_symbols(symbols)
    timeframe_list = _coerce_timeframes(timeframes)
    clean_view = run_training_data_clean_view(db, hours=max(int(hours), 24))
    since_iso = _since_iso(int(hours))
    raw = _raw_trade_metrics(db, since_iso, symbol_list)
    clean = _clean_trade_metrics(db, since_iso, symbol_list)
    raw_count = int(raw["count"])
    clean_count = int(clean["count"])
    duplicate_count = max(0, raw_count - clean_count)
    duplicate_rate = (duplicate_count / raw_count) if raw_count > 0 else 0.0
    dedupe_ratio = (clean_count / raw_count) if raw_count > 0 else 0.0
    duplicate_impact_pct = abs(raw["ev_pct"] - clean["ev_pct"])
    # Use the view's status if present; otherwise classify locally.
    data_quality_status = clean_view.overall_status if clean_view else STATUS_UNKNOWN
    if data_quality_status == STATUS_UNKNOWN and raw_count > 0:
        if duplicate_rate >= BAD_DUPLICATE_RATE:
            data_quality_status = STATUS_BAD
        elif duplicate_rate >= WARN_DUPLICATE_RATE:
            data_quality_status = STATUS_WARNING
        else:
            data_quality_status = STATUS_OK
    confidence = _confidence(clean_count, data_quality_status)
    blocked_gate = _blocked_gate(data_quality_status, clean_count)
    reasons: list[str] = []
    if data_quality_status == STATUS_BAD:
        reasons.append(f"duplicate_rate={duplicate_rate:.4f}_above_bad_threshold")
    elif data_quality_status == STATUS_WARNING:
        reasons.append(f"duplicate_rate={duplicate_rate:.4f}_above_warning_threshold")
    if clean_count < MIN_CLEAN_SAMPLES_LOW_CONFIDENCE:
        reasons.append(f"clean_sample_count={clean_count}_below_min_{MIN_CLEAN_SAMPLES_LOW_CONFIDENCE}")
    if duplicate_impact_pct >= 0.05:
        reasons.append(f"duplicate_impact_pct={duplicate_impact_pct:.4f}_high_diff_raw_vs_clean")
    return CleanResearchMetrics(
        hours=int(hours),
        symbols=symbol_list or [],
        timeframes=timeframe_list,
        raw_sample_count=raw_count,
        clean_sample_count=clean_count,
        duplicate_count=duplicate_count,
        duplicate_rate=duplicate_rate,
        dedupe_ratio=dedupe_ratio,
        raw_ev_pct=raw["ev_pct"],
        clean_ev_pct=clean["ev_pct"],
        raw_pf=raw["pf"],
        clean_pf=clean["pf"],
        raw_win_rate=raw["win_rate"],
        clean_win_rate=clean["win_rate"],
        raw_tp_rate=raw["tp_rate"],
        clean_tp_rate=clean["tp_rate"],
        raw_sl_rate=raw["sl_rate"],
        clean_sl_rate=clean["sl_rate"],
        raw_time_rate=raw["time_rate"],
        clean_time_rate=clean["time_rate"],
        duplicate_impact_pct=duplicate_impact_pct,
        confidence=confidence,
        data_quality_status=data_quality_status,
        blocked_gate=blocked_gate,
        reasons=reasons,
    )


def render_clean_metrics_text(report: CleanResearchMetrics) -> str:
    lines = [
        "CLEAN RESEARCH METRICS START",
        f"hours: {report.hours}",
        f"symbols: {','.join(report.symbols) if report.symbols else 'ALL'}",
        f"data_quality_status: {report.data_quality_status}",
        f"confidence: {report.confidence}",
        f"blocked_gate: {report.blocked_gate or 'none'}",
        f"raw_sample_count: {report.raw_sample_count}",
        f"clean_sample_count: {report.clean_sample_count}",
        f"duplicate_count: {report.duplicate_count}",
        f"duplicate_rate: {report.duplicate_rate:.4f}",
        f"dedupe_ratio: {report.dedupe_ratio:.4f}",
        f"duplicate_impact_pct: {report.duplicate_impact_pct:.4f}",
        "metric | raw | clean",
        f"net_ev_pct | {report.raw_ev_pct:.6f} | {report.clean_ev_pct:.6f}",
        f"net_pf     | {report.raw_pf:.4f} | {report.clean_pf:.4f}",
        f"win_rate   | {report.raw_win_rate:.4f} | {report.clean_win_rate:.4f}",
        f"tp_rate    | {report.raw_tp_rate:.4f} | {report.clean_tp_rate:.4f}",
        f"sl_rate    | {report.raw_sl_rate:.4f} | {report.clean_sl_rate:.4f}",
        f"time_rate  | {report.raw_time_rate:.4f} | {report.clean_time_rate:.4f}",
        "reasons:",
    ]
    for reason in report.reasons:
        lines.append(f"- {reason}")
    lines.extend([
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "do_not_promote_raw: true",
        "final_recommendation: NO LIVE",
        "CLEAN RESEARCH METRICS END",
    ])
    return "\n".join(lines)
