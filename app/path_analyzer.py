from __future__ import annotations

from typing import Any, Iterable

from .database import Database
from .utils import iso_utc, safe_float, safe_int


class PricePathAnalyzer:
    def __init__(self, db: Database | None = None, logger=None) -> None:
        self.db = db
        self.logger = logger

    def analyze(self, row: dict[str, Any], candles: Iterable[dict[str, Any]] | None = None) -> dict[str, Any]:
        side = str(row.get("side") or "").upper()
        entry = safe_float(row.get("entry_price"))
        stop = safe_float(row.get("stop_loss"))
        tp1 = safe_float(row.get("take_profit_1"))
        tp2 = safe_float(row.get("take_profit_2"))
        path = list(candles or [])
        if not path or entry <= 0:
            return self._fallback(row)

        max_fav = -10**9
        max_adv = 10**9
        t_fav = t_adv = t_sl = t_tp1 = t_tp2 = 0
        first_fav = first_adv = 0
        closes = []
        volumes = []
        for index, candle in enumerate(path, start=1):
            high = safe_float(candle.get("high") or candle.get("h") or candle.get("close"))
            low = safe_float(candle.get("low") or candle.get("l") or candle.get("close"))
            close = safe_float(candle.get("close") or candle.get("c") or entry)
            volume = safe_float(candle.get("volume") or candle.get("vol"))
            closes.append(close)
            if volume:
                volumes.append(volume)
            if side == "SHORT":
                favorable = (entry - low) / entry
                adverse = (entry - high) / entry
                hit_sl = high >= stop if stop > 0 else False
                hit_tp1 = low <= tp1 if tp1 > 0 else False
                hit_tp2 = low <= tp2 if tp2 > 0 else False
            else:
                favorable = (high - entry) / entry
                adverse = (low - entry) / entry
                hit_sl = low <= stop if stop > 0 else False
                hit_tp1 = high >= tp1 if tp1 > 0 else False
                hit_tp2 = high >= tp2 if tp2 > 0 else False
            if favorable > max_fav:
                max_fav, t_fav = favorable, index
            if adverse < max_adv:
                max_adv, t_adv = adverse, index
            if first_fav == 0 and favorable > 0:
                first_fav = index
            if first_adv == 0 and adverse < 0:
                first_adv = index
            if t_sl == 0 and hit_sl:
                t_sl = index
            if t_tp1 == 0 and hit_tp1:
                t_tp1 = index
            if t_tp2 == 0 and hit_tp2:
                t_tp2 = index

        return {
            "observation_id": safe_int(row.get("observation_id") or row.get("id")),
            "label_id": safe_int(row.get("label_id")),
            "max_favorable_excursion_pct": max(max_fav, 0.0),
            "max_adverse_excursion_pct": min(max_adv, 0.0),
            "time_to_max_favorable": t_fav,
            "time_to_max_adverse": t_adv,
            "time_to_sl": t_sl,
            "time_to_tp1": t_tp1,
            "time_to_tp2": t_tp2,
            "candles_until_exit": safe_int(row.get("bars_to_outcome") or len(path)),
            "did_price_move_in_favor_first": int(first_fav > 0 and (first_adv == 0 or first_fav <= first_adv)),
            "did_price_move_against_first": int(first_adv > 0 and (first_fav == 0 or first_adv < first_fav)),
            "adverse_before_favorable_pct": min(max_adv, 0.0) if first_adv and (not first_fav or first_adv < first_fav) else 0.0,
            "favorable_before_adverse_pct": max(max_fav, 0.0) if first_fav and (not first_adv or first_fav <= first_adv) else 0.0,
            "close_vs_entry_pct": _signed_return(side, entry, closes[-1] if closes else entry),
            "volatility_during_trade": _volatility(closes),
            "volume_during_trade_relative": _relative_volume(volumes),
            "btc_move_during_trade": safe_float(row.get("btc_momentum_5")) + safe_float(row.get("btc_momentum_15")),
            "eth_move_during_trade": safe_float(row.get("eth_momentum_5")),
            "created_at": iso_utc(),
        }

    def generate(self) -> list[dict[str, Any]]:
        if self.db is None:
            return []
        outputs = []
        labels = {safe_int(row.get("observation_id")): row for row in self.db.fetch_signal_labels()}
        for row in self.db.fetch_labeled_signal_rows():
            merged = dict(row)
            merged["observation_id"] = safe_int(row.get("id"))
            merged["label_id"] = labels.get(safe_int(row.get("id")), {}).get("id")
            path = self.analyze(merged)
            self.db.record_signal_price_path(path)
            outputs.append(path)
        return outputs

    def _fallback(self, row: dict[str, Any]) -> dict[str, Any]:
        mfe = abs(safe_float(row.get("max_favorable_excursion")))
        mae = -abs(safe_float(row.get("max_adverse_excursion")))
        if mfe == 0 and safe_int(row.get("label")) == 1:
            mfe = _tp1_distance(row)
        if mae == 0 and safe_int(row.get("label")) == -1:
            mae = -_stop_distance(row)
        bars = safe_int(row.get("bars_to_outcome") or row.get("holding_bars"))
        return {
            "observation_id": safe_int(row.get("observation_id") or row.get("id")),
            "label_id": safe_int(row.get("label_id")),
            "max_favorable_excursion_pct": mfe,
            "max_adverse_excursion_pct": mae,
            "time_to_max_favorable": bars if mfe > 0 else 0,
            "time_to_max_adverse": bars if mae < 0 else 0,
            "time_to_sl": bars if str(row.get("first_barrier_hit")) == "SL" else 0,
            "time_to_tp1": bars if str(row.get("first_barrier_hit")) == "TP1" else 0,
            "time_to_tp2": bars if str(row.get("first_barrier_hit")) == "TP2" else 0,
            "candles_until_exit": bars,
            "did_price_move_in_favor_first": int(mfe > abs(mae)),
            "did_price_move_against_first": int(abs(mae) >= mfe and mae < 0),
            "adverse_before_favorable_pct": mae if abs(mae) >= mfe else 0.0,
            "favorable_before_adverse_pct": mfe if mfe > abs(mae) else 0.0,
            "close_vs_entry_pct": safe_float(row.get("realized_return_pct")),
            "volatility_during_trade": safe_float(row.get("normalized_atr")),
            "volume_during_trade_relative": safe_float(row.get("volume_relative")),
            "btc_move_during_trade": safe_float(row.get("btc_momentum_5")) + safe_float(row.get("btc_momentum_15")),
            "eth_move_during_trade": safe_float(row.get("eth_momentum_5")),
            "created_at": iso_utc(),
        }


def _signed_return(side: str, entry: float, close: float) -> float:
    if entry <= 0:
        return 0.0
    return (entry - close) / entry if side == "SHORT" else (close - entry) / entry


def _volatility(closes: list[float]) -> float:
    if len(closes) < 2:
        return 0.0
    returns = [abs((closes[i] - closes[i - 1]) / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1]]
    return sum(returns) / max(len(returns), 1)


def _relative_volume(volumes: list[float]) -> float:
    if not volumes:
        return 0.0
    avg = sum(volumes) / len(volumes)
    return volumes[-1] / avg if avg else 0.0


def _stop_distance(row: dict[str, Any]) -> float:
    entry = safe_float(row.get("entry_price"))
    stop = safe_float(row.get("stop_loss"))
    return abs(entry - stop) / entry if entry > 0 and stop > 0 else 0.0


def _tp1_distance(row: dict[str, Any]) -> float:
    entry = safe_float(row.get("entry_price"))
    tp1 = safe_float(row.get("take_profit_1"))
    return abs(tp1 - entry) / entry if entry > 0 and tp1 > 0 else 0.0

