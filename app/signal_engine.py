from __future__ import annotations

from dataclasses import dataclass, field

from .config import BotConfig
from .dynamic_exit_policy import propose_dynamic_tp_sl
from .indicators import has_enough_data, latest_row, trend_bias
from .market_data import MarketSnapshot
from .net_rr import calculate_net_rr
from .regime_detector import MarketRegime
from .strategy_engine import StrategyEngine, StrategyType
from .structural_stop import calculate_structural_stop
from .utils import clamp, safe_float


@dataclass
class Signal:
    symbol: str
    side: str
    strategy_type: str
    confidence_score: int
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    trailing_stop_enabled: bool
    trailing_stop_rule: str
    risk_reward_ratio: float
    leverage_recommendation: int
    position_size: float
    reason: str
    confirmations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    timeframe_alignment: str = "neutral"
    invalidation_level: float = 0.0
    estimated_fees: float = 0.0
    estimated_slippage: float = 0.0
    gross_rr: float = 0.0
    net_rr: float = 0.0
    rr_cost_adjusted: bool = False
    cost_breakdown: dict = field(default_factory=dict)
    rr_warning: str = ""
    minimum_winrate_required_from_net_rr: float = 0.0
    stop_quality: str = ""
    whipsaw_risk: float = 0.0
    dynamic_exit_candidate: dict = field(default_factory=dict)


