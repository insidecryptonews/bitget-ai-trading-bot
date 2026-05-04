from __future__ import annotations

import json
from typing import Any

from .database import Database
from .utils import iso_utc, json_dumps, safe_float, safe_int


REASON_CODES = {
    "CHOPPY_MARKET",
    "BTC_NOT_ALIGNED",
    "ETH_NOT_ALIGNED",
    "MARKET_NOT_RISK_ON",
    "MARKET_RISK_OFF",
    "LOW_VOLUME_RELATIVE",
    "HIGH_SPREAD",
    "WEAK_MOMENTUM",
    "MOMENTUM_DIVERGENCE",
    "RSI_OVEREXTENDED_LONG",
    "RSI_OVEREXTENDED_SHORT",
    "RSI_TOO_WEAK",
    "FAKE_BREAKOUT",
    "FAILED_PULLBACK",
    "BAD_SUPPORT_REJECTION",
    "BAD_RESISTANCE_REJECTION",
    "STOP_TOO_TIGHT",
    "TP_TOO_AMBITIOUS",
    "ENTRY_TOO_LATE",
    "ENTRY_TOO_EARLY",
    "HIGH_VOLATILITY_STOP_HUNT",
    "LOW_VOLATILITY_NO_FOLLOW_THROUGH",
    "ATR_TOO_LOW",
    "ATR_TOO_HIGH",
    "FUNDING_WARNING",
    "OPEN_INTEREST_WARNING",
    "SYMBOL_UNDERPERFORMING",
    "BTC_DOMINANCE_OR_BTC_DRAG",
    "CORRELATED_MARKET_DUMP",
    "CORRELATED_MARKET_PUMP_AGAINST_SHORT",
    "SPREAD_TOO_EXPENSIVE_FOR_TARGET",
    "POOR_RISK_REWARD",
    "TIME_DECAY_NO_FOLLOW_THROUGH",
    "UNKNOWN_INSUFFICIENT_CONTEXT",
}


