from __future__ import annotations

import importlib
import math
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pandas as pd

from .bitget_client import BitgetClient
from .config import BotConfig
from .database import Database
from .market_data import MarketDataProvider
from .research_lab import ResearchMetrics
from .utils import iso_utc, json_dumps, safe_float, safe_int, timeframe_to_seconds


@dataclass
class KronosPredictionFeatures:
    current_close: float = 0.0
    predicted_return_pct: float = 0.0
    predicted_close: float = 0.0
    predicted_high: float = 0.0
    predicted_low: float = 0.0
    predicted_range_pct: float = 0.0
    direction: str = "FLAT"
    confidence_score: float = 0.0
    volatility_score: float = 0.0
    tp1_hit_possible: bool = False
    sl_hit_possible: bool = False
    forecast_json: str = "{}"


@dataclass
class KronosRunResult:
    predictions_created: int = 0
    model_used: str = ""
    symbols_processed: list[str] = field(default_factory=list)
    average_predicted_return: float = 0.0
    disagreement_count: int = 0
    unavailable_reason: str = ""
    errors: int = 0

    def to_text(self) -> str:
        lines = [
            "KRONOS RESEARCH START",
            f"predictions created: {self.predictions_created}",
            f"model used: {self.model_used or 'none'}",
            f"symbols processed: {', '.join(self.symbols_processed) if self.symbols_processed else 'none'}",
            f"average predicted return: {self.average_predicted_return:.5f}",
            f"disagreement count: {self.disagreement_count}",
        ]
        if self.unavailable_reason:
            lines.append(f"Kronos research unavailable: {self.unavailable_reason}")
        if self.errors:
            lines.append(f"errors: {self.errors}")
        lines.extend([
            "final recommendation: NO LIVE",
            "KRONOS RESEARCH END",
        ])
        return "\n".join(lines)


@dataclass
class KronosEvaluationResult:
    rows: int = 0
    agree_metrics: dict[str, float] = field(default_factory=dict)
    disagree_metrics: dict[str, float] = field(default_factory=dict)
    by_symbol: list[dict[str, Any]] = field(default_factory=list)
    by_strategy: list[dict[str, Any]] = field(default_factory=list)
    by_direction: list[dict[str, Any]] = field(default_factory=list)
    avoided_sl_count: int = 0
    filtered_tp_count: int = 0

    def to_text(self) -> str:
        lines = [
            "KRONOS EVALUATION START",
            f"rows evaluated: {self.rows}",
            f"PF Kronos agrees with bot: {safe_float(self.agree_metrics.get('profit_factor')):.2f}",
            f"PF Kronos contradicts bot: {safe_float(self.disagree_metrics.get('profit_factor')):.2f}",
            f"would have avoided SL: {self.avoided_sl_count}",
            f"would have filtered good TP: {self.filtered_tp_count}",
            "",
            "PF by symbol:",
            *_group_lines(self.by_symbol),
            "",
            "PF by strategy_type:",
            *_group_lines(self.by_strategy),
            "",
            "PF by Kronos direction:",
            *_group_lines(self.by_direction),
            "",
            "final recommendation: NO LIVE",
            "KRONOS EVALUATION END",
        ]
        return "\n".join(lines)


class OptionalKronosBackend:
    """Small adapter around shiyu-coder/Kronos, loaded only when requested."""

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.predictor = None

    def load(self) -> None:
        if self.predictor is not None:
            return
        module = self._import_kronos_module()
        tokenizer = module.KronosTokenizer.from_pretrained(self.config.kronos_tokenizer_name)
        model = module.Kronos.from_pretrained(self.config.kronos_model_name)
        device = _resolve_device(self.config.kronos_device)
        try:
            self.predictor = module.KronosPredictor(model, tokenizer, max_context=self.config.kronos_lookback, device=device)
        except TypeError:
            self.predictor = module.KronosPredictor(model, tokenizer, max_context=self.config.kronos_lookback)

    @staticmethod
    def _import_kronos_module() -> Any:
        last_error: Exception | None = None
        for module_name in ("model", "kronos"):
            try:
                module = importlib.import_module(module_name)
                if all(hasattr(module, name) for name in ("Kronos", "KronosTokenizer", "KronosPredictor")):
                    return module
            except Exception as exc:
                last_error = exc
        raise ImportError("No se pudo importar Kronos. Instala el repo/libreria Kronos y sus dependencias opcionales.") from last_error

    def predict(
        self,
        frame: pd.DataFrame,
        x_timestamp: pd.Series,
        y_timestamp: pd.Series,
        *,
        pred_len: int,
        temperature: float,
        top_p: float,
        sample_count: int,
    ) -> pd.DataFrame:
        self.load()
        assert self.predictor is not None
        return self.predictor.predict(
            df=frame,
            x_timestamp=x_timestamp,
            y_timestamp=y_timestamp,
            pred_len=pred_len,
            T=temperature,
            top_p=top_p,
            sample_count=sample_count,
        )


