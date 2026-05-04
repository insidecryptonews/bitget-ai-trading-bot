from __future__ import annotations

import json
from typing import Any, Iterable

from .database import Database
from .research_lab import ResearchMetrics
from .utils import iso_utc, json_dumps, safe_float, safe_int


SCENARIOS = [
    "REVERSE_SIDE",
    "WIDER_STOP_1_5X",
    "WIDER_STOP_2X",
    "TIGHTER_STOP_0_75X",
    "CLOSER_TP_0_5X",
    "CLOSER_TP_0_75X",
    "FARTHER_TP_1_5X",
    "BREAKEVEN_AFTER_0_5R",
    "BREAKEVEN_AFTER_1R",
    "NO_TRADE_IF_CHOPPY",
    "NO_TRADE_IF_BTC_NOT_ALIGNED",
    "NO_TRADE_IF_LOW_VOLUME",
    "NO_TRADE_IF_HIGH_SPREAD",
    "NO_TRADE_IF_SCORE_BELOW_80",
    "NO_TRADE_IF_SCORE_BELOW_90",
    "DELAY_ENTRY_1_CANDLE",
    "DELAY_ENTRY_2_CANDLES",
    "ENTER_ONLY_AFTER_CONFIRMATION",
    "EXIT_EARLY_ON_MOMENTUM_LOSS",
    "TIME_STOP_SHORTER",
    "TIME_STOP_LONGER",
]


