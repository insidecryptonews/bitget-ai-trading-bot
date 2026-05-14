from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .config import BotConfig
from .database import Database
from .utils import safe_float, safe_int


START = "MFE MAE DIAGNOSTIC START"
END = "MFE MAE DIAGNOSTIC END"


class MfeMaeDiagnostic:
    """Cheap read-only diagnostics for compact path metrics."""

    def __init__(self, config: BotConfig, db: Database) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24, counters: Any | None = None) -> dict[str, Any]:
        hours = max(1, int(hours or 24))
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        table_exists = True
        try:
            summary = self.db.get_signal_path_metrics_summary_since(since)
            by_source = self.db.get_signal_path_metrics_source_summary_since(since)
        except Exception:
            table_exists = False
            summary = _empty_summary()
            by_source = []
        counter_payload = _counter_payload(counters)
        diagnosis = _diagnosis(summary, by_source, counter_payload, self.config)
        return {
            "hours": hours,
            "enabled": bool(self.config.enable_mfe_mae_capture),
            "table_exists": table_exists,
            "summary": summary,
            "by_source": by_source,
            "counters": counter_payload,
            "diagnosis": diagnosis,
            "recommendation": _recommendations(diagnosis),
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24, counters: Any | None = None) -> str:
        payload = self.build(hours=hours, counters=counters)
        summary = payload["summary"]
        counters_payload = payload["counters"]
        lines = [
            START,
            f"hours: {payload['hours']}",
            f"enabled: {payload['enabled']}",
            f"table_exists: {payload['table_exists']}",
            f"rows_total: {safe_int(summary.get('total'))}",
            f"active: {safe_int(summary.get('active_count'))}",
            f"matured: {safe_int(summary.get('matured_count'))}",
            f"insufficient: {safe_int(summary.get('insufficient_count'))}",
            f"coverage: {safe_float(summary.get('coverage_pct')) * 100:.1f}%",
            f"candidates_seen: {safe_int(counters_payload.get('candidates_seen'))}",
            f"candidates_tracked: {safe_int(counters_payload.get('candidates_tracked'))}",
            f"skipped_low_score: {safe_int(counters_payload.get('skipped_low_score'))}",
            f"skipped_no_price: {safe_int(counters_payload.get('skipped_no_price'))}",
            f"skipped_duplicate: {safe_int(counters_payload.get('skipped_duplicate'))}",
            f"skipped_max_active: {safe_int(counters_payload.get('skipped_max_active'))}",
            f"market_probes_created: {safe_int(counters_payload.get('market_probes_created'))}",
            f"low_score_samples_tracked: {safe_int(counters_payload.get('low_score_samples_tracked'))}",
            "by_source:",
            *_source_lines(payload["by_source"]),
            "diagnosis:",
            *[f"- {item}" for item in payload["diagnosis"]],
            "recommendation:",
            *[f"- {item}" for item in payload["recommendation"]],
            "final_recommendation: NO LIVE",
            END,
        ]
        return "\n".join(lines)


def _empty_summary() -> dict[str, Any]:
    return {
        "total": 0,
        "active_count": 0,
        "matured_count": 0,
        "insufficient_count": 0,
        "coverage_pct": 0.0,
    }


def _counter_payload(counters: Any | None) -> dict[str, Any]:
    if counters is None:
        return {
            "candidates_seen": 0,
            "candidates_tracked": 0,
            "skipped_low_score": 0,
            "skipped_no_price": 0,
            "skipped_duplicate": 0,
            "skipped_max_active": 0,
            "market_probes_created": 0,
            "low_score_samples_tracked": 0,
        }
    return {
        "candidates_seen": safe_int(getattr(counters, "candidates_seen", 0)),
        "candidates_tracked": safe_int(getattr(counters, "candidates_tracked", 0)),
        "skipped_low_score": safe_int(getattr(counters, "skipped_low_score", 0)),
        "skipped_no_price": safe_int(getattr(counters, "skipped_no_price", 0)),
        "skipped_duplicate": safe_int(getattr(counters, "skipped_duplicate", 0)),
        "skipped_max_active": safe_int(getattr(counters, "skipped_max_active", 0)),
        "market_probes_created": safe_int(getattr(counters, "market_probes_created", 0)),
        "low_score_samples_tracked": safe_int(getattr(counters, "low_score_samples_tracked", 0)),
    }


def _diagnosis(summary: dict[str, Any], by_source: list[dict[str, Any]], counters: dict[str, Any], config: BotConfig) -> list[str]:
    total = safe_int(summary.get("total"))
    active = safe_int(summary.get("active_count"))
    matured = safe_int(summary.get("matured_count"))
    market_probe_total = sum(safe_int(row.get("total")) for row in by_source if str(row.get("source")) == "market_probe")
    if total <= 0:
        if safe_int(counters.get("candidates_seen")) > 0 and safe_int(counters.get("skipped_low_score")) >= safe_int(counters.get("candidates_seen")) * 0.8:
            return ["filtered_by_low_score", "market_probes_should_create_samples"]
        return ["table_empty"]
    if active > 0 and matured <= 0:
        return ["collecting_wait_maturity"]
    if matured > 0:
        if market_probe_total >= total:
            return ["probe_data_only_not_signal_edge"]
        return ["ready_for_exit_simulation"]
    if safe_int(summary.get("insufficient_count")) >= total:
        return ["no_price_source"]
    if config.enable_mfe_mae_market_probes and market_probe_total > 0:
        return ["collecting_market_probes"]
    return ["collecting_wait_maturity"]


def _recommendations(diagnosis: list[str]) -> list[str]:
    joined = " ".join(diagnosis)
    if "filtered_by_low_score" in joined:
        return ["market probes research-only activos", "esperar maduracion MFE/MAE", "NO LIVE"]
    if "table_empty" in joined:
        return ["esperar ciclos con snapshots validos", "NO LIVE"]
    if "ready_for_exit_simulation" in joined:
        return ["ejecutar exit-simulation por source", "NO LIVE"]
    if "probe_data_only" in joined:
        return ["usar probes solo para calibrar TP/SL de mercado", "NO LIVE"]
    return ["seguir capturando muestras compactas", "NO LIVE"]


def _source_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- {row.get('source')}: total={safe_int(row.get('total'))} "
            f"active={safe_int(row.get('active_count'))} matured={safe_int(row.get('matured_count'))} "
            f"insufficient={safe_int(row.get('insufficient_count'))}"
        )
        for row in rows[:20]
    ]
