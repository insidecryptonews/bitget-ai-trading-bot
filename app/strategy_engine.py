from __future__ import annotations

from dataclasses import dataclass, field

from .indicators import has_enough_data, latest_row, trend_bias
from .market_data import MarketSnapshot
from .regime_detector import MarketRegime
from .utils import safe_float


class StrategyType:
    TREND_FOLLOWING = "TREND_FOLLOWING"
    BREAKOUT = "BREAKOUT"
    PULLBACK = "PULLBACK"
    MOMENTUM_FAST = "MOMENTUM_FAST"
    MEAN_REVERSION_CONTROLLED = "MEAN_REVERSION_CONTROLLED"
    SUPPORT_RESISTANCE_REJECTION = "SUPPORT_RESISTANCE_REJECTION"
    NO_TRADE = "NO_TRADE"


@dataclass
class StrategyDecision:
    strategy_type: str = StrategyType.NO_TRADE
    confidence: int = 0
    reason: str = "Sin ventaja clara"
    preferred_symbols: list[str] = field(default_factory=list)
    blocked_symbols: list[str] = field(default_factory=list)
    allowed_direction: str = "NONE"


class StrategyEngine:
    def evaluate(self, symbol: str, snapshot: MarketSnapshot, regime: MarketRegime) -> StrategyDecision:
        df5 = snapshot.candles.get("5m")
        df15 = snapshot.candles.get("15m")
        df1h = snapshot.candles.get("1h")
        if not has_enough_data(df5, 60) or not has_enough_data(df15, 60):
            return StrategyDecision(reason="Datos insuficientes", blocked_symbols=[symbol])

        row5 = latest_row(df5)
        row15 = latest_row(df15)
        row1h = latest_row(df1h) if has_enough_data(df1h, 50) else row15
        bias5 = trend_bias(df5)
        bias15 = trend_bias(df15)
        bias1h = trend_bias(df1h) if df1h is not None else "neutral"
        volume_rel = safe_float(row5.get("volume_relative"), 1.0)
        atr = safe_float(row5.get("atr_14"))
        close = safe_float(row5.get("close"))
        rsi = safe_float(row5.get("rsi_14"), 50)
        macd_hist = safe_float(row5.get("macd_hist"))
        support = safe_float(row5.get("support_recent"))
        resistance = safe_float(row5.get("resistance_recent"))
        prev_range_high = safe_float(df5["range_high_30"].iloc[-2]) if len(df5) > 31 else resistance
        prev_range_low = safe_float(df5["range_low_30"].iloc[-2]) if len(df5) > 31 else support

        if regime.allowed_direction == "NONE":
            return StrategyDecision(reason=f"Régimen {regime.regime} bloquea entradas")

        candidates: list[StrategyDecision] = []

        if close and atr:
            near_ema21 = abs(close - safe_float(row5.get("ema_21"))) <= atr * 0.8
            if bias5 == bias15 and bias15 in {"bullish", "bearish"} and near_ema21 and 35 < rsi < 70:
                direction = "LONG" if bias15 == "bullish" else "SHORT"
                candidates.append(
                    StrategyDecision(
                        StrategyType.PULLBACK,
                        78,
                        "Pullback sano en tendencia con EMA21/50 respetadas",
                        [symbol],
                        [],
                        direction,
                    )
                )

        if volume_rel > 1.45 and close:
            if close > prev_range_high and macd_hist > 0:
                candidates.append(
                    StrategyDecision(StrategyType.BREAKOUT, 82, "Ruptura alcista con volumen", [symbol], [], "LONG")
                )
            elif close < prev_range_low and macd_hist < 0:
                candidates.append(
                    StrategyDecision(StrategyType.BREAKOUT, 82, "Ruptura bajista con volumen", [symbol], [], "SHORT")
                )

        if volume_rel > 1.8 and abs(safe_float(row5.get("momentum_5"))) > safe_float(row5.get("normalized_atr")) * 1.2:
            if safe_float(row5.get("momentum_5")) > 0 and regime.allowed_direction != "SHORT":
                candidates.append(
                    StrategyDecision(StrategyType.MOMENTUM_FAST, 80, "Momentum rápido alcista con volumen", [symbol], [], "LONG")
                )
            elif safe_float(row5.get("momentum_5")) < 0 and regime.allowed_direction != "LONG":
                candidates.append(
                    StrategyDecision(StrategyType.MOMENTUM_FAST, 80, "Momentum rápido bajista con volumen", [symbol], [], "SHORT")
                )

        if symbol in {"BTCUSDT", "ETHUSDT"}:
            bullish_reversal = rsi < 28 and bool(row5.get("bullish_rejection")) and bias1h != "bearish"
            bearish_reversal = rsi > 72 and bool(row5.get("bearish_rejection")) and bias1h != "bullish"
            if bullish_reversal:
                candidates.append(
                    StrategyDecision(
                        StrategyType.MEAN_REVERSION_CONTROLLED,
                        68,
                        "Reversión controlada en BTC/ETH desde RSI extremo",
                        [symbol],
                        [],
                        "LONG",
                    )
                )
            if bearish_reversal:
                candidates.append(
                    StrategyDecision(
                        StrategyType.MEAN_REVERSION_CONTROLLED,
                        68,
                        "Reversión controlada en BTC/ETH desde RSI extremo",
                        [symbol],
                        [],
                        "SHORT",
                    )
                )

        if support and resistance and close:
            if bool(row5.get("bullish_rejection")) and abs(close - support) / close < 0.008:
                candidates.append(
                    StrategyDecision(
                        StrategyType.SUPPORT_RESISTANCE_REJECTION,
                        74,
                        "Rechazo limpio en soporte reciente",
                        [symbol],
                        [],
                        "LONG",
                    )
                )
            if bool(row5.get("bearish_rejection")) and abs(close - resistance) / close < 0.008:
                candidates.append(
                    StrategyDecision(
                        StrategyType.SUPPORT_RESISTANCE_REJECTION,
                        74,
                        "Rechazo limpio en resistencia reciente",
                        [symbol],
                        [],
                        "SHORT",
                    )
                )

        if bias5 == bias15 == bias1h and bias15 in {"bullish", "bearish"}:
            direction = "LONG" if bias15 == "bullish" else "SHORT"
            candidates.append(
                StrategyDecision(
                    StrategyType.TREND_FOLLOWING,
                    76,
                    "Tendencia alineada 5m/15m/1h",
                    [symbol],
                    [],
                    direction,
                )
            )

        if not candidates:
            return StrategyDecision(reason="No hay confluencia suficiente")

        candidates = [c for c in candidates if regime.allowed_direction in {"BOTH", c.allowed_direction}]
        if not candidates:
            return StrategyDecision(reason=f"Señal contradice régimen {regime.regime}")
        return max(candidates, key=lambda item: item.confidence)

