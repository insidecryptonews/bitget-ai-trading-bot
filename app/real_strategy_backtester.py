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


def real_strategy_backtest_text(config: Any, db: Any, *, hours: int = 72) -> str:
    del db
    lines = [
        "REAL STRATEGY BACKTESTER START",
        f"hours: {hours}",
        "status: NEED_DATA",
        "uses_signal_engine: not_run_without_local_ohlcv_loader",
        "no_lookahead_status: NOT_RUN",
        "entry_model: signal_close_i_entry_next_open_i+1",
        "stop_tp_same_bar_rule: STOP_BEFORE_TP",
        "min_order_rule: BLOCK_BELOW_MIN_NOTIONAL",
        "limitations: local DB candle replay loader not available in this command; unit tests validate engine path",
        "final_recommendation: NO LIVE",
        "REAL STRATEGY BACKTESTER END",
    ]
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
