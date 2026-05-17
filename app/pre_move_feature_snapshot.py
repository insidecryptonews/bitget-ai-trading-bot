from __future__ import annotations

from typing import Any

from .edge_hardening_utils import FINAL_NO_LIVE, apply_net_costs, cost_config, fetch_group_metrics, format_num, format_pct, since_iso
from .pre_move_event_labeler import PreMoveEventLabeler
from .utils import safe_float, safe_int


START = "PRE MOVE FEATURE SNAPSHOT START"
END = "PRE MOVE FEATURE SNAPSHOT END"
WINDOWS = (1, 3, 5, 10, 20)


class PreMoveFeatureSnapshot:
    """Build lightweight feature proxies before strong LONG/SHORT events."""

    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        events = PreMoveEventLabeler(self.config, self.db).build(hours=hours).get("events", [])
        context = _context(self.config, self.db, hours)
        snapshots = []
        missing = 0
        for event in events:
            for window in WINDOWS:
                snap = _snapshot(event, window, context)
                missing += safe_int(snap.get("missing_features_count"))
                snapshots.append(snap)
        return {
            "hours": max(1, int(hours or 24)),
            "snapshots": snapshots[:1000],
            "top_PRE_LONG_EDGE_patterns": _top(snapshots, "PRE_LONG_EDGE"),
            "top_PRE_SHORT_EDGE_patterns": _top(snapshots, "PRE_SHORT_EDGE"),
            "top_TRAP_LONG_patterns": _top(snapshots, "TRAP_LONG"),
            "top_TRAP_SHORT_patterns": _top(snapshots, "TRAP_SHORT"),
            "top_TIME_DEATH_LIKELY_patterns": _top(snapshots, "TIME_DEATH_LIKELY"),
            "missing_features_count": missing,
            "final_recommendation": FINAL_NO_LIVE,
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        return "\n".join([
            START,
            f"hours: {payload['hours']}",
            f"snapshots: {len(payload['snapshots'])}",
            f"missing_features_count: {payload['missing_features_count']}",
            "top PRE_LONG_EDGE patterns:",
            *_snapshot_lines(payload["top_PRE_LONG_EDGE_patterns"]),
            "top PRE_SHORT_EDGE patterns:",
            *_snapshot_lines(payload["top_PRE_SHORT_EDGE_patterns"]),
            "top TRAP_LONG patterns:",
            *_snapshot_lines(payload["top_TRAP_LONG_patterns"]),
            "top TRAP_SHORT patterns:",
            *_snapshot_lines(payload["top_TRAP_SHORT_patterns"]),
            "top TIME_DEATH_LIKELY patterns:",
            *_snapshot_lines(payload["top_TIME_DEATH_LIKELY_patterns"]),
            "final_recommendation: NO LIVE",
            END,
        ])


def _context(config: Any, db: Any, hours: int) -> dict[str, dict[str, Any]]:
    costs = cost_config(config)
    since = since_iso(hours)
    context: dict[str, dict[str, Any]] = {}
    for group in ("symbol", "side", "market_regime", "score_bucket", "source", "strategy", "policy_id"):
        for row in fetch_group_metrics(db, since=since, group_key=group, limit=120, min_samples=1):
            item = apply_net_costs(row, costs)
            context[f"{group}:{str(item.get('group_value') or '').upper()}"] = item
    return context


def _snapshot(event: dict[str, Any], window: int, context: dict[str, dict[str, Any]]) -> dict[str, Any]:
    symbol = str(event.get("symbol") or "NA").upper()
    direction = str(event.get("direction") or "UNKNOWN").upper()
    regime = str(event.get("regime_before_event") or "NA").upper()
    score_bucket = str(event.get("score_bucket") or "NA").upper()
    side = str(event.get("signal_side_before_event") or "NA").upper()
    source = str(event.get("source") or "NA")
    strategy = str(event.get("strategy") or "NA")
    symbol_ctx = context.get(f"symbol:{symbol}", {})
    side_ctx = context.get(f"side:{side}", {})
    regime_ctx = context.get(f"market_regime:{regime}", {})
    bucket_ctx = context.get(f"score_bucket:{score_bucket}", {})
    policy_id = f"policy_{symbol}_{side}_{regime}_{score_bucket}"
    policy_ctx = context.get(f"policy_id:{policy_id.upper()}", {})
    net_ev = safe_float(policy_ctx.get("net_EV") or symbol_ctx.get("net_EV") or side_ctx.get("net_EV"))
    net_pf = safe_float(policy_ctx.get("net_PF") or symbol_ctx.get("net_PF") or side_ctx.get("net_PF"))
    time_ratio = safe_float(policy_ctx.get("time_ratio") or symbol_ctx.get("time_ratio") or regime_ctx.get("time_ratio"))
    tp_ratio = safe_float(policy_ctx.get("tp_ratio") or symbol_ctx.get("tp_ratio") or regime_ctx.get("tp_ratio"))
    sl_ratio = safe_float(policy_ctx.get("sl_ratio") or symbol_ctx.get("sl_ratio") or regime_ctx.get("sl_ratio"))
    missing = sum(1 for value in (source, strategy, regime, score_bucket) if value in {"", "NA", "UNKNOWN"})
    row = {
        "symbol": symbol,
        "side": direction,
        "lookback_bars": window,
        "market_regime": regime,
        "score": safe_int(event.get("score_before_event")),
        "score_bucket": score_bucket,
        "signal_side": side,
        "strategy": strategy,
        "source": source,
        "recent_return_proxy": safe_float(event.get("move_pct")) / max(window, 1),
        "volatility_proxy": event.get("volatility_proxy") or "missing",
        "atr_proxy": event.get("atr_proxy") or "missing",
        "rsi_proxy": event.get("rsi_proxy") or "missing",
        "volume_proxy": event.get("volume_change") or "missing",
        "volume_spike_proxy": "missing",
        "wick_body_proxy": event.get("candle_structure_proxy") or "missing",
        "candle_momentum_proxy": event.get("candle_structure_proxy") or "missing",
        "support_resistance_rejection_proxy": _sr_proxy(event),
        "breakout_breakdown_proxy": _break_proxy(event),
        "btc_eth_alignment_proxy": event.get("btc_eth_context_proxy") or "missing",
        "MFE_before_event": safe_float(event.get("max_favorable_excursion")),
        "MAE_before_event": safe_float(event.get("max_adverse_excursion")),
        "recent_TIME_ratio": time_ratio,
        "recent_TP_ratio": tp_ratio,
        "recent_SL_ratio": sl_ratio,
        "net_EV": net_ev,
        "net_PF": net_pf,
        "time_death_risk": "high" if time_ratio > 0.80 and tp_ratio < 0.10 else "normal",
        "anti_overfit_status": "unknown",
        "policy_status": "candidate" if net_ev > 0 and net_pf >= 1.2 else "not_confirmed",
        "missing_features_count": missing,
    }
    row["classification"] = classify_snapshot(row, event)
    return row


def classify_snapshot(row: dict[str, Any], event: dict[str, Any]) -> str:
    direction = str(row.get("side") or "").upper()
    quality = str(event.get("result_quality") or "")
    net_ev = safe_float(row.get("net_EV"))
    net_pf = safe_float(row.get("net_PF"))
    time_ratio = safe_float(row.get("recent_TIME_ratio"))
    tp_ratio = safe_float(row.get("recent_TP_ratio"))
    sl_ratio = safe_float(row.get("recent_SL_ratio"))
    if quality == "FAKEOUT":
        return "TRAP_LONG" if direction == "LONG" else "TRAP_SHORT"
    if time_ratio > 0.80 and tp_ratio < 0.10:
        return "TIME_DEATH_LIKELY"
    if net_ev <= 0 or net_pf < 1.0:
        return "NO_EDGE"
    if direction == "LONG" and sl_ratio > 0.20 and tp_ratio < 0.05:
        return "TRAP_LONG"
    if direction == "SHORT" and time_ratio > 0.80:
        return "TRAP_SHORT"
    if direction == "LONG":
        return "PRE_LONG_EDGE"
    if direction == "SHORT":
        return "PRE_SHORT_EDGE"
    return "UNKNOWN"


def _sr_proxy(event: dict[str, Any]) -> str:
    kind = str(event.get("event_type") or "")
    if "REJECTION" in kind:
        return "REJECTION"
    if "SUPPORT" in kind:
        return "SUPPORT_LOSS"
    return "missing"


def _break_proxy(event: dict[str, Any]) -> str:
    kind = str(event.get("event_type") or "")
    if "BREAKOUT" in kind:
        return "BREAKOUT"
    if "BREAKDOWN" in kind:
        return "BREAKDOWN"
    return "missing"


def _top(rows: list[dict[str, Any]], classification: str) -> list[dict[str, Any]]:
    selected = [row for row in rows if row.get("classification") == classification]
    selected.sort(key=lambda row: (safe_float(row.get("net_EV")), safe_float(row.get("net_PF")), safe_float(row.get("recent_TP_ratio"))), reverse=True)
    return selected[:10]


def _snapshot_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- {row.get('symbol')} {row.get('side')} class={row.get('classification')} "
            f"regime={row.get('market_regime')} score_bucket={row.get('score_bucket')} "
            f"net_EV={format_num(row.get('net_EV'), 4)} net_PF={format_num(row.get('net_PF'))} "
            f"TP={format_pct(row.get('recent_TP_ratio'))} TIME={format_pct(row.get('recent_TIME_ratio'))}"
        )
        for row in rows[:8]
    ]
