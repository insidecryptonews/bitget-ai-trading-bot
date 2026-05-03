from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import PROJECT_ROOT, load_config
from .database import Database
from .logger import setup_logger
from .utils import json_dumps, safe_float


MIN_VARIANT_LABELS = 100


class ResearchEngine:
    def __init__(self, db: Database, logger=None) -> None:
        self.db = db
        self.logger = logger

    def build_report(self) -> str:
        observations = self.db.fetch_signal_observations()
        labeled = self.db.fetch_labeled_signal_rows()
        paper_summary = self.db.get_paper_trade_summary()
        counts = self.db.get_table_counts()
        latest_trades = self.db.latest_trades(5)
        latest_operated = self.db.latest_operated_signal_observations(5)
        latest_labels = self.db.latest_signal_labels(5)
        lines: list[str] = []
        lines.append("Research report")
        lines.append("=" * 15)
        lines.append("Conteo real de tablas")
        for table, count in counts.items():
            lines.append(f"- {table}: {count}")
        lines.append(f"total senales: {len(observations)}")
        lines.append(f"senales operadas: {sum(1 for row in observations if int(row.get('operated') or 0) == 1)}")
        lines.append(f"senales no operadas: {sum(1 for row in observations if int(row.get('operated') or 0) == 0)}")
        lines.append(f"senales shadow: {sum(1 for row in observations if int(row.get('shadow_strategy') or 0) == 1)}")
        lines.append(f"senales etiquetadas: {len(labeled)}")
        lines.append(f"operaciones paper abiertas: {paper_summary['open']}")
        lines.append(f"operaciones paper cerradas: {paper_summary['closed']}")
        if counts.get("trades", 0) > 0 and not latest_trades:
            warning = "WARNING: trades count > 0 pero latest trades query salio vacia"
            lines.append(warning)
            if self.logger:
                self.logger.warning(warning)
        lines.extend(self._latest_section("Ultimas 5 trades", latest_trades, ["id", "timestamp", "symbol", "side", "status", "realized_pnl"]))
        lines.extend(
            self._latest_section(
                "Ultimas 5 signal_observations operadas",
                latest_operated,
                ["id", "timestamp", "symbol", "side", "strategy_type", "confidence_score", "shadow_strategy"],
            )
        )
        lines.extend(self._latest_section("Ultimas 5 labels", latest_labels, ["id", "timestamp", "observation_id", "label", "first_barrier_hit"]))
        if not labeled:
            lines.append("Aun no hay etiquetas triple-barrier suficientes.")
            return "\n".join(lines)

        overall = self._stats(labeled)
        lines.append(f"win rate labels: {overall['win_rate']:.1%}")
        lines.append(f"profit factor labels: {overall['profit_factor']:.2f}")
        lines.extend(self._ranked_section("Win rate por estrategia", labeled, "strategy_type"))
        lines.extend(self._ranked_section("Resumen por simbolo", labeled, "symbol"))
        lines.extend(self._ranked_section("Mejor/peor regimen", labeled, "market_regime"))
        lines.extend(self._bucket_section("RSI bucket", labeled, lambda row: self._bucket(safe_float(row.get("rsi_14")), [30, 45, 60, 72])))
        lines.extend(self._bucket_section("Volume relative bucket", labeled, lambda row: self._bucket(safe_float(row.get("volume_relative")), [0.8, 1.2, 1.8, 2.5])))
        lines.extend(self._bucket_section("ATR bucket", labeled, lambda row: self._bucket(safe_float(row.get("normalized_atr")), [0.006, 0.012, 0.02, 0.03])))
        lines.extend(self._bucket_section("Spread bucket", labeled, lambda row: self._bucket(safe_float(row.get("spread_pct")), [0.0003, 0.0008, 0.0015, 0.003])))
        lines.extend(self._bucket_section("Distance EMA21 bucket", labeled, lambda row: self._bucket(safe_float(row.get("distance_to_ema_21")), [-0.02, -0.006, 0.006, 0.02])))
        lines.extend(self._recommendations(labeled))
        return "\n".join(lines)

    def export(self, export_dir: Path | None = None) -> Path:
        export_dir = export_dir or PROJECT_ROOT / "exports"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = export_dir / f"research_export_{stamp}"
        target.mkdir(parents=True, exist_ok=True)

        observations = self.db.fetch_signal_observations()
        labels = self.db.fetch_signal_labels()
        trades = self.db.fetch_trades()
        labeled = self.db.fetch_labeled_signal_rows()
        summaries = {
            "by_symbol": self._group_stats(labeled, "symbol"),
            "by_strategy": self._group_stats(labeled, "strategy_type"),
            "by_regime": self._group_stats(labeled, "market_regime"),
            "by_rsi_bucket": self._bucket_stats(labeled, lambda row: self._bucket(safe_float(row.get("rsi_14")), [30, 45, 60, 72])),
            "by_volume_relative_bucket": self._bucket_stats(labeled, lambda row: self._bucket(safe_float(row.get("volume_relative")), [0.8, 1.2, 1.8, 2.5])),
            "by_atr_bucket": self._bucket_stats(labeled, lambda row: self._bucket(safe_float(row.get("normalized_atr")), [0.006, 0.012, 0.02, 0.03])),
            "by_spread_bucket": self._bucket_stats(labeled, lambda row: self._bucket(safe_float(row.get("spread_pct")), [0.0003, 0.0008, 0.0015, 0.003])),
        }
        self._write_csv(target / "signal_observations.csv", observations)
        self._write_csv(target / "signal_labels.csv", labels)
        self._write_csv(target / "trades.csv", trades)
        (target / "signal_observations.json").write_text(json_dumps(observations), encoding="utf-8")
        (target / "signal_labels.json").write_text(json_dumps(labels), encoding="utf-8")
        (target / "trades.json").write_text(json_dumps(trades), encoding="utf-8")
        (target / "summaries.json").write_text(json.dumps(summaries, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")
        for name, rows in summaries.items():
            self._write_csv(target / f"{name}.csv", [{"key": key, **value} for key, value in rows.items()])
        if self.logger:
            self.logger.info("Research export generado en %s", target)
        return target

    def build_variants_report(self) -> str:
        rows = self.db.fetch_strategy_variant_labeled_rows()
        self._refresh_variant_results(rows)
        results = self.db.fetch_strategy_variant_results()
        lines = ["Strategy variants report", "=" * 24]
        if not rows:
            lines.append("Aun no hay labels de variantes shadow.")
            return "\n".join(lines)
        lines.append(f"labels shadow totales: {len(rows)}")
        lines.append("")
        lines.append("Mejores variantes generales")
        for row in results[:15]:
            total = int(row.get("total_labels") or 0)
            evidence = self._evidence_label(total, safe_float(row.get("time_count")) / max(total, 1))
            variant_rows = [item for item in rows if int(item.get("strategy_variant_id") or 0) == int(row.get("variant_id") or row.get("id") or 0)]
            lines.append(
                f"- {row.get('name')}: labels={total}, win_rate={safe_float(row.get('win_rate')):.1%}, "
                f"profit_factor={safe_float(row.get('profit_factor')):.2f}, score={safe_float(row.get('score')):.3f}, "
                f"{evidence}, {self._walkforward_note(variant_rows)}"
            )
        lines.extend(self._variant_group_section("Por regimen", rows, "market_regime"))
        lines.extend(self._variant_group_section("Por simbolo", rows, "symbol"))
        lines.extend(self._variant_group_section("Por score bucket", rows, "score_bucket"))
        lines.extend(self._variant_group_section("Por estrategia original", rows, "original_strategy_type"))
        lines.extend(self._reverse_comparison(rows))
        return "\n".join(lines)

    def _refresh_variant_results(self, rows: list[dict[str, Any]]) -> None:
        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            variant_id = int(row.get("strategy_variant_id") or 0)
            if variant_id:
                grouped[variant_id].append(row)
        for variant_id, items in grouped.items():
            stats = self._stats(items)
            result = {
                "variant_id": variant_id,
                "total_labels": len(items),
                "tp1_count": sum(1 for row in items if row.get("first_barrier_hit") == "TP1"),
                "tp2_count": sum(1 for row in items if row.get("first_barrier_hit") == "TP2"),
                "sl_count": sum(1 for row in items if row.get("first_barrier_hit") == "SL"),
                "time_count": sum(1 for row in items if row.get("first_barrier_hit") == "TIME"),
                "win_rate": stats["win_rate"],
                "profit_factor": stats["profit_factor"],
                "avg_return": stats["avg_return"],
                "max_drawdown_estimated": stats["max_drawdown_estimated"],
                "score": stats["profit_factor"] * stats["win_rate"] - stats["max_drawdown_estimated"],
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
            self.db.upsert_strategy_variant_result(result)

    def _variant_group_section(self, title: str, rows: list[dict[str, Any]], key: str) -> list[str]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[(str(row.get("variant_name") or row.get("strategy_variant_id")), str(row.get(key) or "NA"))].append(row)
        ranked = sorted(grouped.items(), key=lambda item: self._stats(item[1])["profit_factor"], reverse=True)
        lines = ["", title]
        for (variant, bucket), items in ranked[:12]:
            stats = self._stats(items)
            evidence = self._evidence_label(len(items), self._time_ratio(items))
            lines.append(
                f"- {variant} / {bucket}: labels={len(items)}, win_rate={stats['win_rate']:.1%}, "
                f"profit_factor={stats['profit_factor']:.2f}, {evidence}"
            )
        return lines

    def _reverse_comparison(self, rows: list[dict[str, Any]]) -> list[str]:
        reverse_rows = [row for row in rows if self._params(row).get("reverse") is True]
        normal_labeled = self.db.fetch_labeled_signal_rows()
        normal_rows = [row for row in normal_labeled if int(row.get("shadow_strategy") or 0) == 0]
        lines = ["", "Reverse vs normal"]
        if not reverse_rows:
            lines.append("- sin reverse labels aun")
            return lines
        keys = sorted({
            (str(row.get("symbol")), str(row.get("market_regime")), str(row.get("score_bucket") or self._score_bucket(row)), str(row.get("original_strategy_type") or row.get("strategy_type")))
            for row in reverse_rows
        })
        for symbol, regime, score_bucket, strategy in keys[:20]:
            rev = [
                row for row in reverse_rows
                if str(row.get("symbol")) == symbol
                and str(row.get("market_regime")) == regime
                and str(row.get("score_bucket") or self._score_bucket(row)) == score_bucket
                and str(row.get("original_strategy_type") or row.get("strategy_type")) == strategy
            ]
            normal = [
                row for row in normal_rows
                if str(row.get("symbol")) == symbol
                and str(row.get("market_regime")) == regime
                and self._score_bucket(row) == score_bucket
                and str(row.get("strategy_type")) == strategy
            ]
            rev_stats = self._stats(rev)
            normal_stats = self._stats(normal)
            note = "insuficiente evidencia"
            if len(rev) >= MIN_VARIANT_LABELS:
                note = "reverse mejor PF" if rev_stats["profit_factor"] > normal_stats["profit_factor"] else "normal >= reverse"
                if self._time_ratio(rev) > 0.7:
                    note += ", evidencia debil por muchas TIME"
            lines.append(
                f"- {symbol}/{regime}/{score_bucket}/{strategy}: reverse labels={len(rev)} PF={rev_stats['profit_factor']:.2f}, "
                f"normal labels={len(normal)} PF={normal_stats['profit_factor']:.2f}, {note}"
            )
        return lines

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

    def _bucket_section(self, title: str, rows: list[dict[str, Any]], bucket_fn: Callable[[dict[str, Any]], str]) -> list[str]:
        stats_rows = sorted(self._bucket_stats(rows, bucket_fn).items(), key=lambda item: item[1]["profit_factor"], reverse=True)
        lines = ["", title]
        for bucket, stats in stats_rows:
            lines.append(
                f"- {bucket}: win_rate={stats['win_rate']:.1%}, "
                f"profit_factor={stats['profit_factor']:.2f}, trades={stats['count']}"
            )
        return lines

    def _bucket_stats(self, rows: list[dict[str, Any]], bucket_fn: Callable[[dict[str, Any]], str]) -> dict[str, dict[str, float]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[bucket_fn(row)].append(row)
        return {bucket: self._stats(items) for bucket, items in grouped.items()}

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
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for row in rows:
            equity += safe_float(row.get("realized_return_pct"))
            peak = max(peak, equity)
            max_dd = min(max_dd, equity - peak)
        return {
            "count": float(len(rows)),
            "win_rate": wins / max(len(rows), 1),
            "profit_factor": gains / losses if losses > 0 else gains if gains > 0 else 0.0,
            "avg_return": sum(safe_float(row.get("realized_return_pct")) for row in rows) / max(len(rows), 1),
            "max_drawdown_estimated": abs(max_dd),
        }

    @staticmethod
    def _latest_section(title: str, rows: list[dict[str, Any]], keys: list[str]) -> list[str]:
        lines = ["", title]
        if not rows:
            lines.append("- none")
            return lines
        for row in rows:
            lines.append("- " + ", ".join(f"{key}={row.get(key)}" for key in keys))
        return lines

    @staticmethod
    def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
        if not rows:
            path.write_text("", encoding="utf-8")
            return
        keys = sorted({key for row in rows for key in row})
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=keys)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key) for key in keys})

    @staticmethod
    def _params(row: dict[str, Any]) -> dict[str, Any]:
        raw = row.get("strategy_variant_params_json") or row.get("variant_params_json") or "{}"
        try:
            return json.loads(raw)
        except Exception:
            return {}

    @staticmethod
    def _score_bucket(row: dict[str, Any]) -> str:
        score = int(safe_float(row.get("confidence_score")))
        if score >= 90:
            return "90+"
        if score >= 85:
            return "85-89"
        if score >= 80:
            return "80-84"
        if score >= 75:
            return "75-79"
        if score >= 70:
            return "70-74"
        if score >= 65:
            return "65-69"
        if score >= 60:
            return "60-64"
        return "<60"

    @staticmethod
    def _time_ratio(rows: list[dict[str, Any]]) -> float:
        return sum(1 for row in rows if row.get("first_barrier_hit") == "TIME") / max(len(rows), 1)

    def _walkforward_note(self, rows: list[dict[str, Any]]) -> str:
        if len(rows) < MIN_VARIANT_LABELS:
            return "walk_forward=insuficiente"
        ordered = sorted(rows, key=lambda row: str(row.get("timestamp", "")))
        block_size = max(1, len(ordered) // 3)
        blocks = [ordered[index:index + block_size] for index in range(0, len(ordered), block_size)][:3]
        pfs = [self._stats(block)["profit_factor"] for block in blocks if block]
        if len(pfs) < 2:
            return "walk_forward=insuficiente"
        stable = min(pfs) > 1.0 and max(pfs) / max(min(pfs), 1e-9) < 3.0
        status = "estable" if stable else "inestable"
        return "walk_forward=" + status + "(" + ",".join(f"{pf:.2f}" for pf in pfs) + ")"

    @staticmethod
    def _evidence_label(total: int, time_ratio: float) -> str:
        if total < MIN_VARIANT_LABELS:
            return "insuficiente evidencia"
        if time_ratio > 0.7:
            return "evidencia debil: muchas TIME"
        return "evidencia suficiente para seguimiento, no live"

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
    parser.add_argument("command", choices=["report", "export", "variants"])
    args = parser.parse_args()
    config = load_config()
    logger = setup_logger()
    db = Database(config, logger)
    db.initialize()
    engine = ResearchEngine(db, logger)
    if args.command == "report":
        print(engine.build_report())
    elif args.command == "export":
        path = engine.export()
        print(f"Research export generado en {path}")
    elif args.command == "variants":
        print(engine.build_variants_report())


if __name__ == "__main__":
    main()
