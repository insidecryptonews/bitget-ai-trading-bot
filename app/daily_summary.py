from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import BotConfig
from .database import Database
from .utils import safe_float, safe_int


START_MARKER = "DAILY RESEARCH SUMMARY START"
END_MARKER = "DAILY RESEARCH SUMMARY END"


@dataclass(frozen=True)
class DailySummaryWindow:
    now: datetime
    since: datetime
    hours: int


class DailyResearchSummary:
    """Cheap 24h research summary for Railway logs. Research-only."""

    def __init__(self, config: BotConfig, db: Database, logger=None) -> None:
        self.config = config
        self.db = db
        self.logger = logger

    def build(self, *, hours: int = 24) -> str:
        hours = max(1, int(hours or 24))
        now = datetime.now(timezone.utc)
        window = DailySummaryWindow(now=now, since=now - timedelta(hours=hours), hours=hours)
        since_iso = window.since.isoformat()
        stale_before_iso = (now - timedelta(hours=max(1, self.config.stale_paper_trade_hours))).isoformat()

        counts = self._safe_counts()
        labels = self._safe_label_summary(since_iso)
        paper = self._safe_paper_summary()
        stale = self._safe_stale_paper(stale_before_iso)
        autopilot_runs = self._safe_autopilot_runs()

        lines = [
            START_MARKER,
            "",
            "Ventana temporal",
            f"- now: {window.now.isoformat()}",
            f"- since: {window.since.isoformat()}",
            f"- hours: {window.hours}",
            "",
            "Estado de seguridad",
            f"- LIVE_TRADING: {self.config.live_trading}",
            f"- DRY_RUN: {self.config.dry_run}",
            f"- PAPER_TRADING: {self.config.paper_trading}",
            f"- ENABLE_KRONOS_RESEARCH: {self.config.enable_kronos_research}",
            f"- ENABLE_RESEARCH_AUTOPILOT: {self.config.enable_research_autopilot}",
            f"- ENABLE_VIRTUAL_POSITION_RESEARCH: {self.config.enable_virtual_position_research}",
            "",
            "Conteos actuales",
            *_count_lines(counts),
            "",
            "Metricas compactas 24h",
            f"- total labels: {safe_int(labels.get('total_labels'))}",
            f"- TIME: {safe_int(labels.get('time_count'))}",
            f"- SL: {safe_int(labels.get('sl_count'))}",
            f"- TP1: {safe_int(labels.get('tp1_count'))}",
            f"- TP2: {safe_int(labels.get('tp2_count'))}",
            f"- profit factor aproximado: {safe_float(labels.get('profit_factor')):.2f}",
            f"- expectancy media: {safe_float(labels.get('avg_return_all')):.5f}",
            f"- win rate decisivo: {safe_float(labels.get('decisive_win_rate')):.1%}",
            "- recomendacion final: NO LIVE",
            "",
            "Paper",
            f"- operaciones paper abiertas: {safe_int(paper.get('open'))}",
            f"- operaciones paper cerradas: {safe_int(paper.get('closed'))}",
            f"- stale threshold hours: {self.config.stale_paper_trade_hours}",
            f"- stale paper trades: {len(stale)}",
            *_trade_lines(stale, prefix="  "),
            "",
            "Research Autopilot",
            *_autopilot_lines(autopilot_runs),
            "",
            "Best/worst 24h por PF con muestra suficiente",
            *_group_section("estrategias", self._safe_group("strategy_type", since_iso, best=True), self._safe_group("strategy_type", since_iso, best=False)),
            *_group_section("simbolos", self._safe_group("symbol", since_iso, best=True), self._safe_group("symbol", since_iso, best=False)),
            *_group_section("regimenes", self._safe_group("market_regime", since_iso, best=True), self._safe_group("market_regime", since_iso, best=False)),
            "",
            END_MARKER,
        ]
        return "\n".join(lines)

    def _safe_counts(self) -> dict[str, int]:
        try:
            return self.db.get_table_counts()
        except Exception as exc:
            self._warn("daily-summary counts fallo: %s", exc)
            return {}

    def _safe_label_summary(self, since_iso: str) -> dict[str, float]:
        try:
            return self.db.get_signal_label_summary_since(since_iso)
        except Exception as exc:
            self._warn("daily-summary labels fallo: %s", exc)
            return {}

    def _safe_paper_summary(self) -> dict[str, int]:
        try:
            return self.db.get_paper_trade_summary()
        except Exception as exc:
            self._warn("daily-summary paper fallo: %s", exc)
            return {"open": 0, "closed": 0, "total": 0}

    def _safe_stale_paper(self, older_than_iso: str) -> list[dict[str, Any]]:
        try:
            return self.db.fetch_stale_open_paper_trades(older_than_iso=older_than_iso, limit=10)
        except Exception as exc:
            self._warn("daily-summary stale paper fallo: %s", exc)
            return []

    def _safe_autopilot_runs(self) -> list[dict[str, Any]]:
        try:
            return self.db.fetch_research_autopilot_runs(limit=5)
        except Exception as exc:
            self._warn("daily-summary autopilot runs fallo: %s", exc)
            return []

    def _safe_group(self, key: str, since_iso: str, *, best: bool) -> list[dict[str, Any]]:
        try:
            return self.db.get_label_group_summaries(key, since_iso=since_iso, min_labels=100, limit=5, best=best)
        except Exception as exc:
            self._warn("daily-summary group %s fallo: %s", key, exc)
            return []

    def _warn(self, message: str, *args: Any) -> None:
        if self.logger:
            self.logger.warning(message, *args)


