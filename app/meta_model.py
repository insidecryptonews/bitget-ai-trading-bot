from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .config import BotConfig
from .utils import safe_float


FEATURE_COLUMNS = [
    "confidence_score",
    "risk_reward_ratio",
    "spread_pct",
    "volume_24h_usdt",
    "funding_rate",
    "open_interest",
    "rsi_14",
    "macd_hist",
    "atr_14",
    "normalized_atr",
    "volume_relative",
    "distance_to_ema_21",
    "distance_to_ema_50",
    "distance_to_ema_200",
    "momentum_5",
    "momentum_15",
    "range_width_pct",
    "body_pct",
    "upper_wick_pct",
    "lower_wick_pct",
    "btc_momentum_5",
    "btc_momentum_15",
    "btc_normalized_atr",
    "eth_momentum_5",
    "number_of_symbols_bullish",
    "number_of_symbols_bearish",
    "market_risk_on",
    "market_risk_off",
]


@dataclass
class MetaDecision:
    meta_probability: float | None
    meta_decision: str
    reason: str
    top_features: list[str] = field(default_factory=list)
    can_block: bool = False

    @property
    def blocks_trade(self) -> bool:
        return self.meta_decision == "SKIP" and self.can_block


class MetaModel:
    """A second-stage filter: it can reject weak bot signals, never create trades."""

    def __init__(self, config: BotConfig, db=None, logger=None) -> None:
        self.config = config
        self.db = db
        self.logger = logger
        self.validated = False
        self.model: Any | None = None
        self.scaler: Any | None = None
        self.static_probability: float | None = None
        self.training_reason = "modelo no entrenado"
        self.feature_columns = FEATURE_COLUMNS

    def train(self, rows: list[dict[str, Any]]) -> bool:
        positives = sum(1 for row in rows if int(row.get("label", 0)) == 1)
        negatives = sum(1 for row in rows if int(row.get("label", 0)) <= 0)
        if len(rows) < self.config.meta_model_min_samples:
            self.validated = False
            self.training_reason = f"muestras insuficientes: {len(rows)} < {self.config.meta_model_min_samples}"
            return False
        if positives < self.config.meta_model_min_positives or negatives < self.config.meta_model_min_negatives:
            self.validated = False
            self.training_reason = (
                f"clases insuficientes: positives={positives}, negatives={negatives}, "
                f"min={self.config.meta_model_min_positives}/{self.config.meta_model_min_negatives}"
            )
            return False

        try:
            from sklearn.ensemble import RandomForestClassifier  # type: ignore
            from sklearn.linear_model import LogisticRegression  # type: ignore
            from sklearn.preprocessing import StandardScaler  # type: ignore

            x = [[self._value(row, col) for col in self.feature_columns] for row in rows]
            y = [1 if int(row.get("label", 0)) == 1 else 0 for row in rows]
            self.scaler = StandardScaler()
            x_scaled = self.scaler.fit_transform(x)
            model: Any
            try:
                model = LogisticRegression(max_iter=500, class_weight="balanced")
                model.fit(x_scaled, y)
            except Exception:
                model = RandomForestClassifier(n_estimators=100, max_depth=5, random_state=7, class_weight="balanced")
                model.fit(x, y)
                self.scaler = None
            self.model = model
            self.static_probability = None
            self.validated = False
            self.training_reason = "modelo entrenado, pendiente de walk-forward out-of-sample"
            return True
        except Exception as exc:
            self.model = self._build_bucket_fallback(rows)
            self.static_probability = None
            self.validated = False
            self.training_reason = f"fallback estadistico entrenado, pendiente de validacion: {exc}"
            return True

    def mark_validated(self, reason: str = "walk-forward validado") -> None:
        self.validated = True
        self.training_reason = reason

    def evaluate(
        self,
        features: dict[str, Any],
        *,
        risk_manager_approved: bool = True,
    ) -> MetaDecision:
        if not risk_manager_approved:
            return MetaDecision(None, "SKIP", "RiskManager ya bloqueo la senal", can_block=True)
        if not self.config.enable_meta_model or self.config.meta_model_mode == "off":
            return MetaDecision(None, "TRADE", "meta_model desactivado", can_block=False)

        probability = self.predict_probability(features)
        if self.config.meta_model_mode == "observe_only":
            reason = "observe_only: no bloquea senales"
            if probability is None:
                reason = f"observe_only: {self.training_reason}"
            return MetaDecision(probability, "TRADE", reason, self._top_features(features), can_block=False)

        if not self.validated:
            return MetaDecision(probability, "TRADE", f"modelo no validado: {self.training_reason}", self._top_features(features), False)
        if probability is None:
            return MetaDecision(None, "TRADE", "modelo sin probabilidad usable", can_block=False)
        if probability < self.config.meta_min_probability:
            return MetaDecision(
                probability,
                "SKIP",
                f"meta_probability {probability:.3f} < META_MIN_PROBABILITY {self.config.meta_min_probability:.3f}",
                self._top_features(features),
                can_block=True,
            )
        return MetaDecision(probability, "TRADE", f"meta_probability {probability:.3f} aprobada", self._top_features(features), True)

    def predict_probability(self, features: dict[str, Any]) -> float | None:
        if self.static_probability is not None:
            return self.static_probability
        if self.model is None:
            return None
        if isinstance(self.model, dict):
            return self._predict_bucket_probability(features)
        row = [[self._value(features, col) for col in self.feature_columns]]
        try:
            data = self.scaler.transform(row) if self.scaler is not None else row
            probability = float(self.model.predict_proba(data)[0][1])
            return probability if math.isfinite(probability) else None
        except Exception:
            return None

    @staticmethod
    def _value(row: dict[str, Any], key: str) -> float:
        value = safe_float(row.get(key), 0.0)
        return value if math.isfinite(value) else 0.0

    def _build_bucket_fallback(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        buckets: dict[str, list[int]] = {}
        for row in rows:
            key = self._bucket_key(row)
            buckets.setdefault(key, []).append(1 if int(row.get("label", 0)) == 1 else 0)
        return {
            "global_win_rate": sum(sum(v) for v in buckets.values()) / max(sum(len(v) for v in buckets.values()), 1),
            "buckets": {key: sum(values) / len(values) for key, values in buckets.items() if len(values) >= 10},
        }

    def _predict_bucket_probability(self, features: dict[str, Any]) -> float:
        assert isinstance(self.model, dict)
        return float(self.model.get("buckets", {}).get(self._bucket_key(features), self.model.get("global_win_rate", 0.5)))

    @staticmethod
    def _bucket_key(row: dict[str, Any]) -> str:
        strategy = str(row.get("strategy_type", "NA"))
        regime = str(row.get("market_regime", "NA"))
        score = safe_float(row.get("confidence_score"), 0.0)
        score_bucket = "score90" if score >= 90 else "score80" if score >= 80 else "score70" if score >= 70 else "score_low"
        return f"{strategy}|{regime}|{score_bucket}"

    @staticmethod
    def _top_features(features: dict[str, Any]) -> list[str]:
        candidates = [
            ("confidence_score", safe_float(features.get("confidence_score"))),
            ("risk_reward_ratio", safe_float(features.get("risk_reward_ratio"))),
            ("volume_relative", safe_float(features.get("volume_relative"))),
            ("normalized_atr", safe_float(features.get("normalized_atr"))),
            ("spread_pct", safe_float(features.get("spread_pct"))),
        ]
        return [name for name, _ in sorted(candidates, key=lambda item: abs(item[1]), reverse=True)[:3]]
