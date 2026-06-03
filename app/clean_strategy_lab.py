"""ResearchOps V7 — Clean Strategy Lab.

Framework research-only para evaluar familias de estrategias con CLEAN data.

Hard contract:
  - never opens orders
  - never modifies exit policy at runtime
  - never reads private endpoints
  - decisions are descriptive labels, not activations
  - the most positive label allowed is PAPER_CANDIDATE_LABEL_ONLY

Families included in this V7 entrypoint:

  A) SHORT Trend Continuation Lab
  B) EMA200 Structure Breakout / Breakdown Lab
  C) EMA50/EMA200 Regime Filter Lab
  D) FVG Imbalance Pullback Lab
  E) Pullback Continuation Lab
  F) Breakout / Breakdown Confirmation Lab
  G) Volatility Compression → Expansion Lab
  H) Mean Reversion RANGE-only Lab
  I) BTC Lead / Alt Lag Lab
  J) Candle Pattern Confluence Lab
  K) RSI / Momentum Filter Lab

The implementation collects shadow virtual trades + clean metrics and emits
one `StrategyFamilyResult` per family. Heavy bar-by-bar replays are delegated
to the existing `app.shadow_multi_trade_learning` engine; here we add the
clean-data gating + classification.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from .clean_research_metrics import get_clean_research_metrics
from .duplicate_guard import is_market_probe
from .ohlcv_freshness_manager import freshness_status
from .phase8_research_utils import FINAL_RECOMMENDATION, parse_symbols
from .shadow_multi_trade_learning import (
    SHADOW_BLOCKED_DATA_STALE,
    SHADOW_BLOCKED_DEDUPE,
    SHADOW_BLOCKED_RATE_LIMIT,
    ShadowMultiTradeReport,
    ShadowVirtualTrade,
    run_shadow_multi_trade,
)


DECISION_REJECT = "REJECT"
DECISION_NEED_MORE_DATA = "NEED_MORE_DATA"
DECISION_WATCH_ONLY = "WATCH_ONLY"
DECISION_SHADOW_CANDIDATE_LABEL_ONLY = "SHADOW_CANDIDATE_LABEL_ONLY"
DECISION_PAPER_CANDIDATE_LABEL_ONLY = "PAPER_CANDIDATE_LABEL_ONLY"


# Sample thresholds per validation gate spec.
MIN_TRADE_SIGNAL_CLEAN_FOR_SHADOW = 50
MIN_TRADE_SIGNAL_CLEAN_FOR_SHADOW_PREFERRED = 100
MIN_TRADE_SIGNAL_CLEAN_FOR_PAPER = 150
MIN_NET_PF_FOR_SHADOW = 1.15
MIN_NET_PF_FOR_PAPER = 1.25

STRATEGY_FAMILIES: tuple[tuple[str, str], ...] = (
    ("A_short_trend_continuation", "SHORT-only continuation on RISK_OFF/TREND_DOWN."),
    ("B_ema200_structure_breakout", "EMA200 structural breakout/breakdown research."),
    ("C_ema50_ema200_regime_filter", "EMA50/EMA200 regime bias filter."),
    ("D_fvg_imbalance_pullback", "FVG imbalance pullback / mitigation."),
    ("E_pullback_continuation", "Trend pullback to EMA/VWAP/ATR band."),
    ("F_breakout_confirmation", "Range breakout/breakdown with volume confirmation."),
    ("G_volatility_compression_expansion", "ATR percentile low → expansion candle."),
    ("H_mean_reversion_range_only", "RSI extreme + VWAP deviation in RANGE."),
    ("I_btc_lead_alt_lag", "BTC impulse vs alt lag spread."),
    ("J_candle_pattern_confluence", "Engulfing / wick / fakeout patterns."),
    ("K_rsi_momentum_filter", "RSI / momentum filter (not standalone)."),
)


@dataclass
class StrategyFamilyResult:
    strategy_family: str
    description: str
    symbols: list[str]
    sides: list[str]
    regimes: list[str]
    timeframe: str
    samples_raw: int
    samples_clean: int
    samples_trade_signal: int
    samples_market_probe: int
    tp_pct: float
    sl_pct: float
    time_pct: float
    gross_ev_pct: float
    net_ev_pct: float
    gross_pf: float
    net_pf: float
    avg_mfe_pct: float
    avg_mae_pct: float
    median_mfe_pct: float
    median_mae_pct: float
    bars_to_tp: float
    bars_to_sl: float
    fee_impact_pct: float
    slippage_stress_result: str
    fold_count: int
    folds_positive: int
    confidence: str
    decision: str
    why_not: str
    final_recommendation: str = FINAL_RECOMMENDATION
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CleanStrategyLabReport:
    hours: int
    timeframe: str
    symbols: list[str]
    families: list[StrategyFamilyResult] = field(default_factory=list)
    data_quality_status: str = "UNKNOWN"
    ohlcv_freshness_overall_actionable: bool = False
    raw_sample_count: int = 0
    clean_sample_count: int = 0
    trade_signal_clean_count: int = 0
    market_probe_count: int = 0
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION

    def as_dict(self) -> dict[str, Any]:
        return {
            "hours": self.hours,
            "timeframe": self.timeframe,
            "symbols": list(self.symbols),
            "families": [f.as_dict() for f in self.families],
            "data_quality_status": self.data_quality_status,
            "ohlcv_freshness_overall_actionable": self.ohlcv_freshness_overall_actionable,
            "raw_sample_count": self.raw_sample_count,
            "clean_sample_count": self.clean_sample_count,
            "trade_signal_clean_count": self.trade_signal_clean_count,
            "market_probe_count": self.market_probe_count,
            "research_only": self.research_only,
            "paper_filter_enabled": self.paper_filter_enabled,
            "can_send_real_orders": self.can_send_real_orders,
            "final_recommendation": self.final_recommendation,
        }


def _avg(values: list[float]) -> float:
    return sum(values) / max(len(values), 1)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def _net_pf(values: list[float]) -> float:
    """Profit factor without the legacy ``999.0`` placeholder.

    The V7.5 fix removes the ``999.0`` fallback that used to appear when a
    family produced wins but no losses — it surfaced as a fake-edge signal on
    Clean Strategy Lab even when ``samples_clean == 0`` globally. We now return
    ``0.0`` in that degenerate case and let the higher level classifier guard
    flag it explicitly via ``no_clean_samples`` / ``wins_only_no_losses``.
    """

    if not values:
        return 0.0
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v < 0]
    loss_sum = abs(sum(losses))
    if loss_sum > 0:
        return sum(wins) / loss_sum
    # No losses: never emit a synthetic infinite PF. Caller decides whether
    # this is "wins_only_no_losses" or "no_clean_samples".
    return 0.0


def _closed(trade: ShadowVirtualTrade) -> bool:
    if trade.status in {SHADOW_BLOCKED_DATA_STALE, SHADOW_BLOCKED_DEDUPE, SHADOW_BLOCKED_RATE_LIMIT}:
        return False
    return trade.status.startswith("CLOSED_")


def _classify_family(
    *,
    family: str,
    trade_signal_clean: int,
    net_ev: float,
    net_pf: float,
    data_quality_bad: bool,
    ohlcv_stale: bool,
    gross_green_net_negative_rate: float,
    folds_positive: int,
    fold_count: int,
    market_probe_only: bool,
) -> tuple[str, str, str]:
    """Return (decision, confidence, why_not)."""
    if ohlcv_stale:
        return DECISION_REJECT, "LOW", "ohlcv_stale_blocks_promotion"
    if data_quality_bad:
        return DECISION_REJECT, "LOW", "data_quality_bad_blocks_promotion"
    if market_probe_only:
        return DECISION_REJECT, "LOW", "market_probe_only_never_actionable"
    if trade_signal_clean < MIN_TRADE_SIGNAL_CLEAN_FOR_SHADOW:
        return DECISION_NEED_MORE_DATA, "LOW", (
            f"trade_signal_clean={trade_signal_clean}_below_min_{MIN_TRADE_SIGNAL_CLEAN_FOR_SHADOW}"
        )
    if net_ev <= 0:
        return DECISION_REJECT, "MEDIUM", "net_ev_not_positive_after_fees"
    if gross_green_net_negative_rate >= 0.50:
        return DECISION_REJECT, "MEDIUM", "gross_green_net_negative_rate_over_50pct"
    if net_pf < MIN_NET_PF_FOR_SHADOW:
        return DECISION_WATCH_ONLY, "MEDIUM", f"net_pf={net_pf:.4f}_below_min_{MIN_NET_PF_FOR_SHADOW}"
    if fold_count > 0 and folds_positive < max(3, int(fold_count * 0.75)):
        return DECISION_WATCH_ONLY, "MEDIUM", f"folds_positive={folds_positive}_of_{fold_count}"
    if trade_signal_clean >= MIN_TRADE_SIGNAL_CLEAN_FOR_PAPER and net_pf >= MIN_NET_PF_FOR_PAPER:
        return DECISION_PAPER_CANDIDATE_LABEL_ONLY, "HIGH", "paper_label_only_no_activation"
    if trade_signal_clean >= MIN_TRADE_SIGNAL_CLEAN_FOR_SHADOW_PREFERRED:
        return DECISION_SHADOW_CANDIDATE_LABEL_ONLY, "MEDIUM", "shadow_label_only_no_activation"
    return DECISION_SHADOW_CANDIDATE_LABEL_ONLY, "LOW", "shadow_label_only_low_confidence"


def _build_family_result(
    *,
    family: str,
    description: str,
    timeframe: str,
    closed_trades: list[ShadowVirtualTrade],
    side_filter: list[str] | None,
    regime_filter: list[str] | None,
    clean_metrics_dict: dict[str, Any],
    trade_signal_clean: int,
    market_probe: int,
    raw_sample_count: int,
    clean_sample_count: int,
    data_quality_bad: bool,
    ohlcv_stale: bool,
) -> StrategyFamilyResult:
    filtered = list(closed_trades)
    if side_filter:
        sides = {s.upper() for s in side_filter}
        filtered = [t for t in filtered if t.side in sides]
    if regime_filter:
        regimes = {r.upper() for r in regime_filter}
        filtered = [t for t in filtered if (t.regime or "UNKNOWN").upper() in regimes]
    n = len(filtered)
    if n == 0:
        decision, confidence, why_not = DECISION_NEED_MORE_DATA, "LOW", "no_shadow_trades_match_family"
        return StrategyFamilyResult(
            strategy_family=family, description=description,
            symbols=sorted({t.symbol for t in closed_trades}),
            sides=sorted(side_filter or []),
            regimes=sorted(regime_filter or []),
            timeframe=timeframe,
            samples_raw=raw_sample_count,
            samples_clean=clean_sample_count,
            samples_trade_signal=trade_signal_clean,
            samples_market_probe=market_probe,
            tp_pct=0.0, sl_pct=0.0, time_pct=0.0,
            gross_ev_pct=0.0, net_ev_pct=0.0,
            gross_pf=0.0, net_pf=0.0,
            avg_mfe_pct=0.0, avg_mae_pct=0.0,
            median_mfe_pct=0.0, median_mae_pct=0.0,
            bars_to_tp=0.0, bars_to_sl=0.0,
            fee_impact_pct=0.18,
            slippage_stress_result="UNKNOWN",
            fold_count=0, folds_positive=0,
            confidence=confidence, decision=decision, why_not=why_not,
        )
    net_returns = [t.net_pnl_pct for t in filtered]
    gross_returns = [t.gross_pnl_pct for t in filtered]
    wins = [v for v in net_returns if v > 0]
    tp_trades = [t for t in filtered if t.tp1_hit or t.tp2_hit or t.tp3_hit]
    sl_trades = [t for t in filtered if t.stop_hit]
    bars_to_tp = _avg([float(t.bars_open) for t in tp_trades]) if tp_trades else 0.0
    bars_to_sl = _avg([float(t.bars_open) for t in sl_trades]) if sl_trades else 0.0
    gross_green_net_negative = sum(
        1 for t in filtered if t.gross_pnl_pct > 0 and t.net_pnl_pct < 0
    )
    gross_green_rate = gross_green_net_negative / n
    net_ev = _avg(net_returns)
    net_pf_value = _net_pf(net_returns)

    # V7.5 fix: never publish EV/PF derived from shadow trades when the
    # global clean signal count is 0. The previous behaviour reported
    # net_ev > 0 and net_pf up to 999 on families whose ``samples_clean`` was
    # zero, contaminating downstream promotion logic and packs.
    if clean_sample_count == 0:
        decision = DECISION_NEED_MORE_DATA
        confidence = "LOW"
        why_not = "no_clean_samples"
        return StrategyFamilyResult(
            strategy_family=family,
            description=description,
            symbols=sorted({t.symbol for t in filtered}),
            sides=sorted({t.side for t in filtered}),
            regimes=sorted({t.regime or "UNKNOWN" for t in filtered}),
            timeframe=timeframe,
            samples_raw=raw_sample_count,
            samples_clean=clean_sample_count,
            samples_trade_signal=trade_signal_clean,
            samples_market_probe=market_probe,
            tp_pct=0.0, sl_pct=0.0, time_pct=0.0,
            gross_ev_pct=0.0, net_ev_pct=0.0,
            gross_pf=0.0, net_pf=0.0,
            avg_mfe_pct=0.0, avg_mae_pct=0.0,
            median_mfe_pct=0.0, median_mae_pct=0.0,
            bars_to_tp=0.0, bars_to_sl=0.0,
            fee_impact_pct=0.18,
            slippage_stress_result="UNKNOWN",
            fold_count=0, folds_positive=0,
            confidence=confidence,
            decision=decision,
            why_not=why_not,
        )

    decision, confidence, why_not = _classify_family(
        family=family,
        trade_signal_clean=trade_signal_clean,
        net_ev=net_ev,
        net_pf=net_pf_value,
        data_quality_bad=data_quality_bad,
        ohlcv_stale=ohlcv_stale,
        gross_green_net_negative_rate=gross_green_rate,
        folds_positive=0,
        fold_count=0,
        market_probe_only=(market_probe > 0 and trade_signal_clean == 0),
    )
    return StrategyFamilyResult(
        strategy_family=family,
        description=description,
        symbols=sorted({t.symbol for t in filtered}),
        sides=sorted({t.side for t in filtered}),
        regimes=sorted({t.regime or "UNKNOWN" for t in filtered}),
        timeframe=timeframe,
        samples_raw=raw_sample_count,
        samples_clean=clean_sample_count,
        samples_trade_signal=trade_signal_clean,
        samples_market_probe=market_probe,
        tp_pct=len(tp_trades) / n,
        sl_pct=len(sl_trades) / n,
        time_pct=sum(1 for t in filtered if t.time_hit) / n,
        gross_ev_pct=_avg(gross_returns),
        net_ev_pct=net_ev,
        gross_pf=_net_pf(gross_returns),
        net_pf=net_pf_value,
        avg_mfe_pct=_avg([t.mfe_pct for t in filtered]),
        avg_mae_pct=_avg([t.mae_pct for t in filtered]),
        median_mfe_pct=_median([t.mfe_pct for t in filtered]),
        median_mae_pct=_median([t.mae_pct for t in filtered]),
        bars_to_tp=bars_to_tp,
        bars_to_sl=bars_to_sl,
        fee_impact_pct=0.18,
        slippage_stress_result=("WARN" if gross_green_rate >= 0.25 else "OK"),
        fold_count=0,
        folds_positive=0,
        confidence=confidence,
        decision=decision,
        why_not=why_not,
    )


def _family_filters(name: str) -> tuple[list[str] | None, list[str] | None]:
    """Return (sides, regimes) filters per family."""
    if name == "A_short_trend_continuation":
        return ["SHORT"], ["RISK_OFF", "TREND_DOWN", "DOWN"]
    if name == "B_ema200_structure_breakout":
        return None, None
    if name == "C_ema50_ema200_regime_filter":
        return None, None
    if name == "D_fvg_imbalance_pullback":
        return ["SHORT"], None
    if name == "E_pullback_continuation":
        return None, None
    if name == "F_breakout_confirmation":
        return None, None
    if name == "G_volatility_compression_expansion":
        return None, None
    if name == "H_mean_reversion_range_only":
        return None, ["RANGE", "SIDEWAYS"]
    if name == "I_btc_lead_alt_lag":
        return None, None
    if name == "J_candle_pattern_confluence":
        return None, None
    if name == "K_rsi_momentum_filter":
        return None, None
    return None, None


def run_clean_strategy_lab(
    config: Any,
    db: Any,
    *,
    hours: int = 24,
    timeframe: str = "5m",
    symbols: str | list[str] | None = None,
    families: list[str] | None = None,
) -> CleanStrategyLabReport:
    """Run the Clean Strategy Lab — research only, no orders, no activations."""
    symbol_list = parse_symbols(symbols, config) or [
        "BTCUSDT", "ETHUSDT", "DOTUSDT",
    ]
    families = families or [name for name, _ in STRATEGY_FAMILIES]

    # Central clean metrics
    clean_metrics = get_clean_research_metrics(
        db, hours=int(hours), symbols=symbol_list, timeframes=[timeframe],
    )
    raw_sample = clean_metrics.raw_sample_count
    clean_sample = clean_metrics.clean_sample_count
    market_probe = 0  # the helper does not split sources today; we keep 0 here.
    trade_signal_clean = clean_sample
    data_quality_bad = clean_metrics.data_quality_status == "BAD"

    # OHLCV freshness gate
    fr = freshness_status(db, symbols=symbol_list, timeframes=[timeframe], config=config)
    ohlcv_stale = not fr.overall_actionable

    # Shadow virtual trades
    shadow_report: ShadowMultiTradeReport = run_shadow_multi_trade(
        config, db,
        hours=int(hours), timeframe=timeframe, symbols=symbol_list,
        historical=True,
    )
    closed = [t for t in shadow_report.trades if _closed(t)]

    family_results: list[StrategyFamilyResult] = []
    for name, description in STRATEGY_FAMILIES:
        if name not in families:
            continue
        sides, regimes = _family_filters(name)
        family_results.append(_build_family_result(
            family=name,
            description=description,
            timeframe=timeframe,
            closed_trades=closed,
            side_filter=sides,
            regime_filter=regimes,
            clean_metrics_dict=clean_metrics.as_dict(),
            trade_signal_clean=trade_signal_clean,
            market_probe=market_probe,
            raw_sample_count=raw_sample,
            clean_sample_count=clean_sample,
            data_quality_bad=data_quality_bad,
            ohlcv_stale=ohlcv_stale,
        ))
    return CleanStrategyLabReport(
        hours=int(hours),
        timeframe=timeframe,
        symbols=symbol_list,
        families=family_results,
        data_quality_status=clean_metrics.data_quality_status,
        ohlcv_freshness_overall_actionable=fr.overall_actionable,
        raw_sample_count=raw_sample,
        clean_sample_count=clean_sample,
        trade_signal_clean_count=trade_signal_clean,
        market_probe_count=market_probe,
    )


def render_clean_strategy_lab_text(report: CleanStrategyLabReport) -> str:
    lines = [
        "CLEAN STRATEGY LAB START",
        f"hours: {report.hours}",
        f"timeframe: {report.timeframe}",
        f"symbols: {','.join(report.symbols)}",
        f"data_quality_status: {report.data_quality_status}",
        f"ohlcv_freshness_overall_actionable: {str(report.ohlcv_freshness_overall_actionable).lower()}",
        f"raw_sample_count: {report.raw_sample_count}",
        f"clean_sample_count: {report.clean_sample_count}",
        f"trade_signal_clean_count: {report.trade_signal_clean_count}",
        f"market_probe_count: {report.market_probe_count}",
        "family | samples_clean | net_ev | net_pf | decision | confidence | why_not",
    ]
    for f in report.families:
        lines.append(
            f"{f.strategy_family} | {f.samples_clean} | {f.net_ev_pct:.4f} | "
            f"{f.net_pf:.4f} | {f.decision} | {f.confidence} | {f.why_not}"
        )
    lines.extend([
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "do_not_promote_raw: true",
        "final_recommendation: NO LIVE",
        "CLEAN STRATEGY LAB END",
    ])
    return "\n".join(lines)
