"""ResearchOps V10.1 — External Event Study Engine (research-only).

Given a set of *events* (funding/OI extremes, big unlocks, listings) and a
per-symbol price *market series*, this measures forward returns, compares
against a random baseline, computes bootstrap confidence intervals, and
flags one-event / one-symbol dominance — all WITHOUT lookahead.

The event DEFINITION uses only data up to (and including) the event bar.
The forward RETURN is measured from the first bar at/after the event
(entry can only happen once the event is known). MFE/MAE are diagnostics
only — never inputs to the signal.

HARD CONTRACT — research only:

- never opens orders, never calls private endpoints, never touches the DB,
- never calls any network / paid API,
- missing series/events => ``NEED_DATA`` (honest),
- the verdict ceiling is ``RESEARCH_GREEN`` — the report NEVER returns
  ``PAPER_READY`` or ``LIVE_READY``, and always ``NO LIVE``.
"""

from __future__ import annotations

import bisect
import random
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from .external_edge_schemas_v10_1 import (
    FINAL_RECOMMENDATION_NO_LIVE,
    normalize_timestamp_to_ms,
)

STATUS_NEED_DATA = "NEED_DATA"
STATUS_NEED_MORE = "NEED_MORE_DATA"
STATUS_REJECT = "REJECT"
STATUS_WATCH = "WATCH_ONLY"
STATUS_GREEN = "RESEARCH_GREEN"

MIN_EVENTS = 20
DEFAULT_HORIZONS_H = (1.0, 4.0, 8.0, 24.0)
EVENT_HORIZONS_H = (24.0, 72.0, 168.0)
MS_PER_HOUR = 3_600_000.0
MAX_EVENT_DOMINANCE = 0.40
MAX_SYMBOL_DOMINANCE = 0.60
# Trailing window (bars) used by funding/OI event definition. The hours
# filter must keep this much lookback BEFORE the cutoff so z-scores are
# computed on the same history they would have had unfiltered.
FUNDING_OI_LOOKBACK_BARS = 48


def _ms_iso(ms: int | None) -> str:
    if ms is None:
        return ""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _ev_ts(ev: dict[str, Any]) -> int | None:
    t = ev.get("timestamp_ms")
    if t is None:
        t = normalize_timestamp_to_ms(ev.get("timestamp") or ev.get("event_time"))
    return int(t) if t is not None else None


def _median_delta(market_by_symbol: dict[str, dict[str, list]]) -> float:
    deltas: list[int] = []
    for s in market_by_symbol.values():
        ts = sorted(set(s.get("ts", [])))
        deltas.extend(b - a for a, b in zip(ts, ts[1:]) if b > a)
    return float(statistics.median(deltas)) if deltas else 0.0


def _filter_market(market_by_symbol: dict[str, dict[str, list]], start_ms: int) -> dict[str, dict[str, list]]:
    """Keep only points with ts >= start_ms (drops OLD data only; never
    touches the future). Rebuilds the parallel arrays index-aligned."""
    out: dict[str, dict[str, list]] = {}
    for sym, s in market_by_symbol.items():
        ts = s.get("ts", [])
        keep = [i for i, t in enumerate(ts) if t >= start_ms]
        if not keep:
            continue
        out[sym] = {k: [s[k][i] for i in keep] for k in ("ts", "close", "high", "low", "funding", "oi")}
    return out


