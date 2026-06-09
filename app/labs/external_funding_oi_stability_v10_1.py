"""ResearchOps V10.1 — Funding/OI/Liquidation STABILITY + OOS validator.

The diagnostics step surfaced candidate sub-buckets (e.g. ETH SHORT on
crowded longs / extreme OI). This module is the honest next gate: it does
NOT change any threshold or bucket definition; it stress-tests the
EXISTING buckets for temporal robustness before anything could ever be
considered for shadow research.

Per bucket it computes:

- temporal split: first half vs second half (+ thirds for regime check),
- cost stress: x1 (0.0018), x2, x3 — must survive x2 to be green,
- horizon stability: 1h/4h/8h/24h (flags 24h-only edges),
- missing-OI risk: flags OI-based buckets when missing_oi > 10%,
- a conservative verdict: STABILITY_GREEN / WATCH_ONLY / OOS_FAIL /
  COST_FAIL / REGIME_UNSTABLE / MISSING_OI_RISK / NEED_MORE_DATA / REJECT.

It reuses the audited, no-lookahead event study and the existing bucket
definitions. HARD CONTRACT — research only: no orders, no private
endpoints, no DB writes, no runtime touched, no threshold optimisation,
no forcing of green. The ceiling is research; never paper/live.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from .external_event_study_v10_1 import DEFAULT_HORIZONS_H, run_event_study
from .external_funding_oi_diagnostics_v10_1 import (
    MAX_EVENT_DOMINANCE,
    PRIMARY_HORIZON_H,
    _bucket_verdict,
    _bucket_specs,
    aggregate_liquidations,
    build_features,
)
from .external_event_study_v10_1 import build_market_series

# Status vocabulary.
STATUS_NEED_DATA = "NEED_DATA"
STATUS_NEED_MORE = "NEED_MORE_DATA"
STATUS_REJECT = "REJECT"
STATUS_WATCH = "WATCH_ONLY"
STATUS_GREEN = "STABILITY_GREEN"
STATUS_OOS_FAIL = "OOS_FAIL"
STATUS_COST_FAIL = "COST_FAIL"
STATUS_REGIME_UNSTABLE = "REGIME_UNSTABLE"
STATUS_MISSING_OI = "MISSING_OI_RISK"
STATUS_OK_LABEL = "OK"

COST_X1 = 0.0018
MISSING_OI_THRESHOLD = 0.10
MS_PER_HOUR = 3_600_000.0

# Conservative green gates.
GREEN_TOTAL_MATCHED = 150
GREEN_SPLIT_MATCHED = 40
NEED_MORE_SPLIT_MATCHED = 20

# Buckets to validate (name, symbol_scope). Names map to _bucket_specs().
TARGET_BUCKETS: list[tuple[str, str]] = [
    ("crowded_longs_flush_z15__SHORT", "ETHUSDT"),
    ("crowded_longs_flush_z1__SHORT", "ETHUSDT"),
    ("oi_z_ge_15__SHORT", "ETHUSDT"),
    ("oi_pct_ge_90__SHORT", "ETHUSDT"),
    ("oi_up_24h_price_down__SHORT", "ETHUSDT"),
    ("funding_pos__SHORT", "ETHUSDT"),
    ("crowded_longs_flush_z15__SHORT", "ALL"),
    ("crowded_longs_flush_z1__SHORT", "ALL"),
    ("oi_up_24h_price_down__SHORT", "ALL"),
    ("funding_pos__SHORT", "ALL"),
    ("funding_neg__LONG", "ALL"),  # control — not assumed actionable
]

# Buckets whose definition depends on OI (subject to missing-OI risk).
OI_BASED = frozenset({
    "oi_z_ge_15__SHORT", "oi_pct_ge_90__SHORT", "oi_up_24h_price_down__SHORT",
})


@dataclass
class StabilityResult:
    bucket_id: str = ""
    symbol_scope: str = ""
    direction: str = ""
    oi_based: bool = False
    total_matched: int = 0
    total_status: str = ""
    total_blocker: str = ""
    total_net_ev_24h: float = 0.0
    total_ci_low: float = 0.0
    total_ci_high: float = 0.0
    one_event_dominance: float = 0.0
    first_half_matched: int = 0
    second_half_matched: int = 0
    first_half_net_ev_24h: float = 0.0
    second_half_net_ev_24h: float = 0.0
    first_half_edge_vs_baseline_24h: float = 0.0
    second_half_edge_vs_baseline_24h: float = 0.0
    thirds_net_ev_24h: list[float] = field(default_factory=list)
    thirds_matched: list[int] = field(default_factory=list)
    cost_x1_net_ev_24h: float = 0.0
    cost_x2_net_ev_24h: float = 0.0
    cost_x3_net_ev_24h: float = 0.0
    horizon_1h_net: float = 0.0
    horizon_4h_net: float = 0.0
    horizon_8h_net: float = 0.0
    horizon_24h_net: float = 0.0
    horizon_risk: bool = False
    missing_oi_ratio: float = 0.0
    missing_oi_risk: bool = False
    regime_unstable: bool = False
    stability_status: str = STATUS_NEED_MORE
    stability_blocker: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StabilityReport:
    hours: int = 2160
    cost_x1: float = COST_X1
    symbols: list[str] = field(default_factory=list)
    market_rows: int = 0
    liq_rows: int = 0
    missing_oi_ratio: float = 0.0
    buckets: list[dict[str, Any]] = field(default_factory=list)
    stability_green: list[str] = field(default_factory=list)
    watch_only: list[str] = field(default_factory=list)
    next_research_decision: dict[str, Any] = field(default_factory=dict)
    status: str = STATUS_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    paper_ready: bool = False
    live_ready: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def collect_bucket_events(feats, spec, scope: str) -> list[dict[str, Any]]:
    """Collect events for a bucket using the EXISTING predicate (no
    threshold change). ``scope`` restricts to a symbol or 'ALL'."""
    _name, _hyp, direction, pred = spec
    events: list[dict[str, Any]] = []
    for sym, flist in feats.items():
        if scope != "ALL" and sym != scope:
            continue
        for fdict in flist:
            try:
                ok = pred(fdict)
            except Exception:
                ok = False
            if ok:
                events.append({"symbol": sym, "timestamp_ms": fdict["t"], "direction": direction})
    return events


def _run(events, mbs, cost, *, bootstrap_n, baseline_n, seed):
    if not events:
        return None
    return run_event_study(
        events, mbs, horizons_h=DEFAULT_HORIZONS_H, primary_horizon_h=PRIMARY_HORIZON_H,
        cost=cost, bootstrap_n=bootstrap_n, baseline_n=baseline_n, seed=seed,
        hours=None, min_events=1,
    )


def _net24(rep) -> float:
    if rep is None:
        return 0.0
    for p in rep.per_horizon:
        if abs(p["horizon_h"] - 24.0) < 1e-9:
            return p["net_ev_pct"]
    return rep.net_ev_pct


def _matched(rep) -> int:
    return 0 if rep is None else rep.matched_events


def _stability_verdict(m: dict[str, Any], *, oi_based: bool) -> tuple[str, str]:
    """Conservative verdict from computed metrics. Pure + testable."""
    tm = m["total_matched"]
    if tm < 50 or m["first_half_matched"] < NEED_MORE_SPLIT_MATCHED or m["second_half_matched"] < NEED_MORE_SPLIT_MATCHED:
        return STATUS_NEED_MORE, "insufficient_matched_or_split_sample"
    if m["total_net_24h"] <= 0:
        return STATUS_REJECT, "total_net_ev_non_positive"
    if m["cost_x1_net_24h"] > 0 and m["cost_x2_net_24h"] <= 0:
        return STATUS_COST_FAIL, "cost_x2_non_positive"
    fh, sh = m["first_half_net_24h"], m["second_half_net_24h"]
    if (fh > 0) != (sh > 0):
        return STATUS_OOS_FAIL, "net_ev_sign_flip_between_halves"
    fhe, she = m["first_half_edge_24h"], m["second_half_edge_24h"]
    if (fhe > 0) != (she > 0):
        return STATUS_OOS_FAIL, "edge_vs_baseline_sign_flip_between_halves"
    if fh <= 0 and sh <= 0:
        return STATUS_REJECT, "both_halves_non_positive"
    if m["regime_unstable"]:
        return STATUS_REGIME_UNSTABLE, "edge_concentrated_in_single_third"
    if oi_based and m["missing_oi_risk"]:
        return STATUS_MISSING_OI, "oi_based_bucket_missing_oi_gt_10pct"
    green = (
        tm >= GREEN_TOTAL_MATCHED
        and m["first_half_matched"] >= GREEN_SPLIT_MATCHED
        and m["second_half_matched"] >= GREEN_SPLIT_MATCHED
        and fh > 0 and sh > 0 and fhe > 0 and she > 0
        and m["total_ci_low"] > 0
        and m["cost_x2_net_24h"] > 0
        and m["one_event_dominance"] < MAX_EVENT_DOMINANCE
        and (m["total_blocker"] in ("", "NONE", None))
    )
    if green and m["horizon_risk"]:
        return STATUS_WATCH, "horizon_risk_only_24h_no_promote"
    if green:
        return STATUS_GREEN, "NONE"
    return STATUS_WATCH, "positive_but_below_green_criteria"


def run_funding_oi_stability(
    market_clean: list[dict[str, Any]] | None,
    liq_clean: list[dict[str, Any]] | None,
    *,
    hours: int = 2160,
    missing_oi_ratio: float = 0.0,
    missing_oi_by_symbol: dict[str, float] | None = None,
    bootstrap_n: int = 600,
    baseline_n: int = 300,
    seed: int = 7,
) -> StabilityReport:
    report = StabilityReport(hours=int(hours), missing_oi_ratio=round(missing_oi_ratio, 4))
    rows = list(market_clean or [])
    report.market_rows = len(rows)
    report.liq_rows = len(list(liq_clean or []))
    if not rows:
        report.status = STATUS_NEED_DATA
        report.next_research_decision = {
            "recommendation": "No external market data available; collect data first.",
            "suggested_next_code_prompt_type": "FIX_MISSING_OI",
            "max_label": "SHADOW_RESEARCH_ONLY_FUTURE",
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
        }
        return report

    mbs = build_market_series(rows)
    report.symbols = sorted(mbs.keys())
    feats = build_features(mbs, aggregate_liquidations(liq_clean))
    spec_by_name = {s[0]: s for s in _bucket_specs()}
    by_sym = missing_oi_by_symbol or {}

    all_ts = sorted(t for s in mbs.values() for t in s["ts"])
    max_ts = all_ts[-1]
    cutoff = max_ts - int(hours) * MS_PER_HOUR

    results: list[StabilityResult] = []
    for name, scope in TARGET_BUCKETS:
        spec = spec_by_name.get(name)
        if spec is None:
            continue
        events = [e for e in collect_bucket_events(feats, spec, scope)
                  if e["timestamp_ms"] >= cutoff]
        oi_based = name in OI_BASED
        miss = by_sym.get(scope, missing_oi_ratio) if scope != "ALL" else missing_oi_ratio
        res = StabilityResult(
            bucket_id=name, symbol_scope=scope,
            direction="SHORT" if spec[2] < 0 else "LONG",
            oi_based=oi_based, missing_oi_ratio=round(miss, 4),
            missing_oi_risk=bool(oi_based and miss > MISSING_OI_THRESHOLD),
        )
        if not events:
            res.stability_status = STATUS_NEED_MORE
            res.stability_blocker = "no_events_in_window"
            results.append(res)
            continue

        ev_ts = [e["timestamp_ms"] for e in events]
        lo, hi = min(ev_ts), max(ev_ts)
        span = max(hi - lo, 1)
        mid = lo + span / 2
        first = [e for e in events if e["timestamp_ms"] < mid]
        second = [e for e in events if e["timestamp_ms"] >= mid]
        thirds = [
            [e for e in events if lo + k * span / 3 <= e["timestamp_ms"] < lo + (k + 1) * span / 3
             or (k == 2 and e["timestamp_ms"] >= lo + 2 * span / 3)]
            for k in range(3)
        ]

        total = _run(events, mbs, COST_X1, bootstrap_n=bootstrap_n, baseline_n=baseline_n, seed=seed)
        fh = _run(first, mbs, COST_X1, bootstrap_n=max(150, bootstrap_n // 3), baseline_n=max(100, baseline_n // 3), seed=seed)
        sh = _run(second, mbs, COST_X1, bootstrap_n=max(150, bootstrap_n // 3), baseline_n=max(100, baseline_n // 3), seed=seed)
        third_reps = [_run(t, mbs, COST_X1, bootstrap_n=120, baseline_n=80, seed=seed) for t in thirds]
        cost2 = _run(events, mbs, COST_X1 * 2, bootstrap_n=200, baseline_n=120, seed=seed)
        cost3 = _run(events, mbs, COST_X1 * 3, bootstrap_n=200, baseline_n=120, seed=seed)

        # Horizon stability from the total run.
        nbh = {p["horizon_h"]: p["net_ev_pct"] for p in (total.per_horizon if total else [])}
        res.horizon_1h_net = nbh.get(1.0, 0.0)
        res.horizon_4h_net = nbh.get(4.0, 0.0)
        res.horizon_8h_net = nbh.get(8.0, 0.0)
        res.horizon_24h_net = nbh.get(24.0, 0.0)
        res.horizon_risk = bool(
            res.horizon_24h_net > 0
            and res.horizon_1h_net <= 0 and res.horizon_4h_net <= 0 and res.horizon_8h_net <= 0
        )

        res.total_matched = _matched(total)
        res.total_net_ev_24h = _net24(total)
        res.total_ci_low = total.bootstrap_ci_low if total else 0.0
        res.total_ci_high = total.bootstrap_ci_high if total else 0.0
        res.one_event_dominance = total.one_event_dominance if total else 0.0
        if total is not None:
            tstatus, tblocker = _bucket_verdict(total, symbol_scope=scope)
            res.total_status, res.total_blocker = tstatus, (tblocker or "NONE")
        res.first_half_matched = _matched(fh)
        res.second_half_matched = _matched(sh)
        res.first_half_net_ev_24h = _net24(fh)
        res.second_half_net_ev_24h = _net24(sh)
        res.first_half_edge_vs_baseline_24h = fh.edge_vs_baseline_pct if fh else 0.0
        res.second_half_edge_vs_baseline_24h = sh.edge_vs_baseline_pct if sh else 0.0
        res.thirds_net_ev_24h = [round(_net24(t), 4) for t in third_reps]
        res.thirds_matched = [_matched(t) for t in third_reps]
        res.cost_x1_net_ev_24h = res.total_net_ev_24h
        res.cost_x2_net_ev_24h = _net24(cost2)
        res.cost_x3_net_ev_24h = _net24(cost3)

        # Regime concentration: among thirds with >=20 matched, if only one
        # has net_ev_24h > 0 (out of >=2 evaluable), the edge is concentrated.
        evaluable = [(n24, mt) for n24, mt in zip(res.thirds_net_ev_24h, res.thirds_matched) if mt >= 20]
        pos = sum(1 for n24, _ in evaluable if n24 > 0)
        res.regime_unstable = bool(len(evaluable) >= 2 and pos == 1)

        metrics = {
            "total_matched": res.total_matched,
            "first_half_matched": res.first_half_matched,
            "second_half_matched": res.second_half_matched,
            "total_net_24h": res.total_net_ev_24h,
            "first_half_net_24h": res.first_half_net_ev_24h,
            "second_half_net_24h": res.second_half_net_ev_24h,
            "first_half_edge_24h": res.first_half_edge_vs_baseline_24h,
            "second_half_edge_24h": res.second_half_edge_vs_baseline_24h,
            "total_ci_low": res.total_ci_low,
            "cost_x1_net_24h": res.cost_x1_net_ev_24h,
            "cost_x2_net_24h": res.cost_x2_net_ev_24h,
            "one_event_dominance": res.one_event_dominance,
            "total_blocker": res.total_blocker,
            "missing_oi_risk": res.missing_oi_risk,
            "regime_unstable": res.regime_unstable,
            "horizon_risk": res.horizon_risk,
        }
        res.stability_status, res.stability_blocker = _stability_verdict(metrics, oi_based=oi_based)
        results.append(res)

    report.buckets = [r.as_dict() for r in results]
    report.stability_green = [f"{r.bucket_id}[{r.symbol_scope}]" for r in results if r.stability_status == STATUS_GREEN]
    report.watch_only = [f"{r.bucket_id}[{r.symbol_scope}]" for r in results if r.stability_status == STATUS_WATCH]
    report.next_research_decision = build_next_research_decision(results)
    report.status = STATUS_OK_LABEL
    return report


def build_next_research_decision(results: list[StabilityResult]) -> dict[str, Any]:
    """Recommend the next research step. Recommends ONLY; implements nothing.
    Never recommends live or paper filter; ceiling is SHADOW_RESEARCH_ONLY_FUTURE."""
    greens = [r for r in results if r.stability_status == STATUS_GREEN]
    missing_oi_blocked = [r for r in results if r.stability_status == STATUS_MISSING_OI]
    watch = [r for r in results if r.stability_status == STATUS_WATCH]

    def _scope_set(rs):
        return {r.symbol_scope for r in rs}

    decision = {
        "any_stability_green": bool(greens),
        "best_candidate": "",
        "eth_specific_candidate": False,
        "horizon_risk_on_best": False,
        "missing_oi_risk_on_best": False,
        "cost_x2_block": False,
        "recommendation": "",
        "suggested_next_code_prompt_type": "PIVOT_TO_UNLOCKS",
        "max_label": "SHADOW_RESEARCH_ONLY_FUTURE",
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }

    if greens:
        best = max(greens, key=lambda r: r.total_net_ev_24h)
        decision["best_candidate"] = f"{best.bucket_id}[{best.symbol_scope}]"
        decision["horizon_risk_on_best"] = best.horizon_risk
        # green requires cost x2 > 0, so no cost block here
        scopes = _scope_set(greens)
        decision["eth_specific_candidate"] = (scopes == {"ETHUSDT"} or scopes <= {"ETHUSDT"})
        decision["suggested_next_code_prompt_type"] = "EXTEND_HISTORY"
        decision["recommendation"] = (
            "At least one bucket is STABILITY_GREEN without critical blockers. "
            "Recommend EXTENDING history (BTC/ETH 180-365 days) and repeating the "
            "stability gate before ANY shadow research. Do not promote to paper/live."
        )
        return decision

    if missing_oi_blocked:
        best = max(missing_oi_blocked, key=lambda r: r.total_net_ev_24h)
        decision["best_candidate"] = f"{best.bucket_id}[{best.symbol_scope}]"
        decision["missing_oi_risk_on_best"] = True
        decision["eth_specific_candidate"] = (best.symbol_scope == "ETHUSDT")
        decision["suggested_next_code_prompt_type"] = "FIX_MISSING_OI"
        decision["recommendation"] = (
            "The strongest candidate is an OI-based bucket blocked by missing OI "
            "(>10%). Recommend FIXING/REDUCING missing OI (re-ingest / time-window "
            "features) before judging this family. No promotion."
        )
        return decision

    if watch:
        best = max(watch, key=lambda r: r.total_net_ev_24h)
        decision["best_candidate"] = f"{best.bucket_id}[{best.symbol_scope}]"
        decision["horizon_risk_on_best"] = best.horizon_risk
        decision["eth_specific_candidate"] = (_scope_set(watch) <= {"ETHUSDT"})
        decision["suggested_next_code_prompt_type"] = "EXTEND_HISTORY"
        decision["recommendation"] = (
            "No STABILITY_GREEN, but reasonable WATCH_ONLY candidates exist. "
            "Recommend EXTENDING history cautiously and re-running stability. "
            "Do not promote."
        )
        return decision

    # Nothing survived: pivot.
    decision["suggested_next_code_prompt_type"] = "PIVOT_TO_UNLOCKS"
    decision["recommendation"] = (
        "No bucket survived the stability/OOS/cost gates. Recommend rejecting the "
        "funding/OI family for BTC/ETH 1h and pivoting (e.g. token unlocks or "
        "intraday volatility breakdown). No promotion."
    )
    return decision


# Auditable table columns.
STABILITY_TABLE_COLUMNS = [
    "bucket_id", "symbol_scope", "direction", "total_matched", "total_status",
    "stability_status", "first_half_matched", "second_half_matched",
    "first_half_net_ev_24h", "second_half_net_ev_24h",
    "first_half_edge_vs_baseline_24h", "second_half_edge_vs_baseline_24h",
    "cost_x1_net_ev_24h", "cost_x2_net_ev_24h", "cost_x3_net_ev_24h",
    "horizon_1h_net", "horizon_4h_net", "horizon_8h_net", "horizon_24h_net",
    "horizon_risk", "missing_oi_risk", "regime_unstable", "one_event_dominance",
    "exact_blocker", "stability_blocker", "final_recommendation",
]


def stability_table_rows(report: StabilityReport) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for b in report.buckets:
        rows.append({
            "bucket_id": b.get("bucket_id", ""),
            "symbol_scope": b.get("symbol_scope", ""),
            "direction": b.get("direction", ""),
            "total_matched": b.get("total_matched", 0),
            "total_status": b.get("total_status", ""),
            "stability_status": b.get("stability_status", ""),
            "first_half_matched": b.get("first_half_matched", 0),
            "second_half_matched": b.get("second_half_matched", 0),
            "first_half_net_ev_24h": b.get("first_half_net_ev_24h", 0.0),
            "second_half_net_ev_24h": b.get("second_half_net_ev_24h", 0.0),
            "first_half_edge_vs_baseline_24h": b.get("first_half_edge_vs_baseline_24h", 0.0),
            "second_half_edge_vs_baseline_24h": b.get("second_half_edge_vs_baseline_24h", 0.0),
            "cost_x1_net_ev_24h": b.get("cost_x1_net_ev_24h", 0.0),
            "cost_x2_net_ev_24h": b.get("cost_x2_net_ev_24h", 0.0),
            "cost_x3_net_ev_24h": b.get("cost_x3_net_ev_24h", 0.0),
            "horizon_1h_net": b.get("horizon_1h_net", 0.0),
            "horizon_4h_net": b.get("horizon_4h_net", 0.0),
            "horizon_8h_net": b.get("horizon_8h_net", 0.0),
            "horizon_24h_net": b.get("horizon_24h_net", 0.0),
            "horizon_risk": str(bool(b.get("horizon_risk", False))).lower(),
            "missing_oi_risk": str(bool(b.get("missing_oi_risk", False))).lower(),
            "regime_unstable": str(bool(b.get("regime_unstable", False))).lower(),
            "one_event_dominance": b.get("one_event_dominance", 0.0),
            "exact_blocker": b.get("total_blocker", "") or "NONE",
            "stability_blocker": b.get("stability_blocker", "") or "NONE",
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
        })
    return rows