class SignalEngine:
    def __init__(self, config: BotConfig, strategy_engine: StrategyEngine | None = None) -> None:
        self.config = config
        self.strategy_engine = strategy_engine or StrategyEngine()

    def no_trade(self, symbol: str, reason: str, score: int = 0, warnings: list[str] | None = None) -> Signal:
        return Signal(
            symbol=symbol,
            side="NO_TRADE",
            strategy_type=StrategyType.NO_TRADE,
            confidence_score=score,
            entry_price=0.0,
            stop_loss=0.0,
            take_profit_1=0.0,
            take_profit_2=0.0,
            trailing_stop_enabled=False,
            trailing_stop_rule="",
            risk_reward_ratio=0.0,
            leverage_recommendation=0,
            position_size=0.0,
            reason=reason,
            warnings=warnings or [],
        )

    def generate_signal(self, symbol: str, snapshot: MarketSnapshot, market_regime: MarketRegime) -> Signal:
        if snapshot.error:
            return self.no_trade(symbol, f"Error de mercado: {snapshot.error}", warnings=[snapshot.error])
        df5 = snapshot.candles.get(self.config.main_timeframe.lower())
        if df5 is None:
            df5 = snapshot.candles.get("5m")
        df15 = snapshot.candles.get(self.config.confirmation_timeframe.lower())
        if df15 is None:
            df15 = snapshot.candles.get("15m")
        df1h = snapshot.candles.get(self.config.higher_timeframe.lower())
        if df1h is None:
            df1h = snapshot.candles.get("1h")
        if not has_enough_data(df5, 60) or not has_enough_data(df15, 60):
            return self.no_trade(symbol, "No trade: datos OHLCV insuficientes", warnings=["datos incompletos"])

        row5 = latest_row(df5)
        row15 = latest_row(df15)
        row1h = latest_row(df1h) if has_enough_data(df1h, 50) else row15
        decision = self.strategy_engine.evaluate(symbol, snapshot, market_regime)
        if decision.strategy_type == StrategyType.NO_TRADE:
            return self.no_trade(symbol, f"No trade: {decision.reason}", score=decision.confidence)

        proposed_side = decision.allowed_direction
        if market_regime.blocked_longs and proposed_side == "LONG":
            return self.no_trade(symbol, "No trade: BTC contradice violentamente longs de altcoins", score=decision.confidence)
        if market_regime.blocked_shorts and proposed_side == "SHORT":
            return self.no_trade(symbol, "No trade: BTC contradice violentamente shorts", score=decision.confidence)

        entry = snapshot.current_price or safe_float(row5.get("close"))
        if entry <= 0:
            return self.no_trade(symbol, "No trade: precio actual inválido")

        atr = safe_float(row5.get("atr_14"))
        if atr <= 0:
            return self.no_trade(symbol, "No trade: ATR no disponible", warnings=["datos incompletos"])

        score = 0
        confirmations: list[str] = []
        warnings: list[str] = []
        bias5 = trend_bias(df5)
        bias15 = trend_bias(df15)
        bias1h = trend_bias(df1h) if df1h is not None else "neutral"
        desired_bias = "bullish" if proposed_side == "LONG" else "bearish"

        if bias5 == desired_bias and bias15 == desired_bias:
            score += 20
            confirmations.append("tendencia 5m/15m alineada")
        if bias1h in {desired_bias, "neutral"}:
            score += 10
            confirmations.append("1h no contradice")

        ema_ok = (
            row5["close"] > row5["ema_21"] > row5["ema_50"]
            if proposed_side == "LONG"
            else row5["close"] < row5["ema_21"] < row5["ema_50"]
        )
        if ema_ok or decision.strategy_type == StrategyType.MEAN_REVERSION_CONTROLLED:
            score += 15
            confirmations.append("EMAs a favor")

        macd_ok = safe_float(row5.get("macd_hist")) > 0 if proposed_side == "LONG" else safe_float(row5.get("macd_hist")) < 0
        if macd_ok:
            score += 12
            confirmations.append("MACD a favor")

        rsi = safe_float(row5.get("rsi_14"), 50)
        rsi_ok = 45 <= rsi <= 72 if proposed_side == "LONG" else 28 <= rsi <= 55
        if rsi_ok or decision.strategy_type == StrategyType.MEAN_REVERSION_CONTROLLED:
            score += 10
            confirmations.append("RSI sano")

        volume_rel = safe_float(row5.get("volume_relative"), 1.0)
        if volume_rel > 1.2:
            score += 15 if volume_rel > 1.8 else 10
            confirmations.append("volumen relativo alto")

        support = safe_float(row5.get("support_recent"))
        resistance = safe_float(row5.get("resistance_recent"))
        normalized_atr = safe_float(row5.get("normalized_atr"))
        stop_result = calculate_structural_stop(
            side=proposed_side,
            entry=entry,
            atr=atr,
            support=support,
            resistance=resistance,
            regime=market_regime.regime,
            volatility=normalized_atr,
            config=self.config,
        )
        stop_loss = stop_result.stop_loss
        if stop_result.stop_quality in {"TOO_TIGHT_REJECT", "TOO_WIDE_REJECT", "INVALID_INPUT"}:
            return self.no_trade(symbol, f"No trade: stop estructural invalido ({stop_result.stop_quality})", warnings=[stop_result.reason])
        if stop_result.stop_quality:
            confirmations.append(f"stop_quality={stop_result.stop_quality}")
        if stop_result.whipsaw_risk >= 0.8:
            warnings.append("alto riesgo de whipsaw en stop")
        min_stop_distance = max(entry * self.config.min_stop_distance_pct, atr)
        if abs(entry - stop_loss) < min_stop_distance:
            stop_loss = entry - min_stop_distance if proposed_side == "LONG" else entry + min_stop_distance
            warnings.append(f"stop ensanchado por MIN_STOP_DISTANCE_PCT/ATR: {min_stop_distance / entry:.4%}")
        risk_per_unit = abs(entry - stop_loss)
        if risk_per_unit <= 0:
            return self.no_trade(symbol, "No trade: falta stop lógico", warnings=["sin stop lógico"])
        take_profit_1 = entry + risk_per_unit * 1.6 if proposed_side == "LONG" else entry - risk_per_unit * 1.6
        take_profit_2 = entry + risk_per_unit * 2.4 if proposed_side == "LONG" else entry - risk_per_unit * 2.4
        gross_risk_reward = abs(take_profit_1 - entry) / risk_per_unit
        rr_result = calculate_net_rr(
            entry=entry,
            stop_loss=stop_loss,
            take_profit_1=take_profit_1,
            side=proposed_side,
            slippage_bps=self.config.net_edge_slippage_bps,
            funding_rate=snapshot.funding_rate,
            min_net_rr=self.config.min_risk_reward,
        )
        risk_reward = rr_result.net_rr
        if risk_reward >= 1.8:
            score += 10
            confirmations.append("R:R neto mayor a 1.8")
        elif risk_reward < self.config.min_risk_reward:
            score -= 20
            warnings.append("R:R neto insuficiente")
        if rr_result.gross_rr > rr_result.net_rr + 0.15:
            warnings.append("R:R bruto cae al ajustar fees/slippage")
        dynamic_exit = propose_dynamic_tp_sl(
            symbol=symbol,
            side=proposed_side,
            regime=market_regime.regime,
            entry=entry,
            atr=atr,
            support=support,
            resistance=resistance,
            volatility=normalized_atr,
            score=decision.confidence,
            context={"strategy_type": decision.strategy_type},
        )
        if dynamic_exit.prefer_no_trade:
            score -= 8
            warnings.append("dynamic_exit_shadow prefiere no trade en este regimen")

        if decision.strategy_type in {StrategyType.BREAKOUT, StrategyType.SUPPORT_RESISTANCE_REJECTION}:
            score += 10
            confirmations.append("ruptura/rechazo claro")
        if abs(entry - safe_float(row5.get("ema_21"))) <= atr * 1.1:
            score += 8
            confirmations.append("entrada cerca de zona óptima")
        if snapshot.funding_rate:
            funding_favorable = snapshot.funding_rate <= 0 if proposed_side == "LONG" else snapshot.funding_rate >= 0
            if funding_favorable:
                score += 5
                confirmations.append("funding favorable")
        if market_regime.allowed_direction in {"BOTH", proposed_side}:
            score += 5
            confirmations.append("mercado general alineado")
        if market_regime.market_bias in {desired_bias, "neutral"}:
            score += 5
            confirmations.append("BTC/ETH acompañan")

        score += market_regime.score_adjustment

        if normalized_atr > 0.025:
            score -= 25
            warnings.append("ATR extremo")
        if snapshot.spread_pct > 0.0015:
            score -= 20
            warnings.append("spread alto")
        if market_regime.regime in {"CHOPPY_MARKET", "RANGE"}:
            score -= 20 if market_regime.regime == "CHOPPY_MARKET" else 8
            warnings.append("mercado lateral/choppy")
        if desired_bias == "bullish" and market_regime.market_bias == "bearish":
            score -= 15
            warnings.append("señal contra BTC dominante")
        if desired_bias == "bearish" and market_regime.market_bias == "bullish":
            score -= 15
            warnings.append("señal contra BTC dominante")
        if snapshot.volume_24h_usdt and snapshot.volume_24h_usdt < 20_000_000:
            score -= 15
            warnings.append("símbolo con liquidez baja")
        candle_extension = safe_float(row5.get("body_pct")) > normalized_atr * 1.8 if normalized_atr else False
        if candle_extension and decision.strategy_type != StrategyType.BREAKOUT:
            score -= 10
            warnings.append("entrada tarde tras vela extendida")
        if len(confirmations) < 3:
            score -= 25
            warnings.append("menos de 3 confirmaciones fuertes")

        score = int(clamp(score, 0, 100))
        leverage = self.config.default_leverage
        if score >= 90:
            leverage = min(self.config.max_leverage, 5)

        side = proposed_side if score >= self.config.min_score_to_trade and len(confirmations) >= 3 else "NO_TRADE"
        reason = decision.reason
        if side == "NO_TRADE":
            reason = f"No trade: score {score} < {self.config.min_score_to_trade} o confirmaciones insuficientes. {decision.reason}"

        return Signal(
            symbol=symbol,
            side=side,
            strategy_type=decision.strategy_type if side != "NO_TRADE" else StrategyType.NO_TRADE,
            confidence_score=score,
            entry_price=entry,
            stop_loss=stop_loss,
            take_profit_1=take_profit_1,
            take_profit_2=take_profit_2,
            trailing_stop_enabled=score >= self.config.min_score_excellent,
            trailing_stop_rule="ATR 1.2 tras TP1; mover SL a break-even",
            risk_reward_ratio=risk_reward,
            leverage_recommendation=leverage,
            position_size=0.0,
            reason=reason,
            confirmations=confirmations,
            warnings=warnings,
            timeframe_alignment=f"5m={bias5},15m={bias15},1h={bias1h}",
            invalidation_level=stop_loss,
            gross_rr=gross_risk_reward,
            net_rr=rr_result.net_rr,
            rr_cost_adjusted=rr_result.rr_cost_adjusted,
            cost_breakdown=rr_result.cost_breakdown,
            rr_warning=rr_result.rr_warning,
            minimum_winrate_required_from_net_rr=rr_result.minimum_winrate_required_from_net_rr,
            stop_quality=stop_result.stop_quality,
            whipsaw_risk=stop_result.whipsaw_risk,
            dynamic_exit_candidate=dynamic_exit.as_dict(),
        )

    @staticmethod
    def _calculate_stop(side: str, entry: float, atr: float, row) -> float:
        result = calculate_structural_stop(
            side=side,
            entry=entry,
            atr=atr,
            support=safe_float(row.get("support_recent")),
            resistance=safe_float(row.get("resistance_recent")),
            regime=str(row.get("market_regime") or ""),
        )
        return result.stop_loss
