from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import BotConfig
from .database import Database
from .phase2_persist import Phase2PersistResult, Phase2Persister
from .virtual_portfolio import VirtualPortfolioResearch, VirtualPortfolioResult


@dataclass
class ResearchAutopilotResult:
    pending_labels: int = 0
    phase2: Phase2PersistResult | None = None
    virtual: VirtualPortfolioResult | None = None
    errors: int = 0

    def to_text(self) -> str:
        phase2 = self.phase2 or Phase2PersistResult()
        virtual = self.virtual or VirtualPortfolioResult()
        lines = [
            "RESEARCH AUTOPILOT START",
            f"pending labels: {self.pending_labels}",
            f"explanations created: {phase2.explanations_created}",
            f"counterfactuals created: {phase2.counterfactuals_created}",
            f"clusters updated: {phase2.stop_loss_clusters_updated + phase2.win_clusters_updated}",
            f"rules generated: {phase2.research_rules_generated}",
            f"virtual trades simulated: {virtual.virtual_trades_simulated}",
            "best virtual strategies:",
            *_summary_lines(virtual.best_virtual_strategies),
            "worst virtual strategies:",
            *_summary_lines(virtual.worst_virtual_strategies),
            f"errors: {self.errors + phase2.errors + virtual.errors}",
            "final recommendation: NO LIVE",
            "RESEARCH AUTOPILOT END",
        ]
        return "\n".join(lines)


class ResearchAutopilot:
    """Research-only harvester. It never activates live or paper slots."""

    def __init__(self, config: BotConfig, db: Database, logger=None) -> None:
        self.config = config
        self.db = db
        self.logger = logger
        self.running = False

    def run_once(self) -> ResearchAutopilotResult:
        result = ResearchAutopilotResult()
        try:
            result.pending_labels = self.db.count_phase2_pending_labels()
        except Exception as exc:
            self._warn("research autopilot no pudo contar labels pendientes: %s", exc)
            result.errors += 1
        try:
            result.phase2 = Phase2Persister(self.db, self.logger).persist(
                limit=self.config.research_autopilot_phase2_limit_per_run,
                batch_size=self.config.research_autopilot_batch_size,
                progress=None,
            )
        except Exception as exc:
            self._warn("research autopilot phase2-persist fallo: %s", exc)
            result.errors += 1
        if self.config.enable_virtual_position_research:
            try:
                result.virtual = VirtualPortfolioResearch(self.db, self.logger).simulate(
                    limit=self.config.virtual_portfolio_max_labels_per_run,
                    max_concurrent=self.config.virtual_max_concurrent_positions,
                )
            except Exception as exc:
                self._warn("research autopilot virtual portfolio fallo: %s", exc)
                result.errors += 1
        return result

    def _warn(self, message: str, *args: Any) -> None:
        if self.logger:
            self.logger.warning(message, *args)


def _summary_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- sin evidencia suficiente"]
    return [
        (
            f"- {row.get('variant_name')}: trades={row.get('total_trades', 0)}, "
            f"PF={float(row.get('profit_factor') or 0):.2f}, "
            f"expectancy={float(row.get('expectancy') or 0):.5f}"
        )
        for row in rows[:5]
    ]
