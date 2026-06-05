"""V8.2.4 — Future Returns Bridge (research-only).

For each signal observation, computes side-oriented future returns, MFE/MAE
and first-barrier-hit using OHLCV bars after the observation timestamp.

Rules:

- LONG favorable = price moves up; SHORT favorable = price moves down.
- Same-bar TP and SL → STOP_BEFORE_TP (stop wins; conservative).
- Bars strictly AFTER the observation timestamp are used. No lookahead into
  the bar containing the observation.
- Missing OHLCV → ``NEED_DATA``. Never invents values.

No order placement, no DB writes, no private endpoints.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import (
    FINAL_RECOMMENDATION_NO_LIVE,
    SIDE_LONG,
    SIDE_SHORT,
    STATUS_NEED_DATA,
    STATUS_OK,
    STATUS_PARTIAL,
)


DEFAULT_HORIZONS_MINUTES = (15, 30, 60, 240, 1440)
DEFAULT_TP_PCT = 0.96
DEFAULT_SL_PCT = 0.60
DEFAULT_TIMEFRAME = "5m"
DEFAULT_MAX_BARS = 288


@dataclass
class FutureReturnsResult:
    """Side-oriented future return / barrier outcome for one observation."""

    signal_id: int | None
    timestamp: str
    symbol: str
    side: str
    entry_price: float
    horizons_minutes: list[int] = field(default_factory=list)
    returns_by_horizon_pct: dict[str, float | None] = field(default_factory=dict)
    mfe_pct: float | None = None
    mae_pct: float | None = None
    first_barrier_hit: str | None = None  # "TP" / "SL" / "TIME" / None
    tp_before_sl: bool | None = None
    sl_before_tp: bool | None = None
    time_no_hit: bool | None = None
    bars_to_tp: int | None = None
    bars_to_sl: int | None = None
    bars_to_mfe: int | None = None
    bars_to_mae: int | None = None
    bars_evaluated: int = 0
    status: str = STATUS_NEED_DATA
    need_data_reason: str = ""
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _safe_call(db: Any, method: str, *args, **kwargs) -> tuple[bool, Any]:
    if db is None:
        return False, None
    fn = getattr(db, method, None)
    if fn is None or not callable(fn):
        return False, None
    try:
        return True, fn(*args, **kwargs)
    except Exception:
        return False, None


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _direction(side: str) -> int:
    if side.upper() == SIDE_LONG:
        return 1
    if side.upper() == SIDE_SHORT:
        return -1
    return 0


def _timeframe_minutes(tf: str) -> int:
    text = (tf or DEFAULT_TIMEFRAME).strip().lower()
    try:
        if text.endswith("m"):
            return max(1, int(text[:-1]))
        if text.endswith("h"):
            return max(1, int(text[:-1])) * 60
        if text.endswith("d"):
            return max(1, int(text[:-1])) * 60 * 24
    except Exception:
        return 5
    return 5


def _fetch_future_bars(
    db: Any,
    *,
    symbol: str,
    timeframe: str,
    start_dt: datetime,
    max_bars: int,
    bars_override: Iterable[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Fetch bars strictly after ``start_dt``. ``bars_override`` is used by
    tests to inject a synthetic bar path.
    """
    if bars_override is not None:
        return list(bars_override)
    tf_min = _timeframe_minutes(timeframe)
    end_dt = start_dt + timedelta(minutes=tf_min * (max_bars + 1))
    ok, value = _safe_call(
        db, "fetch_ohlcv_range",
        symbol=symbol,
        timeframe=timeframe,
        since_iso=start_dt.isoformat(),
        until_iso=end_dt.isoformat(),
        limit=max_bars + 1,
    )
    if not ok or not value:
        return []
    bars: list[dict[str, Any]] = []
    for row in value:
        ts = _parse_dt(row.get("timestamp"))
        if ts is None or ts <= start_dt:
            # No-lookahead: skip bars at or before the signal timestamp.
            continue
        try:
            bars.append({
                "timestamp": ts.isoformat(),
                "open": float(row.get("open", 0) or 0),
                "high": float(row.get("high", 0) or 0),
                "low": float(row.get("low", 0) or 0),
                "close": float(row.get("close", 0) or 0),
            })
        except Exception:
            continue
    return bars[:max_bars]


