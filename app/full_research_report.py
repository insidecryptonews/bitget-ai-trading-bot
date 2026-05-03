from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import BotConfig
from .database import Database
from .research_engine import ResearchEngine
from .research_lab import ResearchLab, ResearchMetrics
from .utils import safe_float, safe_int


START_MARKER = "===== FULL RESEARCH LAB REPORT START ====="
END_MARKER = "===== FULL RESEARCH LAB REPORT END ====="


@dataclass(frozen=True)
class FullResearchSummary:
    recommendation: str
    reason: str
    profit_factor: float
    expectancy: float
    decisive_win_rate: float
    time_ratio: float


class FullResearchReporter:
    """Human-readable, Railway-log friendly research report."""

    def __init__(self, db: Database, config: BotConfig, logger=None, reports_dir: Path | None = None) -> None:
        self.db = db
        self.config = config
        self.logger = logger
        self.research_engine = ResearchEngine(db, logger)
        self.research_lab = ResearchLab(db, config, logger, reports_dir=reports_dir)

    def build_report(self) -> str:
        # Variants report refreshes strategy_variant_results when labels exist.
        variants_report = self._safe_section("Variants / reverse shadow summary", self.research_engine.build_variants_report)
        counts = self.db.get_table_counts()
        observations = self.db.fetch_signal_observations()
        labels = self.db.fetch_signal_labels()
        labeled_rows = self.db.fetch_labeled_signal_rows()
        trades = self.db.latest_trades(10)
        variants = self.db.fetch_strategy_variants()
        variant_results = self.db.fetch_strategy_variant_results()
        dataset = self.research_lab.builder.build()
        lab_discovery = self.research_lab.discover()
        lab_report = self._safe_section("Research Lab markdown report", lambda: self.research_lab.build_markdown_report(dataset))
        recommended_config_path = self.research_lab.recommend_config()
        engine_report = self._safe_section("Research Engine report", self.research_engine.build_report)

        summary = self._summary(labeled_rows)
        lines = [
            START_MARKER,
            "FULL RESEARCH LAB REPORT",
            "========================",
            "",
            "Resumen para usuario",
            f"- Recomendacion: {summary.recommendation}",
            f"- Motivo: {summary.reason}",
            f"- Profit factor aproximado: {summary.profit_factor:.2f}",
            f"- Expectancy media: {summary.expectancy:.5f}",
            f"- Win rate en labels decisivas: {summary.decisive_win_rate:.1%}",
            f"- TIME ratio: {summary.time_ratio:.1%}",
            "",
            "Conteo de tablas",
            *self._table_count_lines(counts),
            "",
            "Senales",
            *self._signal_lines(observations),
            "",
            "Labels",
            *self._label_lines(labeled_rows),
            "",
            "Diagnostico",
            *self._diagnostic_lines(summary, labeled_rows),
            "",
            "Normal vs reverse",
            *self._normal_reverse_lines(dataset),
            "",
            "Top variantes",
            *self._variant_lines(variants, variant_results, counts),
            "",
            "Ultimas trades",
            *self._trade_lines(trades),
            "",
            "Errores o incoherencias",
            *self._inconsistency_lines(counts, observations, trades, variant_results),
            "",
            "Research Lab discover",
            f"- Dataset: {lab_discovery['dataset_rows']} observations, {lab_discovery['labels']} labels, {lab_discovery['shadow_labels']} shadow labels",
            f"- Live recommendation: {lab_discovery['live_recommendation']}",
            f"- Best candidate: {lab_discovery['best_candidate']['name'] if lab_discovery['best_candidate'] else 'none'}",
            f"- recommended_config.env: {recommended_config_path}",
            "",
            engine_report,
            "",
            lab_report,
            "",
            variants_report,
            END_MARKER,
        ]
        return "\n".join(lines)

    def _safe_section(self, title: str, builder) -> str:
        try:
            return builder()
        except Exception as exc:
            if self.logger:
                self.logger.warning("No se pudo generar seccion %s: %s", title, exc)
            return f"{title}: no disponible ({exc})"

    @staticmethod
    def _summary(rows: list[dict[str, Any]]) -> FullResearchSummary:
        metrics = ResearchMetrics.calculate(rows)
        decisive = [row for row in rows if row.get("first_barrier_hit") in {"TP1", "TP2", "SL"}]
        decisive_wins = sum(1 for row in decisive if safe_int(row.get("label")) == 1)
        decisive_wr = decisive_wins / max(len(decisive), 1)
        pf = metrics["profit_factor"]
        expectancy = metrics["expectancy"]
        time_ratio = metrics["time_ratio"]
        if not rows:
            return FullResearchSummary("NO LIVE", "Aun no hay labels suficientes para evaluar edge.", pf, expectancy, decisive_wr, time_ratio)
        if pf >= 1.2 and expectancy > 0 and decisive_wr >= 0.55 and metrics["total_labels"] >= 100 and time_ratio < 0.7:
            return FullResearchSummary("CANDIDATE FOR FURTHER TESTING", "Hay senales iniciales positivas, pero solo para mas paper/research.", pf, expectancy, decisive_wr, time_ratio)
        if pf >= 1.0 and expectancy > 0:
            return FullResearchSummary("PAPER ONLY", "Edge debil o inestable; necesita mas datos antes de plantear live.", pf, expectancy, decisive_wr, time_ratio)
        return FullResearchSummary("NO LIVE", "Profit factor/expectancy insuficientes o demasiadas salidas negativas/TIME.", pf, expectancy, decisive_wr, time_ratio)

    @staticmethod
    def _table_count_lines(counts: dict[str, int]) -> list[str]:
        tables = [
            "signal_observations",
            "signal_labels",
            "trades",
            "events",
            "strategy_variants",
            "strategy_variant_results",
        ]
        return [f"- {table}: {counts.get(table, 0)}" for table in tables]

    @staticmethod
    def _signal_lines(observations: list[dict[str, Any]]) -> list[str]:
        total = len(observations)
        shadow = sum(1 for row in observations if safe_int(row.get("shadow_strategy")) == 1)
        operated = sum(1 for row in observations if safe_int(row.get("operated")) == 1)
        selected = sum(1 for row in observations if safe_int(row.get("selected_by_allocator")) == 1)
        approved = sum(1 for row in observations if safe_int(row.get("risk_manager_approved")) == 1)
        return [
            f"- total senales: {total}",
            f"- senales normales: {total - shadow}",
            f"- senales shadow: {shadow}",
            f"- senales operadas: {operated}",
            f"- seleccionadas por allocator: {selected}",
            f"- aprobadas por risk manager: {approved}",
        ]

    @staticmethod
    def _label_lines(rows: list[dict[str, Any]]) -> list[str]:
        metrics = ResearchMetrics.calculate(rows)
        decisive = [row for row in rows if row.get("first_barrier_hit") in {"TP1", "TP2", "SL"}]
        decisive_wins = sum(1 for row in decisive if safe_int(row.get("label")) == 1)
        return [
            f"- total labels: {safe_int(metrics['total_labels'])}",
            f"- TIME count: {safe_int(metrics['time_count'])}",
            f"- SL count: {safe_int(metrics['sl_count'])}",
            f"- TP1 count: {safe_int(metrics['tp1_count'])}",
            f"- TP2 count: {safe_int(metrics['tp2_count'])}",
            f"- win rate real sobre labels decisivas: {decisive_wins / max(len(decisive), 1):.1%}",
            f"- profit factor aproximado: {metrics['profit_factor']:.2f}",
            f"- expectancy media: {metrics['expectancy']:.5f}",
            f"- retorno medio TIME: {mean_return_by_barrier(rows, 'TIME'):.5f}",
            f"- retorno medio SL: {mean_return_by_barrier(rows, 'SL'):.5f}",
            f"- retorno medio TP1: {mean_return_by_barrier(rows, 'TP1'):.5f}",
            f"- retorno medio TP2: {mean_return_by_barrier(rows, 'TP2'):.5f}",
        ]

    @staticmethod
    def _diagnostic_lines(summary: FullResearchSummary, rows: list[dict[str, Any]]) -> list[str]:
        metrics = ResearchMetrics.calculate(rows)
        tp_count = metrics["tp1_count"] + metrics["tp2_count"]
        lines = []
        lines.append("- demasiadas TIME: " + ("SI" if summary.time_ratio > 0.60 else "NO"))
        lines.append("- SL superior a TP: " + ("SI" if metrics["sl_count"] > tp_count else "NO"))
        lines.append("- profit factor insuficiente: " + ("SI" if summary.profit_factor < 1.2 else "NO"))
        lines.append("- hay edge demostrado: " + ("SI" if summary.recommendation == "CANDIDATE FOR FURTHER TESTING" else "NO"))
        lines.append(f"- recomendacion clara: {summary.recommendation}")
        return lines

    @staticmethod
    def _normal_reverse_lines(dataset: list[dict[str, Any]]) -> list[str]:
        labeled = [row for row in dataset if row.get("label") is not None]
        normal = [row for row in labeled if safe_int(row.get("shadow_strategy")) == 0]
        shadow = [row for row in labeled if safe_int(row.get("shadow_strategy")) == 1]
        normal_metrics = ResearchMetrics.calculate(normal)
        shadow_metrics = ResearchMetrics.calculate(shadow)
        conclusion = "sin evidencia suficiente"
        if len(shadow) >= 100:
            if shadow_metrics["profit_factor"] > normal_metrics["profit_factor"]:
                conclusion = "reverse/shadow parece mejor, solo para investigar; no operar real"
            else:
                conclusion = "normal iguala o supera a reverse/shadow"
        return [
            f"- labels normal: {len(normal)}",
            f"- labels reverse/shadow: {len(shadow)}",
            f"- PF normal: {normal_metrics['profit_factor']:.2f}",
            f"- PF reverse: {shadow_metrics['profit_factor']:.2f}",
            f"- win rate normal: {normal_metrics['win_rate']:.1%}",
            f"- win rate reverse: {shadow_metrics['win_rate']:.1%}",
            f"- conclusion: {conclusion}",
        ]

    @staticmethod
    def _variant_lines(variants: list[dict[str, Any]], results: list[dict[str, Any]], counts: dict[str, int]) -> list[str]:
        lines = [
            f"- strategy_variants: {counts.get('strategy_variants', len(variants))}",
            f"- strategy_variant_results: {counts.get('strategy_variant_results', len(results))}",
        ]
        if variants and counts.get("strategy_variant_results", 0) == 0:
            lines.append("- aviso: hay strategy_variants pero strategy_variant_results esta en 0.")
            lines.append("- accion tomada: el full report ejecuta el agregador de variantes; si sigue en 0, faltan labels de variantes o hay que revisar la DB.")
        if not variants:
            lines.append("- no hay variantes registradas todavia.")
            return lines
        result_by_variant = {safe_int(row.get("variant_id") or row.get("id")): row for row in results}
        for variant in variants[:10]:
            variant_id = safe_int(variant.get("id"))
            result = result_by_variant.get(variant_id, {})
            lines.append(
                f"- {variant.get('name')}: labels={safe_int(result.get('total_labels'))}, "
                f"PF={safe_float(result.get('profit_factor')):.2f}, WR={safe_float(result.get('win_rate')):.1%}, "
                f"score={safe_float(result.get('score')):.3f}"
            )
        return lines

    @staticmethod
    def _trade_lines(trades: list[dict[str, Any]]) -> list[str]:
        if not trades:
            return ["- sin trades registradas."]
        lines = []
        for trade in trades[:10]:
            lines.append(
                "- id={id} status={status} symbol={symbol} side={side} entry={entry} "
                "SL={stop_loss} TP1={take_profit_1} TP2={take_profit_2} "
                "realized_pnl={realized_pnl} unrealized_pnl={unrealized_pnl}".format(**{key: trade.get(key, "") for key in (
                    "id",
                    "status",
                    "symbol",
                    "side",
                    "entry",
                    "stop_loss",
                    "take_profit_1",
                    "take_profit_2",
                    "realized_pnl",
                    "unrealized_pnl",
                )})
            )
        return lines

    def _inconsistency_lines(
        self,
        counts: dict[str, int],
        observations: list[dict[str, Any]],
        trades: list[dict[str, Any]],
        variant_results: list[dict[str, Any]],
    ) -> list[str]:
        lines: list[str] = []
        shadow_count = sum(1 for row in observations if safe_int(row.get("shadow_strategy")) == 1)
        shadow_labeled_count = len(self.db.fetch_strategy_variant_labeled_rows())
        if shadow_count > 0 and shadow_labeled_count == 0:
            lines.append("- shadow_strategy suma > 0 pero no hay shadow labels agregables todavia.")
        if counts.get("trades", 0) > 0 and not trades:
            lines.append("- INCOHERENCIA: trades count > 0 pero SELECT ultimas trades devuelve 0.")
        if counts.get("strategy_variants", 0) > 0 and counts.get("strategy_variant_results", 0) == 0:
            lines.append("- INCOHERENCIA/AVISO: strategy_variants > 0 pero strategy_variant_results = 0.")
        if not lines:
            lines.append("- no se detectaron incoherencias criticas.")
        return lines


def mean_return_by_barrier(rows: list[dict[str, Any]], barrier: str) -> float:
    values = [safe_float(row.get("realized_return_pct")) for row in rows if row.get("first_barrier_hit") == barrier]
    return sum(values) / max(len(values), 1)
