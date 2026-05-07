from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .counterfactual_engine import CounterfactualEngine
from .database import Database
from .explainability_engine import ExplainabilityEngine
from .path_analyzer import PricePathAnalyzer
from .rule_miner import RuleMiner
from .stop_loss_analyzer import StopLossAnalyzer
from .utils import safe_int
from .win_analyzer import WinAnalyzer


@dataclass
class Phase2PersistResult:
    target_labels: int = 0
    processed_labels: int = 0
    explanations_created: int = 0
    price_paths_created: int = 0
    counterfactuals_created: int = 0
    stop_loss_clusters_updated: int = 0
    win_clusters_updated: int = 0
    research_rules_generated: int = 0
    errors: int = 0

    def to_text(self) -> str:
        return "\n".join(
            [
                "Phase 2 persist research-only",
                "=============================",
                f"labels objetivo: {self.target_labels}",
                f"labels procesadas: {self.processed_labels}",
                f"explanations creadas: {self.explanations_created}",
                f"price paths creados: {self.price_paths_created}",
                f"counterfactuals creados: {self.counterfactuals_created}",
                f"clusters SL actualizados: {self.stop_loss_clusters_updated}",
                f"win clusters actualizados: {self.win_clusters_updated}",
                f"reglas generadas: {self.research_rules_generated}",
                f"errores: {self.errors}",
                "modo: research-only; live trading no se modifica ni se activa.",
            ]
        )


class Phase2Persister:
    """Batch writer for Phase 2 research artifacts. It never sends orders."""

    def __init__(self, db: Database, logger=None) -> None:
        self.db = db
        self.logger = logger
        self.explainer = ExplainabilityEngine(db, logger)
        self.path_analyzer = PricePathAnalyzer(db, logger)
        self.counterfactuals = CounterfactualEngine(db, logger)
        self.sl_analyzer = StopLossAnalyzer(db, logger)
        self.win_analyzer = WinAnalyzer(db, logger)
        self.rule_miner = RuleMiner(db, logger)

    def persist(
        self,
        *,
        limit: int | None = 5000,
        batch_size: int = 250,
        progress: Callable[[str], None] | None = print,
    ) -> Phase2PersistResult:
        batch_size = max(1, int(batch_size or 250))
        pending = self._safe_count_pending()
        target = pending if limit is None else min(max(0, int(limit)), pending)
        result = Phase2PersistResult(target_labels=target)
        processed_rows: list[dict[str, Any]] = []

        while result.processed_labels < target:
            current_batch_size = min(batch_size, target - result.processed_labels)
            rows = self._safe_fetch_batch(current_batch_size)
            if not rows:
                break
            for raw_row in rows:
                row = self._normalize_row(raw_row)
                try:
                    if self.db.record_signal_explanation_once(self.explainer.explain_row(row)):
                        result.explanations_created += 1
                except Exception as exc:
                    self._warn("phase2-persist explanation fallo: %s", exc)
                    result.errors += 1
                try:
                    if self.db.record_signal_price_path_once(self.path_analyzer.analyze(row)):
                        result.price_paths_created += 1
                except Exception as exc:
                    self._warn("phase2-persist price path fallo: %s", exc)
                    result.errors += 1
                try:
                    for counterfactual in self.counterfactuals.simulate_row(row):
                        if self.db.record_signal_counterfactual_once(counterfactual):
                            result.counterfactuals_created += 1
                except Exception as exc:
                    self._warn("phase2-persist counterfactual fallo: %s", exc)
                    result.errors += 1
                processed_rows.append(row)
                result.processed_labels += 1
            self._progress(progress, result)

        self._persist_aggregates(processed_rows, result)
        self._progress(progress, result, final=True)
        return result

    def _persist_aggregates(self, rows: list[dict[str, Any]], result: Phase2PersistResult) -> None:
        if not rows:
            return
        try:
            sl_result = self.sl_analyzer.analyze_rows(rows)
            for cluster in sl_result.get("clusters", []):
                self.db.upsert_stop_loss_failure_cluster(cluster)
                result.stop_loss_clusters_updated += 1
        except Exception as exc:
            self._warn("phase2-persist clusters SL fallo: %s", exc)
            result.errors += 1
        try:
            for cluster in self.win_analyzer.analyze_rows(rows):
                self.db.upsert_win_cluster(cluster)
                result.win_clusters_updated += 1
        except Exception as exc:
            self._warn("phase2-persist win clusters fallo: %s", exc)
            result.errors += 1
        try:
            for rule in self.rule_miner.mine_rows(rows):
                self.db.upsert_research_rule(rule)
                result.research_rules_generated += 1
        except Exception as exc:
            self._warn("phase2-persist research rules fallo: %s", exc)
            result.errors += 1

    def _safe_count_pending(self) -> int:
        try:
            return self.db.count_phase2_pending_labels()
        except Exception as exc:
            self._warn("phase2-persist no pudo contar pendientes: %s", exc)
            return 0

    def _safe_fetch_batch(self, batch_size: int) -> list[dict[str, Any]]:
        try:
            return self.db.fetch_phase2_labeled_rows(limit=batch_size, missing_only=True)
        except Exception as exc:
            self._warn("phase2-persist no pudo leer batch: %s", exc)
            return []

    @staticmethod
    def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(row)
        normalized["observation_id"] = safe_int(normalized.get("observation_id") or normalized.get("id"))
        normalized["label_id"] = safe_int(normalized.get("label_id"))
        return normalized

    def _progress(self, progress: Callable[[str], None] | None, result: Phase2PersistResult, final: bool = False) -> None:
        prefix = "final" if final else "progreso"
        message = (
            f"{prefix}: procesadas {result.processed_labels}/{result.target_labels} | "
            f"explanations creadas={result.explanations_created} | "
            f"counterfactuals creados={result.counterfactuals_created} | "
            f"clusters actualizados={result.stop_loss_clusters_updated + result.win_clusters_updated} | "
            f"reglas generadas={result.research_rules_generated} | errores={result.errors}"
        )
        if progress:
            progress(message)
        if self.logger:
            self.logger.info(message)

    def _warn(self, message: str, *args: Any) -> None:
        if self.logger:
            self.logger.warning(message, *args)
