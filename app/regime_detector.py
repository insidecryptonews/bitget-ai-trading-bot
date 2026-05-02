from __future__ import annotations

from dataclasses import dataclass, field

from .indicators import has_enough_data, latest_row, trend_bias
from .market_data import MarketSnapshot
from .utils import safe_float


REGIMES = {
    "TREND_UP",
    "TREND_DOWN",
    "RANGE",
    "HIGH_VOLATILITY",
    "BREAKOUT_POSSIBLE",
    "CHOPPY_MARKET",
    "RISK_OFF",
    "RISK_ON",
}


@dataclass
class MarketRegime:
    regime: str
    allowed_direction: str = "BOTH"
    market_bias: str = "neutral"
    volatility_level: str = "normal"
    risk_on: bool = False
    risk_off: bool = False
    score_adjustment: int = 0
    notes: list[str] = field(default_factory=list)
    blocked_longs: bool = False
    blocked_shorts: bool = False


class RegimeDetector:
    def __init__(self, logger=None) -> None:
        self.logger = logger

    def detect(self, snapshots: dict[str, MarketSnapshot]) -> MarketRegime:
        btc = snapshots.get("BTCUSDT")
        eth = snapshots.get("ETHUSDT")
        if not btc or "15m" not in btc.candles or "1h" not in btc.candles:
            return MarketRegime("CHOPPY_MARKET", allowed_direction="NONE", notes=["BTC sin datos suficientes"])

        btc_15 = btc.candles["15m"]
        btc_1h = btc.candles["1h"]
        if not has_enough_data(btc_15, 50) or not has_enough_data(btc_1h, 50):
            return MarketRegime("CHOPPY_MARKET", allowed_direction="NONE", notes=["Historial BTC insuficiente"])

        row15 = latest_row(btc_15)
        row1h = latest_row(btc_1h)
        btc_bias_15 = trend_bias(btc_15)
        btc_bias_1h = trend_bias(btc_1h)
        eth_bias = trend_bias(eth.candles.get("15m")) if eth and "15m" in eth.candles else "neutral"

        normalized_atr = safe_float(row15.get("normalized_atr"))
        momentum_15 = safe_float(row15.get("momentum_15"))
        momentum_1h = safe_float(row1h.get("momentum_5"))
        volume_rel = safe_float(row15.get("volume_relative"), 1.0)

        bullish = 0
        bearish = 0
        for snap in snapshots.values():
            df = snap.candles.get("15m")
            bias = trend_bias(df) if df is not None else "neutral"
            bullish += bias == "bullish"
            bearish += bias == "bearish"

        notes: list[str] = []
        volatility_level = "normal"
        if normalized_atr > 0.025 or abs(momentum_15) > 0.04:
            volatility_level = "extreme"
            notes.append("ATR/momentum BTC extremo")
            return MarketRegime(
                "HIGH_VOLATILITY",
                allowed_direction="NONE" if abs(momentum_15) > 0.06 else "BOTH",
                market_bias="bearish" if momentum_15 < 0 else "bullish",
                volatility_level=volatility_level,
                score_adjustment=-25,
                notes=notes,
            )

        if momentum_15 < -0.025 or (btc_bias_15 == "bearish" and btc_bias_1h == "bearish" and eth_bias != "bullish"):
            notes.append("BTC domina a la baja")
            return MarketRegime(
                "RISK_OFF",
                allowed_direction="SHORT",
                market_bias="bearish",
                volatility_level=volatility_level,
                risk_off=True,
                blocked_longs=True,
                score_adjustment=-10,
                notes=notes,
            )

        if momentum_15 > 0.025 or (btc_bias_15 == "bullish" and btc_bias_1h == "bullish" and eth_bias != "bearish"):
            notes.append("BTC/ETH acompañan al alza")
            return MarketRegime(
                "RISK_ON",
                allowed_direction="LONG",
                market_bias="bullish",
                risk_on=True,
                blocked_shorts=True,
                score_adjustment=5,
                notes=notes,
            )

        if btc_bias_15 == "bullish" and btc_bias_1h in {"bullish", "neutral"} and bullish >= bearish:
            return MarketRegime("TREND_UP", allowed_direction="LONG", market_bias="bullish", score_adjustment=3)

        if btc_bias_15 == "bearish" and btc_bias_1h in {"bearish", "neutral"} and bearish >= bullish:
            return MarketRegime("TREND_DOWN", allowed_direction="SHORT", market_bias="bearish", score_adjustment=3)

        range_width = safe_float(row15.get("range_width_pct"))
        if volume_rel > 1.6 and safe_float(row15.get("volatility_compression")):
            return MarketRegime("BREAKOUT_POSSIBLE", allowed_direction="BOTH", notes=["Compresión con volumen"])

        if range_width < 0.012 or abs(momentum_1h) < 0.006:
            return MarketRegime(
                "CHOPPY_MARKET",
                allowed_direction="NONE",
                market_bias="neutral",
                score_adjustment=-20,
                notes=["Mercado lateral/choppy"],
            )

        return MarketRegime("RANGE", allowed_direction="BOTH", market_bias="neutral", score_adjustment=-5)