def _entry_price_from_observation(obs: dict[str, Any], first_bar: dict[str, Any]) -> float:
    """Resolve entry price honestly: prefer observation.entry_price; fall
    back to the first OHLCV close after the signal (open of the first
    post-signal bar)."""
    raw = obs.get("entry_price") or obs.get("entry")
    try:
        candidate = float(raw) if raw is not None else 0.0
    except Exception:
        candidate = 0.0
    if candidate > 0:
        return candidate
    try:
        return float(first_bar.get("open", 0)) or float(first_bar.get("close", 0))
    except Exception:
        return 0.0


def compute_future_returns(
    db: Any,
    *,
    observation: dict[str, Any],
    horizons_minutes: Iterable[int] = DEFAULT_HORIZONS_MINUTES,
    tp_pct: float = DEFAULT_TP_PCT,
    sl_pct: float = DEFAULT_SL_PCT,
    timeframe: str = DEFAULT_TIMEFRAME,
    max_bars: int = DEFAULT_MAX_BARS,
    bars_override: Iterable[dict[str, Any]] | None = None,
) -> FutureReturnsResult:
    """Side-oriented future returns + first-barrier-hit for a single
    observation.

    LONG: favorable = price up. SHORT: favorable = price down.
    Same-bar TP and SL → STOP_BEFORE_TP.
    """
    symbol = str(observation.get("symbol") or "").upper()
    side = str(observation.get("side") or observation.get("proposed_side") or "").upper()
    timestamp = str(observation.get("timestamp") or "")
    signal_id_raw = observation.get("id") or observation.get("signal_id")
    try:
        signal_id = int(signal_id_raw) if signal_id_raw is not None else None
    except Exception:
        signal_id = None

    result = FutureReturnsResult(
        signal_id=signal_id,
        timestamp=timestamp,
        symbol=symbol,
        side=side,
        entry_price=0.0,
        horizons_minutes=list(horizons_minutes),
        returns_by_horizon_pct={f"{m}m": None for m in horizons_minutes},
    )

    if not symbol or side not in {SIDE_LONG, SIDE_SHORT}:
        result.need_data_reason = "no_side_or_symbol"
        return result

    start_dt = _parse_dt(timestamp)
    if start_dt is None:
        result.need_data_reason = "invalid_timestamp"
        return result

    bars = _fetch_future_bars(
        db, symbol=symbol, timeframe=timeframe, start_dt=start_dt,
        max_bars=max_bars, bars_override=bars_override,
    )
    if not bars:
        result.need_data_reason = "ohlcv_missing"
        return result

    entry = _entry_price_from_observation(observation, bars[0])
    if entry <= 0:
        result.need_data_reason = "invalid_entry_price"
        return result
    result.entry_price = entry

    direction = _direction(side)
    tp_price = (
        entry * (1 + tp_pct / 100.0)
        if direction == 1
        else entry * (1 - tp_pct / 100.0)
    )
    sl_price = (
        entry * (1 - sl_pct / 100.0)
        if direction == 1
        else entry * (1 + sl_pct / 100.0)
    )

    mfe = 0.0
    mae = 0.0
    bars_to_mfe: int | None = None
    bars_to_mae: int | None = None
    first_barrier_hit: str | None = None
    tp_before_sl: bool | None = None
    sl_before_tp: bool | None = None
    bars_to_tp: int | None = None
    bars_to_sl: int | None = None
    bars_evaluated = 0
    tf_min = _timeframe_minutes(timeframe)
    horizon_set = sorted(set(int(m) for m in horizons_minutes))
    horizon_bar_index = {m: max(1, m // max(tf_min, 1)) for m in horizon_set}
    # Pre-fill horizon returns with None; will fill if enough bars.
    returns: dict[str, float | None] = {f"{m}m": None for m in horizon_set}

    for idx, bar in enumerate(bars):
        bar_index = idx + 1
        bars_evaluated = bar_index
        high = float(bar.get("high", 0) or 0)
        low = float(bar.get("low", 0) or 0)
        close = float(bar.get("close", 0) or 0)
        if high <= 0 or low <= 0 or close <= 0:
            continue
        # Side-oriented favourable / adverse extremes.
        fav_price = high if direction == 1 else low
        adv_price = low if direction == 1 else high
        fav_pct = ((fav_price - entry) / entry) * 100.0 * direction
        adv_pct = ((adv_price - entry) / entry) * 100.0 * direction
        if fav_pct > mfe:
            mfe = fav_pct
            bars_to_mfe = bar_index
        if adv_pct < mae:
            mae = adv_pct
            bars_to_mae = bar_index
        # Horizon returns (close-based).
        close_pct = ((close - entry) / entry) * 100.0 * direction
        for m, idx_target in horizon_bar_index.items():
            if bar_index == idx_target:
                returns[f"{m}m"] = close_pct
        # Same-bar STOP_BEFORE_TP.
        if direction == 1:
            stop_hit = low <= sl_price
            tp_hit = high >= tp_price
        else:
            stop_hit = high >= sl_price
            tp_hit = low <= tp_price
        if first_barrier_hit is None:
            if stop_hit:
                first_barrier_hit = "SL"
                sl_before_tp = True
                tp_before_sl = False
                bars_to_sl = bar_index
                if tp_hit:
                    # Both touched same-bar; STOP_BEFORE_TP still: stop wins.
                    bars_to_tp = bar_index  # informational
            elif tp_hit:
                first_barrier_hit = "TP"
                tp_before_sl = True
                sl_before_tp = False
                bars_to_tp = bar_index
        # Stop the loop early if we already have first barrier AND we've
        # passed every horizon target so we don't waste cycles.
        if first_barrier_hit is not None and bar_index >= max(horizon_bar_index.values()):
            break

    if first_barrier_hit is None:
        first_barrier_hit = "TIME"
        tp_before_sl = False
        sl_before_tp = False
        time_no_hit = True
    else:
        time_no_hit = False

    result.returns_by_horizon_pct = returns
    result.mfe_pct = mfe
    result.mae_pct = mae
    result.first_barrier_hit = first_barrier_hit
    result.tp_before_sl = tp_before_sl
    result.sl_before_tp = sl_before_tp
    result.time_no_hit = time_no_hit
    result.bars_to_tp = bars_to_tp
    result.bars_to_sl = bars_to_sl
    result.bars_to_mfe = bars_to_mfe
    result.bars_to_mae = bars_to_mae
    result.bars_evaluated = bars_evaluated
    # Status: OK if at least one horizon return and a barrier hit were filled.
    any_horizon = any(v is not None for v in returns.values())
    if any_horizon and first_barrier_hit:
        result.status = STATUS_OK
    elif any_horizon or first_barrier_hit:
        result.status = STATUS_PARTIAL
    else:
        result.status = STATUS_NEED_DATA
    return result


def batch_compute_future_returns(
    db: Any,
    *,
    observations: Iterable[dict[str, Any]],
    horizons_minutes: Iterable[int] = DEFAULT_HORIZONS_MINUTES,
    tp_pct: float = DEFAULT_TP_PCT,
    sl_pct: float = DEFAULT_SL_PCT,
    timeframe: str = DEFAULT_TIMEFRAME,
    max_bars: int = DEFAULT_MAX_BARS,
) -> list[FutureReturnsResult]:
    return [
        compute_future_returns(
            db, observation=obs,
            horizons_minutes=horizons_minutes,
            tp_pct=tp_pct, sl_pct=sl_pct,
            timeframe=timeframe, max_bars=max_bars,
        )
        for obs in observations
    ]


def summarise_future_returns(
    results: list[FutureReturnsResult],
) -> dict[str, Any]:
    if not results:
        return {
            "total": 0,
            "ok": 0, "partial": 0, "need_data": 0,
            "tp_first_count": 0, "sl_first_count": 0, "time_count": 0,
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
        }
    ok = sum(1 for r in results if r.status == STATUS_OK)
    partial = sum(1 for r in results if r.status == STATUS_PARTIAL)
    need = sum(1 for r in results if r.status == STATUS_NEED_DATA)
    tp_count = sum(1 for r in results if r.first_barrier_hit == "TP")
    sl_count = sum(1 for r in results if r.first_barrier_hit == "SL")
    time_count = sum(1 for r in results if r.first_barrier_hit == "TIME")
    return {
        "total": len(results),
        "ok": ok, "partial": partial, "need_data": need,
        "tp_first_count": tp_count,
        "sl_first_count": sl_count,
        "time_count": time_count,
        "research_only": True,
        "paper_filter_enabled": False,
        "can_send_real_orders": False,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }
