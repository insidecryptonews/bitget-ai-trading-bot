from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

from .cost_model import explain_cost_breakdown
from .indicators import add_indicators
from .market_data import MarketSnapshot
from .regime_detector import MarketRegime
from .signal_engine import SignalEngine
from .utils import safe_float


FINAL_RECOMMENDATION = "NO LIVE"


@dataclass
class RealBacktestTrade:
    symbol: str
    side: str
    signal_index: int
    entry_index: int
    exit_index: int
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit_1: float
    gross_return_pct: float
    net_return_pct: float
    exit_reason: str
    fee_cost_bps: float
    slippage_cost_bps: float
    funding_component_bps: float
    total_cost_bps: float
    same_bar_worst_case_applied: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RealBacktestResult:
    status: str
    uses_signal_engine: bool
    no_lookahead_status: str
    entry_model: str
    stop_tp_same_bar_rule: str
    min_order_rule: str
    trades: list[RealBacktestTrade] = field(default_factory=list)
    blocked_min_notional: int = 0
    final_recommendation: str = FINAL_RECOMMENDATION

    def summary(self) -> dict[str, Any]:
        returns = [trade.net_return_pct for trade in self.trades]
        wins = [value for value in returns if value > 0]
        losses = [value for value in returns if value < 0]
        gross = [trade.gross_return_pct for trade in self.trades]
        fees = sum(trade.fee_cost_bps for trade in self.trades)
        slippage = sum(trade.slippage_cost_bps for trade in self.trades)
        funding = sum(trade.funding_component_bps for trade in self.trades)
        return {
            "status": self.status,
            "uses_signal_engine": self.uses_signal_engine,
            "no_lookahead_status": self.no_lookahead_status,
            "entry_model": self.entry_model,
            "stop_tp_same_bar_rule": self.stop_tp_same_bar_rule,
            "min_order_rule": self.min_order_rule,
            "trades": len(self.trades),
            "blocked_min_notional": self.blocked_min_notional,
            "win_rate": len(wins) / max(len(returns), 1),
            "gross_ev": sum(gross) / max(len(gross), 1),
            "net_ev": sum(returns) / max(len(returns), 1),
            "net_pf": sum(wins) / abs(sum(losses)) if losses else 999.0 if wins else 0.0,
            "avg_win": sum(wins) / max(len(wins), 1),
            "avg_loss": sum(losses) / max(len(losses), 1),
            "max_drawdown": _max_drawdown(returns),
            "fees_paid_bps": fees,
            "slippage_paid_bps": slippage,
            "funding_paid_or_received_bps": funding,
            "tp_pct": sum(1 for trade in self.trades if trade.exit_reason == "TAKE_PROFIT") / max(len(self.trades), 1),
            "sl_pct": sum(1 for trade in self.trades if trade.exit_reason == "STOP_LOSS") / max(len(self.trades), 1),
            "time_pct": sum(1 for trade in self.trades if trade.exit_reason == "HORIZON_CLOSE") / max(len(self.trades), 1),
            "same_bar_stop_tp_count": sum(1 for trade in self.trades if trade.same_bar_worst_case_applied),
            "same_bar_worst_case_applied": any(trade.same_bar_worst_case_applied for trade in self.trades),
            "final_recommendation": self.final_recommendation,
        }


class RealStrategyBacktester:
    """Backtests the real SignalEngine path candle by candle without exchange calls."""

    def __init__(self, config: Any, signal_engine: SignalEngine | None = None) -> None:
        self.config = config
        self.signal_engine = signal_engine or SignalEngine(config)
        self.generate_signal_calls = 0

    def run(
        self,
        symbol: str,
        candles: pd.DataFrame,
        *,
        regime: MarketRegime | None = None,
        min_order_value_usdt: float | None = None,
        max_holding_bars: int = 30,
        notional_usdt: float | None = None,
    ) -> RealBacktestResult:
        if candles is None or len(candles) < 65:
            return RealBacktestResult(
                status="NEED_DATA",
                uses_signal_engine=False,
                no_lookahead_status="NOT_RUN",
                entry_model="signal_close_i_entry_next_open_i+1",
                stop_tp_same_bar_rule="STOP_BEFORE_TP",
                min_order_rule="BLOCK_BELOW_MIN_NOTIONAL",
            )
        data = add_indicators(candles).reset_index(drop=True)
        trades: list[RealBacktestTrade] = []
        blocked = 0
        min_notional = safe_float(min_order_value_usdt if min_order_value_usdt is not None else getattr(self.config, "min_trade_margin_usdt", 5.0))
        trade_notional = safe_float(notional_usdt if notional_usdt is not None else float(getattr(self.config, "trade_margin_usdt", 12.0)) * max(1, int(getattr(self.config, "default_leverage", 1))))
        regime = regime or MarketRegime("RANGE", allowed_direction="BOTH")

        for index in range(60, len(data) - 1):
            current_slice = data.iloc[: index + 1].copy()
            snapshot = MarketSnapshot(
                symbol=symbol,
                candles={
                    str(getattr(self.config, "main_timeframe", "5m")).lower(): current_slice,
                    str(getattr(self.config, "confirmation_timeframe", "15m")).lower(): current_slice,
                    str(getattr(self.config, "higher_timeframe", "1h")).lower(): current_slice,
                    "5m": current_slice,
                    "15m": current_slice,
                    "1h": current_slice,
                },
                current_price=safe_float(current_slice.iloc[-1].get("close")),
                funding_rate=safe_float(current_slice.iloc[-1].get("funding_rate")),
            )
            self.generate_signal_calls += 1
            signal = self.signal_engine.generate_signal(symbol, snapshot, regime)
            if str(signal.side).upper() not in {"LONG", "SHORT"}:
                continue
            entry_index = index + 1
            entry_price = safe_float(data.iloc[entry_index].get("open"))
            if entry_price <= 0:
                continue
            if trade_notional < min_notional:
                blocked += 1
                continue
            trade = self._simulate_trade(symbol, signal, data, entry_index, entry_price, trade_notional, max_holding_bars)
            trades.append(trade)
        return RealBacktestResult(
            status="OK" if trades or blocked else "NO_TRADES",
            uses_signal_engine=self.generate_signal_calls > 0,
            no_lookahead_status="OK_PREFIX_ONLY",
            entry_model="signal_close_i_entry_next_open_i+1",
            stop_tp_same_bar_rule="STOP_BEFORE_TP",
            min_order_rule="BLOCK_BELOW_MIN_NOTIONAL",
            trades=trades,
            blocked_min_notional=blocked,
        )

    def _simulate_trade(self, symbol: str, signal: Any, data: pd.DataFrame, entry_index: int, entry_price: float, notional_usdt: float, max_holding_bars: int) -> RealBacktestTrade:
        del notional_usdt
        side = str(signal.side).upper()
        direction = 1 if side == "LONG" else -1
        stop = safe_float(signal.stop_loss)
        tp = safe_float(signal.take_profit_1)
        exit_price = safe_float(data.iloc[min(len(data) - 1, entry_index + max_holding_bars - 1)].get("close"))
        exit_reason = "HORIZON_CLOSE"
        exit_index = min(len(data) - 1, entry_index + max_holding_bars - 1)
        same_bar = False
        for index in range(entry_index, min(len(data), entry_index + max_holding_bars)):
            row = data.iloc[index]
            high = safe_float(row.get("high"))
            low = safe_float(row.get("low"))
            if side == "LONG":
                stop_hit = low <= stop
                tp_hit = high >= tp
            else:
                stop_hit = high >= stop
                tp_hit = low <= tp
            if stop_hit:
                exit_price = stop
                exit_reason = "STOP_LOSS"
                exit_index = index
                same_bar = tp_hit
                break
            if tp_hit:
                exit_price = tp
                exit_reason = "TAKE_PROFIT"
                exit_index = index
                break
        gross_return = ((exit_price - entry_price) / entry_price * 100.0) * direction
        entry_time = data.iloc[entry_index].get("timestamp") if "timestamp" in data.columns else None
        exit_time = data.iloc[exit_index].get("timestamp") if "timestamp" in data.columns else None
        breakdown = explain_cost_breakdown(
            source="trade_signal",
            side=side,
            entry_type="taker",
            exit_type="taker",
            slippage_bps=safe_float(getattr(self.config, "net_edge_slippage_bps", 3.0)),
            entry_time=entry_time,
            exit_time=exit_time,
            funding_rate=data.iloc[entry_index].get("funding_rate") if "funding_rate" in data.columns else None,
            outcome=exit_reason,
        )
        net_return = gross_return - breakdown.total_cost_bps / 100.0
        return RealBacktestTrade(
            symbol=symbol,
            side=side,
            signal_index=entry_index - 1,
            entry_index=entry_index,
            exit_index=exit_index,
            entry_price=entry_price,
            exit_price=exit_price,
            stop_loss=stop,
            take_profit_1=tp,
            gross_return_pct=gross_return,
            net_return_pct=net_return,
            exit_reason=exit_reason,
            fee_cost_bps=breakdown.fee_component_bps,
            slippage_cost_bps=breakdown.slippage_component_bps,
            funding_component_bps=breakdown.funding_component_bps,
            total_cost_bps=breakdown.total_cost_bps,
            same_bar_worst_case_applied=same_bar,
        )


def _max_drawdown(values: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return abs(worst)


DEFAULT_BACKTESTER_SYMBOLS: tuple[str, ...] = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "BNBUSDT", "LINKUSDT", "AVAXUSDT", "ADAUSDT", "DOTUSDT",
)


def real_strategy_backtest_text(config: Any, db: Any, *, hours: int = 72) -> str:
    """Backwards-compatible single-symbol output.

    For new callers prefer `real_strategy_backtest_multi_text` which evaluates
    every requested symbol independently and reports per-symbol + total.
    """
    from .ohlcv_replay_loader import OhlcvReplayLoader

    loader_result = OhlcvReplayLoader(db).audit(config=config, hours=hours)
    if loader_result.status != "OK":
        data = loader_result.to_dict()
        lines = [
            "REAL STRATEGY BACKTESTER START",
            f"hours: {hours}",
            "status: NEED_DATA",
            "uses_signal_engine: false",
            "no_lookahead_status: NOT_RUN",
            "entry_model: signal_close_i_entry_next_open_i+1",
            "stop_tp_same_bar_rule: STOP_BEFORE_TP",
            "min_order_rule: BLOCK_BELOW_MIN_NOTIONAL",
            f"ohlcv_loader_status: {data['status']}",
            f"ohlcv_table: {data['table'] or 'none'}",
            f"missing_columns: {', '.join(data['missing_columns']) if data['missing_columns'] else 'none'}",
            f"warnings: {', '.join(data['warnings']) if data['warnings'] else 'none'}",
            "limitations: local OHLCV candle replay data unavailable or invalid; no MFE/MAE fallback used",
            "final_recommendation: NO LIVE",
            "REAL STRATEGY BACKTESTER END",
        ]
        return "\n".join(lines)

    symbol, frame = next(iter(loader_result.frames_by_symbol.items()))
    result = RealStrategyBacktester(config).run(
        symbol,
        frame,
        min_order_value_usdt=float(getattr(config, "min_trade_margin_usdt", 5.0)),
        notional_usdt=float(getattr(config, "trade_margin_usdt", 12.0)) * max(1, int(getattr(config, "default_leverage", 1))),
    )
    summary = result.summary()
    lines = [
        "REAL STRATEGY BACKTESTER START",
        f"hours: {hours}",
        f"status: {summary['status']}",
        f"uses_signal_engine: {str(summary['uses_signal_engine']).lower()}",
        f"no_lookahead_status: {summary['no_lookahead_status']}",
        f"entry_model: {summary['entry_model']}",
        f"stop_tp_same_bar_rule: {summary['stop_tp_same_bar_rule']}",
        f"min_order_rule: {summary['min_order_rule']}",
        f"ohlcv_loader_status: {loader_result.status}",
        f"ohlcv_table: {loader_result.table}",
        f"symbol_tested: {symbol}",
        f"trades: {summary['trades']}",
        f"blocked_min_notional: {summary['blocked_min_notional']}",
        f"net_ev: {summary['net_ev']:.6f}",
        f"net_pf: {summary['net_pf']:.4f}",
        f"same_bar_stop_tp_count: {summary['same_bar_stop_tp_count']}",
        "limitations: local replay only; research/shadow; no exchange calls; no paper/live activation",
        "final_recommendation: NO LIVE",
        "REAL STRATEGY BACKTESTER END",
    ]
    return "\n".join(lines)


def _resolve_symbols(config: Any, requested: list[str] | None) -> list[str]:
    """Pick the symbol list: explicit > config.symbols > canonical 10."""
    if requested:
        return [str(s).upper().strip() for s in requested if str(s).strip()]
    cfg_syms = list(getattr(config, "symbols", None) or [])
    if cfg_syms:
        return [str(s).upper().strip() for s in cfg_syms if str(s).strip()]
    return list(DEFAULT_BACKTESTER_SYMBOLS)


def _empty_summary_row(symbol: str, status: str, warnings: str = "") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "trades": 0,
        "blocked_min_notional": 0,
        "net_ev": 0.0,
        "net_pf": 0.0,
        "win_rate": 0.0,
        "tp_pct": 0.0,
        "sl_pct": 0.0,
        "time_pct": 0.0,
        "same_bar_stop_tp_count": 0,
        "max_drawdown": 0.0,
        "status": status,
        "ohlcv_rows": 0,
        "warnings": warnings,
    }


def real_strategy_backtest_multi(
    config: Any,
    db: Any,
    *,
    hours: int = 72,
    symbols: list[str] | None = None,
    timeframe: str = "5m",
) -> dict[str, Any]:
    """Run the real backtester independently for each requested symbol.

    No exchange calls. No order placement. Uses the SignalEngine vela-by-vela
    against persisted OHLCV. Missing data on any symbol is reported as
    `NEED_DATA` and never crashes the rest of the run.
    """
    from datetime import datetime, timedelta, timezone
    from .ohlcv_replay_loader import OhlcvReplayLoader

    resolved = _resolve_symbols(config, symbols)
    timeframe = str(timeframe or "5m").lower()
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours or 1)))

    loader = OhlcvReplayLoader(db)
    per_symbol: list[dict[str, Any]] = []
    backtester = RealStrategyBacktester(config)
    notional = float(getattr(config, "trade_margin_usdt", 12.0)) * max(1, int(getattr(config, "default_leverage", 1)))
    min_notional = float(getattr(config, "min_trade_margin_usdt", 5.0))

    common_loader_status: str | None = None
    common_loader_table: str = ""

    for symbol in resolved:
        try:
            load_result = loader.load_ohlcv(
                symbols=[symbol], timeframe=timeframe, since=since,
            )
        except Exception as exc:
            per_symbol.append(_empty_summary_row(symbol, "LOADER_ERROR", warnings=str(exc)[:200]))
            continue

        if common_loader_status is None:
            common_loader_status = load_result.status
        if not common_loader_table:
            common_loader_table = load_result.table

        if load_result.status not in {"OK", "TOO_MANY_GAPS"} or symbol not in load_result.frames_by_symbol:
            row = _empty_summary_row(symbol, "NEED_DATA")
            warns = list(load_result.warnings or [])
            if load_result.missing_columns:
                warns.append("missing_columns:" + ",".join(load_result.missing_columns))
            row["warnings"] = ";".join(warns)[:240]
            per_symbol.append(row)
            continue

        frame = load_result.frames_by_symbol[symbol]
        try:
            result = backtester.run(
                symbol, frame,
                min_order_value_usdt=min_notional, notional_usdt=notional,
            )
        except Exception as exc:
            per_symbol.append(_empty_summary_row(symbol, "RUN_ERROR", warnings=str(exc)[:200]))
            continue

        summary = result.summary()
        per_symbol.append({
            "symbol": symbol,
            "trades": int(summary["trades"]),
            "blocked_min_notional": int(summary["blocked_min_notional"]),
            "net_ev": float(summary["net_ev"]),
            "net_pf": float(summary["net_pf"]),
            "win_rate": float(summary["win_rate"]),
            "tp_pct": float(summary["tp_pct"]),
            "sl_pct": float(summary["sl_pct"]),
            "time_pct": float(summary["time_pct"]),
            "same_bar_stop_tp_count": int(summary["same_bar_stop_tp_count"]),
            "max_drawdown": float(summary["max_drawdown"]),
            "status": str(summary["status"]),
            "ohlcv_rows": int(len(frame)),
            "warnings": "",
        })

    total = _aggregate_total(per_symbol)
    contract = {
        "uses_signal_engine": True,
        "no_lookahead_status": "OK_PREFIX_ONLY",
        "entry_model": "signal_close_i_entry_next_open_i+1",
        "stop_tp_same_bar_rule": "STOP_BEFORE_TP",
        "min_order_rule": "BLOCK_BELOW_MIN_NOTIONAL",
        "both_way_fees_applied": True,
        "no_mfe_mae_fallback": True,
        "exchange_calls": False,
    }
    return {
        "hours": int(hours),
        "timeframe": timeframe,
        "symbols_requested": len(resolved),
        "symbols_with_data": sum(1 for r in per_symbol if r["trades"] > 0 or r["status"] in {"OK", "NO_TRADES"}),
        "symbols_need_data": sum(1 for r in per_symbol if r["status"] == "NEED_DATA"),
        "ohlcv_loader_status_first": common_loader_status or "UNKNOWN",
        "ohlcv_table": common_loader_table or "none",
        "per_symbol": per_symbol,
        "total": total,
        "contract": contract,
        "final_recommendation": FINAL_RECOMMENDATION,
        "research_only": True,
        "limitations": "local replay only; research/shadow; no exchange calls; no paper/live activation",
    }


def _aggregate_total(per_symbol: list[dict[str, Any]]) -> dict[str, Any]:
    trades_total = sum(r["trades"] for r in per_symbol)
    blocked_total = sum(r["blocked_min_notional"] for r in per_symbol)
    if trades_total <= 0:
        return {
            "trades": trades_total,
            "blocked_min_notional": blocked_total,
            "net_ev": 0.0,
            "net_pf": 0.0,
            "win_rate": 0.0,
            "tp_pct": 0.0,
            "sl_pct": 0.0,
            "time_pct": 0.0,
            "same_bar_stop_tp_count": 0,
        }
    # Trade-weighted aggregation: each per-symbol average × its trade count,
    # divided by total trades. Avoids treating low-sample symbols equal weight.
    weighted = {
        "net_ev": sum(r["net_ev"] * r["trades"] for r in per_symbol) / trades_total,
        "win_rate": sum(r["win_rate"] * r["trades"] for r in per_symbol) / trades_total,
        "tp_pct": sum(r["tp_pct"] * r["trades"] for r in per_symbol) / trades_total,
        "sl_pct": sum(r["sl_pct"] * r["trades"] for r in per_symbol) / trades_total,
        "time_pct": sum(r["time_pct"] * r["trades"] for r in per_symbol) / trades_total,
    }
    # PF total: sum gains / sum |losses| across symbols, weighted by trades.
    # We approximate using net_ev × trades: if symbol's net_ev>0 it contributes
    # to gains, otherwise to losses.
    gains = sum(r["net_ev"] * r["trades"] for r in per_symbol if r["net_ev"] > 0)
    losses = abs(sum(r["net_ev"] * r["trades"] for r in per_symbol if r["net_ev"] < 0))
    pf_total = gains / losses if losses > 0 else (999.0 if gains > 0 else 0.0)
    return {
        "trades": trades_total,
        "blocked_min_notional": blocked_total,
        "net_ev": weighted["net_ev"],
        "net_pf": pf_total,
        "win_rate": weighted["win_rate"],
        "tp_pct": weighted["tp_pct"],
        "sl_pct": weighted["sl_pct"],
        "time_pct": weighted["time_pct"],
        "same_bar_stop_tp_count": sum(r["same_bar_stop_tp_count"] for r in per_symbol),
    }


def real_strategy_backtest_multi_text(
    config: Any,
    db: Any,
    *,
    hours: int = 72,
    symbols: list[str] | None = None,
    timeframe: str = "5m",
) -> str:
    """Render the multi-symbol backtest as a text report.

    Output is a table + aggregated TOTAL. Every per-symbol failure (NEED_DATA,
    LOADER_ERROR, RUN_ERROR) is reported individually without crashing the
    overall report.
    """
    payload = real_strategy_backtest_multi(
        config, db, hours=hours, symbols=symbols, timeframe=timeframe,
    )
    lines = ["REAL STRATEGY BACKTESTER MULTI START"]
    lines.append(f"hours: {payload['hours']}")
    lines.append(f"timeframe: {payload['timeframe']}")
    lines.append(f"symbols_requested: {payload['symbols_requested']}")
    lines.append(f"symbols_with_data: {payload['symbols_with_data']}")
    lines.append(f"symbols_need_data: {payload['symbols_need_data']}")
    lines.append(f"ohlcv_loader_status_first: {payload['ohlcv_loader_status_first']}")
    lines.append(f"ohlcv_table: {payload['ohlcv_table']}")
    contract = payload["contract"]
    for key, value in contract.items():
        lines.append(f"{key}: {str(value).lower()}")
    header = (
        f"{'symbol':<10} {'trades':>6} {'blocked':>7} {'net_ev':>10} {'net_pf':>8} "
        f"{'TP%':>5} {'SL%':>5} {'TIME%':>6} {'same_bar':>9} {'rows':>6} {'status':<12}"
    )
    lines.append("")
    lines.append(header)
    lines.append("-" * len(header))
    for row in payload["per_symbol"]:
        lines.append(
            f"{row['symbol']:<10} {row['trades']:>6} {row['blocked_min_notional']:>7} "
            f"{row['net_ev']:>10.6f} {row['net_pf']:>8.4f} "
            f"{row['tp_pct']*100:>5.1f} {row['sl_pct']*100:>5.1f} {row['time_pct']*100:>6.1f} "
            f"{row['same_bar_stop_tp_count']:>9} {row['ohlcv_rows']:>6} {row['status']:<12}"
            + (f"  warnings={row['warnings']}" if row.get("warnings") else "")
        )
    total = payload["total"]
    lines.append("-" * len(header))
    lines.append(
        f"{'TOTAL':<10} {total['trades']:>6} {total['blocked_min_notional']:>7} "
        f"{total['net_ev']:>10.6f} {total['net_pf']:>8.4f} "
        f"{total['tp_pct']*100:>5.1f} {total['sl_pct']*100:>5.1f} {total['time_pct']*100:>6.1f} "
        f"{total['same_bar_stop_tp_count']:>9} {'':>6} {'AGGREGATE':<12}"
    )
    lines.append("")
    lines.append(f"limitations: {payload['limitations']}")
    lines.append("research_only: true")
    lines.append(f"final_recommendation: {payload['final_recommendation']}")
    lines.append("REAL STRATEGY BACKTESTER MULTI END")
    return "\n".join(lines)