class ExplainabilityEngine:
    def __init__(self, db: Database | None = None, logger=None) -> None:
        self.db = db
        self.logger = logger

    def explain_row(self, row: dict[str, Any]) -> dict[str, Any]:
        reasons = self._reason_codes(row)
        primary = reasons[0] if reasons else "UNKNOWN_INSUFFICIENT_CONTEXT"
        label = safe_int(row.get("label"))
        barrier = str(row.get("first_barrier_hit") or "UNKNOWN")
        failure_type = self._failure_type(row)
        action = self._recommended_action(primary, row)
        text = self._text(row, primary, reasons[1:], action, barrier)
        return {
            "observation_id": safe_int(row.get("observation_id") or row.get("id")),
            "label_id": safe_int(row.get("label_id")),
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "strategy_type": row.get("strategy_type"),
            "label": label,
            "first_barrier_hit": barrier,
            "primary_reason": primary,
            "secondary_reasons_json": json_dumps(reasons[1:]),
            "failure_type": failure_type,
            "confidence": min(0.95, 0.45 + 0.08 * max(len(reasons), 1)),
            "explanation_text": text,
            "recommended_action": action,
            "created_at": iso_utc(),
        }

    def generate(self) -> list[dict[str, Any]]:
        if self.db is None:
            return []
        labels = {safe_int(row.get("observation_id")): row for row in self.db.fetch_signal_labels()}
        outputs: list[dict[str, Any]] = []
        for row in self.db.fetch_labeled_signal_rows():
            observation_id = safe_int(row.get("id"))
            label = labels.get(observation_id, {})
            merged = dict(row)
            merged["observation_id"] = observation_id
            merged["label_id"] = label.get("id")
            explanation = self.explain_row(merged)
            self.db.record_signal_explanation(explanation)
            outputs.append(explanation)
        return outputs

    def report(self) -> str:
        explanations = self.generate()
        if not explanations:
            return "Explainability report\n====================\nEvidencia insuficiente."
        counts: dict[str, int] = {}
        for row in explanations:
            key = str(row.get("primary_reason") or "UNKNOWN")
            counts[key] = counts.get(key, 0) + 1
        lines = ["Explainability report", "====================", f"explicaciones generadas: {len(explanations)}", "", "Top reason codes"]
        for reason, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)[:15]:
            lines.append(f"- {reason}: {count}")
        return "\n".join(lines)

    def _reason_codes(self, row: dict[str, Any]) -> list[str]:
        side = str(row.get("side") or "").upper()
        strategy = str(row.get("strategy_type") or "").upper()
        regime = str(row.get("market_regime") or "").upper()
        label = safe_int(row.get("label"))
        barrier = str(row.get("first_barrier_hit") or "").upper()
        reasons: list[str] = []
        if regime == "CHOPPY_MARKET":
            reasons.append("CHOPPY_MARKET")
        if safe_int(row.get("market_risk_off")) == 1:
            reasons.append("MARKET_RISK_OFF")
        if safe_int(row.get("market_risk_on")) == 0 and side == "LONG":
            reasons.append("MARKET_NOT_RISK_ON")
        btc = safe_float(row.get("btc_momentum_5")) + safe_float(row.get("btc_momentum_15"))
        eth = safe_float(row.get("eth_momentum_5"))
        if (side == "LONG" and btc < 0) or (side == "SHORT" and btc > 0):
            reasons.append("BTC_NOT_ALIGNED")
        if (side == "LONG" and eth < 0) or (side == "SHORT" and eth > 0):
            reasons.append("ETH_NOT_ALIGNED")
        if safe_float(row.get("volume_relative")) and safe_float(row.get("volume_relative")) < 1.0:
            reasons.append("LOW_VOLUME_RELATIVE")
        if safe_float(row.get("spread_pct")) >= 0.0015:
            reasons.append("HIGH_SPREAD")
        if abs(safe_float(row.get("momentum_5"))) + abs(safe_float(row.get("momentum_15"))) < 0.006:
            reasons.append("WEAK_MOMENTUM")
        if safe_float(row.get("momentum_5")) * safe_float(row.get("momentum_15")) < 0:
            reasons.append("MOMENTUM_DIVERGENCE")
        rsi = safe_float(row.get("rsi_14"))
        if side == "LONG" and rsi > 72:
            reasons.append("RSI_OVEREXTENDED_LONG")
        if side == "SHORT" and 0 < rsi < 28:
            reasons.append("RSI_OVEREXTENDED_SHORT")
        if side == "LONG" and 0 < rsi < 45:
            reasons.append("RSI_TOO_WEAK")
        if side == "SHORT" and rsi > 55:
            reasons.append("RSI_TOO_WEAK")
        stop_distance = _stop_distance(row)
        if 0 < stop_distance < 0.006:
            reasons.append("STOP_TOO_TIGHT")
        if safe_float(row.get("risk_reward_ratio")) > 2.5:
            reasons.append("TP_TOO_AMBITIOUS")
        if safe_float(row.get("normalized_atr")) < 0.004:
            reasons.append("ATR_TOO_LOW")
        if safe_float(row.get("normalized_atr")) > 0.03:
            reasons.append("ATR_TOO_HIGH")
        if safe_float(row.get("spread_pct")) > max(_tp1_distance(row) * 0.25, 0.0015):
            reasons.append("SPREAD_TOO_EXPENSIVE_FOR_TARGET")
        if safe_float(row.get("risk_reward_ratio")) and safe_float(row.get("risk_reward_ratio")) < 1.2:
            reasons.append("POOR_RISK_REWARD")
        if barrier == "TIME":
            reasons.append("TIME_DECAY_NO_FOLLOW_THROUGH")
            if safe_float(row.get("normalized_atr")) < 0.008:
                reasons.append("LOW_VOLATILITY_NO_FOLLOW_THROUGH")
        if label == -1:
            if "BREAKOUT" in strategy:
                reasons.append("FAKE_BREAKOUT")
            if "PULLBACK" in strategy:
                reasons.append("FAILED_PULLBACK")
            if "SUPPORT" in strategy:
                reasons.append("BAD_SUPPORT_REJECTION")
            if "RESISTANCE" in strategy:
                reasons.append("BAD_RESISTANCE_REJECTION")
        ordered: list[str] = []
        for reason in reasons or ["UNKNOWN_INSUFFICIENT_CONTEXT"]:
            if reason in REASON_CODES and reason not in ordered:
                ordered.append(reason)
        return ordered

    @staticmethod
    def _failure_type(row: dict[str, Any]) -> str:
        barrier = str(row.get("first_barrier_hit") or "").upper()
        if barrier in {"TP1", "TP2"}:
            return "WINNER"
        if barrier == "TIME":
            return "TIME_DECAY_NO_RESOLUTION"
        if barrier == "SL":
            bars = safe_int(row.get("bars_to_outcome") or row.get("holding_bars"))
            mfe = safe_float(row.get("max_favorable_excursion"))
            if bars and bars <= 3:
                return "FAST_STOP_LOSS"
            if mfe > 0:
                return "STOP_AFTER_BEING_IN_PROFIT"
            return "DIRECT_STOP_LOSS"
        return "UNKNOWN"

    @staticmethod
    def _recommended_action(primary: str, row: dict[str, Any]) -> str:
        if primary in {"CHOPPY_MARKET", "MARKET_RISK_OFF"}:
            return "Bloquear o exigir confirmacion adicional en este regimen."
        if primary in {"BTC_NOT_ALIGNED", "ETH_NOT_ALIGNED"}:
            return "Evitar la senal salvo alineacion clara de BTC/ETH."
        if primary == "LOW_VOLUME_RELATIVE":
            return "Exigir volumen relativo mayor antes de entrar."
        if primary == "STOP_TOO_TIGHT":
            return "Probar stop minimo mayor en research; no ampliar riesgo real automaticamente."
        if primary == "TIME_DECAY_NO_FOLLOW_THROUGH":
            return "Investigar time-stop o filtro de momentum antes de operar."
        if primary == "FAKE_BREAKOUT":
            return "Exigir cierre/confirmacion posterior antes de validar breakout."
        return "Mantener en observacion; evidencia insuficiente para regla fuerte."

    @staticmethod
    def _text(row: dict[str, Any], primary: str, secondary: list[str], action: str, barrier: str) -> str:
        extras = ", ".join(secondary[:4]) if secondary else "sin razones secundarias claras"
        return (
            f"Esta senal {row.get('symbol')} {row.get('side')} termino en {barrier}. "
            f"La causa principal probable es {primary}; tambien aparecen {extras}. "
            f"Accion recomendada: {action}"
        )


def _stop_distance(row: dict[str, Any]) -> float:
    entry = safe_float(row.get("entry_price"))
    stop = safe_float(row.get("stop_loss"))
    return abs(entry - stop) / entry if entry > 0 and stop > 0 else 0.0


def _tp1_distance(row: dict[str, Any]) -> float:
    entry = safe_float(row.get("entry_price"))
    tp1 = safe_float(row.get("take_profit_1"))
    return abs(tp1 - entry) / entry if entry > 0 and tp1 > 0 else 0.0

