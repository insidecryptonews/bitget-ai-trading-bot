from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .config import BotConfig
from .database import Database
from .utils import safe_float


START = "EVOLUTION SCORE START"
END = "EVOLUTION SCORE END"


class EvolutionScore:
    def __init__(self, config: BotConfig, db: Database) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        hours = max(1, int(hours or 24))
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        labels = self.db.get_high_score_label_summary_since(since, self.config.min_score_to_trade)
        paths = self.db.get_signal_path_metrics_summary_since(since)
        recent_since = (datetime.now(timezone.utc) - timedelta(hours=max(1, self.config.edge_guard_recent_hours))).isoformat()
        recent_labels = self.db.get_high_score_label_summary_since(recent_since, self.config.min_score_to_trade)
        data_quality = _data_quality(labels, paths)
        edge_quality = _edge_quality(labels)
        stability = _stability(labels, recent_labels)
        safety = _safety(self.config)
        final_status = _final_status(data_quality, edge_quality, stability)
        return {
            "hours": hours,
            "data_quality": data_quality,
            "edge_quality": edge_quality,
            "stability": stability,
            "safety": safety,
            "final_status": final_status,
            "labels": labels,
            "path_metrics": paths,
            "recommendation": _recommendations(final_status),
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        lines = [
            START,
            f"hours: {payload['hours']}",
            f"data_quality: {payload['data_quality']:.1f}",
            f"edge_quality: {payload['edge_quality']:.1f}",
            f"stability: {payload['stability']:.1f}",
            f"safety: {payload['safety']:.1f}",
            f"final_status: {payload['final_status']}",
            "recommendation:",
            *[f"- {item}" for item in payload["recommendation"]],
            "final_recommendation: NO LIVE",
            END,
        ]
        return "\n".join(lines)


def _data_quality(labels: dict[str, Any], paths: dict[str, Any]) -> float:
    label_score = min(safe_float(labels.get("total_labels")) / 5000.0, 1.0) * 55.0
    coverage_score = safe_float(paths.get("coverage_pct")) * 35.0
    path_sample_score = min(safe_float(paths.get("total")) / 2000.0, 1.0) * 10.0
    return max(0.0, min(100.0, label_score + coverage_score + path_sample_score))


def _edge_quality(labels: dict[str, Any]) -> float:
    total = safe_float(labels.get("total_labels"))
    tp = safe_float(labels.get("tp1_count")) + safe_float(labels.get("tp2_count"))
    sl = safe_float(labels.get("sl_count"))
    time_count = safe_float(labels.get("time_count"))
    pf = safe_float(labels.get("profit_factor"))
    tp_ratio = tp / max(total, 1.0) if total else 0.0
    sl_ratio = sl / max(total, 1.0) if total else 0.0
    time_ratio = time_count / max(total, 1.0) if total else 0.0
    score = min(pf / 1.5, 1.0) * 45.0 + min(tp_ratio / 0.05, 1.0) * 30.0
    score -= min(sl_ratio / 0.20, 1.0) * 15.0
    score -= min(time_ratio / 0.90, 1.0) * 10.0
    return max(0.0, min(100.0, score))


def _stability(labels: dict[str, Any], recent_labels: dict[str, Any]) -> float:
    pf = safe_float(labels.get("profit_factor"))
    recent_pf = safe_float(recent_labels.get("profit_factor"))
    if pf <= 0 or recent_pf <= 0:
        return 20.0
    drop = max(0.0, pf - recent_pf) / max(pf, 1.0)
    return max(0.0, min(100.0, 80.0 - drop * 100.0))


def _safety(config: BotConfig) -> float:
    score = 0.0
    score += 30.0 if not config.live_trading else 0.0
    score += 25.0 if config.dry_run else 0.0
    score += 20.0 if config.paper_trading else 0.0
    score += 15.0 if config.worker_lightweight_mode else 0.0
    score += 10.0 if not config.enable_kronos_research and not config.enable_full_research_auto_report else 0.0
    return min(100.0, score)


def _final_status(data_quality: float, edge_quality: float, stability: float) -> str:
    if data_quality < 35:
        return "NEED_MORE_DATA"
    if edge_quality < 45:
        return "EDGE_NEGATIVE"
    if stability < 45:
        return "KEEP_TRAINING"
    return "PAPER_ONLY"


def _recommendations(status: str) -> list[str]:
    if status == "NEED_MORE_DATA":
        return ["NO LIVE", "seguir capturando MFE/MAE", "revisar exit-simulation cuando haya cobertura"]
    if status == "EDGE_NEGATIVE":
        return ["NO LIVE", "no ampliar slots", "usar Edge Guard y shadow experiments"]
    return ["NO LIVE", "seguir paper/research", "validar estabilidad temporal antes de cualquier cambio"]
