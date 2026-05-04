from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT
from .database import Database
from .research_lab import ResearchMetrics, write_csv
from .utils import iso_utc, json_dumps, safe_float, safe_int
from .walkforward_validator import WalkForwardValidator


class RuleMiner:
    def __init__(self, db: Database | None = None, logger=None) -> None:
        self.db = db
        self.logger = logger
        self.validator = WalkForwardValidator()

    def mine_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates = []
        for key in ["symbol", "strategy_type", "market_regime", "side", "score_bucket"]:
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in rows:
                grouped[str(row.get(key) or "NA")].append(row)
            for value, items in grouped.items():
                candidates.append(self._rule_from_group(key, value, items))
        candidates.sort(key=lambda row: safe_float(row.get("evidence_score")), reverse=True)
        return candidates

    def generate(self, reports_dir: Path | None = None) -> list[dict[str, Any]]:
        rows = self.db.fetch_labeled_signal_rows() if self.db else []
        rules = self.mine_rows(rows)
        if self.db:
            for rule in rules:
                self.db.record_research_rule(rule)
        target = reports_dir or PROJECT_ROOT / "reports"
        target.mkdir(parents=True, exist_ok=True)
        (target / "recommended_rules.json").write_text(json_dumps(rules), encoding="utf-8")
        (target / "recommended_rules.md").write_text(self.markdown(rules), encoding="utf-8")
        return rules

    def markdown(self, rules: list[dict[str, Any]]) -> str:
        lines = ["# Recommended Research Rules", ""]
        if not rules:
            lines.append("Evidencia insuficiente.")
            return "\n".join(lines) + "\n"
        for rule in rules[:30]:
            lines.append(
                f"- **{rule['rule_name']}** [{rule['rule_type']}]: {rule['recommendation']} "
                f"(labels={rule['total_labels']}, PF={rule['profit_factor']:.2f}, expectancy={rule['expectancy']:.5f})"
            )
        return "\n".join(lines) + "\n"

    def report(self) -> str:
        rules = self.generate()
        return self.markdown(rules)

    def _rule_from_group(self, key: str, value: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        metrics = ResearchMetrics.calculate(rows)
        validation = self.validator.validate(rows)
        total = safe_int(metrics["total_labels"])
        pf = metrics["profit_factor"]
        expectancy = metrics["expectancy"]
        time_ratio = metrics["time_ratio"]
        sl_count = safe_int(metrics["sl_count"])
        tp_count = safe_int(metrics["tp1_count"] + metrics["tp2_count"])
        if total < 100:
            rule_type, action, recommendation = "OBSERVE_ONLY", "observe", "No recomendar fuerte: menos de 100 labels."
        elif pf < 1.2 or expectancy <= 0:
            rule_type, action, recommendation = "BLOCK", "block_or_raise_threshold", "Bloquear o subir score: PF bajo / expectancy negativa."
        elif time_ratio > 0.80:
            rule_type, action, recommendation = "REQUIRE_CONFIRMATION", "require_confirmation", "Demasiadas TIME; exigir confirmacion/momentum."
        elif validation["stable"]:
            rule_type, action, recommendation = "ALLOW_ONLY", "paper_only", "Prometedora solo para paper/research, no live."
        else:
            rule_type, action, recommendation = "OBSERVE_ONLY", "observe", "Posible edge, pero no estable en walk-forward."
        evidence = max(0.0, min(1.0, (total / 300) * 0.35 + min(pf / 2, 1) * 0.35 + (1 - validation["overfit_risk"]) * 0.3))
        return {
            "rule_name": f"{rule_type}_{key}_{value}",
            "rule_type": rule_type,
            "condition_json": json.dumps({key: value}, sort_keys=True),
            "action": action,
            "affected_symbols_json": json.dumps(sorted({row.get("symbol") for row in rows if row.get("symbol")})),
            "affected_strategies_json": json.dumps(sorted({row.get("strategy_type") for row in rows if row.get("strategy_type")})),
            "total_labels": total,
            "tp_count": tp_count,
            "sl_count": sl_count,
            "time_count": safe_int(metrics["time_count"]),
            "win_rate": metrics["win_rate"],
            "profit_factor": pf,
            "expectancy": expectancy,
            "time_ratio": time_ratio,
            "evidence_score": evidence,
            "overfit_risk": validation["overfit_risk"],
            "recommendation": recommendation,
            "explanation": f"Regla basada en {key}={value}. {validation['reason']}. No activa live.",
            "created_at": iso_utc(),
        }

