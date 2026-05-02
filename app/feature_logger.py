from __future__ import annotations

import math
from dataclasses import asdict, is_dataclass
from typing import Any

from .database import Database
from .indicators import latest_row, trend_bias
from .market_data import MarketSnapshot
from .regime_detector import MarketRegime
from .signal_engine import Signal
from .utils import iso_utc, json_dumps, safe_float


class FeatureLogger:
    """Persists every generated signal so research never depends only on executed trades."""

    def __init__(self, db: Database, logger=None) -> None:
        self.db = db
        self.logger = logger

    def log_signal(
        self,
        *,
        signal: Signal,
        snapshot: MarketSnapshot | None,
        market_regime: MarketRegime | None,
        all_snapshots: dict[str, MarketSnapshot] | None = None,
        operated: bool = False,
        block_reason: str = "",
        selected_by_allocator: bool = False,
        risk_manager_approved: bool = False,
    ) -> int:
        observation = self.build_observation(
            signal=signal,
            snapshot=snapshot,
            market_regime=market_regime,
            all_snapshots=all_snapshots or {},
            operated=operated,
            block_reason=block_reason,
            selected_by_allocator=selected_by_allocator,
            risk_manager_approved=risk_manager_approved,
        )
        return self.record_observation(observation)

    def record_observation(self, observation: dict[str, Any]) -> int:
        observation_id = self.db.record_signal_observation(observation)
        if self.logger:
            self.logger.debug("Signal observation saved %s id=%s", observation.get("symbol"), observation_id)
        return observation_id

    def update_observation(self, observation_id: int | None, **updates: Any) -> None:
        if not observation_id:
            return
        cleaned = {
            key: self._bool_int(value) if key in {"operated", "selected_by_allocator", "risk_manager_approved"} else value
            for key, value in updates.items()
        }
        self.db.update_signal_observation(observation_id, **cleaned)

    def build_observation(
        self,
        *,
        signal: Signal,
        snapshot: MarketSnapshot | None,
        market_regime: MarketRegime | None,
        all_snapshots: dict[str, MarketSnapshot],
        operated: bool,
        block_reason: str,
        selected_by_allocator: bool,
        risk_manager_approved: bool,
    ) -> dict[str, Any]:
        row = self._latest_feature_row(snapshot)
        btc = all_snapshots.get("BTCUSDT")
        eth = all_snapshots.get("ETHUSDT")
        btc_row = self._latest_feature_row(btc, timeframe="15m")
        eth_row = self._latest_feature_row(eth, timeframe="15m")
        bullish, bearish = self._market_breadth(all_snapshots)

        close = self._feature(row, "close", signal.entry_price)
        observation = {
            "timestamp": iso_utc(),
            "symbol": signal.symbol,
            "side": signal.side,
            "strategy_type": signal.strategy_type,
            "confidence_score": signal.confidence_score,
            "market_regime": market_regime.regime if market_regime else "",
            "entry_price": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "take_profit_1": signal.take_profit_1,
            "take_profit_2": signal.take_profit_2,
            "risk_reward_ratio": signal.risk_reward_ratio,
            "leverage_recommendation": signal.leverage_recommendation,
            "spread_pct": snapshot.spread_pct if snapshot else 0.0,
            "volume_24h_usdt": snapshot.volume_24h_usdt if snapshot else 0.0,
            "funding_rate": snapshot.funding_rate if snapshot else 0.0,
            "open_interest": snapshot.open_interest if snapshot else 0.0,
            "timeframe_alignment": signal.timeframe_alignment,
            "confirmations_json": json_dumps(signal.confirmations),
            "warnings_json": json_dumps(signal.warnings),
            "rsi_14": self._feature(row, "rsi_14"),
            "macd_hist": self._feature(row, "macd_hist"),
            "atr_14": self._feature(row, "atr_14"),
            "normalized_atr": self._feature(row, "normalized_atr"),
            "volume_relative": self._feature(row, "volume_relative"),
            "distance_to_ema_21": self._distance(row, close, "ema_21"),
            "distance_to_ema_50": self._distance(row, close, "ema_50"),
            "distance_to_ema_200": self._feature(row, "distance_to_ema_200", self._distance(row, close, "ema_200")),
            "momentum_5": self._feature(row, "momentum_5"),
            "momentum_15": self._feature(row, "momentum_15"),
            "range_width_pct": self._feature(row, "range_width_pct"),
            "body_pct": self._feature(row, "body_pct"),
            "upper_wick_pct": self._feature(row, "upper_wick_pct"),
            "lower_wick_pct": self._feature(row, "lower_wick_pct"),
            "bullish_rejection": self._bool_int(self._raw(row, "bullish_rejection")),
            "bearish_rejection": self._bool_int(self._raw(row, "bearish_rejection")),
            "btc_regime": market_regime.regime if market_regime else "",
            "btc_momentum_5": self._feature(btc_row, "momentum_5"),
            "btc_momentum_15": self._feature(btc_row, "momentum_15"),
            "btc_normalized_atr": self._feature(btc_row, "normalized_atr"),
            "eth_momentum_5": self._feature(eth_row, "momentum_5"),
            "number_of_symbols_bullish": bullish,
            "number_of_symbols_bearish": bearish,
            "market_risk_on": self._bool_int(market_regime.risk_on if market_regime else False),
            "market_risk_off": self._bool_int(market_regime.risk_off if market_regime else False),
            "operated": self._bool_int(operated),
            "block_reason": block_reason,
            "selected_by_allocator": self._bool_int(selected_by_allocator),
            "risk_manager_approved": self._bool_int(risk_manager_approved),
            "raw_signal_json": json_dumps(asdict(signal) if is_dataclass(signal) else signal.__dict__),
        }
        observation["raw_features_json"] = json_dumps(observation)
        return observation

    @staticmethod
    def _latest_feature_row(snapshot: MarketSnapshot | None, timeframe: str = "5m") -> Any:
        if not snapshot:
            return {}
        df = snapshot.candles.get(timeframe)
        if df is None:
            df = snapshot.candles.get(timeframe.lower())
        if df is None or df.empty:
            df = snapshot.candles.get("5m")
        if df is None or df.empty:
            return {}
        try:
            return latest_row(df)
        except Exception:
            return {}

    @staticmethod
    def _market_breadth(snapshots: dict[str, MarketSnapshot]) -> tuple[int, int]:
        bullish = 0
        bearish = 0
        for snapshot in snapshots.values():
            df = snapshot.candles.get("15m")
            if df is None:
                df = snapshot.candles.get("5m")
            bias = trend_bias(df) if df is not None else "neutral"
            bullish += bias == "bullish"
            bearish += bias == "bearish"
        return bullish, bearish

    @staticmethod
    def _raw(row: Any, key: str) -> Any:
        if isinstance(row, dict):
            return row.get(key)
        try:
            return row.get(key)
        except Exception:
            return None

    @classmethod
    def _feature(cls, row: Any, key: str, default: float = 0.0) -> float:
        value = safe_float(cls._raw(row, key), default)
        return value if math.isfinite(value) else default

    @classmethod
    def _distance(cls, row: Any, close: float, ema_key: str) -> float:
        ema = cls._feature(row, ema_key)
        if close <= 0 or ema <= 0:
            return 0.0
        return (close - ema) / close

    @staticmethod
    def _bool_int(value: Any) -> int:
        try:
            return int(bool(value))
        except Exception:
            return 0
