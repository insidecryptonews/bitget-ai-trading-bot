from __future__ import annotations

from dataclasses import replace
from typing import Any

from .database import Database
from .feature_logger import FeatureLogger
from .regime_detector import MarketRegime
from .signal_engine import Signal
from .utils import json_dumps


SCORE_THRESHOLDS = [60, 65, 70, 75, 80, 85, 90]
TP_SL_RR_VARIANTS = [1.2, 1.6, 2.0, 2.4]
REGIME_FILTERS = ["TREND_UP", "RISK_ON", "BREAKOUT_POSSIBLE", "CHOPPY_MARKET"]


class ShadowStrategyEngine:
    """Creates research-only strategy variants. It never returns executable signals."""

    def __init__(self, db: Database, feature_logger: FeatureLogger, logger=None) -> None:
        self.db = db
        self.feature_logger = feature_logger
        self.logger = logger
        self._variant_cache: dict[str, int] = {}

    def log_variants(
        self,
        *,
        signal: Signal,
        base_observation: dict[str, Any],
        market_regime: MarketRegime,
    ) -> int:
        if signal.side not in {"LONG", "SHORT"}:
            return 0
        count = 0
        for params in self._variant_params(signal, market_regime):
            variant_signal = self._apply_variant(signal, params)
            if not variant_signal:
                continue
            observation = dict(base_observation)
            observation.update(
                {
                    "side": variant_signal.side,
                    "strategy_type": variant_signal.strategy_type,
                    "entry_price": variant_signal.entry_price,
                    "stop_loss": variant_signal.stop_loss,
                    "take_profit_1": variant_signal.take_profit_1,
                    "take_profit_2": variant_signal.take_profit_2,
                    "risk_reward_ratio": variant_signal.risk_reward_ratio,
                    "shadow_strategy": 1,
                    "strategy_variant_id": self._variant_id(params),
                    "variant_params_json": json_dumps(params),
                    "original_side": signal.side,
                    "original_strategy_type": signal.strategy_type,
                    "score_bucket": self.score_bucket(signal.confidence_score),
                    "operated": 0,
                    "selected_by_allocator": 0,
                    "risk_manager_approved": 0,
                    "block_reason": "shadow_only",
                    "raw_signal_json": json_dumps(variant_signal.__dict__),
                }
            )
            observation["raw_features_json"] = json_dumps(observation)
            self.feature_logger.record_observation(observation)
            count += 1
        if count and self.logger:
            self.logger.debug("Shadow variants saved %s count=%s", signal.symbol, count)
        return count

    def _variant_params(self, signal: Signal, market_regime: MarketRegime) -> list[dict[str, Any]]:
        params: list[dict[str, Any]] = []
        for threshold in SCORE_THRESHOLDS:
            if signal.confidence_score >= threshold:
                params.append({"family": "score_threshold", "score_threshold": threshold})
        for rr in TP_SL_RR_VARIANTS:
            params.append({"family": "tp_sl_ratio", "tp1_rr": rr, "tp2_rr": round(rr * 1.5, 4)})
        for regime in REGIME_FILTERS:
            if market_regime.regime == regime:
                params.append({"family": "regime_filter", "regime": regime})
        if signal.side == "LONG":
            params.append({"family": "side_filter", "side": "LONG_ONLY"})
        if signal.side == "SHORT":
            params.append({"family": "side_filter", "side": "SHORT_ONLY"})
        params.append({"family": "reverse", "reverse": True, "source_side": signal.side})
        return params

    def _apply_variant(self, signal: Signal, params: dict[str, Any]) -> Signal | None:
        family = params.get("family")
        if family == "reverse":
            return self._reverse_signal(signal)
        if family == "tp_sl_ratio":
            return self._tp_sl_variant(signal, float(params["tp1_rr"]), float(params["tp2_rr"]))
        return signal

    def _variant_id(self, params: dict[str, Any]) -> int:
        name = self._variant_name(params)
        if name not in self._variant_cache:
            self._variant_cache[name] = self.db.ensure_strategy_variant(name, params, enabled=True)
        return self._variant_cache[name]

    @staticmethod
    def _variant_name(params: dict[str, Any]) -> str:
        family = params.get("family", "variant")
        if family == "score_threshold":
            return f"score_threshold_{params.get('score_threshold')}"
        if family == "tp_sl_ratio":
            return f"tp_sl_rr_{params.get('tp1_rr')}_{params.get('tp2_rr')}"
        if family == "regime_filter":
            return f"regime_{params.get('regime')}"
        if family == "side_filter":
            return str(params.get("side", "side_filter")).lower()
        if family == "reverse":
            return f"reverse_{str(params.get('source_side', 'both')).lower()}"
        return family

    @staticmethod
    def _tp_sl_variant(signal: Signal, tp1_rr: float, tp2_rr: float) -> Signal:
        distance = abs(signal.entry_price - signal.stop_loss)
        if signal.side == "LONG":
            tp1 = signal.entry_price + distance * tp1_rr
            tp2 = signal.entry_price + distance * tp2_rr
        else:
            tp1 = signal.entry_price - distance * tp1_rr
            tp2 = signal.entry_price - distance * tp2_rr
        return replace(signal, take_profit_1=tp1, take_profit_2=tp2, risk_reward_ratio=tp1_rr)

    @staticmethod
    def _reverse_signal(signal: Signal) -> Signal:
        distance = abs(signal.entry_price - signal.stop_loss)
        tp1_distance = abs(signal.take_profit_1 - signal.entry_price) or distance * max(signal.risk_reward_ratio, 1.0)
        tp2_distance = abs(signal.take_profit_2 - signal.entry_price) or tp1_distance * 1.5
        if signal.side == "LONG":
            side = "SHORT"
            stop = signal.entry_price + distance
            tp1 = signal.entry_price - tp1_distance
            tp2 = signal.entry_price - tp2_distance
        else:
            side = "LONG"
            stop = signal.entry_price - distance
            tp1 = signal.entry_price + tp1_distance
            tp2 = signal.entry_price + tp2_distance
        rr = abs(tp1 - signal.entry_price) / max(abs(signal.entry_price - stop), 1e-12)
        return replace(
            signal,
            side=side,
            strategy_type=f"REVERSE_{signal.strategy_type}",
            stop_loss=stop,
            take_profit_1=tp1,
            take_profit_2=tp2,
            risk_reward_ratio=rr,
            reason=f"Shadow reverse of {signal.side} {signal.strategy_type}",
        )

    @staticmethod
    def score_bucket(score: int) -> str:
        if score >= 90:
            return "90+"
        if score >= 85:
            return "85-89"
        if score >= 80:
            return "80-84"
        if score >= 75:
            return "75-79"
        if score >= 70:
            return "70-74"
        if score >= 65:
            return "65-69"
        if score >= 60:
            return "60-64"
        return "<60"
