"""V8.2.4 — Pseudo-trades bridge (research-only).

When the ``trades`` table is empty (no executed paper trades), the V8.2
campaign and profit-lock simulators would receive ``[]`` and report NEED_DATA
forever. This bridge reconstructs **pseudo-trades** from
``signal_observations`` enriched with OHLCV bar paths so the simulators can
still produce useful research outputs.

Hard contract:
- Never touches the PaperTrader.
- Never opens orders.
- Every pseudo-trade is tagged ``source="pseudo_trade_from_signal_observation"``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import SIDE_LONG, SIDE_SHORT
from .future_returns_bridge import (
    DEFAULT_MAX_BARS,
    DEFAULT_TIMEFRAME,
    _fetch_future_bars,
    _parse_dt,
    _timeframe_minutes,
)


PSEUDO_TRADE_SOURCE = "pseudo_trade_from_signal_observation"


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


def _observation_to_pseudo_trade(
    obs: dict[str, Any],
    *,
    db: Any,
    timeframe: str,
    max_bars: int,
) -> dict[str, Any] | None:
    side = str(obs.get("side") or obs.get("proposed_side") or "").upper()
    if side not in {SIDE_LONG, SIDE_SHORT}:
        return None
    symbol = str(obs.get("symbol") or "").upper()
    if not symbol:
        return None
    start = _parse_dt(obs.get("timestamp"))
    if start is None:
        return None
    bars = _fetch_future_bars(
        db, symbol=symbol, timeframe=timeframe,
        start_dt=start, max_bars=max_bars, bars_override=None,
    )
    if not bars:
        return None
    entry = float(obs.get("entry_price") or obs.get("entry") or 0)
    if entry <= 0:
        try:
            entry = float(bars[0]["open"])
        except Exception:
            return None
    stop = float(obs.get("stop_loss") or 0)
    tp1 = float(obs.get("take_profit_1") or 0)
    tp2 = float(obs.get("take_profit_2") or 0)
    # Heuristic defaults when the observation lacks TP/SL.
    if stop <= 0:
        stop = entry * (1 - 0.006) if side == SIDE_LONG else entry * (1 + 0.006)
    if tp1 <= 0:
        tp1 = entry * (1 + 0.0096) if side == SIDE_LONG else entry * (1 - 0.0096)
    if tp2 <= 0:
        tp2 = entry * (1 + 0.0144) if side == SIDE_LONG else entry * (1 - 0.0144)
    bar_path = [
        {
            "open": float(b.get("open", 0)),
            "high": float(b.get("high", 0)),
            "low": float(b.get("low", 0)),
            "close": float(b.get("close", 0)),
        }
        for b in bars
    ]
    atr_norm = obs.get("normalized_atr")
    atr_pct = 0.5
    if isinstance(atr_norm, (int, float)) and float(atr_norm) > 0:
        atr_pct = float(atr_norm) * 100.0
    return {
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "bar_path": bar_path,
        "fees_pct": 0.18,
        "atr_pct": atr_pct,
        "atr_pct_at_entry": atr_pct,
        "regime": str(obs.get("market_regime") or "UNKNOWN").upper(),
        "source": PSEUDO_TRADE_SOURCE,
    }


def build_pseudo_trades_from_observations(
    db: Any,
    *,
    hours: int = 168,
    side: str | None = None,
    timeframe: str = DEFAULT_TIMEFRAME,
    max_bars: int = DEFAULT_MAX_BARS,
    limit: int = 5000,
    rows: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Reconstruct pseudo-trades from signal observations + OHLCV.

    Returns an empty list when no usable rows are present.
    """
    if rows is None:
        ok, value = _safe_call(
            db, "fetch_signal_observations",
            hours=int(hours), side=side, limit=int(limit),
        )
        if not ok or value is None:
            return []
        obs_list = list(value)
    else:
        obs_list = list(rows)
    out: list[dict[str, Any]] = []
    for obs in obs_list:
        pt = _observation_to_pseudo_trade(
            obs, db=db, timeframe=timeframe, max_bars=max_bars,
        )
        if pt is not None:
            out.append(pt)
    return out
