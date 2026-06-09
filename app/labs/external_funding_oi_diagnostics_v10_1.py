"""ResearchOps V10.1 — Funding/OI/Liquidation diagnostics by sub-bucket.

The aggregate funding/OI event study came back REJECT. This module does
the honest next step: it slices the SAME data into sub-families (by
symbol, funding sign/z, OI momentum/z/percentile, price+OI quadrants, and
liquidation regimes) and runs the no-lookahead event study on each, then
applies CONSERVATIVE gates. It does NOT optimise or curve-fit; it just
reports per-bucket EV so we can see whether any pocket is even worth more
data.

HARD CONTRACT — research only:

- reuses the audited ``run_event_study`` (entry next bar open, SL before
  TP, cost-stress, bootstrap CI, dominance) — no lookahead,
- bucket conditions use ONLY trailing windows (data <= the event bar),
- never opens orders / private endpoints / DB writes / touches runtime,
- missing data => ``NEED_DATA`` (honest),
- the ceiling is ``RESEARCH_GREEN`` (conservative); never PAPER/LIVE ready.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from . import FINAL_RECOMMENDATION_NO_LIVE
from .external_event_study_v10_1 import (
    DEFAULT_HORIZONS_H,
    FUNDING_OI_LOOKBACK_BARS,
    build_market_series,
    run_event_study,
)

STATUS_NEED_DATA = "NEED_DATA"
STATUS_NEED_MORE = "NEED_MORE_DATA"
STATUS_REJECT = "REJECT"
STATUS_WATCH = "WATCH_ONLY"
STATUS_GREEN = "RESEARCH_GREEN"
STATUS_OK_LABEL = "OK"  # report-level status when buckets were evaluated

# Conservative gates (from the research brief).
WATCH_MIN_EVENTS = 50
GREEN_MIN_EVENTS = 100
MAX_EVENT_DOMINANCE = 0.25
MAX_SYMBOL_DOMINANCE = 0.75

# Feature windows (trailing => no lookahead).
Z_LOOKBACK = 168          # ~7 days of 1h bars for funding/OI z-scores
PCT_LOOKBACK = 168
MOM_4H = 4
MOM_24H = 24
PRIMARY_HORIZON_H = 24.0


@dataclass
class BucketResult:
    name: str = ""
    hypothesis: str = ""
    direction: int = 0
    symbol_scope: str = "ALL"
    event_count: int = 0
    matched_events: int = 0
    net_ev_1h: float = 0.0
    net_ev_4h: float = 0.0
    net_ev_8h: float = 0.0
    net_ev_24h: float = 0.0
    gross_ev_pct: float = 0.0
    winrate: float = 0.0
    baseline_net_ev_pct: float = 0.0
    baseline_direction: str = ""
    edge_vs_baseline_pct: float = 0.0
    bootstrap_ci_low: float = 0.0
    bootstrap_ci_high: float = 0.0
    one_event_dominance: float = 0.0
    one_symbol_dominance: float = 0.0
    symbol_dominance_blocking: bool = False
    avg_mfe_pct: float = 0.0
    avg_mae_pct: float = 0.0
    median_time_to_tp_h: float | None = None
    median_time_to_sl_h: float | None = None
    exact_blocker: str = ""
    status: str = STATUS_NEED_MORE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FundingOiDiagnosticsReport:
    hours: int = 2160
    cost_pct: float = 0.0018
    symbols: list[str] = field(default_factory=list)
    market_rows: int = 0
    liq_rows: int = 0
    buckets_evaluated: int = 0
    top_by_net_ev_24h: list[dict[str, Any]] = field(default_factory=list)
    rejected_count: int = 0
    need_more_count: int = 0
    watch_only: list[str] = field(default_factory=list)
    research_green: list[str] = field(default_factory=list)
    buckets: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    paper_ready: bool = False
    live_ready: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


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


def _z(value: float | None, window: list[float | None]) -> float | None:
    vals = [x for x in window if x is not None]
    if value is None or len(vals) < 8:
        return None
    mu = statistics.fmean(vals)
    sd = statistics.pstdev(vals)
    if sd <= 0:
        return 0.0
    return (value - mu) / sd


def _pct_rank(value: float | None, window: list[float | None]) -> float | None:
    vals = [x for x in window if x is not None]
    if value is None or len(vals) < 8:
        return None
    return sum(1 for x in vals if x <= value) / len(vals) * 100.0


def aggregate_liquidations(liq_clean: list[dict[str, Any]] | None) -> dict[str, dict[int, dict[str, float]]]:
    """Aggregate clean perp_liquidations into {symbol: {t_ms: {long, short}}}."""
    out: dict[str, dict[int, dict[str, float]]] = {}
    for r in liq_clean or []:
        sym = str(r.get("symbol") or "").strip().upper()
        ts = r.get("timestamp_ms")
        if ts is None or not sym:
            continue
        ts = int(ts)
        side = str(r.get("side") or "").strip().upper()
        notional = _f(r.get("notional_usd")) or 0.0
        bucket = out.setdefault(sym, {}).setdefault(ts, {"long": 0.0, "short": 0.0})
        if side == "LONG":
            bucket["long"] += notional
        elif side == "SHORT":
            bucket["short"] += notional
    return out


def build_features(mbs: dict[str, dict[str, list]], liq_by_symbol: dict[str, dict[int, dict[str, float]]]):
    """Per-symbol per-bar features computed with TRAILING windows only.
    Returns {symbol: list[feature dict]} index-aligned with the series."""
    feats: dict[str, list[dict[str, Any]]] = {}
    for sym, s in mbs.items():
        ts, close, funding, oi = s["ts"], s["close"], s["funding"], s["oi"]
        liq_map = liq_by_symbol.get(sym, {})
        # total-liquidation series for z-scoring
        total_liq_series = [
            (liq_map.get(t, {}).get("long", 0.0) + liq_map.get(t, {}).get("short", 0.0))
            for t in ts
        ]
        out: list[dict[str, Any]] = []
        for i in range(len(ts)):
            fwin = funding[max(0, i - Z_LOOKBACK + 1): i + 1]
            owin = oi[max(0, i - PCT_LOOKBACK + 1): i + 1]
            lwin = total_liq_series[max(0, i - Z_LOOKBACK + 1): i + 1]
            f_now = funding[i]
            o_now = oi[i]
            price_4h = ((close[i] - close[i - MOM_4H]) / close[i - MOM_4H]
                        if i >= MOM_4H and close[i - MOM_4H] else None)
            price_24h = ((close[i] - close[i - MOM_24H]) / close[i - MOM_24H]
                         if i >= MOM_24H and close[i - MOM_24H] else None)
            oi_mom_4h = ((o_now - oi[i - MOM_4H]) / oi[i - MOM_4H]
                         if i >= MOM_4H and oi[i - MOM_4H] else None)
            oi_mom_24h = ((o_now - oi[i - MOM_24H]) / oi[i - MOM_24H]
                          if i >= MOM_24H and oi[i - MOM_24H] else None)
            liq_now = liq_map.get(ts[i], {"long": 0.0, "short": 0.0})
            long_liq = liq_now.get("long", 0.0)
            short_liq = liq_now.get("short", 0.0)
            total_liq = long_liq + short_liq
            out.append({
                "symbol": sym, "t": ts[i],
                "funding": f_now, "funding_z": _z(f_now, fwin),
                "oi": o_now, "oi_z": _z(o_now, owin), "oi_pct": _pct_rank(o_now, owin),
                "oi_mom_4h": oi_mom_4h, "oi_mom_24h": oi_mom_24h,
                "price_4h": price_4h, "price_24h": price_24h,
                "long_liq": long_liq, "short_liq": short_liq,
                "total_liq_z": _z(total_liq, lwin),
                "long_liq_ratio": (long_liq / total_liq) if total_liq > 0 else None,
                "short_liq_ratio": (short_liq / total_liq) if total_liq > 0 else None,
            })
        feats[sym] = out
    return feats


# Bucket specs: (name, hypothesis, direction, predicate(feat)->bool).
def _bucket_specs() -> list[tuple[str, str, int, Callable[[dict], bool]]]:
    def fz_ge(x):
        return lambda f: f["funding_z"] is not None and f["funding_z"] >= x
    def fz_le(x):
        return lambda f: f["funding_z"] is not None and f["funding_z"] <= x
    return [
        ("funding_pos__SHORT", "funding_rate>0 -> short", -1,
         lambda f: f["funding"] is not None and f["funding"] > 0),
        ("funding_neg__LONG", "funding_rate<0 -> long", 1,
         lambda f: f["funding"] is not None and f["funding"] < 0),
        ("crowded_longs_flush_z1__SHORT", "funding_z>=+1 -> short", -1, fz_ge(1.0)),
        ("crowded_longs_flush_z15__SHORT", "funding_z>=+1.5 -> short", -1, fz_ge(1.5)),
        ("crowded_shorts_squeeze_z1__LONG", "funding_z<=-1 -> long", 1, fz_le(-1.0)),
        ("crowded_shorts_squeeze_z15__LONG", "funding_z<=-1.5 -> long", 1, fz_le(-1.5)),
        ("oi_up_24h_price_up__SHORT", "OI up + price up exhaustion -> short", -1,
         lambda f: (f["oi_mom_24h"] or 0) > 0 and (f["price_24h"] or 0) > 0),
        ("oi_up_24h_price_down__SHORT", "OI up + price down breakdown -> short", -1,
         lambda f: (f["oi_mom_24h"] or 0) > 0 and (f["price_24h"] or 0) < 0),
        ("oi_up_24h_price_down__LONG", "OI up + price down squeeze risk -> long", 1,
         lambda f: (f["oi_mom_24h"] or 0) > 0 and (f["price_24h"] or 0) < 0),
        ("oi_z_ge_15__SHORT", "oi_z>=1.5 -> short", -1,
         lambda f: f["oi_z"] is not None and f["oi_z"] >= 1.5),
        ("oi_pct_ge_90__SHORT", "oi_percentile>=90 -> short", -1,
         lambda f: f["oi_pct"] is not None and f["oi_pct"] >= 90),
        ("long_liq_spike__SHORT_continuation", "long-liq spike -> short continuation", -1,
         lambda f: f["total_liq_z"] is not None and f["total_liq_z"] >= 1.5
         and (f["long_liq_ratio"] or 0) >= 0.6),
        ("long_liq_spike__LONG_bounce", "long-liq spike -> long bounce", 1,
         lambda f: f["total_liq_z"] is not None and f["total_liq_z"] >= 1.5
         and (f["long_liq_ratio"] or 0) >= 0.6),
        ("short_liq_spike__LONG_continuation", "short-liq spike -> long continuation", 1,
         lambda f: f["total_liq_z"] is not None and f["total_liq_z"] >= 1.5
         and (f["short_liq_ratio"] or 0) >= 0.6),
        ("short_liq_spike__SHORT_fade", "short-liq spike -> short fade", -1,
         lambda f: f["total_liq_z"] is not None and f["total_liq_z"] >= 1.5
         and (f["short_liq_ratio"] or 0) >= 0.6),
    ]


def _bucket_verdict(rep, *, symbol_scope: str = "ALL") -> tuple[str, str]:
    """Conservative verdict + the exact blocker.

    FIX-1: ``one_symbol_dominance`` is only a blocker for ``ALL`` scope. For
    a per-symbol bucket (``symbol_scope != "ALL"``) it is 1.0 BY
    CONSTRUCTION (every event is that symbol), so using it as a blocker is
    tautological — it is reported but not enforced. ``one_event_dominance``
    is always enforced."""
    ev = rep.matched_events
    net = rep.net_ev_pct
    if ev < WATCH_MIN_EVENTS:
        return STATUS_NEED_MORE, f"insufficient_events({ev}<{WATCH_MIN_EVENTS})"
    if net <= 0:
        return STATUS_REJECT, "net_ev_non_positive"
    if rep.edge_vs_baseline_pct <= 0:
        return STATUS_REJECT, "no_edge_vs_baseline"
    if rep.one_event_dominance >= MAX_EVENT_DOMINANCE:
        return STATUS_REJECT, "one_event_dominance"
    symbol_dom_block = (symbol_scope == "ALL"
                        and rep.one_symbol_dominance >= MAX_SYMBOL_DOMINANCE)
    if symbol_dom_block:
        return STATUS_REJECT, "one_symbol_dominance"
    if ev >= GREEN_MIN_EVENTS and rep.bootstrap_ci_low > 0:
        return STATUS_GREEN, ""
    if rep.bootstrap_ci_high > 0:
        return STATUS_WATCH, "ci_low<=0_watch_only"
    return STATUS_REJECT, "bootstrap_ci_high_non_positive"


def _net_by_horizon(rep) -> dict[float, float]:
    return {p["horizon_h"]: p["net_ev_pct"] for p in rep.per_horizon}


def run_funding_oi_diagnostics(
    market_clean: list[dict[str, Any]] | None,
    liq_clean: list[dict[str, Any]] | None,
    *,
    hours: int = 2160,
    cost: float = 0.0018,
    bootstrap_n: int = 1000,
    baseline_n: int = 400,
    seed: int = 7,
    per_symbol: bool = True,
) -> FundingOiDiagnosticsReport:
    report = FundingOiDiagnosticsReport(hours=int(hours), cost_pct=cost)
    market_rows = list(market_clean or [])
    report.market_rows = len(market_rows)
    report.liq_rows = len(list(liq_clean or []))
    if not market_rows:
        report.status = STATUS_NEED_DATA
        report.notes.append("no_perp_market_state_clean_rows")
        return report

    mbs = build_market_series(market_rows)
    report.symbols = sorted(mbs.keys())
    liq_by_symbol = aggregate_liquidations(liq_clean)
    feats = build_features(mbs, liq_by_symbol)

    specs = _bucket_specs()
    scopes = [("ALL", None)]
    if per_symbol:
        scopes += [(s, s) for s in report.symbols]

    results: list[BucketResult] = []
    for name, hyp, direction, pred in specs:
        for scope_label, scope_sym in scopes:
            events = []
            for sym, flist in feats.items():
                if scope_sym is not None and sym != scope_sym:
                    continue
                for fdict in flist:
                    try:
                        ok = pred(fdict)
                    except Exception:
                        ok = False
                    if ok:
                        events.append({"symbol": sym, "timestamp_ms": fdict["t"], "direction": direction})
            if not events:
                continue
            rep = run_event_study(
                events, mbs, module=f"{name}[{scope_label}]",
                horizons_h=DEFAULT_HORIZONS_H, primary_horizon_h=PRIMARY_HORIZON_H,
                cost=cost, bootstrap_n=bootstrap_n, baseline_n=baseline_n, seed=seed,
                hours=hours, lookback_bars_for_events=FUNDING_OI_LOOKBACK_BARS,
                min_events=WATCH_MIN_EVENTS,
            )
            nbh = _net_by_horizon(rep)
            status, blocker = _bucket_verdict(rep, symbol_scope=scope_label)
            br = BucketResult(
                name=name, hypothesis=hyp, direction=direction, symbol_scope=scope_label,
                event_count=rep.events_after_filter or rep.event_count,
                matched_events=rep.matched_events,
                net_ev_1h=nbh.get(1.0, 0.0), net_ev_4h=nbh.get(4.0, 0.0),
                net_ev_8h=nbh.get(8.0, 0.0), net_ev_24h=nbh.get(24.0, 0.0),
                gross_ev_pct=rep.gross_ev_pct, winrate=rep.winrate,
                baseline_net_ev_pct=rep.baseline_net_ev_pct,
                baseline_direction=rep.baseline_direction,
                edge_vs_baseline_pct=rep.edge_vs_baseline_pct,
                bootstrap_ci_low=rep.bootstrap_ci_low, bootstrap_ci_high=rep.bootstrap_ci_high,
                one_event_dominance=rep.one_event_dominance,
                one_symbol_dominance=rep.one_symbol_dominance,
                # one_symbol_dominance is informative for per-symbol buckets,
                # only a blocker for ALL scope (FIX-1).
                symbol_dominance_blocking=(scope_label == "ALL"
                                           and rep.one_symbol_dominance >= MAX_SYMBOL_DOMINANCE),
                avg_mfe_pct=rep.avg_mfe_pct, avg_mae_pct=rep.avg_mae_pct,
                median_time_to_tp_h=rep.median_time_to_tp_h,
                median_time_to_sl_h=rep.median_time_to_sl_h,
                exact_blocker=blocker,
            )
            br.status = status
            results.append(br)

    report.buckets_evaluated = len(results)
    report.buckets = [r.as_dict() for r in results]
    report.rejected_count = sum(1 for r in results if r.status == STATUS_REJECT)
    report.need_more_count = sum(1 for r in results if r.status == STATUS_NEED_MORE)
    report.watch_only = [f"{r.name}[{r.symbol_scope}]" for r in results if r.status == STATUS_WATCH]
    report.research_green = [f"{r.name}[{r.symbol_scope}]" for r in results if r.status == STATUS_GREEN]
    # Top buckets by 24h net EV (descriptive, not a recommendation).
    top = sorted(results, key=lambda r: r.net_ev_24h, reverse=True)[:10]
    report.top_by_net_ev_24h = [
        {"name": r.name, "scope": r.symbol_scope, "net_ev_24h": r.net_ev_24h,
         "matched_events": r.matched_events, "edge_vs_baseline_pct": r.edge_vs_baseline_pct,
         "bootstrap_ci_low": r.bootstrap_ci_low, "status": r.status}
        for r in top
    ]
    report.status = STATUS_OK_LABEL
    return report


# Auditable table columns (FIX-4).
TABLE_COLUMNS = [
    "bucket_id", "symbol_scope", "direction", "event_count", "matched_events",
    "net_ev_1h", "net_ev_4h", "net_ev_8h", "net_ev_24h",
    "baseline_ev_24h", "baseline_direction", "edge_vs_baseline_24h",
    "bootstrap_ci_low", "bootstrap_ci_high",
    "one_event_dominance", "one_symbol_dominance", "symbol_dominance_blocking",
    "exact_blocker", "status", "final_recommendation",
]


def diagnostics_table_rows(report: FundingOiDiagnosticsReport) -> list[dict[str, Any]]:
    """Flatten the report buckets into the auditable FIX-4 table rows."""
    rows: list[dict[str, Any]] = []
    for b in report.buckets:
        rows.append({
            "bucket_id": b.get("name", ""),
            "symbol_scope": b.get("symbol_scope", ""),
            "direction": "SHORT" if b.get("direction", 0) < 0 else "LONG",
            "event_count": b.get("event_count", 0),
            "matched_events": b.get("matched_events", 0),
            "net_ev_1h": b.get("net_ev_1h", 0.0),
            "net_ev_4h": b.get("net_ev_4h", 0.0),
            "net_ev_8h": b.get("net_ev_8h", 0.0),
            "net_ev_24h": b.get("net_ev_24h", 0.0),
            "baseline_ev_24h": b.get("baseline_net_ev_pct", 0.0),
            "baseline_direction": b.get("baseline_direction", ""),
            "edge_vs_baseline_24h": b.get("edge_vs_baseline_pct", 0.0),
            "bootstrap_ci_low": b.get("bootstrap_ci_low", 0.0),
            "bootstrap_ci_high": b.get("bootstrap_ci_high", 0.0),
            "one_event_dominance": b.get("one_event_dominance", 0.0),
            "one_symbol_dominance": b.get("one_symbol_dominance", 0.0),
            "symbol_dominance_blocking": str(bool(b.get("symbol_dominance_blocking", False))).lower(),
            "exact_blocker": b.get("exact_blocker", "") or "NONE",
            "status": b.get("status", ""),
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
        })
    return rows
