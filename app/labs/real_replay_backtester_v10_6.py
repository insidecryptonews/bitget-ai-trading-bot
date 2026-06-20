"""ResearchOps V10.6 — Replay / Backtester Research Skeleton (research-only).

A PURE, no-lookahead bar-by-bar replay skeleton + backtester-readiness gate.
No exchange, no orders, no DB, no live, no paper execution, no network. It
operates only on in-memory validated bars and emits a research report
(net EV / net PF / drawdown / TP-SL-TIME distribution / cost x2 stress).

Hard no-lookahead rules enforced by construction:
- entry is decided on a signal bar and FILLED on the NEXT bar's open;
- only bars up to the current index are visible to the decision;
- when both TP and SL fall inside the SAME bar, the WORST case is assumed
  (SL first) — never the optimistic one;
- costs (fees + slippage + funding + spread) are always subtracted.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from .data_foundation_v10_5 import (
    ST_INVALID_V105,
    ST_NEED_STRUCTURED_INVENTORY,
    ST_SEMANTIC_FAIL,
    evaluate_manifest_v105,
)

# Backtester readiness statuses.
BR_NEED_DATA = "NEED_DATA"
BR_NEED_LONG_HISTORY = "NEED_LONG_HISTORY"
BR_NEED_CONTENT_VALIDATION = "NEED_CONTENT_VALIDATION"
BR_NEED_VALID_MANIFEST = "NEED_VALID_MANIFEST"
BR_NEED_COST_MODEL = "NEED_COST_MODEL"
BR_READY = "READY_FOR_REPLAY_RESEARCH"

# V10.6.1 — self-declared manifest fields that must NEVER be trusted as
# readiness; the real V10.5.6 gate is the only source of truth.
_DECLARED_READINESS_FIELDS = (
    "promote_allowed", "gate_promote_allowed", "valid_manifest_v105",
    "paper_ready", "live_ready", "ready",
)
_DECLARED_READY_STATUSES = {
    "READY", "READY_FOR_REPLAY_RESEARCH", "STAGED_READY_FOR_PROMOTE",
}

# Replay run statuses.
RUN_NEED_VALIDATED_DATA = "NEED_VALIDATED_DATA"
RUN_OK = "REPLAY_RESEARCH_COMPLETE"

MIN_REPLAY_DAYS = 180
STRONG_REPLAY_DAYS = 365


@dataclass
class CostModel:
    """Conservative default cost model (bps unless noted). Research-only."""
    taker_fee_bps: float = 6.0
    maker_fee_bps: float = 2.0
    slippage_bps: float = 3.0
    spread_bps: float = 2.0
    funding_bps_per_8h: float = 1.0
    latency_bars: int = 1  # entry fills latency_bars after the signal bar

    def round_trip_cost_bps(self, *, taker: bool = True) -> float:
        fee = self.taker_fee_bps if taker else self.maker_fee_bps
        # entry + exit fee + entry + exit slippage + spread crossing
        return 2.0 * fee + 2.0 * self.slippage_bps + self.spread_bps

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def replay_backtester_contract() -> dict[str, Any]:
    """The frozen design contract for the future operational backtester."""
    return {
        "engine": "bar_by_bar_replay",
        "no_lookahead_rules": [
            "decision uses only bars[:i+1] (no future bars visible)",
            "entry fills on bar i+latency_bars open (latency_bars>=1)",
            "same-bar TP and SL => worst case (SL assumed first)",
            "no peeking at the close to decide the same-bar entry",
        ],
        "cost_model": ["maker_taker_fees", "slippage_bps", "spread_bps",
                       "funding_bps_per_8h", "latency_bars"],
        "position_lifecycle": ["signal", "entry_next_bar", "manage", "exit"],
        "exit_reasons": ["TP", "SL", "TIME"],
        "sides": ["LONG", "SHORT"],
        "metrics": ["samples", "net_EV", "net_PF", "gross_PF", "win_rate",
                    "max_drawdown", "exposure", "tp_sl_time_distribution",
                    "cost_x1_x2_x3_stress", "equity_curve_research_only"],
        "never": ["exchange_calls", "real_orders", "paper_execution",
                  "db_writes", "live", "lookahead"],
        "paper_ready": False, "live_ready": False,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }


def _f(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def simulate_position(bars: list[dict[str, Any]], signal_idx: int, *,
                      side: str, tp_pct: float, sl_pct: float,
                      time_limit_bars: int, costs: CostModel) -> dict[str, Any] | None:
    """Simulate ONE position with strict no-lookahead + worst-case same-bar.

    bars: list of {open,high,low,close} dicts (chronological).
    Returns a trade result dict or None if it cannot be opened (no future bar).
    """
    entry_idx = signal_idx + max(1, costs.latency_bars)
    if entry_idx >= len(bars):
        return None  # cannot fill: no future bar (no lookahead cheat)
    entry_open = _f(bars[entry_idx].get("open"))
    if entry_open is None or entry_open <= 0:
        return None
    is_long = side.upper() == "LONG"
    if is_long:
        tp_price = entry_open * (1 + tp_pct)
        sl_price = entry_open * (1 - sl_pct)
    else:
        tp_price = entry_open * (1 - tp_pct)
        sl_price = entry_open * (1 + sl_pct)

    exit_reason = "TIME"
    exit_price = _f(bars[min(entry_idx + time_limit_bars, len(bars) - 1)].get("close")) or entry_open
    last = min(entry_idx + time_limit_bars, len(bars) - 1)
    for j in range(entry_idx, last + 1):
        hi, lo = _f(bars[j].get("high")), _f(bars[j].get("low"))
        if hi is None or lo is None:
            continue
        hit_tp = (hi >= tp_price) if is_long else (lo <= tp_price)
        hit_sl = (lo <= sl_price) if is_long else (hi >= sl_price)
        if hit_tp and hit_sl:
            # WORST CASE: SL is assumed to trigger first (never optimistic).
            exit_reason, exit_price = "SL", sl_price
            break
        if hit_sl:
            exit_reason, exit_price = "SL", sl_price
            break
        if hit_tp:
            exit_reason, exit_price = "TP", tp_price
            break

    gross_ret = ((exit_price - entry_open) / entry_open) if is_long \
        else ((entry_open - exit_price) / entry_open)
    cost = costs.round_trip_cost_bps() / 10_000.0
    held_bars = (last if exit_reason == "TIME" else j) - entry_idx + 1
    funding = (costs.funding_bps_per_8h / 10_000.0) * (held_bars / 8.0)
    net_ret = gross_ret - cost - funding
    return {"side": side.upper(), "entry_idx": entry_idx, "exit_reason": exit_reason,
            "gross_ret": gross_ret, "net_ret": net_ret, "held_bars": held_bars}


def run_replay_research(*, bars_by_symbol: dict[str, list[dict[str, Any]]] | None,
                        signals: list[dict[str, Any]] | None,
                        costs: CostModel | None = None) -> dict[str, Any]:
    """Research replay over in-memory validated bars. Returns NEED_VALIDATED_DATA
    when no bars/signals are supplied. Never trades/persists anything."""
    costs = costs or CostModel()
    base = {"cost_model": costs.as_dict(), "paper_ready": False,
            "live_ready": False, "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}
    if not bars_by_symbol or not signals:
        return {**base, "status": RUN_NEED_VALIDATED_DATA,
                "reason": "no validated dataset / signals supplied"}

    trades: list[dict[str, Any]] = []
    for sig in signals:
        sym = str(sig.get("symbol") or "")
        bars = bars_by_symbol.get(sym) or []
        idx = sig.get("signal_idx")
        if not isinstance(idx, int) or idx < 0 or idx >= len(bars):
            continue
        res = simulate_position(
            bars, idx, side=str(sig.get("side") or "LONG"),
            tp_pct=float(sig.get("tp_pct", 0.01)),
            sl_pct=float(sig.get("sl_pct", 0.01)),
            time_limit_bars=int(sig.get("time_limit_bars", 24)), costs=costs)
        if res is not None:
            trades.append(res)

    n = len(trades)
    if n == 0:
        return {**base, "status": RUN_OK, "samples": 0,
                "note": "no fillable positions (no-lookahead respected)"}
    wins = [t for t in trades if t["net_ret"] > 0]
    gains = sum(t["net_ret"] for t in wins)
    losses = -sum(t["net_ret"] for t in trades if t["net_ret"] <= 0)
    net_ev = sum(t["net_ret"] for t in trades) / n
    gross_ev = sum(t["gross_ret"] for t in trades) / n
    net_pf = (gains / losses) if losses > 0 else (math.inf if gains > 0 else 0.0)
    dist = {"TP": 0, "SL": 0, "TIME": 0}
    for t in trades:
        dist[t["exit_reason"]] = dist.get(t["exit_reason"], 0) + 1
    # research-only equity curve + drawdown
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for t in trades:
        equity += t["net_ret"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {**base, "status": RUN_OK, "samples": n,
            "net_EV": round(net_ev, 6), "gross_EV": round(gross_ev, 6),
            "net_PF": (round(net_pf, 4) if math.isfinite(net_pf) else "inf"),
            "win_rate": round(len(wins) / n, 4),
            "max_drawdown": round(max_dd, 6),
            "tp_sl_time_distribution": dist,
            "cost_x1_x2_x3_stress": _cost_stress(trades, costs)}


def _cost_stress(trades: list[dict[str, Any]], costs: CostModel) -> dict[str, Any]:
    """Net EV under 1x/2x/3x the base round-trip cost (gross unchanged)."""
    base_cost = costs.round_trip_cost_bps() / 10_000.0
    out = {}
    for mult in (1, 2, 3):
        extra = base_cost * (mult - 1)
        ev = sum(t["net_ret"] - extra for t in trades) / max(1, len(trades))
        out[f"x{mult}"] = round(ev, 6)
    return out


# ---------------------------------------------------------------------------
# D. Backtester readiness gate over a manifest evaluation
# ---------------------------------------------------------------------------

@dataclass
class BacktesterReadinessV106:
    status: str = BR_NEED_DATA
    clean_days: Any = "UNKNOWN"
    required_min_days: int = MIN_REPLAY_DAYS
    strong_days: int = STRONG_REPLAY_DAYS
    manifest_promotable: bool = False
    oi_ok: bool = False
    cost_model_present: bool = True
    blockers: list[str] = field(default_factory=list)
    paper_ready: bool = False
    live_ready: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_backtester_readiness(manifest: dict[str, Any] | None,
                                  *, cost_model: dict[str, Any] | None = None) -> BacktesterReadinessV106:
    """Decide whether a manifest can feed the research replay.

    V10.6.1 (Codex fix) — the manifest is NEVER trusted on self-declared
    readiness fields (``promote_allowed`` / ``gate_promote_allowed`` /
    ``valid_manifest_v105`` / ``paper_ready`` / ``live_ready`` / ``status``).
    It is ALWAYS re-validated against the real V10.5.6 gate
    ``evaluate_manifest_v105`` (which itself strips any input ``promote_allowed``
    and recomputes everything from content + file inventory + path safety +
    coverage/auth gates). Promotability is derived EXCLUSIVELY from that gate.
    A crafted/minimal/unauthorized/unsafe-path manifest can never reach READY.
    Declared readiness is ignored and flagged ``declared_readiness_ignored``.
    """
    r = BacktesterReadinessV106()
    m = dict(manifest or {})
    blockers: list[str] = []
    if not m:
        r.status = BR_NEED_DATA
        r.blockers = ["no_manifest"]
        return r

    # Record (never honour) any self-declared readiness so a caller cannot be
    # misled into thinking it influenced the verdict — it did not.
    declared = any(m.get(k) for k in _DECLARED_READINESS_FIELDS) or (
        str(m.get("status") or "").upper() in _DECLARED_READY_STATUSES)
    if declared:
        blockers.append("declared_readiness_ignored")

    # Re-run the REAL gate. promotability comes only from here.
    gate = evaluate_manifest_v105(m)
    r.manifest_promotable = bool(gate.promote_allowed)

    # clean_days / OI are content fields the gate already validates; read them
    # only to surface the backtester-specific long-history threshold.
    clean_days = _f(m.get("clean_days"))
    r.clean_days = clean_days if clean_days is not None else "UNKNOWN"
    oi_status = str(m.get("missing_oi_status") or "").upper()
    r.oi_ok = oi_status == "DATA_OK"
    r.cost_model_present = True  # a conservative default model always exists

    if not r.manifest_promotable:
        # Fail-closed: a manifest the real gate won't promote is never READY.
        blockers.append("manifest_not_promotable")
        blockers.extend(f"manifest_gate:{b}"
                        for b in (list(gate.blockers) or [gate.status])[:8])
        r.status = (BR_NEED_VALID_MANIFEST
                    if gate.status in (ST_INVALID_V105, ST_SEMANTIC_FAIL,
                                       ST_NEED_STRUCTURED_INVENTORY)
                    else BR_NEED_CONTENT_VALIDATION)
    elif clean_days is None:
        blockers.append("clean_days_unknown")
        r.status = BR_NEED_DATA
    elif clean_days < MIN_REPLAY_DAYS:
        blockers.append(f"clean_days={clean_days}<{MIN_REPLAY_DAYS}")
        r.status = BR_NEED_LONG_HISTORY
    elif not r.oi_ok:
        blockers.append(f"oi_not_audited_ok ({oi_status or 'UNKNOWN'})")
        r.status = BR_NEED_CONTENT_VALIDATION
    else:
        # Gate PASSED + >=180 clean days + OI audited OK. Research replay only.
        r.status = BR_READY

    r.blockers = blockers
    return r
