from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .config import BotConfig
from .database import Database
from .utils import safe_float, safe_int


START = "SHADOW OPPORTUNITY START"
END = "SHADOW OPPORTUNITY END"


class ShadowOpportunityLab:
    """Research-only view of high-score opportunities that did not need a paper slot."""

    def __init__(self, config: BotConfig, db: Database) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        hours = max(1, int(hours or 24))
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=hours)
        since_iso = since.isoformat()
        observations = self.db.get_training_observation_summary_since(
            since_iso,
            min_score=self.config.min_score_to_trade,
            limit=8,
        )
        labels = self.db.get_high_score_label_summary_since(since_iso, self.config.min_score_to_trade)
        by_symbol = self.db.get_shadow_opportunity_group_summaries_since(
            since_iso,
            min_score=self.config.min_score_to_trade,
            group_key="symbol",
            limit=8,
        )
        by_regime = self.db.get_shadow_opportunity_group_summaries_since(
            since_iso,
            min_score=self.config.min_score_to_trade,
            group_key="market_regime",
            limit=8,
        )
        by_side = self.db.get_shadow_opportunity_group_summaries_since(
            since_iso,
            min_score=self.config.min_score_to_trade,
            group_key="side",
            limit=8,
        )
        by_score_bucket = self.db.get_shadow_opportunity_group_summaries_since(
            since_iso,
            min_score=self.config.min_score_to_trade,
            group_key="score_bucket",
            limit=8,
        )
        by_block_reason = self.db.get_shadow_opportunity_group_summaries_since(
            since_iso,
            min_score=self.config.min_score_to_trade,
            group_key="block_reason",
            limit=8,
        )
        missed = self.db.get_missed_high_score_summary_since(since_iso, limit=500)
        groups = by_symbol + by_regime + by_side + by_score_bucket + by_block_reason
        best = [
            row for row in groups
            if safe_int(row.get("total_labels")) >= 50 and safe_float(row.get("profit_factor")) > 1.2
        ]
        best.sort(key=lambda row: (safe_float(row.get("profit_factor")), safe_float(row.get("tp_ratio"))), reverse=True)
        worst = [row for row in groups if safe_int(row.get("total_labels")) >= 50]
        worst.sort(key=lambda row: (safe_float(row.get("profit_factor")), -safe_float(row.get("sl_ratio"))))
        return {
            "hours": hours,
            "now": now.isoformat(),
            "since": since_iso,
            "observations": observations,
            "labels": labels,
            "overall": _metrics(labels),
            "by_symbol": by_symbol,
            "by_regime": by_regime,
            "by_side": by_side,
            "by_score_bucket": by_score_bucket,
            "by_block_reason": by_block_reason,
            "missed_high_score": missed,
            "best_candidates": best[:5],
            "worst_candidates": worst[:5],
            "recommendation": _recommendation(labels),
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        observations = payload["observations"]
        labels = payload["labels"]
        rec_lines = payload["recommendation"]
        lines = [
            START,
            f"hours: {payload['hours']}",
            f"observations: {safe_int(observations.get('total'))}",
            f"high_score: {safe_int(observations.get('high_score_count'))}",
            f"labels: {safe_int(labels.get('total_labels'))}",
            "overall:",
            _overall_line(labels),
            "by_symbol:",
            *_group_lines(payload["by_symbol"]),
            "by_regime:",
            *_group_lines(payload["by_regime"]),
            "by_side:",
            *_group_lines(payload["by_side"]),
            "by_score_bucket:",
            *_group_lines(payload["by_score_bucket"]),
            "missed_high_score:",
            f"- total={safe_int(payload['missed_high_score'].get('total'))}",
            *_missed_reason_lines(payload["missed_high_score"].get("by_reason", [])),
            "best_candidates:",
            *_group_lines(payload["best_candidates"], empty="none - insufficient evidence"),
            "worst_candidates:",
            *_group_lines(payload["worst_candidates"], empty="none"),
            "recommendation:",
            *[f"- {line}" for line in rec_lines],
            END,
        ]
        return "\n".join(lines)


def _metrics(labels: dict[str, Any]) -> dict[str, float]:
    total = safe_float(labels.get("total_labels"))
    tp = safe_float(labels.get("tp1_count")) + safe_float(labels.get("tp2_count"))
    sl = safe_float(labels.get("sl_count"))
    time_count = safe_float(labels.get("time_count"))
    return {
        "profit_factor": safe_float(labels.get("profit_factor")),
        "time_ratio": time_count / max(total, 1.0) if total else 0.0,
        "sl_ratio": sl / max(total, 1.0) if total else 0.0,
        "tp_ratio": tp / max(total, 1.0) if total else 0.0,
        "tp_count": tp,
    }


def _recommendation(labels: dict[str, Any]) -> list[str]:
    metrics = _metrics(labels)
    lines = ["NO LIVE"]
    if safe_float(labels.get("total_labels")) <= 0:
        return lines + ["Evidencia insuficiente; seguir acumulando labels paper"]
    if metrics["profit_factor"] < 1.0:
        lines.append("DO NOT EXPAND SLOTS: PF < 1.0, edge negativo")
    if metrics["tp_ratio"] < 0.05:
        lines.append("Score alto no predice TP todavia; revisar scoring/filtros")
    if metrics["time_ratio"] > 0.60:
        lines.append("Demasiadas TIME; revisar max_holding_bars y filtros de regimen")
    if metrics["sl_ratio"] > metrics["tp_ratio"]:
        lines.append("SL supera TP; endurecer entradas/stop/regimen")
    if len(lines) == 1:
        lines.append("PAPER ONLY: seguir acumulando evidencia")
    return lines


def _overall_line(labels: dict[str, Any]) -> str:
    metrics = _metrics(labels)
    return (
        f"- PF={metrics['profit_factor']:.2f} "
        f"TIME={safe_int(labels.get('time_count'))} "
        f"SL={safe_int(labels.get('sl_count'))} "
        f"TP1={safe_int(labels.get('tp1_count'))} "
        f"TP2={safe_int(labels.get('tp2_count'))} "
        f"TIME%={metrics['time_ratio'] * 100:.1f} "
        f"SL%={metrics['sl_ratio'] * 100:.1f} "
        f"TP%={metrics['tp_ratio'] * 100:.1f}"
    )


def _group_lines(rows: list[dict[str, Any]], *, empty: str = "none") -> list[str]:
    if not rows:
        return [f"- {empty}"]
    lines: list[str] = []
    for row in rows[:8]:
        sample = " sample_too_small" if row.get("sample_warning") else ""
        lines.append(
            f"- {row.get('group_value', 'NA')} labels={safe_int(row.get('total_labels'))} "
            f"PF={safe_float(row.get('profit_factor')):.2f} "
            f"TIME%={safe_float(row.get('time_ratio')) * 100:.1f} "
            f"SL%={safe_float(row.get('sl_ratio')) * 100:.1f} "
            f"TP%={safe_float(row.get('tp_ratio')) * 100:.1f}{sample}"
        )
    return lines


def _missed_reason_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- by_reason=none"]
    return [f"- {row.get('reason')}: {safe_int(row.get('count'))}" for row in rows[:8]]
