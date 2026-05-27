"""Phase 9 — Global data freshness gate.

Read-only helper that any research/dashboard surface can call to determine
whether OHLCV data for a symbol+timeframe is recent enough to support actionable
recommendations. Returns a structured verdict; callers decide how to surface it
(typically by masking actionable badges).

Contract:
- never modifies DB
- never calls the exchange
- never returns "actionable" when data is stale, missing, or loader failed
- always returns the same dataclass shape so callers can serialise it

Verdict.status values:
  OK                         -> data freshness within window for the timeframe
  STALE                      -> data older than the per-timeframe staleness budget
  NEED_DATA                  -> no OHLCV row available for the requested symbol
  LOADER_ERROR               -> loader raised; treat as not actionable
  HISTORICAL_RESEARCH_ONLY   -> caller explicitly asked for a historical window

The numeric thresholds are intentionally conservative. They are tuned for
research/paper readiness, not for any live signalling.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .utils import safe_float


FINAL_RECOMMENDATION = "NO LIVE"

# Per-timeframe staleness budgets in minutes. A datapoint older than the budget
# is considered STALE and must not generate ENTER_NOW signals.
DEFAULT_STALENESS_MINUTES: dict[str, int] = {
    "1m": 5,
    "3m": 10,
    "5m": 20,
    "15m": 60,
    "30m": 120,
    "1h": 240,
    "4h": 720,
    "1d": 2880,
}

# Statuses that the gate considers "non-actionable" — used by callers to mask
# any ENTER_NOW / PAPER_DEMO_READY badges. Documented here so test code and the
# dashboard agree on the set.
NON_ACTIONABLE_STATUSES: tuple[str, ...] = (
    "STALE",
    "NEED_DATA",
    "LOADER_ERROR",
    "HISTORICAL_RESEARCH_ONLY",
)


@dataclass
class FreshnessVerdict:
    symbol: str
    timeframe: str
    status: str
    newest_timestamp: str
    age_minutes: float
    staleness_budget_minutes: int
    actionable: bool
    research_only: bool = True
    reasons: list[str] = field(default_factory=list)
    suggested_command: str = ""
    final_recommendation: str = FINAL_RECOMMENDATION

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _staleness_budget_minutes(timeframe: str) -> int:
    return int(DEFAULT_STALENESS_MINUTES.get(str(timeframe or "5m").lower(), 60))


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _newest_ohlcv_timestamp(db: Any, symbol: str, timeframe: str) -> datetime | None:
    """Best-effort lookup of the newest OHLCV row for (symbol, timeframe)."""
    if not db:
        return None
    try:
        if hasattr(db, "table_exists") and not db.table_exists("ohlcv_candles"):
            return None
    except Exception:
        return None
    sql = (
        "SELECT MAX(timestamp) AS newest FROM ohlcv_candles "
        "WHERE symbol = ? AND timeframe = ?"
    )
    if bool(getattr(db, "_use_postgres", False)):
        sql = sql.replace("?", "%s")
    try:
        with db._connect() as conn:
            row = conn.execute(sql, (str(symbol).upper(), str(timeframe).lower())).fetchone()
    except Exception:
        return None
    if not row:
        return None
    raw = None
    try:
        raw = db._row_value(row, "newest", 0, None)
    except Exception:
        raw = None
    return _parse_timestamp(raw)


def evaluate_freshness(
    db: Any,
    *,
    symbol: str,
    timeframe: str = "5m",
    historical: bool = False,
    now: datetime | None = None,
) -> FreshnessVerdict:
    """Compute the freshness verdict for a single symbol/timeframe.

    If `historical=True` the caller is explicitly asking for a historical
    research window — the verdict still reports the age but flags the run as
    `HISTORICAL_RESEARCH_ONLY` so the dashboard cannot surface ENTER_NOW.
    """
    symbol = str(symbol or "").upper()
    timeframe = str(timeframe or "5m").lower()
    budget = _staleness_budget_minutes(timeframe)
    reference = now or datetime.now(timezone.utc)
    newest = _newest_ohlcv_timestamp(db, symbol, timeframe)
    suggested_command = (
        f"python -m app.research_lab ohlcv-replay-loader-audit "
        f"--symbols {symbol} --timeframe {timeframe} --hours 720"
    )
    if newest is None:
        return FreshnessVerdict(
            symbol=symbol,
            timeframe=timeframe,
            status="NEED_DATA",
            newest_timestamp="",
            age_minutes=0.0,
            staleness_budget_minutes=budget,
            actionable=False,
            reasons=["no_ohlcv_row_for_symbol_timeframe"],
            suggested_command=suggested_command,
        )
    age_seconds = (reference - newest).total_seconds()
    age_minutes = max(0.0, age_seconds / 60.0)
    if historical:
        return FreshnessVerdict(
            symbol=symbol,
            timeframe=timeframe,
            status="HISTORICAL_RESEARCH_ONLY",
            newest_timestamp=newest.isoformat(),
            age_minutes=age_minutes,
            staleness_budget_minutes=budget,
            actionable=False,
            reasons=["historical_research_window_requested"],
            suggested_command=suggested_command,
        )
    if age_minutes > budget:
        return FreshnessVerdict(
            symbol=symbol,
            timeframe=timeframe,
            status="STALE",
            newest_timestamp=newest.isoformat(),
            age_minutes=age_minutes,
            staleness_budget_minutes=budget,
            actionable=False,
            reasons=[
                f"newest_ohlcv_age_minutes={age_minutes:.1f}_budget={budget}",
                "stale_data_cannot_be_actionable",
            ],
            suggested_command=suggested_command,
        )
    return FreshnessVerdict(
        symbol=symbol,
        timeframe=timeframe,
        status="OK",
        newest_timestamp=newest.isoformat(),
        age_minutes=age_minutes,
        staleness_budget_minutes=budget,
        actionable=True,
        reasons=["ohlcv_within_freshness_window"],
        suggested_command="",
    )


def evaluate_freshness_many(
    db: Any,
    *,
    symbols: list[str],
    timeframe: str = "5m",
    historical: bool = False,
    now: datetime | None = None,
) -> dict[str, FreshnessVerdict]:
    """Evaluate multiple symbols. Useful for multi-symbol panels."""
    return {
        symbol: evaluate_freshness(db, symbol=symbol, timeframe=timeframe, historical=historical, now=now)
        for symbol in [str(s).upper() for s in symbols if str(s).strip()]
    }


def aggregate_actionable(verdicts: dict[str, FreshnessVerdict]) -> bool:
    """Aggregate verdict: any STALE/NEED_DATA/etc → not actionable globally."""
    if not verdicts:
        return False
    return all(verdict.actionable for verdict in verdicts.values())


def render_freshness_text(verdicts: dict[str, FreshnessVerdict]) -> str:
    lines = [
        "DATA FRESHNESS GATE START",
        f"actionable_overall: {str(aggregate_actionable(verdicts)).lower()}",
        "symbol | timeframe | status | age_min | budget_min | actionable | newest_timestamp",
    ]
    for symbol in sorted(verdicts.keys()):
        verdict = verdicts[symbol]
        lines.append(
            f"{verdict.symbol} | {verdict.timeframe} | {verdict.status} | "
            f"{verdict.age_minutes:.1f} | {verdict.staleness_budget_minutes} | "
            f"{str(verdict.actionable).lower()} | {verdict.newest_timestamp or '-'}"
        )
        for reason in verdict.reasons[:3]:
            lines.append(f"  reason: {reason}")
    lines.extend([
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "final_recommendation: NO LIVE",
        "DATA FRESHNESS GATE END",
    ])
    return "\n".join(lines)
