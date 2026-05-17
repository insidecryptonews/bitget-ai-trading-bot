from __future__ import annotations

from collections import Counter
from typing import Any

from .edge_hardening_utils import FINAL_NO_LIVE, format_num, format_pct, since_iso
from .utils import safe_float, safe_int


START = "PRE MOVE EVENT LABELER START"
END = "PRE MOVE EVENT LABELER END"

LONG_EVENTS = {
    "STRONG_UP_MOVE",
    "CLEAN_BREAKOUT_UP",
    "SHORT_SQUEEZE_LIKE",
    "RECOVERY_PUMP",
    "TREND_CONTINUATION_UP",
}
SHORT_EVENTS = {
    "STRONG_DOWN_MOVE",
    "CLEAN_BREAKDOWN_DOWN",
    "LONG_SQUEEZE_LIKE",
    "SUPPORT_LOSS",
    "RESISTANCE_REJECTION_DROP",
    "TREND_CONTINUATION_DOWN",
}


class PreMoveEventLabeler:
    """Research-only event labeling from compact MFE/MAE path metrics."""

    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        rows = _safe(lambda: self.db.fetch_signal_path_metrics_since(since_iso(hours), limit=50000), [])
        events = [event for row in rows if (event := detect_event(row, self.config))]
        long_events = [row for row in events if row["direction"] == "LONG"]
        short_events = [row for row in events if row["direction"] == "SHORT"]
        return {
            "hours": max(1, int(hours or 24)),
            "total_events": len(events),
            "long_events": len(long_events),
            "short_events": len(short_events),
            "events": events[:500],
            "top_symbols_by_up_events": _top_symbols(long_events),
            "top_symbols_by_down_events": _top_symbols(short_events),
            "strongest_up_events": sorted(long_events, key=lambda row: safe_float(row.get("move_pct")), reverse=True)[:10],
            "strongest_down_events": sorted(short_events, key=lambda row: safe_float(row.get("move_pct")), reverse=True)[:10],
            "fakeout_events": [row for row in events if row.get("result_quality") == "FAKEOUT"][:20],
            "final_recommendation": FINAL_NO_LIVE,
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        return "\n".join([
            START,
            f"hours: {payload['hours']}",
            f"total_events: {payload['total_events']}",
            f"long_events: {payload['long_events']}",
            f"short_events: {payload['short_events']}",
            "top_symbols_by_up_events:",
            *_count_lines(payload["top_symbols_by_up_events"]),
            "top_symbols_by_down_events:",
            *_count_lines(payload["top_symbols_by_down_events"]),
            "strongest_up_events:",
            *_event_lines(payload["strongest_up_events"]),
            "strongest_down_events:",
            *_event_lines(payload["strongest_down_events"]),
            "fakeout_events:",
            *_event_lines(payload["fakeout_events"]),
            "final_recommendation: NO LIVE",
            END,
        ])


def detect_event(row: dict[str, Any], config: Any) -> dict[str, Any] | None:
    side = str(row.get("side") or "").upper()
    if side not in {"LONG", "SHORT"}:
        return None
    mfe = safe_float(row.get("max_favorable_pct"))
    mae = safe_float(row.get("max_adverse_pct"))
    final_return = safe_float(row.get("final_return_pct"))
    bars = safe_int(row.get("bars_tracked"))
    min_move = max(0.25, safe_float(getattr(config, "pre_move_min_event_move_pct", 0.50)))
    if max(mfe, mae, abs(final_return)) < min_move:
        return None
    favorable_direction = side
    adverse_direction = "SHORT" if side == "LONG" else "LONG"
    if mfe >= mae and mfe >= min_move:
        direction = favorable_direction
        move_pct = mfe
    elif mae >= min_move:
        direction = adverse_direction
        move_pct = mae
    else:
        direction = favorable_direction if final_return >= 0 else adverse_direction
        move_pct = abs(final_return)
    quality = _quality(mfe=mfe, mae=mae, final_return=final_return, move_pct=move_pct)
    event_type = _event_type(row, direction, quality)
    return {
        "symbol": str(row.get("symbol") or "NA").upper(),
        "direction": direction,
        "event_type": event_type,
        "event_start_time": row.get("created_at") or "",
        "event_end_time": row.get("matured_at") or row.get("updated_at") or row.get("created_at") or "",
        "move_pct": move_pct,
        "max_favorable_excursion": mfe,
        "max_adverse_excursion": mae,
        "bars_to_move": safe_int(row.get("bars_to_mfe") if direction == side else row.get("bars_to_mae")),
        "bars_tracked": bars,
        "regime_before_event": str(row.get("market_regime") or "NA").upper(),
        "regime_during_event": str(row.get("market_regime") or "NA").upper(),
        "score_before_event": safe_int(row.get("score")),
        "signal_side_before_event": side,
        "source": str(row.get("source") or "trade_signal"),
        "strategy": str(row.get("strategy") or row.get("strategy_type") or "NA"),
        "score_bucket": str(row.get("score_bucket") or _score_bucket(safe_int(row.get("score")))),
        "volume_change": "missing",
        "volatility_proxy": _volatility_bucket(row),
        "atr_proxy": "missing",
        "rsi_proxy": "missing",
        "candle_structure_proxy": _candle_proxy(row),
        "btc_eth_context_proxy": "missing",
        "liquidity_spread_proxy": "missing",
        "result_quality": quality,
    }


def _event_type(row: dict[str, Any], direction: str, quality: str) -> str:
    regime = str(row.get("market_regime") or "").upper()
    source = str(row.get("source") or "").lower()
    if direction == "LONG":
        if quality == "FAKEOUT":
            return "RECOVERY_PUMP"
        if "squeeze" in source:
            return "SHORT_SQUEEZE_LIKE"
        if regime in {"TREND_UP", "RISK_ON"}:
            return "TREND_CONTINUATION_UP"
        return "CLEAN_BREAKOUT_UP" if quality == "CLEAN_MOVE" else "STRONG_UP_MOVE"
    if quality == "FAKEOUT":
        return "RESISTANCE_REJECTION_DROP"
    if "squeeze" in source:
        return "LONG_SQUEEZE_LIKE"
    if regime in {"TREND_DOWN", "RISK_OFF"}:
        return "TREND_CONTINUATION_DOWN"
    return "CLEAN_BREAKDOWN_DOWN" if quality == "CLEAN_MOVE" else "STRONG_DOWN_MOVE"


def _quality(*, mfe: float, mae: float, final_return: float, move_pct: float) -> str:
    if move_pct < 0.50:
        return "TOO_SMALL"
    if mae > max(0.50, move_pct * 0.75) and mfe > 0.50:
        return "CHOPPY_MOVE"
    if move_pct >= 0.75 and final_return * (1 if mfe >= mae else -1) < 0:
        return "FAKEOUT"
    if move_pct >= 0.75 and mae <= move_pct * 0.35:
        return "CLEAN_MOVE"
    return "UNKNOWN"


def _volatility_bucket(row: dict[str, Any]) -> str:
    value = max(safe_float(row.get("max_favorable_pct")), safe_float(row.get("max_adverse_pct")))
    if value >= 2.0:
        return "HIGH"
    if value >= 0.75:
        return "MEDIUM"
    return "LOW"


def _candle_proxy(row: dict[str, Any]) -> str:
    mfe = safe_float(row.get("max_favorable_pct"))
    mae = safe_float(row.get("max_adverse_pct"))
    if mfe > mae * 2:
        return "MOMENTUM_BODY"
    if mae > mfe * 2:
        return "REJECTION_WICK"
    return "MIXED"


def _score_bucket(score: int) -> str:
    if score >= 95:
        return "95-100"
    if score >= 90:
        return "90-94"
    if score >= 80:
        return "80-89"
    if score >= 70:
        return "70-79"
    if score >= 60:
        return "60-69"
    return "<60"


def _top_symbols(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(str(row.get("symbol") or "NA") for row in rows)
    return [{"symbol": key, "count": value} for key, value in counts.most_common(10)]


def _count_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [f"- {row.get('symbol')} count={row.get('count')}" for row in rows]


def _event_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- {row.get('symbol')} {row.get('direction')} {row.get('event_type')} "
            f"move={format_pct(safe_float(row.get('move_pct')) / 100.0)} quality={row.get('result_quality')} "
            f"score={row.get('score_before_event')} regime={row.get('regime_before_event')}"
        )
        for row in rows[:10]
    ]


def _safe(fn, fallback):
    try:
        return fn()
    except Exception:
        return fallback
