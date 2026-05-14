from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .config import BotConfig
from .database import Database
from .edge_guard import EdgeGuard
from .exit_simulation_lab import ExitSimulationLab
from .utils import safe_float, safe_int


START = "SHADOW EXPERIMENTS START"
END = "SHADOW EXPERIMENTS END"


class ShadowExperimentsLab:
    def __init__(self, config: BotConfig, db: Database) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        hours = max(1, int(hours or 24))
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        experiments: list[dict[str, Any]] = []
        experiments.extend(self._symbol_allowlist(since))
        experiments.extend(self._group_filters(since, "market_regime", "block_bad_regimes"))
        experiments.extend(self._group_filters(since, "score_bucket", "score_bucket_filter"))
        experiments.extend(self._group_filters(since, "side", "side_filter"))
        experiments.extend(self._edge_guard_only(hours))
        experiments.extend(self._exit_variants(hours))
        experiments.sort(key=lambda row: (safe_float(row.get("profit_factor")), safe_float(row.get("tp_ratio"))), reverse=True)
        return {
            "hours": hours,
            "experiment_results": experiments,
            "best_experiment": experiments[0] if experiments else {},
            "worst_experiment": experiments[-1] if experiments else {},
            "recommendation": ["NO LIVE", "PAPER ONLY", "do_not_expand_slots"],
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        lines = [
            START,
            f"hours: {payload['hours']}",
            "experiment_results:",
            *_experiment_lines(payload["experiment_results"][:12]),
            "best_experiment:",
            *_experiment_lines([payload["best_experiment"]] if payload["best_experiment"] else []),
            "worst_experiment:",
            *_experiment_lines([payload["worst_experiment"]] if payload["worst_experiment"] else []),
            "recommendation:",
            *[f"- {item}" for item in payload["recommendation"]],
            END,
        ]
        return "\n".join(lines)

    def _symbol_allowlist(self, since: str) -> list[dict[str, Any]]:
        rows = self.db.get_shadow_opportunity_group_summaries_since(
            since,
            min_score=self.config.min_score_to_trade,
            group_key="symbol",
            limit=20,
        )
        return [
            _experiment("symbol_allowlist_candidates", str(row.get("group_value")), row)
            for row in rows
            if safe_float(row.get("profit_factor")) >= 1.2 and safe_int(row.get("total_labels")) >= 100
        ] or [_status_experiment("symbol_allowlist_candidates", "insufficient_edge")]

    def _group_filters(self, since: str, group_key: str, name: str) -> list[dict[str, Any]]:
        rows = self.db.get_shadow_opportunity_group_summaries_since(
            since,
            min_score=self.config.min_score_to_trade,
            group_key=group_key,
            limit=20,
        )
        return [_experiment(name, str(row.get("group_value")), row) for row in rows]

    def _edge_guard_only(self, hours: int) -> list[dict[str, Any]]:
        report = EdgeGuard(self.config, self.db).build_edge_guard_report(hours=hours)
        rows = []
        for decision_key in ("allow_paper_candidates", "watch_only_candidates", "shadow_only_candidates", "block_paper_candidates"):
            for row in report.get(decision_key, [])[:5]:
                rows.append(_experiment(f"edge_guard_{decision_key}", str(row.get("group_value")), row))
        return rows

    def _exit_variants(self, hours: int) -> list[dict[str, Any]]:
        report = ExitSimulationLab(self.config, self.db).build(hours=hours)
        return [
            _experiment("exit_variants", str(row.get("name")), row)
            for row in report.get("best_exit_candidates", [])[:5]
        ]


def _experiment(name: str, group: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "group": group,
        "samples": safe_int(row.get("total_labels") or row.get("samples")),
        "profit_factor": safe_float(row.get("profit_factor")),
        "tp_ratio": safe_float(row.get("tp_ratio")),
        "sl_ratio": safe_float(row.get("sl_ratio")),
        "time_ratio": safe_float(row.get("time_ratio")),
        "status": "candidate" if safe_float(row.get("profit_factor")) >= 1.2 and safe_int(row.get("total_labels") or row.get("samples")) >= 100 else "observe_only",
    }


def _status_experiment(name: str, status: str) -> dict[str, Any]:
    return {"name": name, "group": status, "samples": 0, "profit_factor": 0.0, "tp_ratio": 0.0, "sl_ratio": 0.0, "time_ratio": 0.0, "status": status}


def _experiment_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- name={row.get('name')} group={row.get('group')} samples={safe_int(row.get('samples'))} "
            f"PF={safe_float(row.get('profit_factor')):.2f} TP%={safe_float(row.get('tp_ratio')) * 100:.1f} "
            f"SL%={safe_float(row.get('sl_ratio')) * 100:.1f} TIME%={safe_float(row.get('time_ratio')) * 100:.1f} "
            f"status={row.get('status')}"
        )
        for row in rows
    ]