@dataclass
class EventStudyReport:
    module: str = ""
    generated_at_seed: int = 0
    event_count: int = 0
    matched_events: int = 0
    symbols: list[str] = field(default_factory=list)
    horizons_h: list[float] = field(default_factory=list)
    primary_horizon_h: float = 24.0
    cost_pct: float = 0.0
    # V10.1 hours-window filter (transparent + conservative, no lookahead).
    hours_requested: float | None = None
    filter_applied: bool = False
    reference_now_ms: int | None = None
    reference_now_iso: str = ""
    cutoff_timestamp_ms: int | None = None
    cutoff_timestamp: str = ""
    lookback_required: bool = False
    lookback_ms: int = 0
    effective_start_timestamp_ms: int | None = None
    effective_start_timestamp: str = ""
    rows_before_filter: int = 0
    rows_after_filter: int = 0
    events_before_filter: int = 0
    events_after_filter: int = 0
    # Per-horizon aggregates (net, after cost).
    per_horizon: list[dict[str, Any]] = field(default_factory=list)
    # Primary-horizon headline.
    net_ev_pct: float = 0.0
    gross_ev_pct: float = 0.0
    winrate: float = 0.0
    baseline_net_ev_pct: float = 0.0
    edge_vs_baseline_pct: float = 0.0
    baseline_direction: str = ""  # LONG/SHORT — baseline is direction-matched
    bootstrap_ci_low: float = 0.0
    bootstrap_ci_high: float = 0.0
    bootstrap_n: int = 0
    # Diagnostics.
    avg_mfe_pct: float = 0.0
    avg_mae_pct: float = 0.0
    median_time_to_tp_h: float | None = None
    median_time_to_sl_h: float | None = None
    one_event_dominance: float = 0.0
    one_symbol_dominance: float = 0.0
    top_symbol: str = ""
    sample_count: int = 0
    blockers: list[str] = field(default_factory=list)
    status: str = STATUS_NEED_DATA
    # Hard invariants.
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    paper_ready: bool = False
    live_ready: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# Market series helpers
# --------------------------------------------------------------------------


def build_market_series(clean_rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, list]]:
    """Group clean perp_market_state rows into per-symbol sorted arrays."""
    tmp: dict[str, list[tuple[int, dict]]] = {}
    for r in clean_rows or []:
        sym = str(r.get("symbol") or "").strip().upper()
        ts = r.get("timestamp_ms")
        if ts is None:
            ts = normalize_timestamp_to_ms(r.get("timestamp"))
        if not sym or ts is None:
            continue
        tmp.setdefault(sym, []).append((int(ts), r))
    out: dict[str, dict[str, list]] = {}
    for sym, pairs in tmp.items():
        pairs.sort(key=lambda t: t[0])
        ts_list, close, high, low, funding, oi = [], [], [], [], [], []
        for ts, r in pairs:
            ts_list.append(ts)
            close.append(_f(r.get("price_close")))
            high.append(_f(r.get("price_high")) or _f(r.get("price_close")))
            low.append(_f(r.get("price_low")) or _f(r.get("price_close")))
            funding.append(_f(r.get("funding_rate")))
            oi.append(_f(r.get("oi_usd_close")))
        out[sym] = {"ts": ts_list, "close": close, "high": high, "low": low,
                    "funding": funding, "oi": oi}
    return out


