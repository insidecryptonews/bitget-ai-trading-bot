from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd

from .config import BotConfig
from .data_guards import should_insert_label
from .database import Database
from .utils import iso_utc, safe_float


@dataclass
class LabelOutcome:
    observation_id: int
    label: int
    first_barrier_hit: str
    bars_to_outcome: int
    max_favorable_excursion: float
    max_adverse_excursion: float
    realized_return_pct: float
    simulated_pnl: float
    would_have_won: bool


class TripleBarrierLabeler:
    def __init__(self, config: BotConfig, db: Database | None = None, logger=None) -> None:
        self.config = config
        self.db = db
        self.logger = logger

    def label_observation(
        self,
        observation: dict[str, Any],
        candles: pd.DataFrame,
        *,
        max_holding_bars: int | None = None,
        use_tp2: bool | None = None,
        notional: float = 0.0,
    ) -> LabelOutcome:
        if candles is None or candles.empty:
            raise ValueError("Candles vacios: no se puede etiquetar observacion")

        max_bars = max_holding_bars or self.config.max_holding_bars
        use_tp2 = self.config.label_use_tp2 if use_tp2 is None else use_tp2
        entry = safe_float(observation.get("entry_price"))
        stop = safe_float(observation.get("stop_loss"))
        tp1 = safe_float(observation.get("take_profit_1"))
        tp2 = safe_float(observation.get("take_profit_2"))
        side = str(observation.get("side", "")).upper()
        observation_id = int(observation.get("id") or observation.get("observation_id") or 0)
        if side not in {"LONG", "SHORT"}:
            return self._time_outcome(observation_id, entry, candles.head(max_bars), side, notional)
        if entry <= 0 or stop <= 0 or tp1 <= 0:
            return self._time_outcome(observation_id, entry, candles.head(max_bars), side, notional)

        upper = tp2 if use_tp2 and tp2 > 0 else tp1
        window = self._future_window(observation, candles, max_bars)
        mfe = 0.0
        mae = 0.0

        for index, row in enumerate(window.itertuples(index=False), start=1):
            high = safe_float(getattr(row, "high", 0.0))
            low = safe_float(getattr(row, "low", 0.0))
            if side == "LONG":
                mfe = max(mfe, (high - entry) / entry)
                mae = min(mae, (low - entry) / entry)
                hit_tp = high >= upper
                hit_sl = low <= stop
                if hit_sl and hit_tp:
                    return self._outcome(observation_id, -1, "SL", index, mfe, mae, (stop - entry) / entry, notional)
                if hit_tp:
                    barrier = "TP2" if use_tp2 else "TP1"
                    return self._outcome(observation_id, 1, barrier, index, mfe, mae, (upper - entry) / entry, notional)
                if hit_sl:
                    return self._outcome(observation_id, -1, "SL", index, mfe, mae, (stop - entry) / entry, notional)
            else:
                mfe = max(mfe, (entry - low) / entry)
                mae = min(mae, (entry - high) / entry)
                hit_tp = low <= upper
                hit_sl = high >= stop
                if hit_sl and hit_tp:
                    return self._outcome(observation_id, -1, "SL", index, mfe, mae, (entry - stop) / entry, notional)
                if hit_tp:
                    barrier = "TP2" if use_tp2 else "TP1"
                    return self._outcome(observation_id, 1, barrier, index, mfe, mae, (entry - upper) / entry, notional)
                if hit_sl:
                    return self._outcome(observation_id, -1, "SL", index, mfe, mae, (entry - stop) / entry, notional)

        return self._time_outcome(observation_id, entry, window, side, notional)

    def save_label(self, outcome: LabelOutcome) -> int:
        if not self.db:
            return 0
        payload = asdict(outcome)
        payload["would_have_won"] = int(outcome.would_have_won)
        payload["raw_label_json"] = asdict(outcome)
        if outcome.observation_id:
            existing = []
            fetch_existing = getattr(self.db, "fetch_signal_label_for_observation", None)
            if callable(fetch_existing):
                row = fetch_existing(outcome.observation_id)
                if row:
                    existing.append(row)
            allowed, reason = should_insert_label(existing, payload)
            if not allowed:
                if self.logger:
                    self.logger.info("Label guard skipped observation_id=%s reason=%s", outcome.observation_id, reason)
                return int(existing[0].get("id") or 0) if existing else 0
        return self.db.record_signal_label(payload)

    @staticmethod
    def _future_window(observation: dict[str, Any], candles: pd.DataFrame, max_bars: int) -> pd.DataFrame:
        timestamp = observation.get("timestamp")
        if timestamp and "timestamp" in candles.columns:
            ts = pd.to_datetime(timestamp, utc=True, errors="coerce")
            if pd.notna(ts):
                future = candles[pd.to_datetime(candles["timestamp"], utc=True, errors="coerce") > ts]
                if not future.empty:
                    return future.head(max_bars)
        return candles.head(max_bars)

    def _time_outcome(
        self,
        observation_id: int,
        entry: float,
        window: pd.DataFrame,
        side: str,
        notional: float,
    ) -> LabelOutcome:
        if entry <= 0 or window.empty or side not in {"LONG", "SHORT"}:
            return self._outcome(observation_id, 0, "TIME", 0, 0.0, 0.0, 0.0, notional)
        high = window["high"].max()
        low = window["low"].min()
        close = safe_float(window.iloc[-1].get("close"))
        if side == "LONG":
            mfe = (safe_float(high) - entry) / entry
            mae = (safe_float(low) - entry) / entry
            realized = (close - entry) / entry
        else:
            mfe = (entry - safe_float(low)) / entry
            mae = (entry - safe_float(high)) / entry
            realized = (entry - close) / entry
        return self._outcome(observation_id, 0, "TIME", len(window), mfe, mae, realized, notional)

    @staticmethod
    def _outcome(
        observation_id: int,
        label: int,
        barrier: str,
        bars: int,
        mfe: float,
        mae: float,
        realized_return_pct: float,
        notional: float,
    ) -> LabelOutcome:
        return LabelOutcome(
            observation_id=observation_id,
            label=label,
            first_barrier_hit=barrier,
            bars_to_outcome=bars,
            max_favorable_excursion=mfe,
            max_adverse_excursion=mae,
            realized_return_pct=realized_return_pct,
            simulated_pnl=realized_return_pct * notional,
            would_have_won=label == 1,
        )