class KronosResearch:
    """Research-only Kronos prediction layer. It never sends or approves orders."""

    def __init__(self, config: BotConfig, db: Database, logger=None, backend: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.logger = logger
        self.backend = backend

    def run_once(
        self,
        *,
        limit: int = 100,
        candles_by_symbol: dict[str, pd.DataFrame] | None = None,
    ) -> KronosRunResult:
        result = KronosRunResult(model_used=self.config.kronos_model_name)
        if not self.config.enable_kronos_research and self.backend is None:
            result.unavailable_reason = "ENABLE_KRONOS_RESEARCH=false"
            return result

        backend = self.backend or OptionalKronosBackend(self.config)
        try:
            candidates = self.db.fetch_kronos_candidate_observations(limit=max(0, int(limit or 0)))
        except Exception as exc:
            result.unavailable_reason = f"no se pudieron leer observations: {exc}"
            result.errors += 1
            self._warn("Kronos research unavailable: %s", result.unavailable_reason)
            return result

        symbol_rows = self._latest_row_per_symbol(candidates)
        if not symbol_rows:
            return result
        if isinstance(backend, OptionalKronosBackend):
            try:
                backend.load()
            except Exception as exc:
                result.unavailable_reason = str(exc)
                result.errors += 1
                self._warn("Kronos research unavailable: %s", result.unavailable_reason)
                return result

        returns: list[float] = []
        started = time.monotonic()
        for symbol, observation in symbol_rows.items():
            if time.monotonic() - started > max(1, self.config.kronos_timeout_seconds):
                self._warn("Kronos research unavailable: timeout tras %s segundos", self.config.kronos_timeout_seconds)
                break
            try:
                candles = self._candles_for_symbol(symbol, candles_by_symbol)
                frame = self.to_kronos_frame(candles)
                if len(frame) < max(8, min(self.config.kronos_lookback, 32)):
                    continue
                lookback = min(self.config.kronos_lookback, len(frame))
                x_df = frame.tail(lookback).reset_index(drop=True)
                x_timestamp = pd.to_datetime(x_df["timestamp"], utc=True)
                y_timestamp = self._future_timestamps(x_timestamp)
                forecast = backend.predict(
                    x_df[["open", "high", "low", "close", "volume", "amount"]],
                    x_timestamp,
                    y_timestamp,
                    pred_len=self.config.kronos_pred_len,
                    temperature=self.config.kronos_temperature,
                    top_p=self.config.kronos_top_p,
                    sample_count=self.config.kronos_sample_count,
                )
                features = self.features_from_forecast(observation, x_df, forecast)
                prediction_id = self._persist_prediction(symbol, observation, features, lookback)
                self._update_observation(observation, prediction_id, features)
                result.predictions_created += 1
                result.symbols_processed.append(symbol)
                result.disagreement_count += int(self._disagrees(observation, features.direction))
                returns.append(features.predicted_return_pct)
            except Exception as exc:
                result.errors += 1
                message = str(exc)
                if result.predictions_created == 0 and not result.unavailable_reason:
                    result.unavailable_reason = message
                self._warn("Kronos research unavailable: %s", message)
        result.average_predicted_return = sum(returns) / len(returns) if returns else 0.0
        return result

    @staticmethod
    def to_kronos_frame(candles: pd.DataFrame) -> pd.DataFrame:
        if candles is None or candles.empty:
            return pd.DataFrame()
        frame = candles.copy()
        if "timestamp" not in frame:
            frame["timestamp"] = pd.date_range("2026-01-01", periods=len(frame), freq="5min", tz="UTC")
        for column in ("open", "high", "low", "close", "volume"):
            if column not in frame:
                frame[column] = 0.0
            frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
        if "amount" not in frame:
            quote = frame["quote_volume"] if "quote_volume" in frame else frame["volume"] * frame["close"]
            frame["amount"] = pd.to_numeric(quote, errors="coerce").fillna(0.0)
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        return frame[["timestamp", "open", "high", "low", "close", "volume", "amount"]].dropna().reset_index(drop=True)

    def features_from_forecast(
        self,
        observation: dict[str, Any],
        context: pd.DataFrame,
        forecast: Any,
    ) -> KronosPredictionFeatures:
        pred = _forecast_to_frame(forecast)
        current_close = safe_float(context["close"].iloc[-1])
        if pred.empty or current_close <= 0:
            return KronosPredictionFeatures()
        close = safe_float(pred["close"].iloc[-1], current_close)
        high = max([safe_float(value) for value in pred.get("high", pred["close"])])
        low = min([safe_float(value) for value in pred.get("low", pred["close"])])
        ret = (close - current_close) / current_close if current_close > 0 else 0.0
        range_pct = (high - low) / current_close if current_close > 0 else 0.0
        direction = "LONG" if ret > 0.001 else "SHORT" if ret < -0.001 else "FLAT"
        volatility = _bounded(range_pct / 0.03)
        confidence = _bounded(abs(ret) / max(range_pct, 0.002))
        side = str(observation.get("side") or "").upper()
        tp1 = safe_float(observation.get("take_profit_1"))
        stop = safe_float(observation.get("stop_loss"))
        tp1_possible = (side == "LONG" and high >= tp1 > 0) or (side == "SHORT" and 0 < tp1 >= low)
        sl_possible = (side == "LONG" and 0 < stop >= low) or (side == "SHORT" and high >= stop > 0)
        forecast_json = json_dumps({
            "predicted_high": high,
            "predicted_low": low,
            "tp1_hit_possible": tp1_possible,
            "sl_hit_possible": sl_possible,
            "rows": pred.head(64).to_dict(orient="records"),
        })
        return KronosPredictionFeatures(
            current_close=current_close,
            predicted_return_pct=ret,
            predicted_close=close,
            predicted_high=high,
            predicted_low=low,
            predicted_range_pct=range_pct,
            direction=direction,
            confidence_score=confidence,
            volatility_score=volatility,
            tp1_hit_possible=tp1_possible,
            sl_hit_possible=sl_possible,
            forecast_json=forecast_json,
        )

    def _latest_row_per_symbol(self, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        selected: dict[str, dict[str, Any]] = {}
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            if not symbol or symbol in selected:
                continue
            selected[symbol] = row
            if len(selected) >= max(1, self.config.kronos_max_symbols_per_run):
                break
        return selected

    def _candles_for_symbol(self, symbol: str, candles_by_symbol: dict[str, pd.DataFrame] | None) -> pd.DataFrame:
        if candles_by_symbol and symbol in candles_by_symbol:
            return candles_by_symbol[symbol]
        client = BitgetClient(self.config, self.logger)
        api_tf = MarketDataProvider._api_timeframe(self.config.main_timeframe)
        raw = client.get_candles(symbol, api_tf, limit=max(self.config.kronos_lookback, self.config.kronos_pred_len) + 20)
        return MarketDataProvider.candles_to_frame(raw)

    def _future_timestamps(self, x_timestamp: pd.Series) -> pd.Series:
        last = pd.to_datetime(x_timestamp.iloc[-1], utc=True)
        step = timedelta(seconds=timeframe_to_seconds(self.config.main_timeframe))
        return pd.Series([last + step * (index + 1) for index in range(self.config.kronos_pred_len)])

    def _persist_prediction(
        self,
        symbol: str,
        observation: dict[str, Any],
        features: KronosPredictionFeatures,
        lookback: int,
    ) -> int:
        return self.db.record_kronos_prediction({
            "timestamp": iso_utc(),
            "symbol": symbol,
            "observation_id": safe_int(observation.get("id")),
            "model_name": self.config.kronos_model_name,
            "tokenizer_name": self.config.kronos_tokenizer_name,
            "lookback": lookback,
            "pred_len": self.config.kronos_pred_len,
            "current_close": features.current_close,
            "predicted_close": features.predicted_close,
            "predicted_return_pct": features.predicted_return_pct,
            "predicted_range_pct": features.predicted_range_pct,
            "direction": features.direction,
            "confidence_score": features.confidence_score,
            "volatility_score": features.volatility_score,
            "forecast_json": features.forecast_json,
        })

    def _update_observation(
        self,
        observation: dict[str, Any],
        prediction_id: int,
        features: KronosPredictionFeatures,
    ) -> None:
        observation_id = safe_int(observation.get("id"))
        if not observation_id:
            return
        self.db.update_signal_observation(
            observation_id,
            kronos_predicted_return_pct=features.predicted_return_pct,
            kronos_direction=features.direction,
            kronos_confidence_score=features.confidence_score,
            kronos_disagreement=int(self._disagrees(observation, features.direction)),
            kronos_prediction_id=prediction_id,
        )

    @staticmethod
    def _disagrees(observation: dict[str, Any], kronos_direction: str) -> bool:
        side = str(observation.get("side") or "").upper()
        direction = str(kronos_direction or "").upper()
        return side in {"LONG", "SHORT"} and direction in {"LONG", "SHORT"} and side != direction

    def _warn(self, message: str, *args: Any) -> None:
        if self.logger:
            self.logger.warning(message, *args)


class KronosEvaluator:
    def __init__(self, db: Database) -> None:
        self.db = db

    def evaluate(self) -> KronosEvaluationResult:
        rows = self.db.fetch_kronos_labeled_rows()
        enriched = [self._row_with_agreement(row) for row in rows]
        agree = [row for row in enriched if row["kronos_agrees"]]
        disagree = [row for row in enriched if row["kronos_disagrees"]]
        return KronosEvaluationResult(
            rows=len(enriched),
            agree_metrics=ResearchMetrics.calculate(agree),
            disagree_metrics=ResearchMetrics.calculate(disagree),
            by_symbol=self._group_metrics(enriched, "symbol"),
            by_strategy=self._group_metrics(enriched, "strategy_type"),
            by_direction=self._group_metrics(enriched, "kronos_direction"),
            avoided_sl_count=sum(1 for row in disagree if str(row.get("first_barrier_hit")) == "SL"),
            filtered_tp_count=sum(1 for row in disagree if str(row.get("first_barrier_hit")) in {"TP1", "TP2"}),
        )

    def report(self) -> str:
        return self.evaluate().to_text()

    @staticmethod
    def _row_with_agreement(row: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(row)
        side = str(row.get("side") or "").upper()
        direction = str(row.get("kronos_direction") or "").upper()
        enriched["kronos_agrees"] = side in {"LONG", "SHORT"} and direction == side
        enriched["kronos_disagrees"] = side in {"LONG", "SHORT"} and direction in {"LONG", "SHORT"} and direction != side
        return enriched

    @staticmethod
    def _group_metrics(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row.get(key) or "NA"), []).append(row)
        output: list[dict[str, Any]] = []
        for value, bucket in grouped.items():
            metrics = ResearchMetrics.calculate(bucket)
            output.append({
                "name": value,
                "labels": len(bucket),
                "profit_factor": metrics["profit_factor"],
                "win_rate": metrics["win_rate"],
                "expectancy": metrics["expectancy"],
            })
        output.sort(key=lambda item: (safe_float(item.get("profit_factor")), safe_float(item.get("labels"))), reverse=True)
        return output


def _forecast_to_frame(forecast: Any) -> pd.DataFrame:
    if isinstance(forecast, pd.DataFrame):
        frame = forecast.copy()
    elif isinstance(forecast, list):
        frame = pd.DataFrame(forecast)
    elif isinstance(forecast, dict):
        frame = pd.DataFrame(forecast)
    else:
        frame = pd.DataFrame(forecast)
    if frame.empty:
        return frame
    for column in ("open", "high", "low", "close"):
        if column not in frame:
            frame[column] = frame["close"] if "close" in frame else 0.0
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
    return frame.reset_index(drop=True)


def _resolve_device(raw: str) -> str:
    if raw and raw.lower() != "auto":
        return raw
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            return "cuda:0"
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


def _bounded(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, value))


def _group_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- sin datos Kronos suficientes"]
    return [
        (
            f"- {row.get('name')}: labels={safe_int(row.get('labels'))}, "
            f"PF={safe_float(row.get('profit_factor')):.2f}, "
            f"WR={safe_float(row.get('win_rate')):.1%}, "
            f"expectancy={safe_float(row.get('expectancy')):.5f}"
        )
        for row in rows[:8]
    ]
