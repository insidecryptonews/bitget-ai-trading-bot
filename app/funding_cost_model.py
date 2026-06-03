"""ResearchOps V7.5 — Modelo de coste de funding research-only.

Aplica el coste/ingreso de funding por trade solo cuando el trade cruza un
timestamp de funding (~cada 8h). El modelo es estricto:

  - LONG paga si funding rate > 0; recibe si funding rate < 0.
  - SHORT paga si funding rate < 0; recibe si funding rate > 0.
  - Si no hay tabla `funding_rates` o no hay datos, se devuelve
    `funding_data_status=NEED_DATA` y `net_adjustment_pct=0.0`. Nunca se
    inventan magnitudes.

Hard rules:
  - nunca llama endpoints privados Bitget.
  - nunca llama `place_order` / `set_leverage` / `set_margin_mode`.
  - nunca modifica DB.
  - todos los outputs `research_only=true`, `final_recommendation=NO LIVE`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


FINAL_RECOMMENDATION = "NO LIVE"

# Funding window de Bitget USDT-M (UTC, cada 8 horas).
FUNDING_HOURS_UTC: tuple[int, ...] = (0, 8, 16)


@dataclass
class FundingApplication:
    """Resultado de aplicar funding a un trade individual."""
    side: str
    entry_time: str
    exit_time: str
    crossings: int
    average_funding_rate: float
    net_adjustment_pct: float
    funding_data_status: str  # OK / NEED_DATA / FALLBACK_ZERO
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FundingCostSummary:
    symbols: list[str]
    hours: int
    trades_evaluated: int
    trades_with_funding_crossing: int
    average_net_adjustment_pct: float
    funding_data_status: str
    table_present: bool
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    no_private_endpoints_used: bool = True
    final_recommendation: str = FINAL_RECOMMENDATION
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_dt(value: Any) -> datetime | None:
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


def _funding_crossings(entry: datetime, exit_: datetime) -> list[datetime]:
    if entry is None or exit_ is None or exit_ <= entry:
        return []
    crossings: list[datetime] = []
    cursor = entry.replace(minute=0, second=0, microsecond=0)
    # Avanzamos hora a hora hasta el exit. Eficiente porque trades duran < días.
    while cursor <= exit_:
        if cursor.hour in FUNDING_HOURS_UTC and cursor > entry:
            crossings.append(cursor)
        cursor = cursor + timedelta(hours=1)
        if cursor - entry > timedelta(days=7):
            # Safety cap: trades research nunca duran más de 7 días.
            break
    return crossings


def _funding_table_exists(db: Any) -> bool:
    if not db:
        return False
    try:
        return bool(db.table_exists("funding_rates"))
    except Exception:
        return False


def _fetch_funding_rates(
    db: Any,
    symbol: str,
    crossings: list[datetime],
) -> list[float]:
    """Best-effort lookup. Devuelve [] si la tabla no existe o no hay datos."""
    if not crossings or not _funding_table_exists(db):
        return []
    placeholders = ",".join("?" for _ in crossings)
    sql = (
        f"SELECT funding_rate FROM funding_rates "
        f"WHERE UPPER(symbol) = ? AND timestamp IN ({placeholders}) "
        f"ORDER BY timestamp ASC"
    )
    params: list[Any] = [str(symbol).upper()] + [t.isoformat() for t in crossings]
    if bool(getattr(db, "_use_postgres", False)):
        sql = sql.replace("?", "%s")
    try:
        with db._connect() as conn:
            rows = list(conn.execute(sql, tuple(params)).fetchall())
    except Exception:
        return []
    rates: list[float] = []
    for row in rows:
        try:
            value = db._row_value(row, "funding_rate", 0, 0.0)
        except Exception:
            value = None
        try:
            rates.append(float(value or 0.0))
        except Exception:
            continue
    return rates


def apply_funding_to_trade(
    db: Any,
    *,
    symbol: str,
    side: str,
    entry_time: Any,
    exit_time: Any,
) -> FundingApplication:
    """Aplica funding a un trade. Devuelve `net_adjustment_pct` en porcentaje
    de notional (signo positivo = beneficio para el trade)."""
    side_upper = str(side or "").upper()
    entry = _parse_dt(entry_time)
    exit_ = _parse_dt(exit_time)
    crossings = _funding_crossings(entry, exit_) if entry and exit_ else []
    if not _funding_table_exists(db):
        return FundingApplication(
            side=side_upper,
            entry_time=entry.isoformat() if entry else "",
            exit_time=exit_.isoformat() if exit_ else "",
            crossings=len(crossings),
            average_funding_rate=0.0,
            net_adjustment_pct=0.0,
            funding_data_status="NEED_DATA",
            reasons=["funding_rates_table_missing"],
        )
    if not crossings:
        return FundingApplication(
            side=side_upper,
            entry_time=entry.isoformat() if entry else "",
            exit_time=exit_.isoformat() if exit_ else "",
            crossings=0,
            average_funding_rate=0.0,
            net_adjustment_pct=0.0,
            funding_data_status="OK",
            reasons=["trade_did_not_cross_funding_timestamp"],
        )
    rates = _fetch_funding_rates(db, symbol, crossings)
    if not rates:
        return FundingApplication(
            side=side_upper,
            entry_time=entry.isoformat() if entry else "",
            exit_time=exit_.isoformat() if exit_ else "",
            crossings=len(crossings),
            average_funding_rate=0.0,
            net_adjustment_pct=0.0,
            funding_data_status="NEED_DATA",
            reasons=["funding_rates_table_present_but_rows_missing_for_crossings"],
        )
    average_rate = sum(rates) / len(rates)
    # LONG paga si rate > 0; SHORT paga si rate > 0 invierte signo.
    direction = 1.0 if side_upper == "LONG" else -1.0
    # Cada cruce factura `rate × notional`. Como reportamos % del notional,
    # acumulamos rate × n_cruces y multiplicamos por 100.
    net_adjustment_pct = -1.0 * sum(rates) * direction * 100.0
    return FundingApplication(
        side=side_upper,
        entry_time=entry.isoformat() if entry else "",
        exit_time=exit_.isoformat() if exit_ else "",
        crossings=len(crossings),
        average_funding_rate=average_rate,
        net_adjustment_pct=net_adjustment_pct,
        funding_data_status="OK",
        reasons=["funding_rates_applied_per_crossing"],
    )


def summarise_funding(
    db: Any,
    *,
    trades: Iterable[dict[str, Any]],
    symbols: list[str] | None = None,
    hours: int = 720,
) -> FundingCostSummary:
    """Recibe una iterable de dicts con keys symbol, side, entry_time, exit_time."""
    applied: list[FundingApplication] = []
    with_crossing = 0
    table_present = _funding_table_exists(db)
    for trade in trades:
        verdict = apply_funding_to_trade(
            db,
            symbol=str(trade.get("symbol") or ""),
            side=str(trade.get("side") or ""),
            entry_time=trade.get("entry_time"),
            exit_time=trade.get("exit_time"),
        )
        applied.append(verdict)
        if verdict.crossings > 0:
            with_crossing += 1
    n = len(applied)
    if n == 0:
        avg = 0.0
        status = "OK" if table_present else "NEED_DATA"
    else:
        avg = sum(v.net_adjustment_pct for v in applied) / n
        statuses = {v.funding_data_status for v in applied}
        if "NEED_DATA" in statuses and not table_present:
            status = "NEED_DATA"
        elif "NEED_DATA" in statuses:
            status = "PARTIAL"
        else:
            status = "OK"
    notes: list[str] = []
    if not table_present:
        notes.append("funding_rates_table_not_present_in_db_no_adjustment_applied")
    if with_crossing == 0:
        notes.append("no_trade_crossed_funding_timestamp")
    return FundingCostSummary(
        symbols=symbols or [],
        hours=int(hours),
        trades_evaluated=n,
        trades_with_funding_crossing=with_crossing,
        average_net_adjustment_pct=avg,
        funding_data_status=status,
        table_present=table_present,
        notes=notes,
    )


def render_funding_summary_text(summary: FundingCostSummary) -> str:
    lines = [
        "FUNDING COST MODEL START",
        f"symbols: {','.join(summary.symbols) if summary.symbols else 'ALL'}",
        f"hours: {summary.hours}",
        f"trades_evaluated: {summary.trades_evaluated}",
        f"trades_with_funding_crossing: {summary.trades_with_funding_crossing}",
        f"average_net_adjustment_pct: {summary.average_net_adjustment_pct:.6f}",
        f"funding_data_status: {summary.funding_data_status}",
        f"table_present: {str(summary.table_present).lower()}",
    ]
    if summary.notes:
        lines.append("notes:")
        for note in summary.notes:
            lines.append(f"- {note}")
    lines.extend([
        "no_private_endpoints_used: true",
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "final_recommendation: NO LIVE",
        "FUNDING COST MODEL END",
    ])
    return "\n".join(lines)