def _f(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v.strip():
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _zscore(value: float, window: list[float]) -> float | None:
    vals = [x for x in window if x is not None]
    if len(vals) < 8:
        return None
    mu = statistics.fmean(vals)
    sd = statistics.pstdev(vals)
    if sd <= 0:
        return 0.0
    return (value - mu) / sd


# --------------------------------------------------------------------------
# Event definitions (NO lookahead — use data <= event bar)
# --------------------------------------------------------------------------


def define_funding_oi_extreme_events(
    market_by_symbol: dict[str, dict[str, list]],
    *,
    funding_z_thr: float = 2.0,
    oi_z_thr: float = 2.0,
    lookback: int = 48,
) -> list[dict[str, Any]]:
    """Events where trailing funding/OI z-score is extreme. The z-score at
    bar i uses ONLY bars [i-lookback, i] (no future)."""
    events: list[dict[str, Any]] = []
    for sym, s in market_by_symbol.items():
        ts, funding, oi, close = s["ts"], s["funding"], s["oi"], s["close"]
        for i in range(lookback, len(ts)):
            fwin = funding[i - lookback:i + 1]
            owin = oi[i - lookback:i + 1]
            fz = _zscore(funding[i], fwin) if funding[i] is not None else None
            oz = _zscore(oi[i], owin) if oi[i] is not None else None
            extreme = (fz is not None and abs(fz) >= funding_z_thr) or \
                      (oz is not None and abs(oz) >= oi_z_thr)
            if not extreme:
                continue
            # Direction: crowded longs (funding very positive) => short bias.
            direction = -1 if (fz is not None and fz >= funding_z_thr) else 1
            events.append({
                "symbol": sym, "timestamp_ms": ts[i], "direction": direction,
                "funding_z": round(fz, 4) if fz is not None else None,
                "oi_z": round(oz, 4) if oz is not None else None,
                "kind": "funding_oi_extreme",
            })
    return events


def define_big_unlock_events(
    unlock_rows: Iterable[dict[str, Any]],
    *,
    min_unlock_pct: float = 5.0,
    min_value_to_mcap: float = 0.05,
    min_fdv_to_mcap: float = 3.0,
) -> list[dict[str, Any]]:
    """Big dilution unlocks => short bias. Uses only event metadata."""
    events: list[dict[str, Any]] = []
    for r in unlock_rows or []:
        sym = str(r.get("token_symbol") or r.get("symbol") or "").strip().upper()
        ts = r.get("timestamp_ms") or normalize_timestamp_to_ms(r.get("event_time"))
        if not sym or ts is None:
            continue
        unlock_pct = _f(r.get("unlock_pct_circulating"))
        value = _f(r.get("unlock_value_usd"))
        mcap = _f(r.get("circulating_mcap_usd"))
        fdv2mcap = _f(r.get("fdv_to_mcap"))
        v_to_mcap = (value / mcap) if (value is not None and mcap and mcap > 0) else None
        material = (
            (unlock_pct is not None and unlock_pct >= min_unlock_pct)
            or (v_to_mcap is not None and v_to_mcap >= min_value_to_mcap)
            or (fdv2mcap is not None and fdv2mcap >= min_fdv_to_mcap)
        )
        if not material:
            continue
        events.append({
            "symbol": sym, "timestamp_ms": int(ts), "direction": -1,
            "unlock_pct_circulating": unlock_pct, "fdv_to_mcap": fdv2mcap,
            "kind": "big_unlock",
        })
    return events


def define_post_listing_events(
    listing_rows: Iterable[dict[str, Any]],
    *,
    max_listing_age_days: float = 30.0,
) -> list[dict[str, Any]]:
    """Listing events; default short bias on post-listing hype exhaustion."""
    events: list[dict[str, Any]] = []
    for r in listing_rows or []:
        sym = str(r.get("symbol_perp_bitget") or r.get("symbol") or "").strip().upper()
        ts = r.get("timestamp_ms") or normalize_timestamp_to_ms(r.get("listing_time"))
        if not sym or ts is None:
            continue
        events.append({
            "symbol": sym, "timestamp_ms": int(ts), "direction": -1,
            "kind": "post_listing",
        })
    return events


# --------------------------------------------------------------------------
# Core event study
# --------------------------------------------------------------------------


def _bootstrap_ci(values: list[float], *, n: int, seed: int, alpha: float = 0.05) -> tuple[float, float]:
    if len(values) < 2:
        v = values[0] if values else 0.0
        return v, v
    rng = random.Random(seed)
    m = len(values)
    means = []
    for _ in range(n):
        sample = [values[rng.randrange(m)] for _ in range(m)]
        means.append(statistics.fmean(sample))
    means.sort()
    lo = means[int((alpha / 2) * (len(means) - 1))]
    hi = means[int((1 - alpha / 2) * (len(means) - 1))]
    return round(lo, 5), round(hi, 5)


def run_event_study(
    events: Iterable[dict[str, Any]] | None,
    market_by_symbol: dict[str, dict[str, list]] | None,
    *,
    module: str = "",
    horizons_h: tuple[float, ...] = DEFAULT_HORIZONS_H,
    cost: float = 0.0018,
    primary_horizon_h: float | None = None,
    tp_pct: float = 0.03,
    sl_pct: float = 0.02,
    baseline_n: int = 500,
    bootstrap_n: int = 2000,
    seed: int = 7,
    min_events: int = MIN_EVENTS,
    hours: float | None = None,
    now_ms: int | None = None,
    lookback_bars_for_events: int = 0,
) -> EventStudyReport:
    report = EventStudyReport(
        module=module,
        generated_at_seed=seed,
        horizons_h=list(horizons_h),
        cost_pct=cost,
        bootstrap_n=bootstrap_n,
    )
    ev_list = list(events or [])
    report.event_count = len(ev_list)
    report.events_before_filter = len(ev_list)
    primary = primary_horizon_h if primary_horizon_h is not None else max(horizons_h)
    report.primary_horizon_h = primary

    mbs = market_by_symbol or {}
    report.rows_before_filter = sum(len(s.get("ts", [])) for s in mbs.values())
    report.hours_requested = hours if (hours is not None and hours > 0) else None

    if not mbs or not ev_list:
        report.status = STATUS_NEED_DATA
        report.blockers = ["no_market_series" if not mbs else "no_events"]
        report.rows_after_filter = report.rows_before_filter
        report.events_after_filter = len(ev_list)
        return report

    # ---- V10.1 time-window filter (transparent, conservative, no lookahead) ----
    # The events were DEFINED on the full series (so trailing z-score windows
    # are intact). Here we keep only events within the last ``hours`` and trim
    # market history to ``cutoff - lookback`` so the study cannot silently use
    # all available history while reporting an N-hour window. We only ever drop
    # OLD data; forward-return bars (after each event) are always retained.
    if hours is not None and hours > 0:
        report.filter_applied = True
        all_ts: list[int] = []
        for s in mbs.values():
            all_ts.extend(int(t) for t in s.get("ts", []))
        for ev in ev_list:
            t = _ev_ts(ev)
            if t is not None:
                all_ts.append(t)
        ref_now = int(now_ms) if now_ms is not None else (max(all_ts) if all_ts else None)
        if ref_now is not None:
            report.reference_now_ms = ref_now
            report.reference_now_iso = _ms_iso(ref_now)
            cutoff = int(ref_now - hours * MS_PER_HOUR)
            report.cutoff_timestamp_ms = cutoff
            report.cutoff_timestamp = _ms_iso(cutoff)
            report.lookback_required = lookback_bars_for_events > 0
            lookback_ms = 0
            if lookback_bars_for_events > 0:
                med = _median_delta(mbs)
                lookback_ms = int(lookback_bars_for_events * med) if med else 0
            report.lookback_ms = lookback_ms
            eff_start = cutoff - lookback_ms
            report.effective_start_timestamp_ms = eff_start
            report.effective_start_timestamp = _ms_iso(eff_start)
            ev_list = [e for e in ev_list if (_ev_ts(e) is not None and _ev_ts(e) >= cutoff)]
            mbs = _filter_market(mbs, eff_start)
    else:
        report.filter_applied = False

    market_by_symbol = mbs
    report.events_after_filter = len(ev_list)
    report.rows_after_filter = sum(len(s.get("ts", [])) for s in mbs.values())

    if not mbs:
        report.status = STATUS_NEED_DATA
        report.blockers = ["no_market_series_after_filter"]
        return report
    if not ev_list:
        # Events existed but the window filtered them all out.
        report.status = STATUS_NEED_MORE if report.events_before_filter > 0 else STATUS_NEED_DATA
        report.blockers = ["all_events_outside_window"]
        return report

    max_h = max(horizons_h)
    # Per-event measurement.
    per_horizon_returns: dict[float, list[float]] = {h: [] for h in horizons_h}
    primary_net_by_event: list[tuple[str, float]] = []  # (symbol, net%)
    mfes, maes, ttps, ttsls = [], [], [], []
    symbols_seen: set[str] = set()
    matched = 0

    for ev in ev_list:
        sym = str(ev.get("symbol") or "").strip().upper()
        ev_ts = ev.get("timestamp_ms")
        if ev_ts is None:
            ev_ts = normalize_timestamp_to_ms(ev.get("timestamp") or ev.get("event_time"))
        s = market_by_symbol.get(sym)
        if not s or ev_ts is None:
            continue
        ts_list = s["ts"]
        # Entry = first bar at/after the event (no lookahead on entry).
        a = bisect.bisect_left(ts_list, int(ev_ts))
        if a >= len(ts_list):
            continue
        anchor_close = s["close"][a]
        if anchor_close is None or anchor_close <= 0:
            continue
        direction = int(ev.get("direction", 1) or 1)
        matched += 1
        symbols_seen.add(sym)

        # Forward returns per horizon.
        for h in horizons_h:
            target = ts_list[a] + h * MS_PER_HOUR
            j = bisect.bisect_left(ts_list, target)
            if j >= len(ts_list):
                continue
            pf = s["close"][j]
            if pf is None or pf <= 0:
                continue
            gross = (pf - anchor_close) / anchor_close * 100.0 * direction
            net = gross - cost * 100.0
            per_horizon_returns[h].append(net)
            if abs(h - primary) < 1e-9:
                primary_net_by_event.append((sym, net))

        # MFE/MAE + time-to-TP/SL over [a+1, a + max_h], diagnostics only.
        end_ts = ts_list[a] + max_h * MS_PER_HOUR
        end_idx = bisect.bisect_left(ts_list, end_ts)
        mfe = 0.0
        mae = 0.0
        ttp = None
        ttsl = None
        for k in range(a + 1, min(end_idx + 1, len(ts_list))):
            hi, lo = s["high"][k], s["low"][k]
            if hi is None or lo is None:
                continue
            if direction == 1:
                fav = (hi - anchor_close) / anchor_close * 100.0
                adv = (lo - anchor_close) / anchor_close * 100.0
                tp_hit = hi >= anchor_close * (1 + tp_pct)
                sl_hit = lo <= anchor_close * (1 - sl_pct)
            else:
                fav = (anchor_close - lo) / anchor_close * 100.0
                adv = (anchor_close - hi) / anchor_close * 100.0
                tp_hit = lo <= anchor_close * (1 - tp_pct)
                sl_hit = hi >= anchor_close * (1 + sl_pct)
            mfe = max(mfe, fav)
            mae = min(mae, adv)
            hours = (ts_list[k] - ts_list[a]) / MS_PER_HOUR
            # STOP_BEFORE_TP: SL precedence same bar.
            if sl_hit and ttsl is None:
                ttsl = hours
            if tp_hit and ttp is None and not (sl_hit and ttsl == hours):
                ttp = hours
            if ttp is not None or ttsl is not None:
                # record first barrier only
                break
        mfes.append(mfe)
        maes.append(mae)
        if ttp is not None:
            ttps.append(ttp)
        if ttsl is not None:
            ttsls.append(ttsl)

    report.matched_events = matched
    report.symbols = sorted(symbols_seen)
    report.per_horizon = []
    for h in horizons_h:
        rs = per_horizon_returns[h]
        if rs:
            report.per_horizon.append({
                "horizon_h": h, "samples": len(rs),
                "net_ev_pct": round(statistics.fmean(rs), 4),
                "winrate": round(sum(1 for x in rs if x > 0) / len(rs), 4),
            })
        else:
            report.per_horizon.append({"horizon_h": h, "samples": 0,
                                       "net_ev_pct": 0.0, "winrate": 0.0})

    primary_nets = [net for _, net in primary_net_by_event]
    report.sample_count = len(primary_nets)
    if mfes:
        report.avg_mfe_pct = round(statistics.fmean(mfes), 4)
        report.avg_mae_pct = round(statistics.fmean(maes), 4)
    if ttps:
        report.median_time_to_tp_h = round(statistics.median(ttps), 4)
    if ttsls:
        report.median_time_to_sl_h = round(statistics.median(ttsls), 4)

    if not primary_nets:
        report.status = STATUS_NEED_DATA
        report.blockers = ["no_forward_data_at_primary_horizon"]
        return report

    report.net_ev_pct = round(statistics.fmean(primary_nets), 4)
    gross_primary = [n + cost * 100.0 for n in primary_nets]
    report.gross_ev_pct = round(statistics.fmean(gross_primary), 4)
    report.winrate = round(sum(1 for x in primary_nets if x > 0) / len(primary_nets), 4)
    report.bootstrap_ci_low, report.bootstrap_ci_high = _bootstrap_ci(
        primary_nets, n=bootstrap_n, seed=seed)

    # Random baseline: uniform anchors over the same symbols (reproducible),
    # DIRECTION-MATCHED to the bucket so edge_vs_baseline measures the
    # conditional edge (signal vs random same-direction entries), not the
    # market drift. The representative direction is the (sign) majority of
    # the bucket's events; diagnostics buckets are single-direction.
    dirs = [int(e.get("direction", 1) or 1) for e in ev_list]
    baseline_dir = 1 if (sum(dirs) >= 0) else -1
    report.baseline_direction = "LONG" if baseline_dir == 1 else "SHORT"
    report.baseline_net_ev_pct = _random_baseline(
        market_by_symbol, symbols_seen, primary, cost, n=baseline_n, seed=seed,
        direction=baseline_dir)
    report.edge_vs_baseline_pct = round(report.net_ev_pct - report.baseline_net_ev_pct, 4)

    # Dominance (on absolute contribution at primary horizon).
    report.one_event_dominance, report.one_symbol_dominance, report.top_symbol = \
        _dominance(primary_net_by_event)

    # Verdict.
    report.status, report.blockers = _verdict(report, min_events=min_events)
    return report


def _random_baseline(market_by_symbol, symbols, horizon_h, cost, *, n, seed, direction: int = 1) -> float:
    """Mean net forward return of random entries over the same symbols,
    DIRECTION-MATCHED. ``direction`` (+1 long / -1 short) is applied to the
    forward return so the baseline is a fair "random entry of the same
    side" reference — isolating conditional edge from market drift."""
    rng = random.Random(seed * 31 + 1)
    d = 1 if direction >= 0 else -1
    rets = []
    syms = [s for s in symbols if s in market_by_symbol and len(market_by_symbol[s]["ts"]) > 3]
    if not syms:
        return 0.0
    for _ in range(n):
        sym = syms[rng.randrange(len(syms))]
        s = market_by_symbol[sym]
        ts_list = s["ts"]
        a = rng.randrange(0, len(ts_list) - 1)
        anchor = s["close"][a]
        if anchor is None or anchor <= 0:
            continue
        target = ts_list[a] + horizon_h * MS_PER_HOUR
        j = bisect.bisect_left(ts_list, target)
        if j >= len(ts_list):
            continue
        pf = s["close"][j]
        if pf is None or pf <= 0:
            continue
        gross = (pf - anchor) / anchor * 100.0 * d  # direction-matched
        rets.append(gross - cost * 100.0)
    return round(statistics.fmean(rets), 4) if rets else 0.0


def _dominance(net_by_event: list[tuple[str, float]]) -> tuple[float, float, str]:
    if not net_by_event:
        return 0.0, 0.0, ""
    total_abs = sum(abs(n) for _, n in net_by_event)
    if total_abs <= 0:
        return 0.0, 0.0, ""
    one_event = max(abs(n) for _, n in net_by_event) / total_abs
    by_sym: dict[str, float] = {}
    for sym, n in net_by_event:
        by_sym[sym] = by_sym.get(sym, 0.0) + abs(n)
    top_sym = max(by_sym.items(), key=lambda kv: kv[1])
    one_symbol = top_sym[1] / total_abs
    return round(one_event, 4), round(one_symbol, 4), top_sym[0]


def _verdict(report: EventStudyReport, *, min_events: int) -> tuple[str, list[str]]:
    blockers: list[str] = []
    if report.matched_events < min_events:
        blockers.append("insufficient_events")
        return STATUS_NEED_MORE, blockers
    if report.net_ev_pct <= 0:
        blockers.append("net_ev_non_positive")
        return STATUS_REJECT, blockers
    if report.bootstrap_ci_low <= 0:
        blockers.append("bootstrap_ci_crosses_zero")
    if report.edge_vs_baseline_pct <= 0:
        blockers.append("no_edge_vs_baseline")
    if report.one_event_dominance > MAX_EVENT_DOMINANCE:
        blockers.append("one_event_dominance")
    if report.one_symbol_dominance > MAX_SYMBOL_DOMINANCE:
        blockers.append("one_symbol_dominance")
    if blockers:
        return STATUS_WATCH, blockers
    return STATUS_GREEN, blockers