class CounterfactualEngine:
    def __init__(self, db: Database | None = None, logger=None) -> None:
        self.db = db
        self.logger = logger

    def simulate_row(self, row: dict[str, Any], candles: Iterable[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        return [self._simulate_scenario(row, scenario, list(candles or [])) for scenario in SCENARIOS]

    def generate(self) -> list[dict[str, Any]]:
        if self.db is None:
            return []
        labels = {safe_int(row.get("observation_id")): row for row in self.db.fetch_signal_labels()}
        outputs: list[dict[str, Any]] = []
        for row in self.db.fetch_labeled_signal_rows():
            merged = dict(row)
            merged["observation_id"] = safe_int(row.get("id"))
            merged["label_id"] = labels.get(safe_int(row.get("id")), {}).get("id")
            for result in self.simulate_row(merged):
                self.db.record_signal_counterfactual(result)
                outputs.append(result)
        return outputs

    def summary(self, results: list[dict[str, Any]] | None = None) -> str:
        rows = results if results is not None else self.generate()
        if not rows:
            return "Counterfactual summary\n======================\nEvidencia insuficiente."
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row.get("scenario_name")), []).append(row)
        lines = ["Counterfactual summary", "======================"]
        for scenario, items in sorted(grouped.items()):
            metrics = ResearchMetrics.calculate([
                {
                    "label": item.get("simulated_label"),
                    "realized_return_pct": item.get("simulated_return_pct"),
                    "first_barrier_hit": item.get("simulated_first_barrier_hit"),
                }
                for item in items
                if safe_int(item.get("would_trade")) == 1
            ])
            avoided = sum(1 for item in items if safe_int(item.get("avoided_loss")) == 1)
            improved = sum(1 for item in items if safe_int(item.get("improved_result")) == 1)
            lines.append(
                f"- {scenario}: samples={len(items)}, PF={metrics['profit_factor']:.2f}, "
                f"expectancy={metrics['expectancy']:.5f}, avoided_loss={avoided}, improved={improved}"
            )
        return "\n".join(lines)

    def _simulate_scenario(self, row: dict[str, Any], scenario: str, candles: list[dict[str, Any]]) -> dict[str, Any]:
        side = str(row.get("side") or "LONG").upper()
        entry = safe_float(row.get("entry_price"))
        stop = safe_float(row.get("stop_loss"))
        tp1 = safe_float(row.get("take_profit_1"))
        tp2 = safe_float(row.get("take_profit_2"))
        would_trade = True
        params: dict[str, Any] = {}

        if scenario == "REVERSE_SIDE":
            side = "SHORT" if side == "LONG" else "LONG"
            stop, tp1, tp2 = _reverse_barriers(entry, row)
            params["reverse"] = True
        elif scenario.startswith("WIDER_STOP"):
            factor = 1.5 if scenario.endswith("1_5X") else 2.0
            stop = _move_stop(entry, stop, side, factor)
            params["stop_factor"] = factor
        elif scenario == "TIGHTER_STOP_0_75X":
            stop = _move_stop(entry, stop, side, 0.75)
            params["stop_factor"] = 0.75
        elif scenario.startswith("CLOSER_TP"):
            factor = 0.5 if scenario.endswith("0_5X") else 0.75
            tp1 = _move_tp(entry, tp1, side, factor)
            tp2 = _move_tp(entry, tp2, side, factor)
            params["tp_factor"] = factor
        elif scenario == "FARTHER_TP_1_5X":
            tp1 = _move_tp(entry, tp1, side, 1.5)
            tp2 = _move_tp(entry, tp2, side, 1.5)
            params["tp_factor"] = 1.5
        elif scenario == "NO_TRADE_IF_CHOPPY":
            would_trade = str(row.get("market_regime") or "").upper() != "CHOPPY_MARKET"
        elif scenario == "NO_TRADE_IF_BTC_NOT_ALIGNED":
            would_trade = _btc_aligned(row)
        elif scenario == "NO_TRADE_IF_LOW_VOLUME":
            would_trade = safe_float(row.get("volume_relative")) >= 1.0
        elif scenario == "NO_TRADE_IF_HIGH_SPREAD":
            would_trade = safe_float(row.get("spread_pct")) < 0.0015
        elif scenario == "NO_TRADE_IF_SCORE_BELOW_80":
            would_trade = safe_float(row.get("confidence_score")) >= 80
        elif scenario == "NO_TRADE_IF_SCORE_BELOW_90":
            would_trade = safe_float(row.get("confidence_score")) >= 90
        elif scenario.startswith("DELAY_ENTRY") and candles:
            delay = 1 if "1_CANDLE" in scenario else 2
            if len(candles) > delay:
                entry = safe_float(candles[delay].get("close") or entry)
                stop = _reanchor(entry, stop, side, _stop_distance(row))
                tp1 = _reanchor(entry, tp1, "SHORT" if side == "LONG" else "LONG", _tp1_distance(row))
        elif scenario == "ENTER_ONLY_AFTER_CONFIRMATION" and candles:
            would_trade = _next_candle_confirms(side, candles)
        elif scenario == "EXIT_EARLY_ON_MOMENTUM_LOSS":
            if safe_float(row.get("momentum_5")) * safe_float(row.get("momentum_15")) < 0:
                return self._result(row, scenario, params, False, side, stop, tp1, tp2, 0, "SKIP", 0.0, "Momentum contradictorio; se evita la entrada.")

        if not would_trade:
            return self._result(
                row, scenario, params, False, side, stop, tp1, tp2, 0, "NO_TRADE", 0.0,
                "Filtro no-trade evita esta senal.",
            )

        label, barrier, ret = _simulate_barriers(row, side, entry, stop, tp1, tp2, candles, scenario)
        return self._result(row, scenario, params, True, side, stop, tp1, tp2, label, barrier, ret, "Simulacion research-only.")

    @staticmethod
    def _result(
        row: dict[str, Any],
        scenario: str,
        params: dict[str, Any],
        would_trade: bool,
        side: str,
        stop: float,
        tp1: float,
        tp2: float,
        label: int,
        barrier: str,
        ret: float,
        explanation: str,
    ) -> dict[str, Any]:
        original_ret = safe_float(row.get("realized_return_pct"))
        original_label = safe_int(row.get("label"))
        avoided_loss = int(original_label == -1 and (not would_trade or ret >= 0))
        improved = int(ret > original_ret or (original_label == -1 and not would_trade))
        return {
            "observation_id": safe_int(row.get("observation_id") or row.get("id")),
            "label_id": safe_int(row.get("label_id")),
            "scenario_name": scenario,
            "params_json": json_dumps(params),
            "would_trade": int(would_trade),
            "simulated_side": side,
            "simulated_sl": stop,
            "simulated_tp1": tp1,
            "simulated_tp2": tp2,
            "simulated_label": label,
            "simulated_first_barrier_hit": barrier,
            "simulated_return_pct": ret,
            "avoided_loss": avoided_loss,
            "improved_result": improved,
            "explanation": explanation,
            "created_at": iso_utc(),
        }


