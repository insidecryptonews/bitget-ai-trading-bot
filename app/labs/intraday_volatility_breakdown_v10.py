"""ResearchOps V10 — Intraday Volatility Breakdown lab (research-only).

Studies the *compression -> expansion* family on REAL OHLCV: a market in
low-ATR compression that breaks the prior range, entered on the next bar
open. This is the antithesis of micro-scalping — it tries to capture a
few large intraday moves.

HARD CONTRACT — research only, NO lookahead:

- features at bar ``i`` use ONLY bars ``<= i``,
- entry executes at the OPEN of bar ``i+1`` (never the signal bar),
- ``STOP_BEFORE_TP`` same-bar: if a bar's range straddles both SL and TP,
  the SL is assumed hit first (conservative, never optimistic),
- MFE/MAE are diagnostics computed AFTER the fact — never an input,
- ``market_probe`` rows are irrelevant here (pure OHLCV),
- cost stress is always applied; gross-only never promotes,
- output never yields a paper/live candidate. Worst case label is
  ``RESEARCH_POCKET``.

Missing or stale OHLCV => ``NEED_DATA`` (never a fabricated result).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE
from .edge_data_foundation_v10 import _f, _parse_ts

DECISION_NEED_DATA = "NEED_MORE_DATA"
DECISION_REJECT = "REJECT"
DECISION_WATCH_ONLY = "WATCH_ONLY"
DECISION_RESEARCH_POCKET = "RESEARCH_POCKET"

EXIT_TP = "TP"
EXIT_SL = "SL"
EXIT_TIME = "TIME"

ATR_PERIOD = 14
# A rule needs at least this many trades before any non-NEED_DATA verdict.
MIN_TRADES = 40
# Net PF floor below which a rule is rejected.
MIN_NET_PF = 1.20
# Single-symbol concentration ceiling (fraction of trades from one symbol).
MAX_CONCENTRATION = 0.70

DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "LINKUSDT", "DOTUSDT"]
DEFAULT_TIMEFRAMES = ["5m", "15m"]
DEFAULT_HOLD_BARS = [5, 10, 15, 20, 30]
DEFAULT_TP_ATR = [0.5, 0.75, 1.0, 1.5]
DEFAULT_SL_ATR = [0.5, 0.75, 1.0]
DEFAULT_COST_STRESS = [0.18, 0.22, 0.25]

# Compression: ATR percentile (within the lookback window) at or below
# this is "compressed". Breakout lookback for the prior range.
COMPRESSION_PCT = 0.35
RANGE_LOOKBACK = 20
ATR_PCT_WINDOW = 50


@dataclass
class VolatilityRuleResult:
    rule_id: str = ""
    timeframe: str = ""
    hold_bars: int = 0
    tp_atr: float = 0.0
    sl_atr: float = 0.0
    cost_pct: float = 0.0
    trades: int = 0
    gross_ev_pct: float = 0.0
    net_ev_pct: float = 0.0
    net_pf: float = 0.0
    winrate: float = 0.0
    tp_rate: float = 0.0
    sl_rate: float = 0.0
    time_rate: float = 0.0
    avg_bars: float = 0.0
    max_drawdown_proxy_pct: float = 0.0
    concentration: float = 0.0
    top_symbol: str = ""
    fold_stability: float = 0.0
    cost_stress_status: str = ""
    decision: str = DECISION_NEED_DATA

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VolatilityBreakdownReport:
    hours: int = 168
    generated_at: str = ""
    symbols_requested: list[str] = field(default_factory=list)
    symbols_with_data: list[str] = field(default_factory=list)
    timeframe: str = ""
    bars_loaded: int = 0
    freshness_status: str = "UNKNOWN"
    data_quality_status: str = "NEED_DATA"
    rules_evaluated: int = 0
    best_rule: dict[str, Any] = field(default_factory=dict)
    top_rules: list[dict[str, Any]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    decision: str = DECISION_NEED_DATA
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# No-lookahead feature + simulation engine
# --------------------------------------------------------------------------


def _to_bars(rows: Iterable[dict[str, Any]] | None) -> list[dict[str, float]]:
    """Normalize + sort OHLCV rows ascending by timestamp. Drops rows with
    non-finite OHLC."""
    out: list[tuple[Any, dict[str, float]]] = []
    for r in rows or []:
        o = _f(r.get("open"))
        h = _f(r.get("high"))
        low = _f(r.get("low"))
        c = _f(r.get("close"))
        v = _f(r.get("volume")) or 0.0
        if None in (o, h, low, c):
            continue
        ts = _parse_ts(r.get("timestamp"))
        out.append((ts or datetime.min.replace(tzinfo=timezone.utc),
                    {"open": o, "high": h, "low": low, "close": c, "volume": v}))
    out.sort(key=lambda t: t[0])
    return [b for _, b in out]


def _atr_series(bars: list[dict[str, float]], period: int = ATR_PERIOD) -> list[float | None]:
    """Wilder-ish ATR as a rolling mean of true range. atr[i] uses bars
    up to and including i; None until enough history."""
    trs: list[float] = []
    atr: list[float | None] = []
    prev_close: float | None = None
    for b in bars:
        tr = b["high"] - b["low"]
        if prev_close is not None:
            tr = max(tr, abs(b["high"] - prev_close), abs(b["low"] - prev_close))
        trs.append(tr)
        if len(trs) >= period:
            atr.append(sum(trs[-period:]) / period)
        else:
            atr.append(None)
        prev_close = b["close"]
    return atr


def _percentile_rank(value: float, window: list[float]) -> float | None:
    if not window:
        return None
    below = sum(1 for x in window if x <= value)
    return below / len(window)


def simulate_rule_for_symbol(
    bars: list[dict[str, float]],
    *,
    hold_bars: int,
    tp_atr: float,
    sl_atr: float,
    cost_pct: float,
    range_lookback: int = RANGE_LOOKBACK,
    compression_pct: float = COMPRESSION_PCT,
    atr_pct_window: int = ATR_PCT_WINDOW,
) -> list[dict[str, Any]]:
    """Bar-by-bar, no-lookahead simulation for ONE symbol.

    Signal at bar ``i`` (compression + range breakout). Entry at the OPEN
    of ``i+1``. Exit by SL/TP within the hold window (SL checked before TP
    on the same bar), else TIME at the close of the last hold bar.
    """
    atr = _atr_series(bars)
    trades: list[dict[str, Any]] = []
    n = len(bars)
    warmup = max(ATR_PERIOD, range_lookback) + 1
    # i is the signal bar; need i+1 to exist for entry.
    for i in range(warmup, n - 1):
        a = atr[i]
        if a is None or a <= 0:
            continue
        # Compression: ATR percentile within the trailing window must be low.
        atr_hist = [x for x in atr[max(0, i - atr_pct_window + 1): i + 1] if x is not None]
        pct = _percentile_rank(a, atr_hist)
        if pct is None or pct > compression_pct:
            continue
        # Prior range (strictly before i): [i-range_lookback, i-1].
        prior = bars[i - range_lookback:i]
        if len(prior) < range_lookback:
            continue
        prior_high = max(b["high"] for b in prior)
        prior_low = min(b["low"] for b in prior)
        close_i = bars[i]["close"]
        direction = 0
        if close_i > prior_high:
            direction = 1  # breakout LONG
        elif close_i < prior_low:
            direction = -1  # breakdown SHORT
        if direction == 0:
            continue

        entry = bars[i + 1]["open"]
        if entry <= 0:
            continue
        if direction == 1:
            tp_price = entry + tp_atr * a
            sl_price = entry - sl_atr * a
        else:
            tp_price = entry - tp_atr * a
            sl_price = entry + sl_atr * a

        exit_type = EXIT_TIME
        exit_price = bars[min(i + hold_bars, n - 1)]["close"]
        bars_held = 0
        mfe = 0.0
        mae = 0.0
        for k in range(1, hold_bars + 1):
            j = i + k
            if j >= n:
                break
            bars_held = k
            hi = bars[j]["high"]
            lo = bars[j]["low"]
            # Diagnostic MFE/MAE (post-hoc only).
            if direction == 1:
                mfe = max(mfe, (hi - entry) / entry * 100.0)
                mae = min(mae, (lo - entry) / entry * 100.0)
                sl_hit = lo <= sl_price
                tp_hit = hi >= tp_price
            else:
                mfe = max(mfe, (entry - lo) / entry * 100.0)
                mae = min(mae, (entry - hi) / entry * 100.0)
                sl_hit = hi >= sl_price
                tp_hit = lo <= tp_price
            # STOP_BEFORE_TP: SL takes precedence on the same bar.
            if sl_hit:
                exit_type = EXIT_SL
                exit_price = sl_price
                break
            if tp_hit:
                exit_type = EXIT_TP
                exit_price = tp_price
                break
        else:
            exit_type = EXIT_TIME

        if bars_held == 0:
            continue
        gross = (exit_price - entry) / entry * 100.0 * direction
        net = gross - cost_pct
        trades.append({
            "exit_type": exit_type,
            "gross_pct": gross,
            "net_pct": net,
            "bars_held": bars_held,
            "direction": direction,
            "mfe_pct": round(mfe, 4),
            "mae_pct": round(mae, 4),
        })
    return trades


def _aggregate(
    trades_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    rule_id: str,
    timeframe: str,
    hold_bars: int,
    tp_atr: float,
    sl_atr: float,
    cost_pct: float,
) -> VolatilityRuleResult:
    all_trades = [t for ts in trades_by_symbol.values() for t in ts]
    res = VolatilityRuleResult(
        rule_id=rule_id, timeframe=timeframe, hold_bars=hold_bars,
        tp_atr=tp_atr, sl_atr=sl_atr, cost_pct=cost_pct,
    )
    n = len(all_trades)
    res.trades = n
    if n == 0:
        res.decision = DECISION_NEED_DATA
        return res
    nets = [t["net_pct"] for t in all_trades]
    grosses = [t["gross_pct"] for t in all_trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    res.gross_ev_pct = round(sum(grosses) / n, 4)
    res.net_ev_pct = round(sum(nets) / n, 4)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    res.net_pf = round(gross_win / gross_loss, 4) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)
    res.winrate = round(len(wins) / n, 4)
    res.tp_rate = round(sum(1 for t in all_trades if t["exit_type"] == EXIT_TP) / n, 4)
    res.sl_rate = round(sum(1 for t in all_trades if t["exit_type"] == EXIT_SL) / n, 4)
    res.time_rate = round(sum(1 for t in all_trades if t["exit_type"] == EXIT_TIME) / n, 4)
    res.avg_bars = round(sum(t["bars_held"] for t in all_trades) / n, 4)

    # Max drawdown proxy: worst running equity dip over the trade sequence.
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for x in nets:
        equity += x
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    res.max_drawdown_proxy_pct = round(max_dd, 4)

    # Concentration.
    counts = {s: len(ts) for s, ts in trades_by_symbol.items() if ts}
    if counts:
        top = max(counts.items(), key=lambda kv: kv[1])
        res.top_symbol = top[0]
        res.concentration = round(top[1] / n, 4)

    # Fold stability: split chronologically into 2 halves, compare net EV sign.
    half = n // 2
    if half >= 1:
        first = nets[:half]
        second = nets[half:]
        ev1 = sum(first) / len(first)
        ev2 = sum(second) / len(second)
        same_sign = (ev1 > 0) == (ev2 > 0)
        res.fold_stability = round(min(abs(ev1), abs(ev2)) / (abs(ev1) + abs(ev2) + 1e-9), 4) if same_sign else 0.0

    res.cost_stress_status = "PASS" if res.net_ev_pct > 0 else "FAIL"

    # Decision.
    if n < MIN_TRADES:
        res.decision = DECISION_NEED_DATA
    elif res.net_ev_pct <= 0 or res.net_pf < MIN_NET_PF:
        res.decision = DECISION_REJECT
    elif res.concentration > MAX_CONCENTRATION or res.fold_stability <= 0:
        res.decision = DECISION_WATCH_ONLY
    else:
        res.decision = DECISION_RESEARCH_POCKET
    return res


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


def _freshness_of(bars: list[dict[str, float]], rows: list[dict[str, Any]], *, timeframe: str) -> str:
    if not rows:
        return "UNKNOWN"
    last_ts = None
    for r in rows:
        ts = _parse_ts(r.get("timestamp"))
        if ts and (last_ts is None or ts > last_ts):
            last_ts = ts
    if last_ts is None:
        return "UNKNOWN"
    tf_minutes = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240}.get(timeframe.lower(), 15)
    age_min = (datetime.now(timezone.utc) - last_ts).total_seconds() / 60.0
    # Allow up to ~10 bars of staleness before flagging.
    return "FRESH" if age_min <= tf_minutes * 10 else "STALE"


def run_intraday_volatility_breakdown(
    db: Any,
    *,
    symbols: list[str] | None = None,
    timeframe: str = "5m",
    hours: int = 168,
    hold_bars: list[int] | None = None,
    tp_atr: list[float] | None = None,
    sl_atr: list[float] | None = None,
    cost_stress: list[float] | None = None,
    top_n: int = 10,
) -> VolatilityBreakdownReport:
    syms = [s.strip().upper() for s in (symbols or DEFAULT_SYMBOLS) if s.strip()]
    report = VolatilityBreakdownReport(
        hours=int(hours),
        generated_at=datetime.now(timezone.utc).isoformat(),
        symbols_requested=syms,
        timeframe=timeframe,
    )
    holds = hold_bars or DEFAULT_HOLD_BARS
    tps = tp_atr or DEFAULT_TP_ATR
    sls = sl_atr or DEFAULT_SL_ATR
    costs = cost_stress or DEFAULT_COST_STRESS

    from datetime import timedelta
    since_iso = (datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours)))).isoformat()

    bars_by_symbol: dict[str, list[dict[str, float]]] = {}
    raw_by_symbol: dict[str, list[dict[str, Any]]] = {}
    reader = getattr(db, "fetch_ohlcv_range", None)
    if not callable(reader):
        report.blockers = ["ohlcv_reader_unavailable"]
        report.decision = DECISION_NEED_DATA
        return report

    for sym in syms:
        try:
            rows = reader(sym, timeframe, since_iso=since_iso) or []
        except Exception:
            rows = []
        if rows:
            raw_by_symbol[sym] = rows
            bars_by_symbol[sym] = _to_bars(rows)

    report.symbols_with_data = sorted(b for b, v in bars_by_symbol.items() if v)
    report.bars_loaded = sum(len(v) for v in bars_by_symbol.values())

    if not report.bars_loaded:
        report.blockers = ["ohlcv_missing"]
        report.data_quality_status = "NEED_DATA"
        report.decision = DECISION_NEED_DATA
        return report

    # Freshness across symbols.
    fresh = [
        _freshness_of(bars_by_symbol[s], raw_by_symbol[s], timeframe=timeframe)
        for s in report.symbols_with_data
    ]
    report.freshness_status = (
        "FRESH" if any(f == "FRESH" for f in fresh)
        else ("STALE" if "STALE" in fresh else "UNKNOWN")
    )
    if report.freshness_status == "STALE":
        report.blockers.append("ohlcv_stale")
        report.data_quality_status = "WARNING"
        report.decision = DECISION_NEED_DATA
        return report
    report.data_quality_status = "OK"

    results: list[VolatilityRuleResult] = []
    for hold in holds:
        for tp in tps:
            for sl in sls:
                for cost in costs:
                    trades_by_symbol = {
                        s: simulate_rule_for_symbol(
                            bars_by_symbol[s], hold_bars=hold, tp_atr=tp,
                            sl_atr=sl, cost_pct=cost,
                        )
                        for s in report.symbols_with_data
                    }
                    rule_id = f"{timeframe}|hold={hold}|tp={tp}|sl={sl}|cost={cost}"
                    results.append(_aggregate(
                        trades_by_symbol, rule_id=rule_id, timeframe=timeframe,
                        hold_bars=hold, tp_atr=tp, sl_atr=sl, cost_pct=cost,
                    ))
    report.rules_evaluated = len(results)

    rank = {DECISION_RESEARCH_POCKET: 3, DECISION_WATCH_ONLY: 2, DECISION_REJECT: 1, DECISION_NEED_DATA: 0}
    results.sort(key=lambda r: (rank.get(r.decision, 0), r.net_ev_pct, r.net_pf), reverse=True)
    report.top_rules = [r.as_dict() for r in results[:max(1, int(top_n))]]
    if results:
        best = results[0]
        report.best_rule = best.as_dict()
        report.decision = best.decision
    else:
        report.decision = DECISION_NEED_DATA
    return report