def _count_lines(counts: dict[str, int]) -> list[str]:
    keys = [
        "signal_observations",
        "signal_labels",
        "signal_explanations",
        "signal_price_paths",
        "signal_counterfactuals",
        "stop_loss_failure_clusters",
        "win_clusters",
        "research_rules",
        "virtual_research_trades",
        "virtual_strategy_summary",
        "strategy_lab_candidates",
        "strategy_lab_walkforward",
        "strategy_lab_recommendations",
        "kronos_predictions",
        "research_autopilot_runs",
        "trades",
    ]
    return [f"- {key}: {safe_int(counts.get(key))}" for key in keys]


def _trade_lines(trades: list[dict[str, Any]], *, prefix: str = "") -> list[str]:
    if not trades:
        return [f"{prefix}- sin PAPER_OPEN stale"]
    lines = []
    for trade in trades[:10]:
        lines.append(
            (
                f"{prefix}- id={safe_int(trade.get('id'))} {trade.get('timestamp')} "
                f"{trade.get('symbol')} {trade.get('side')} status={trade.get('status')} "
                f"entry={safe_float(trade.get('entry')):.6f}"
            )
        )
    return lines


def _autopilot_lines(runs: list[dict[str, Any]]) -> list[str]:
    if not runs:
        return ["- sin ejecuciones registradas todavia"]
    lines = []
    for run in runs[:5]:
        lines.append(
            (
                f"- {run.get('started_at')} -> {run.get('ended_at') or 'running'} "
                f"status={run.get('status')} duration={safe_float(run.get('duration_seconds')):.1f}s "
                f"processed={safe_int(run.get('processed'))} "
                f"explanations={safe_int(run.get('explanations_created'))} "
                f"counterfactuals={safe_int(run.get('counterfactuals_created'))} "
                f"clusters={safe_int(run.get('clusters_updated'))} "
                f"rules={safe_int(run.get('rules_generated'))} "
                f"virtual={safe_int(run.get('virtual_trades_simulated'))} "
                f"errors={safe_int(run.get('errors'))}"
            )
        )
    return lines


def _group_section(title: str, best: list[dict[str, Any]], worst: list[dict[str, Any]]) -> list[str]:
    return [
        f"- mejores {title}:",
        *_group_lines(best),
        f"- peores {title}:",
        *_group_lines(worst),
    ]


def _group_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["  - evidencia insuficiente"]
    return [
        (
            f"  - {row.get('group_value')}: labels={safe_int(row.get('total_labels'))}, "
            f"PF={safe_float(row.get('profit_factor')):.2f}, "
            f"expectancy={safe_float(row.get('expectancy')):.5f}, "
            f"WR decisivo={safe_float(row.get('decisive_win_rate')):.1%}"
        )
        for row in rows
    ]
