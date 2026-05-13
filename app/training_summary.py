from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .config import BotConfig
from .database import Database
from .utils import safe_float, safe_int


SUMMARY_START = "TRAINING SUMMARY START"
SUMMARY_END = "TRAINING SUMMARY END"
PLAN_START = "ACCELERATION PLAN START"
PLAN_END = "ACCELERATION PLAN END"


class TrainingSummary:
    """Cheap aggregated research telemetry. No heavy reports, no model training."""

    def __init__(self, config: BotConfig, db: Database) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 6) -> str:
        window = self._window(hours)
        labels = self.db.get_signal_label_summary_since(window["since"])
        observations = self.db.get_training_observation_summary_since(
            window["since"],
            min_score=self.config.min_score_to_trade,
            limit=5,
        )
        paper = self.db.get_paper_trade_summary()
        events = self.db.get_event_type_counts_since(window["since"])
        high_score_labels = self.db.get_high_score_label_summary_since(
            window["since"],
            self.config.min_score_to_trade,
        )
        by_symbol = self.db.get_shadow_opportunity_group_summaries_since(
            window["since"],
            min_score=self.config.min_score_to_trade,
            group_key="symbol",
            limit=3,
        )
        by_regime = self.db.get_shadow_opportunity_group_summaries_since(
            window["since"],
            min_score=self.config.min_score_to_trade,
            group_key="market_regime",
            limit=3,
        )
        recommendation = _recommendation(labels, events)
        metrics = _label_metrics(labels)
        high_score_metrics = _label_metrics(high_score_labels)
        lines = [
            SUMMARY_START,
            f"now: {window['now']}",
            f"since: {window['since']}",
            f"hours: {window['hours']}",
            (
                "safety: "
                f"PAPER={self.config.paper_trading} LIVE={self.config.live_trading} "
                f"DRY={self.config.dry_run} LIGHTWEIGHT={self.config.worker_lightweight_mode}"
            ),
            (
                "observations: "
                f"total={safe_int(observations.get('total'))} "
                f"LONG={safe_int(observations.get('long_count'))} "
                f"SHORT={safe_int(observations.get('short_count'))} "
                f"NO_TRADE={safe_int(observations.get('no_trade_count'))} "
                f"high_score={safe_int(observations.get('high_score_count'))}"
            ),
            (
                "labels: "
                f"total={safe_int(labels.get('total_labels'))} "
                f"TIME={safe_int(labels.get('time_count'))} "
                f"SL={safe_int(labels.get('sl_count'))} "
                f"TP1={safe_int(labels.get('tp1_count'))} "
                f"TP2={safe_int(labels.get('tp2_count'))} "
                f"PF={safe_float(labels.get('profit_factor')):.2f} "
                f"TIME%={metrics['time_ratio'] * 100:.1f} "
                f"SL%={metrics['sl_ratio'] * 100:.1f} "
                f"TP%={metrics['tp_ratio'] * 100:.1f}"
            ),
            (
                "win_loss_time_balance: "
                f"TP={safe_int(labels.get('tp1_count')) + safe_int(labels.get('tp2_count'))} "
                f"SL={safe_int(labels.get('sl_count'))} "
                f"TIME={safe_int(labels.get('time_count'))}"
            ),
            (
                "high_score_performance: "
                f"labels={safe_int(high_score_labels.get('total_labels'))} "
                f"PF={safe_float(high_score_labels.get('profit_factor')):.2f} "
                f"TIME%={high_score_metrics['time_ratio'] * 100:.1f} "
                f"SL%={high_score_metrics['sl_ratio'] * 100:.1f} "
                f"TP%={high_score_metrics['tp_ratio'] * 100:.1f}"
            ),
            f"paper: open={safe_int(paper.get('open'))} closed={safe_int(paper.get('closed'))}",
            (
                "events: "
                f"slot_blocks={events.get('training_slot_block', 0)} "
                f"high_score_missed={events.get('training_high_score_missed', 0)} "
                f"api_429={events.get('training_api_429', 0)} "
                f"paper_reconcile={events.get('training_paper_reconcile', 0)}"
            ),
            "dominant_regimes:",
            *_rows_to_lines(observations.get("regimes", [])),
            "top_high_score_symbols:",
            *_rows_to_lines(observations.get("top_symbols", [])),
            "by_symbol_edge:",
            *_edge_rows_to_lines(by_symbol),
            "by_regime_edge:",
            *_edge_rows_to_lines(by_regime),
            f"recommendation: {recommendation}",
            "final_recommendation: NO LIVE",
            SUMMARY_END,
        ]
        return "\n".join(lines)

    def acceleration_plan(self, *, hours: int = 24) -> str:
        window = self._window(hours)
        labels = self.db.get_signal_label_summary_since(window["since"])
        events = self.db.get_event_type_counts_since(window["since"])
        observations = self.db.get_training_observation_summary_since(
            window["since"],
            min_score=self.config.min_score_to_trade,
            limit=5,
        )
        biggest = _biggest_problem(self.config, labels, events, observations)
        lines = [
            PLAN_START,
            f"hours: {window['hours']}",
            f"biggest_problem: {biggest}",
            "suggested_next_research:",
            *_plan_steps(biggest),
            "do_not_change:",
            "- LIVE_TRADING=false",
            "- DRY_RUN=true",
            "- PAPER_TRADING=true",
            "final_recommendation: NO LIVE",
            PLAN_END,
        ]
        return "\n".join(lines)

    @staticmethod
    def _window(hours: int) -> dict[str, Any]:
        hours = max(1, int(hours or 6))
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=hours)
        return {"now": now.isoformat(), "since": since.isoformat(), "hours": hours}


