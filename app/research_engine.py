from __future__ import annotations

import argparse
from collections import defaultdict
from typing import Any

from .config import load_config
from .database import Database
from .logger import setup_logger
from .utils import safe_float


class ResearchEngine:
    def __init__(self, db: Database, logger=None) -> None:
        self.db = db
        self.logger = logger

    def build_report(self) -> str:
        observations = self.db.fetch_signal_observations()
        labeled = self.db.fetch_labeled_signal_rows()
        lines: list[str] = []
        lines.append("Research report")
        lines.append("=" * 15)
        lines.append(f"total senales: {len(observations)}")
        lines.append(f"senales operadas: {sum(1 for row in observations if int(row.get('operated') or 0) == 1)}")
        lines.append(f"senales no operadas: {sum(1 for row in observations if int(row.get('operated') or 0) == 0)}")
        lines.append(f"senales etiquetadas: {len(labeled)}")
        if not labeled:
            lines.append("Aun no hay etiquetas triple-barrier suficientes para diagnostico.")
            return "\n".join(lines)

        lines.extend(self._ranked_section("Win rate por estrategia", labeled, "strategy_type"))
        lines.extend(self._ranked_section("Mejor/peor simbolo", labeled, "symbol"))
        lines.extend(self._ranked_section("Mejor/peor regimen", labeled, "market_regime"))
        lines.extend(self._bucket_section("RSI bucket", labeled, lambda row: self._bucket(safe_float(row.get("rsi_14")), [30, 45, 60, 72])))
        lines.extend(self._bucket_section("Volume relative bucket", labeled, lambda row: self._bucket(safe_float(row.get("volume_relative")), [0.8, 1.2, 1.8, 2.5])))
        lines.extend(self._bucket_section("ATR bucket", labeled, lambda row: self._bucket(safe_float(row.get("normalized_atr")), [0.006, 0.012, 0.02, 0.03])))
        lines.extend(self._bucket_section("Spread bucket", labeled, lambda row: self._bucket(safe_float(row.get("spread_pct")), [0.0003, 0.0008, 0.0015, 0.003])))
        lines.extend(self._bucket_section("Distance EMA21 bucket", labeled, lambda row: self._bucket(safe_float(row.get("distance_to_ema_21")), [-0.02, -0.006, 0.006, 0.02])))
        lines.extend(self._recommendations(labeled))
        return "\n".join(lines)

    def _ranked_section(self, title: str, rows: list[dict[str, Any]], key: str) -> list[str]:
        stats = self._group_stats(rows, key)
        ordered = sorted(stats.items(), key=lambda item: item[1]["profit_factor"], reverse=True)
        lines = ["", title]
        for name, item in ordered[:8]:
            lines.append(
                f"- {name or 'NA'}: win_rate={item['win_rate']:.1%}, "
                f"profit_factor={item['profit_factor']:.2f}, trades={item['count']}"
            )
        return lines

    def _bucket_section(self, title: str, rows: list[dict[str, Any]], bucket_fn) -> list[str]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[bucket_fn(row)].append(row)
        stats_rows = []
        for bucket, items in grouped.items():
            stats = self._stats(items)
            stats_rows.append((bucket, stats))
        stats_rows.sort(key=lambda item: item[1]["profit_factor"], reverse=True)
        lines = ["", title]
        for bucket, stats in stats_rows:
            lines.append(
                f"- {bucket}: win_rate={stats['win_rate']:.1%}, "
                f"profit_factor={stats['profit_factor']:.2f}, trades={stats['count']}"
            )
        return lines

    def _recommendations(self, rows: list[dict[str, Any]]) -> list[str]:
        lines = ["", "Recomendaciones"]
        strategy_stats = self._group_stats(rows, "strategy_type")
        weak = [name for name, stat in strategy_stats.items() if stat["count"] >= 20 and stat["profit_factor"] < 1.0]
        strong = [name for name, stat in strategy_stats.items() if stat["count"] >= 20 and stat["profit_factor"] > 1.25]
        lines.append(f"- estrategias a filtrar/subir score: {', '.join(weak) if weak else 'sin evidencia suficiente'}")
        lines.append(f"- estrategias a potenciar: {', '.join(strong) if strong else 'sin evidencia suficiente'}")
        return lines

    def _group_stats(self, rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, float]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[str(row.get(key, "NA"))].append(row)
        return {name: self._stats(items) for name, items in grouped.items()}

    @staticmethod
    def _stats(rows: list[dict[str, Any]]) -> dict[str, float]:
        wins = sum(1 for row in rows if int(row.get("label", 0)) == 1)
        gains = sum(max(safe_float(row.get("realized_return_pct")), 0.0) for row in rows)
        losses = abs(sum(min(safe_float(row.get("realized_return_pct")), 0.0) for row in rows))
        return {
            "count": float(len(rows)),
            "win_rate": wins / max(len(rows), 1),
            "profit_factor": gains / losses if losses > 0 else gains if gains > 0 else 0.0,
        }

    @staticmethod
    def _bucket(value: float, edges: list[float]) -> str:
        previous = "-inf"
        for edge in edges:
            if value < edge:
                return f"{previous}..{edge:g}"
            previous = f"{edge:g}"
        return f"{previous}..inf"


def main() -> None:
    parser = argparse.ArgumentParser(description="Research tools for signal observations")
    parser.add_argument("command", choices=["report"])
    args = parser.parse_args()
    config = load_config()
    logger = setup_logger()
    db = Database(config, logger)
    db.initialize()
    engine = ResearchEngine(db, logger)
    if args.command == "report":
        print(engine.build_report())


if __name__ == "__main__":
    main()
