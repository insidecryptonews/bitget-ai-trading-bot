"""Causal support/resistance levels with delayed pivot availability."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class PriceLevel:
    kind: str
    price: float
    touch_count: int
    touch_indices: tuple[int, ...]
    first_touch_at: str
    last_touch_at: str
    tolerance: float
    strength: float
    fatigue: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LevelSnapshot:
    support: PriceLevel | None
    resistance: PriceLevel | None
    tolerance: float
    break_buffer: float
    prefix_end_idx: int


class CausalLevelEngine:
    """Precompute pivot candidates, but release each only after confirmation.

    Vectorization removes repeated rolling-window scans. The causal contract is
    unchanged because ``snapshot(i)`` only exposes pivots ``p`` where
    ``p + right <= i``.
    """

    def __init__(self, frame: pd.DataFrame, *, lookback: int = 96,
                 left: int = 3, right: int = 3, min_touches: int = 2,
                 min_separation: int = 3, price_tolerance: float = 0.0015,
                 atr_tolerance: float = 0.35, break_price_buffer: float = 0.0002,
                 break_atr_buffer: float = 0.05) -> None:
        self.frame = frame
        self.lookback = lookback
        self.left = left
        self.right = right
        self.min_touches = min_touches
        self.min_separation = min_separation
        self.price_tolerance = price_tolerance
        self.atr_tolerance = atr_tolerance
        self.break_price_buffer = break_price_buffer
        self.break_atr_buffer = break_atr_buffer
        window = left + right + 1
        lows = frame["low"].to_numpy(dtype=float)
        highs = frame["high"].to_numpy(dtype=float)
        low_roll = pd.Series(lows).rolling(window, center=True, min_periods=window).min().to_numpy()
        high_roll = pd.Series(highs).rolling(window, center=True, min_periods=window).max().to_numpy()
        self.support_pivots = [
            (idx, float(lows[idx])) for idx in range(len(frame))
            if np.isfinite(low_roll[idx]) and lows[idx] == low_roll[idx]
        ]
        self.resistance_pivots = [
            (idx, float(highs[idx])) for idx in range(len(frame))
            if np.isfinite(high_roll[idx]) and highs[idx] == high_roll[idx]
        ]
        self.support_indices = [idx for idx, _ in self.support_pivots]
        self.resistance_indices = [idx for idx, _ in self.resistance_pivots]
        self._cache: dict[int, LevelSnapshot] = {}

    @staticmethod
    def _window(pivots: list[tuple[int, float]], indices: list[int],
                first: int, last: int) -> list[tuple[int, float]]:
        start = bisect_left(indices, first)
        stop = bisect_right(indices, last)
        return pivots[start:stop]

    def snapshot(self, _frame: pd.DataFrame, idx: int) -> LevelSnapshot:
        cached = self._cache.get(idx)
        if cached is not None:
            return cached
        if idx < self.left + self.right or idx >= len(self.frame):
            value = LevelSnapshot(None, None, 0.0, 0.0, idx)
            self._cache[idx] = value
            return value
        close = float(self.frame["close"].iloc[idx])
        atr_value = self.frame["atr14"].iloc[idx]
        atr = float(atr_value) if pd.notna(atr_value) else 0.0
        tolerance = max(close * self.price_tolerance, atr * self.atr_tolerance)
        break_buffer = max(close * self.break_price_buffer, atr * self.break_atr_buffer)
        first, last = max(self.left, idx - self.lookback), idx - self.right
        support_pivots = self._window(
            self.support_pivots, self.support_indices, first, last,
        )
        resistance_pivots = self._window(
            self.resistance_pivots, self.resistance_indices, first, last,
        )
        supports = _clusters(
            support_pivots, tolerance=tolerance, min_touches=self.min_touches,
            min_separation=self.min_separation, frame=self.frame,
            kind="SUPPORT", current_idx=idx,
        )
        resistances = _clusters(
            resistance_pivots, tolerance=tolerance, min_touches=self.min_touches,
            min_separation=self.min_separation, frame=self.frame,
            kind="RESISTANCE", current_idx=idx,
        )
        support_candidates = [level for level in supports if level.price <= close + tolerance]
        resistance_candidates = [level for level in resistances if level.price >= close - tolerance]
        value = LevelSnapshot(
            min(support_candidates, key=lambda level: abs(close - level.price), default=None),
            min(resistance_candidates, key=lambda level: abs(close - level.price), default=None),
            tolerance, break_buffer, idx,
        )
        self._cache[idx] = value
        return value


def _pivot_indices(frame: pd.DataFrame, idx: int, *, kind: str, lookback: int,
                   left: int, right: int) -> list[tuple[int, float]]:
    # The latest eligible pivot is idx-right: its right-hand confirmation bars
    # are already closed at idx. No pivot using bars after idx is inspected.
    first = max(left, idx - lookback)
    last = idx - right
    if last < first:
        return []
    column = "low" if kind == "SUPPORT" else "high"
    values = frame[column]
    pivots: list[tuple[int, float]] = []
    for pivot_idx in range(first, last + 1):
        window = values.iloc[pivot_idx - left:pivot_idx + right + 1]
        value = float(values.iloc[pivot_idx])
        extreme = float(window.min() if kind == "SUPPORT" else window.max())
        if value == extreme:
            pivots.append((pivot_idx, value))
    return pivots


def _clusters(pivots: list[tuple[int, float]], *, tolerance: float,
              min_touches: int, min_separation: int, frame: pd.DataFrame,
              kind: str, current_idx: int) -> list[PriceLevel]:
    groups: list[list[tuple[int, float]]] = []
    for pivot in sorted(pivots, key=lambda item: item[1]):
        for group in groups:
            center = sum(value for _, value in group) / len(group)
            if abs(pivot[1] - center) <= tolerance:
                group.append(pivot)
                break
        else:
            groups.append([pivot])
    levels: list[PriceLevel] = []
    for group in groups:
        by_time = sorted(group)
        separated: list[tuple[int, float]] = []
        for pivot in by_time:
            if not separated or pivot[0] - separated[-1][0] >= min_separation:
                separated.append(pivot)
        if len(separated) < min_touches:
            continue
        price = sum(value for _, value in separated) / len(separated)
        indices = tuple(index for index, _ in separated)
        fatigue = False
        if kind == "SUPPORT" and len(indices) >= 2:
            previous, latest = indices[-2], indices[-1]
            prior_high = float(frame["high"].iloc[previous:latest + 1].max())
            recent_high = float(frame["high"].iloc[latest:current_idx + 1].max())
            prior_rebound = max(0.0, (prior_high - price) / price)
            recent_rebound = max(0.0, (recent_high - price) / price)
            prior_duration = latest - previous
            recent_duration = max(1, current_idx - latest)
            fatigue = recent_rebound <= prior_rebound * 0.9 or recent_duration <= prior_duration
        levels.append(PriceLevel(
            kind=kind,
            price=price,
            touch_count=len(indices),
            touch_indices=indices,
            first_touch_at=pd.Timestamp(frame["timestamp"].iloc[indices[0]]).isoformat(),
            last_touch_at=pd.Timestamp(frame["timestamp"].iloc[indices[-1]]).isoformat(),
            tolerance=tolerance,
            strength=float(len(indices)),
            fatigue=fatigue,
        ))
    return levels


def level_snapshot(frame: pd.DataFrame, idx: int, *, lookback: int = 96,
                   left: int = 3, right: int = 3, min_touches: int = 2,
                   min_separation: int = 3, price_tolerance: float = 0.0015,
                   atr_tolerance: float = 0.35, break_price_buffer: float = 0.0002,
                   break_atr_buffer: float = 0.05) -> LevelSnapshot:
    if idx < left + right or idx >= len(frame):
        return LevelSnapshot(None, None, 0.0, 0.0, idx)
    close = float(frame["close"].iloc[idx])
    atr = float(frame["atr14"].iloc[idx]) if pd.notna(frame["atr14"].iloc[idx]) else 0.0
    tolerance = max(close * price_tolerance, atr * atr_tolerance)
    break_buffer = max(close * break_price_buffer, atr * break_atr_buffer)
    supports = _clusters(
        _pivot_indices(frame, idx, kind="SUPPORT", lookback=lookback, left=left, right=right),
        tolerance=tolerance, min_touches=min_touches, min_separation=min_separation,
        frame=frame, kind="SUPPORT", current_idx=idx,
    )
    resistances = _clusters(
        _pivot_indices(frame, idx, kind="RESISTANCE", lookback=lookback, left=left, right=right),
        tolerance=tolerance, min_touches=min_touches, min_separation=min_separation,
        frame=frame, kind="RESISTANCE", current_idx=idx,
    )
    support_candidates = [level for level in supports if level.price <= close + tolerance]
    resistance_candidates = [level for level in resistances if level.price >= close - tolerance]
    support = min(support_candidates, key=lambda level: abs(close - level.price), default=None)
    resistance = min(resistance_candidates, key=lambda level: abs(close - level.price), default=None)
    return LevelSnapshot(support, resistance, tolerance, break_buffer, idx)
