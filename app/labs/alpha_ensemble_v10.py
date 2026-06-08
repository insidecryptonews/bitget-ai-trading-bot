"""ResearchOps V10 — Alpha Ensemble (research-only meta-strategy + backtester).

This is the concrete *algorithm*: an ensemble that combines several
low-correlation, asymmetric (R:R >= 1.5), cost-aware sub-strategies and
backtests them with NO lookahead, cost stress, regime gating and a
chronological out-of-sample split. It implements the thesis proven by
the project's Monte-Carlo work: consistency comes from a PORTFOLIO of
weak-but-positive, uncorrelated edges — not from frequent tiny TPs.

Sub-strategies (all ex-ante, no lookahead):

1. Donchian trend breakout, REGIME-GATED (long only in TREND_UP, short
   only in TREND_DOWN). External evidence: a context filter ~3x the
   per-trade expectancy with ~6x fewer trades.
2. Mean reversion (z-score fade) — only in RANGE regime. Post-2021 the
   mean-reversion edge tends to dominate pure momentum in crypto.
3. Cross-sectional momentum — long the strongest / short the weakest of
   the basket at each weekly rebalance.

HARD CONTRACT — research only:

- never opens orders, never calls private endpoints, never writes DB,
- entry is ALWAYS the next bar open; SL is checked before TP same-bar,
- MFE/MAE are diagnostics, never inputs,
- every cost-stress level must be cleared; gross-only never promotes,
- the verdict ceiling is ``SHADOW_CANDIDATE_RESEARCH_ONLY`` — the report
  never returns paper-ready or live-ready, and always NO LIVE.

It is an analysis tool. It does NOT trade.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE
from .edge_data_foundation_v10 import _f, _parse_ts
from .intraday_volatility_breakdown_v10 import _atr_series, _to_bars

# Regimes.
REGIME_UP = "TREND_UP"
REGIME_DOWN = "TREND_DOWN"
REGIME_RANGE = "RANGE"

# Exits.
EXIT_TP = "TP"
EXIT_SL = "SL"
EXIT_TIME = "TIME"

# Strategy ids.
STRAT_TREND = "donchian_trend"
STRAT_MEANREV = "mean_reversion"
STRAT_XMOM = "cross_sectional_momentum"

# Verdict ceiling — never above this.
VERDICT_NEED_DATA = "NEED_MORE_DATA"
VERDICT_REJECT = "REJECT"
VERDICT_WATCH = "WATCH_ONLY"
VERDICT_SHADOW = "SHADOW_CANDIDATE_RESEARCH_ONLY"

# Gates.
MIN_TRADES = 40
MIN_OOS_TRADES = 15
MIN_NET_PF = 1.20
MIN_SHARPE = 0.8
MAX_CONCENTRATION = 0.70

DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "LINKUSDT", "DOTUSDT"]
DEFAULT_COSTS = [0.0018, 0.0022, 0.0025]  # round-trip fractions

# Sub-strategy params (fixed rules; no fitting => no overfit knobs).
DONCHIAN_N = 20
EMA_FAST = 20
EMA_SLOW = 50
ATR_PERIOD = 14
TREND_TP_ATR = 2.0
TREND_SL_ATR = 1.0
TREND_HOLD = 24
MR_Z_LOOKBACK = 20
MR_Z_ENTRY = 2.0
MR_TP_ATR = 1.5
MR_SL_ATR = 1.0
MR_HOLD = 12
XMOM_LOOKBACK_BARS = 96    # ~ momentum window
XMOM_HOLD = 48
XMOM_TP_ATR = 2.0
XMOM_SL_ATR = 1.0
RISK_FRAC = 0.005          # 0.5% equity risked at SL


# --------------------------------------------------------------------------
# Features (no lookahead)
# --------------------------------------------------------------------------


def _ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def classify_regimes(bars: list[dict[str, float]]) -> list[str]:
    """Regime at bar i from data <= i. EMA-fast vs EMA-slow + price side."""
    closes = [b["close"] for b in bars]
    ef = _ema_series(closes, EMA_FAST)
    es = _ema_series(closes, EMA_SLOW)
    out: list[str] = []
    for i in range(len(bars)):
        if i < EMA_SLOW:
            out.append(REGIME_RANGE)
            continue
        c = closes[i]
        if c > es[i] and ef[i] > es[i]:
            out.append(REGIME_UP)
        elif c < es[i] and ef[i] < es[i]:
            out.append(REGIME_DOWN)
        else:
            out.append(REGIME_RANGE)
    return out


def _simulate_exit(bars, i, entry, direction, atr, *, tp_atr, sl_atr, hold):
    """Walk forward from entry (open of i+1) applying SL-before-TP."""
    n = len(bars)
    if direction == 1:
        tp_price = entry + tp_atr * atr
        sl_price = entry - sl_atr * atr
    else:
        tp_price = entry - tp_atr * atr
        sl_price = entry + sl_atr * atr
    exit_type = EXIT_TIME
    exit_price = bars[min(i + hold, n - 1)]["close"]
    bars_held = 0
    for k in range(1, hold + 1):
        j = i + k
        if j >= n:
            break
        bars_held = k
        hi, lo = bars[j]["high"], bars[j]["low"]
        if direction == 1:
            sl_hit, tp_hit = lo <= sl_price, hi >= tp_price
        else:
            sl_hit, tp_hit = hi >= sl_price, lo <= tp_price
        if sl_hit:
            exit_type, exit_price = EXIT_SL, sl_price
            break
        if tp_hit:
            exit_type, exit_price = EXIT_TP, tp_price
            break
    else:
        exit_type = EXIT_TIME
    return exit_type, exit_price, bars_held


def _make_trade(bars, i, direction, atr, *, tp_atr, sl_atr, hold, cost, strat, symbol):
    entry = bars[i + 1]["open"]
    if entry <= 0 or atr is None or atr <= 0:
        return None
    exit_type, exit_price, bars_held = _simulate_exit(
        bars, i, entry, direction, atr, tp_atr=tp_atr, sl_atr=sl_atr, hold=hold)
    if bars_held == 0:
        return None
    gross = (exit_price - entry) / entry * 100.0 * direction
    net = gross - cost * 100.0
    sl_pct = sl_atr * atr / entry * 100.0
    risk_unit = sl_pct + cost * 100.0  # intended net loss magnitude (%)
    r_multiple = net / risk_unit if risk_unit > 0 else 0.0
    return {
        "strategy": strat, "symbol": symbol, "direction": direction,
        "entry_ts": bars[i + 1]["ts"], "exit_type": exit_type,
        "gross_pct": gross, "net_pct": net, "r_multiple": r_multiple,
        "bars_held": bars_held, "risk_unit_pct": risk_unit,
    }


# --------------------------------------------------------------------------
# Sub-strategies (per symbol)
# --------------------------------------------------------------------------


def donchian_trend_trades(bars, *, cost, symbol, n=DONCHIAN_N):
    atr = _atr_series(bars, ATR_PERIOD)
    regimes = classify_regimes(bars)
    trades = []
    warmup = max(EMA_SLOW, n, ATR_PERIOD) + 1
    for i in range(warmup, len(bars) - 1):
        a = atr[i]
        if a is None or a <= 0:
            continue
        prior = bars[i - n:i]
        if len(prior) < n:
            continue
        ph = max(b["high"] for b in prior)
        pl = min(b["low"] for b in prior)
        c = bars[i]["close"]
        reg = regimes[i]
        direction = 0
        if c > ph and reg == REGIME_UP:
            direction = 1
        elif c < pl and reg == REGIME_DOWN:
            direction = -1
        if direction == 0:
            continue
        t = _make_trade(bars, i, direction, a, tp_atr=TREND_TP_ATR,
                        sl_atr=TREND_SL_ATR, hold=TREND_HOLD, cost=cost,
                        strat=STRAT_TREND, symbol=symbol)
        if t:
            trades.append(t)
    return trades


def mean_reversion_trades(bars, *, cost, symbol):
    atr = _atr_series(bars, ATR_PERIOD)
    regimes = classify_regimes(bars)
    closes = [b["close"] for b in bars]
    trades = []
    warmup = max(EMA_SLOW, MR_Z_LOOKBACK, ATR_PERIOD) + 1
    for i in range(warmup, len(bars) - 1):
        a = atr[i]
        if a is None or a <= 0:
            continue
        if regimes[i] != REGIME_RANGE:
            continue  # fade only in range
        window = closes[i - MR_Z_LOOKBACK + 1:i + 1]
        mu = statistics.fmean(window)
        sd = statistics.pstdev(window)
        if sd <= 0:
            continue
        z = (closes[i] - mu) / sd
        direction = 0
        if z >= MR_Z_ENTRY:
            direction = -1  # overextended up -> fade short
        elif z <= -MR_Z_ENTRY:
            direction = 1   # overextended down -> fade long
        if direction == 0:
            continue
        t = _make_trade(bars, i, direction, a, tp_atr=MR_TP_ATR,
                        sl_atr=MR_SL_ATR, hold=MR_HOLD, cost=cost,
                        strat=STRAT_MEANREV, symbol=symbol)
        if t:
            trades.append(t)
    return trades


def cross_sectional_momentum_trades(bars_by_symbol, *, cost, rebalance_bars=XMOM_HOLD):
    """Long the strongest / short the weakest of the basket at each
    rebalance, on a common timestamp grid. No lookahead: ranking uses the
    trailing XMOM_LOOKBACK_BARS return ending at the signal bar; entry is
    the next bar open per symbol."""
    syms = [s for s, b in bars_by_symbol.items() if len(b) > XMOM_LOOKBACK_BARS + 2]
    if len(syms) < 3:
        return []
    # Index each symbol's bars by timestamp string for alignment.
    by_ts = {s: {b["ts"]: idx for idx, b in enumerate(bars_by_symbol[s])} for s in syms}
    atr_by_sym = {s: _atr_series(bars_by_symbol[s], ATR_PERIOD) for s in syms}
    # Common, ordered timestamps (use the first symbol as clock).
    clock = [b["ts"] for b in bars_by_symbol[syms[0]]]
    trades = []
    warmup = XMOM_LOOKBACK_BARS + 1
    step = max(1, rebalance_bars)
    for ci in range(warmup, len(clock) - 1, step):
        ts = clock[ci]
        rets = {}
        for s in syms:
            idx = by_ts[s].get(ts)
            if idx is None or idx < XMOM_LOOKBACK_BARS or idx >= len(bars_by_symbol[s]) - 1:
                continue
            past = bars_by_symbol[s][idx - XMOM_LOOKBACK_BARS]["close"]
            now = bars_by_symbol[s][idx]["close"]
            if past > 0:
                rets[s] = (now - past) / past
        if len(rets) < 3:
            continue
        ranked = sorted(rets.items(), key=lambda kv: kv[1], reverse=True)
        winners = [ranked[0][0]]
        losers = [ranked[-1][0]]
        for s, direction in [(winners[0], 1), (losers[0], -1)]:
            idx = by_ts[s][ts]
            a = atr_by_sym[s][idx]
            if a is None or a <= 0:
                continue
            t = _make_trade(bars_by_symbol[s], idx, direction, a, tp_atr=XMOM_TP_ATR,
                            sl_atr=XMOM_SL_ATR, hold=XMOM_HOLD, cost=cost,
                            strat=STRAT_XMOM, symbol=s)
            if t:
                trades.append(t)
    return trades


# --------------------------------------------------------------------------
# Portfolio backtest
# --------------------------------------------------------------------------


@dataclass
class StrategyStats:
    strategy: str = ""
    trades: int = 0
    net_ev_pct: float = 0.0
    net_pf: float = 0.0
    winrate: float = 0.0
    avg_r: float = 0.0

    def as_dict(self):
        return asdict(self)


@dataclass
class AlphaEnsembleReport:
    generated_at: str = ""
    timeframe: str = ""
    hours: int = 0
    symbols_requested: list[str] = field(default_factory=list)
    symbols_with_data: list[str] = field(default_factory=list)
    bars_loaded: int = 0
    total_trades: int = 0
    cost_pct: float = 0.0
    # Portfolio (at the realistic cost level).
    net_ev_pct: float = 0.0
    net_pf: float = 0.0
    winrate: float = 0.0
    trade_sharpe: float = 0.0
    cagr_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    final_equity_mult: float = 1.0
    concentration: float = 0.0
    top_symbol: str = ""
    per_strategy: list[dict[str, Any]] = field(default_factory=list)
    correlation: dict[str, float] = field(default_factory=dict)
    # Out-of-sample robustness check. NOTE: this is a single chronological
    # 70/30 split of the trade sequence, NOT a rolling walk-forward. It is a
    # weak robustness signal only — do NOT read it as walk-forward validated.
    oos_trades: int = 0
    oos_net_ev_pct: float = 0.0
    oos_net_pf: float = 0.0
    oos_sign_consistent: bool = False
    oos_method: str = "chronological_70_30_split"
    walk_forward_ready: bool = False
    walk_forward_status: str = "NOT_IMPLEMENTED"
    # Cost-stress sweep.
    cost_stress: list[dict[str, Any]] = field(default_factory=list)
    cost_stress_all_pass: bool = False
    blockers: list[str] = field(default_factory=list)
    verdict: str = VERDICT_NEED_DATA
    # Hard invariants.
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    paper_ready: bool = False
    live_ready: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self):
        return asdict(self)


def _pf(nets: list[float]) -> float:
    gw = sum(x for x in nets if x > 0)
    gl = abs(sum(x for x in nets if x < 0))
    if gl > 0:
        return gw / gl
    return 999.0 if gw > 0 else 0.0


def _equity_stats(trades_sorted, *, risk_frac=RISK_FRAC, bars_per_year_hint=None):
    """Sequential fixed-fractional compounding over time-ordered trades."""
    eq = 1.0
    peak = 1.0
    max_dd = 0.0
    rets = []
    for t in trades_sorted:
        ru = t["risk_unit_pct"]
        mult = 1.0 + risk_frac * (t["net_pct"] / ru if ru > 0 else 0.0)
        mult = max(mult, 0.01)
        eq *= mult
        peak = max(peak, eq)
        max_dd = min(max_dd, eq / peak - 1.0)
        rets.append(mult - 1.0)
    sharpe = 0.0
    if len(rets) > 2:
        mu = statistics.fmean(rets)
        sd = statistics.pstdev(rets) or 1e-12
        sharpe = mu / sd * math.sqrt(len(rets))  # per-sequence Sharpe
    return eq, max_dd, sharpe, rets


def backtest_portfolio(
    bars_by_symbol: dict[str, list[dict[str, float]]],
    *,
    timeframe: str = "15m",
    costs: list[float] | None = None,
) -> AlphaEnsembleReport:
    report = AlphaEnsembleReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        timeframe=timeframe,
        symbols_with_data=sorted(bars_by_symbol.keys()),
        bars_loaded=sum(len(b) for b in bars_by_symbol.values()),
    )
    costs = costs or DEFAULT_COSTS
    if not bars_by_symbol or report.bars_loaded == 0:
        report.blockers = ["ohlcv_missing"]
        report.verdict = VERDICT_NEED_DATA
        return report

    # Cost-stress sweep.
    stress_rows = []
    realistic = None
    for cost in costs:
        trades = []
        for s, bars in bars_by_symbol.items():
            trades += donchian_trend_trades(bars, cost=cost, symbol=s)
            trades += mean_reversion_trades(bars, cost=cost, symbol=s)
        trades += cross_sectional_momentum_trades(bars_by_symbol, cost=cost)
        trades.sort(key=lambda t: t["entry_ts"])
        nets = [t["net_pct"] for t in trades]
        eq, mdd, sharpe, _ = _equity_stats(trades)
        row = {
            "cost_pct": cost, "trades": len(trades),
            "net_ev_pct": round(statistics.fmean(nets), 4) if nets else 0.0,
            "net_pf": round(_pf(nets), 4),
            "final_equity_mult": round(eq, 4), "max_drawdown_pct": round(mdd * 100, 4),
            "trade_sharpe": round(sharpe, 4),
            "pass": bool(nets and statistics.fmean(nets) > 0 and _pf(nets) >= MIN_NET_PF),
        }
        stress_rows.append(row)
        if abs(cost - 0.0018) < 1e-9:
            realistic = (trades, nets, eq, mdd, sharpe)
    report.cost_stress = stress_rows
    report.cost_stress_all_pass = bool(stress_rows) and all(r["pass"] for r in stress_rows)

    if realistic is None:
        # use the lowest cost as the headline if 0.0018 not in list
        cost = costs[0]
        trades = []
        for s, bars in bars_by_symbol.items():
            trades += donchian_trend_trades(bars, cost=cost, symbol=s)
            trades += mean_reversion_trades(bars, cost=cost, symbol=s)
        trades += cross_sectional_momentum_trades(bars_by_symbol, cost=cost)
        trades.sort(key=lambda t: t["entry_ts"])
        nets = [t["net_pct"] for t in trades]
        eq, mdd, sharpe, _ = _equity_stats(trades)
        report.cost_pct = cost
    else:
        trades, nets, eq, mdd, sharpe = realistic
        report.cost_pct = 0.0018

    report.total_trades = len(trades)
    if not trades:
        report.blockers = ["no_trades_generated"]
        report.verdict = VERDICT_NEED_DATA
        return report

    report.net_ev_pct = round(statistics.fmean(nets), 4)
    report.net_pf = round(_pf(nets), 4)
    report.winrate = round(sum(1 for x in nets if x > 0) / len(nets), 4)
    report.trade_sharpe = round(sharpe, 4)
    report.final_equity_mult = round(eq, 4)
    report.max_drawdown_pct = round(mdd * 100, 4)

    # CAGR from elapsed wall-clock of trades.
    try:
        t0 = _parse_ts(trades[0]["entry_ts"])
        t1 = _parse_ts(trades[-1]["entry_ts"])
        years = max((t1 - t0).total_seconds() / (365.25 * 86400), 1e-6)
        report.cagr_pct = round((eq ** (1 / years) - 1) * 100, 4)
    except Exception:
        report.cagr_pct = 0.0

    # Per-strategy + concentration.
    by_strat: dict[str, list[float]] = {}
    by_symbol: dict[str, int] = {}
    by_strat_r: dict[str, list[float]] = {}
    for t in trades:
        by_strat.setdefault(t["strategy"], []).append(t["net_pct"])
        by_strat_r.setdefault(t["strategy"], []).append(t["r_multiple"])
        by_symbol[t["symbol"]] = by_symbol.get(t["symbol"], 0) + 1
    per = []
    for s, ns in by_strat.items():
        per.append(StrategyStats(
            strategy=s, trades=len(ns),
            net_ev_pct=round(statistics.fmean(ns), 4), net_pf=round(_pf(ns), 4),
            winrate=round(sum(1 for x in ns if x > 0) / len(ns), 4),
            avg_r=round(statistics.fmean(by_strat_r[s]), 4),
        ).as_dict())
    report.per_strategy = sorted(per, key=lambda d: d["net_ev_pct"], reverse=True)
    if by_symbol:
        top = max(by_symbol.items(), key=lambda kv: kv[1])
        report.top_symbol = top[0]
        report.concentration = round(top[1] / len(trades), 4)

    # Correlation between strategies' daily returns (diversification proof).
    report.correlation = _strategy_correlations(trades)

    # Out-of-sample: last 30% of trades chronologically.
    split = int(len(trades) * 0.7)
    oos = trades[split:]
    oos_nets = [t["net_pct"] for t in oos]
    report.oos_trades = len(oos)
    if oos_nets:
        report.oos_net_ev_pct = round(statistics.fmean(oos_nets), 4)
        report.oos_net_pf = round(_pf(oos_nets), 4)
        report.oos_sign_consistent = bool(
            (report.net_ev_pct > 0) == (report.oos_net_ev_pct > 0)
            and report.oos_net_ev_pct > 0
        )

    # Verdict (gauntlet).
    blockers = []
    if report.total_trades < MIN_TRADES:
        blockers.append("insufficient_trades")
    if report.oos_trades < MIN_OOS_TRADES:
        blockers.append("insufficient_oos_trades")
    if report.net_ev_pct <= 0:
        blockers.append("net_ev_non_positive")
    if report.net_pf < MIN_NET_PF:
        blockers.append("net_pf_below_min")
    if not report.cost_stress_all_pass:
        blockers.append("fails_cost_stress")
    if report.trade_sharpe < MIN_SHARPE:
        blockers.append("sharpe_below_min")
    if report.concentration > MAX_CONCENTRATION:
        blockers.append("single_symbol_dominance")
    if not report.oos_sign_consistent:
        blockers.append("oos_not_consistent")
    report.blockers = blockers

    if report.total_trades < MIN_TRADES or report.oos_trades < MIN_OOS_TRADES:
        report.verdict = VERDICT_NEED_DATA
    elif report.net_ev_pct <= 0 or report.net_pf < MIN_NET_PF or not report.cost_stress_all_pass:
        report.verdict = VERDICT_REJECT
    elif blockers:  # positive but failed a robustness gate
        report.verdict = VERDICT_WATCH
    else:
        report.verdict = VERDICT_SHADOW
    return report


def _strategy_correlations(trades) -> dict[str, float]:
    """Pairwise correlation of per-day net returns across strategies.
    Low/negative correlation is the diversification edge (Monte-Carlo
    showed it is what turns weak edges into a consistent portfolio)."""
    from collections import defaultdict
    daily = defaultdict(lambda: defaultdict(float))  # strat -> day -> sum net
    days = set()
    for t in trades:
        ts = _parse_ts(t["entry_ts"])
        day = ts.date().isoformat() if ts else "NA"
        daily[t["strategy"]][day] += t["net_pct"]
        days.add(day)
    strats = sorted(daily.keys())
    day_list = sorted(days)
    series = {s: [daily[s].get(d, 0.0) for d in day_list] for s in strats}
    out = {}
    for a_i in range(len(strats)):
        for b_i in range(a_i + 1, len(strats)):
            sa, sb = strats[a_i], strats[b_i]
            out[f"{sa}__{sb}"] = round(_corr(series[sa], series[sb]), 4)
    return out


def _corr(a: list[float], b: list[float]) -> float:
    if len(a) < 3:
        return 0.0
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    if da <= 0 or db <= 0:
        return 0.0
    return num / (da * db)


def run_alpha_ensemble(
    db: Any,
    *,
    symbols: list[str] | None = None,
    timeframe: str = "15m",
    hours: int = 2160,
) -> AlphaEnsembleReport:
    syms = [s.strip().upper() for s in (symbols or DEFAULT_SYMBOLS) if s.strip()]
    since_iso = (datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours)))).isoformat()
    reader = getattr(db, "fetch_ohlcv_range", None)
    bars_by_symbol: dict[str, list[dict[str, float]]] = {}
    if callable(reader):
        for s in syms:
            try:
                rows = reader(s, timeframe, since_iso=since_iso) or []
            except Exception:
                rows = []
            bars = _to_bars_with_ts(rows)
            if bars:
                bars_by_symbol[s] = bars
    report = backtest_portfolio(bars_by_symbol, timeframe=timeframe)
    report.symbols_requested = syms
    report.hours = int(hours)
    return report


def _to_bars_with_ts(rows: Iterable[dict[str, Any]] | None) -> list[dict[str, float]]:
    """Like _to_bars but keeps the ISO timestamp string for ordering."""
    out = []
    for r in rows or []:
        o, h, low, c = _f(r.get("open")), _f(r.get("high")), _f(r.get("low")), _f(r.get("close"))
        if None in (o, h, low, c):
            continue
        ts = r.get("timestamp")
        out.append((_parse_ts(ts) or datetime.min.replace(tzinfo=timezone.utc),
                    {"open": o, "high": h, "low": low, "close": c,
                     "volume": _f(r.get("volume")) or 0.0, "ts": str(ts)}))
    out.sort(key=lambda t: t[0])
    return [b for _, b in out]