def _recommendation(labels: dict[str, Any], events: dict[str, int]) -> str:
    total = safe_float(labels.get("total_labels"))
    if total > 0:
        metrics = _label_metrics(labels)
        if safe_float(labels.get("profit_factor")) < 1.0 or metrics["tp_ratio"] < 0.05:
            return "NEED_RESEARCH_POOR_EDGE"
        if metrics["time_ratio"] > 0.80 or safe_float(labels.get("sl_count")) > safe_float(labels.get("tp1_count")) + safe_float(labels.get("tp2_count")):
            return "NEED_RESEARCH"
    if events.get("training_api_429", 0) > 0:
        return "CHECK_RATE_LIMIT"
    if events.get("training_slot_block", 0) > 0:
        return "CHECK_SLOT"
    return "PAPER ONLY"


def _biggest_problem(config: BotConfig, labels: dict[str, Any], events: dict[str, int], observations: dict[str, Any]) -> str:
    if config.live_trading:
        return "safety_live"
    total = safe_float(labels.get("total_labels"))
    if total <= 0 and safe_int(observations.get("total")) == 0:
        return "no_data"
    metrics = _label_metrics(labels)
    if total > 0 and safe_float(labels.get("profit_factor")) < 1.0:
        return "poor_edge"
    if total > 0 and metrics["tp_ratio"] < 0.05:
        return "low_tp_rate"
    if total > 0 and metrics["time_ratio"] > 0.60:
        return "too_many_time"
    if total > 0 and metrics["sl_ratio"] > metrics["tp_ratio"] * 2:
        return "too_many_sl"
    if events.get("training_slot_block", 0) > 0 and safe_float(labels.get("profit_factor")) >= 1.0 and metrics["tp_ratio"] >= 0.05:
        return "slot"
    if events.get("training_api_429", 0) > 0:
        return "rate_limit"
    if safe_int(observations.get("high_score_count")) == 0:
        return "no_strong_signals"
    return "paper_observation"


def _plan_steps(problem: str) -> list[str]:
    if problem == "slot":
        return [
            "1. revisar training-summary --hours 24 para high_score_missed",
            "2. ejecutar reconcile-paper si hay PAPER_OPEN antigua",
            "3. mantener slots reales/paper sin ampliar hasta edge validado",
        ]
    if problem == "rate_limit":
        return [
            "1. revisar frecuencia de escaneo y 429",
            "2. mantener backoff activo",
            "3. no lanzar research pesado en worker",
        ]
    if problem in {"poor_edge", "low_tp_rate", "too_many_time", "too_many_sl"}:
        return [
            "1. ejecutar shadow-opportunity --hours 24",
            "2. analizar por simbolo/regimen/score bucket",
            "3. no ampliar slots hasta PF>1 y TP rate suficiente",
            "4. revisar filtros de CHOPPY/RANGE/TREND_DOWN",
            "5. revisar scoring high_score porque muchos score altos no llegan a TP",
        ]
    if problem in {"TIME", "SL"}:
        return [
            "1. ejecutar strategy-lab offline en local/Railway shell controlada",
            "2. ejecutar virtual-portfolio offline",
            "3. revisar daily-summary/training-summary antes de tocar filtros",
        ]
    return [
        "1. seguir acumulando paper labels",
        "2. revisar training-summary cada pocas horas",
        "3. no activar live sin edge validado",
    ]


def _label_metrics(labels: dict[str, Any]) -> dict[str, float]:
    total = safe_float(labels.get("total_labels"))
    tp = safe_float(labels.get("tp1_count")) + safe_float(labels.get("tp2_count"))
    sl = safe_float(labels.get("sl_count"))
    time_count = safe_float(labels.get("time_count"))
    return {
        "time_ratio": time_count / max(total, 1.0) if total else 0.0,
        "sl_ratio": sl / max(total, 1.0) if total else 0.0,
        "tp_ratio": tp / max(total, 1.0) if total else 0.0,
    }


def _rows_to_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    lines: list[str] = []
    for row in rows[:5]:
        key = row.get("key") or row.get("group_value") or "NA"
        count = row.get("count") or row.get("total_labels") or 0
        extra = f" max_score={safe_int(row.get('max_score'))}" if "max_score" in row else ""
        lines.append(f"- {key}: {safe_int(count)}{extra}")
    return lines


def _edge_rows_to_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- {row.get('group_value') or 'NA'} labels={safe_int(row.get('total_labels'))} "
            f"PF={safe_float(row.get('profit_factor')):.2f} "
            f"TIME%={safe_float(row.get('time_ratio')) * 100:.1f} "
            f"SL%={safe_float(row.get('sl_ratio')) * 100:.1f} "
            f"TP%={safe_float(row.get('tp_ratio')) * 100:.1f}"
        )
        for row in rows[:3]
    ]