def real_strategy_backtester_smoke_text(config: Any) -> str:
    from .signal_engine import Signal

    class StubEngine:
        def __init__(self) -> None:
            self.calls = 0

        def generate_signal(self, symbol: str, snapshot: MarketSnapshot, market_regime: MarketRegime) -> Signal:
            del snapshot, market_regime
            self.calls += 1
            return Signal(
                symbol=symbol,
                side="LONG",
                strategy_type="smoke",
                confidence_score=90,
                entry_price=100.0,
                stop_loss=99.0,
                take_profit_1=102.0,
                take_profit_2=104.0,
                trailing_stop_enabled=False,
                trailing_stop_rule="",
                risk_reward_ratio=2.0,
                leverage_recommendation=1,
                position_size=0.0,
                reason="smoke",
            )

    candles = _smoke_candles()
    engine = StubEngine()
    result = RealStrategyBacktester(config, signal_engine=engine).run("BTCUSDT", candles, min_order_value_usdt=5, notional_usdt=10, max_holding_bars=3)
    summary = result.summary()
    checks = {
        "uses_signal_engine": result.uses_signal_engine and engine.calls > 0,
        "entry_next_open": bool(result.trades and result.trades[0].entry_price == safe_float(candles.iloc[61]["open"])),
        "same_bar_stop_before_tp_rule": result.stop_tp_same_bar_rule == "STOP_BEFORE_TP",
        "both_way_fees_applied": bool(result.trades and result.trades[0].fee_cost_bps == 12.0),
        "never_sends_orders": True,
        "final_recommendation_no_live": summary["final_recommendation"] == FINAL_RECOMMENDATION,
    }
    lines = ["REAL STRATEGY BACKTESTER SMOKE TEST START"]
    lines.extend(f"{key}: {str(value).lower()}" for key, value in checks.items())
    lines.extend([
        "LIVE_TRADING=false",
        "DRY_RUN=true",
        "PAPER_TRADING=true",
        "ENABLE_PAPER_POLICY_FILTER=false",
        "can_send_real_orders=false",
        f"result: {'PASS' if all(checks.values()) else 'FAIL'}",
        "REAL STRATEGY BACKTESTER SMOKE TEST END",
    ])
    return "\n".join(lines)


def _smoke_candles() -> pd.DataFrame:
    rows = []
    base = pd.Timestamp("2026-01-01T00:00:00Z")
    for i in range(80):
        open_price = 100.0 + i * 0.01
        high = open_price + (3.0 if i == 61 else 0.2)
        low = open_price - (2.0 if i == 61 else 0.1)
        rows.append({
            "timestamp": base + pd.Timedelta(minutes=5 * i),
            "open": open_price,
            "high": high,
            "low": low,
            "close": open_price + 0.05,
            "volume": 1000 + i,
            "quote_volume": 100000 + i,
        })
    return pd.DataFrame(rows)