def _simulate_barriers(row: dict[str, Any], side: str, entry: float, stop: float, tp1: float, tp2: float, candles: list[dict[str, Any]], scenario: str) -> tuple[int, str, float]:
    if entry <= 0:
        return 0, "TIME", 0.0
    if not candles:
        return _fallback_result(row, side, entry, stop, tp1, scenario)
    max_bars = len(candles)
    if scenario == "TIME_STOP_SHORTER":
        max_bars = max(1, len(candles) // 2)
    elif scenario == "TIME_STOP_LONGER":
        max_bars = len(candles)
    for candle in candles[:max_bars]:
        high = safe_float(candle.get("high") or candle.get("close"))
        low = safe_float(candle.get("low") or candle.get("close"))
        if side == "SHORT":
            if stop > 0 and high >= stop:
                return -1, "SL", -abs(stop - entry) / entry
            if tp1 > 0 and low <= tp1:
                return 1, "TP1", abs(entry - tp1) / entry
        else:
            if stop > 0 and low <= stop:
                return -1, "SL", -abs(entry - stop) / entry
            if tp1 > 0 and high >= tp1:
                return 1, "TP1", abs(tp1 - entry) / entry
    return 0, "TIME", 0.0


def _fallback_result(row: dict[str, Any], side: str, entry: float, stop: float, tp1: float, scenario: str) -> tuple[int, str, float]:
    original = safe_int(row.get("label"))
    if scenario == "REVERSE_SIDE":
        if original == -1:
            return 1, "TP1", abs(tp1 - entry) / entry if entry else 0.0
        if original == 1:
            return -1, "SL", -abs(stop - entry) / entry if entry else 0.0
    if scenario.startswith("CLOSER_TP") and safe_int(row.get("label")) == -1 and safe_float(row.get("max_favorable_excursion")) >= abs(tp1 - entry) / max(entry, 1e-9):
        return 1, "TP1", abs(tp1 - entry) / entry
    if scenario.startswith("WIDER_STOP") and safe_int(row.get("label")) == -1:
        mae = abs(safe_float(row.get("max_adverse_excursion")))
        stop_distance = abs(stop - entry) / entry if entry else 0.0
        if mae and mae < stop_distance:
            return 0, "TIME", 0.0
    return original, str(row.get("first_barrier_hit") or "TIME"), safe_float(row.get("realized_return_pct"))


def _reverse_barriers(entry: float, row: dict[str, Any]) -> tuple[float, float, float]:
    stop_distance = _stop_distance(row)
    tp1_distance = _tp1_distance(row)
    tp2_distance = _tp2_distance(row)
    original_side = str(row.get("side") or "LONG").upper()
    if original_side == "LONG":
        return entry * (1 + stop_distance), entry * (1 - tp1_distance), entry * (1 - tp2_distance)
    return entry * (1 - stop_distance), entry * (1 + tp1_distance), entry * (1 + tp2_distance)


def _move_stop(entry: float, stop: float, side: str, factor: float) -> float:
    distance = abs(entry - stop) * factor
    return entry + distance if side == "SHORT" else entry - distance


def _move_tp(entry: float, tp: float, side: str, factor: float) -> float:
    distance = abs(tp - entry) * factor
    return entry - distance if side == "SHORT" else entry + distance


def _reanchor(entry: float, old: float, side_for_direction: str, distance_pct: float) -> float:
    return entry * (1 - distance_pct) if side_for_direction == "LONG" else entry * (1 + distance_pct)


def _next_candle_confirms(side: str, candles: list[dict[str, Any]]) -> bool:
    if len(candles) < 2:
        return False
    first = candles[1]
    open_ = safe_float(first.get("open") or first.get("close"))
    close = safe_float(first.get("close"))
    return close >= open_ if side == "LONG" else close <= open_


def _btc_aligned(row: dict[str, Any]) -> bool:
    side = str(row.get("side") or "").upper()
    btc = safe_float(row.get("btc_momentum_5")) + safe_float(row.get("btc_momentum_15"))
    return (side == "LONG" and btc >= 0) or (side == "SHORT" and btc <= 0)


def _stop_distance(row: dict[str, Any]) -> float:
    entry = safe_float(row.get("entry_price"))
    stop = safe_float(row.get("stop_loss"))
    return abs(entry - stop) / entry if entry > 0 and stop > 0 else 0.0


def _tp1_distance(row: dict[str, Any]) -> float:
    entry = safe_float(row.get("entry_price"))
    tp1 = safe_float(row.get("take_profit_1"))
    return abs(tp1 - entry) / entry if entry > 0 and tp1 > 0 else 0.0


def _tp2_distance(row: dict[str, Any]) -> float:
    entry = safe_float(row.get("entry_price"))
    tp2 = safe_float(row.get("take_profit_2"))
    return abs(tp2 - entry) / entry if entry > 0 and tp2 > 0 else 0.0

