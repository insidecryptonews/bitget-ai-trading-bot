from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import BotConfig
from .database import Database
from .signal_engine import Signal
from .utils import safe_float, safe_int


ALLOW_PAPER = "ALLOW_PAPER"
WATCH_ONLY = "WATCH_ONLY"
SHADOW_ONLY = "SHADOW_ONLY"
BLOCK_PAPER = "BLOCK_PAPER"
START = "EDGE GUARD START"
END = "EDGE GUARD END"


@dataclass(frozen=True)
class EdgeDecision:
    decision: str
    reason: str
    matched_group: str = ""
    group_type: str = ""

    @property
    def allows_paper(self) -> bool:
        return self.decision == ALLOW_PAPER


class EdgeGuard:
    """Research-only group quality guard. Optional paper filter is disabled by default."""

    def __init__(self, config: BotConfig, db: Database) -> None:
        self.config = config
        self.db = db

    def build_edge_guard_report(self, *, hours: int = 24) -> dict[str, Any]:
        hours = max(1, int(hours or 24))
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=hours)
        since_iso = since.isoformat()
        overall = self.db.get_high_score_label_summary_since(since_iso, self.config.min_score_to_trade)
        groups: list[dict[str, Any]] = []
        for group_key, group_type in (
            ("symbol", "symbol"),
            ("market_regime", "regime"),
            ("side", "side"),
            ("score_bucket", "score_bucket"),
        ):
            rows = self.db.get_shadow_opportunity_group_summaries_since(
                since_iso,
                min_score=self.config.min_score_to_trade,
                group_key=group_key,
                limit=50,
            )
            for row in rows:
                enriched = dict(row)
                enriched["group_type"] = group_type
                enriched["decision"], enriched["reason"] = self.classify_metrics(enriched)
                groups.append(enriched)
        return {
            "hours": hours,
            "generated_at": now.isoformat(),
            "overall": _overall_metrics(overall),
            "allow_paper_candidates": _select(groups, ALLOW_PAPER),
            "watch_only_candidates": _select(groups, WATCH_ONLY),
            "shadow_only_candidates": _select(groups, SHADOW_ONLY),
            "block_paper_candidates": _select(groups, BLOCK_PAPER),
            "candidate_table": groups,
            "reasons": _reason_counts(groups),
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        report = self.build_edge_guard_report(hours=hours)
        lines = [
            START,
            f"hours: {report['hours']}",
            "overall:",
            _overall_line(report["overall"]),
            "allow_paper_candidates:",
            *_candidate_lines(report["allow_paper_candidates"]),
            "watch_only_candidates:",
            *_candidate_lines(report["watch_only_candidates"]),
            "shadow_only_candidates:",
            *_candidate_lines(report["shadow_only_candidates"]),
            "block_paper_candidates:",
            *_candidate_lines(report["block_paper_candidates"]),
            "reasons:",
            *[f"- {item['reason']}: {item['count']}" for item in report["reasons"][:8]],
            "final_recommendation: NO LIVE",
            END,
        ]
        return "\n".join(lines)

    def classify_metrics(self, row: dict[str, Any]) -> tuple[str, str]:
        sample = safe_int(row.get("total_labels"))
        pf = safe_float(row.get("profit_factor"))
        tp_ratio = safe_float(row.get("tp_ratio"))
        sl_ratio = safe_float(row.get("sl_ratio"))
        time_ratio = safe_float(row.get("time_ratio"))
        if sample < self.config.edge_guard_min_sample:
            return WATCH_ONLY, "sample_too_small"
        if sl_ratio > 0.15 and tp_ratio < 0.03:
            return BLOCK_PAPER, "sl_high_tp_low"
        if pf < 1.0:
            return BLOCK_PAPER, "pf_below_1"
        if tp_ratio < 0.01:
            return SHADOW_ONLY, "tp_ratio_below_1pct"
        if time_ratio > self.config.edge_guard_max_time_ratio:
            return SHADOW_ONLY, "time_ratio_too_high"
        if (
            pf >= self.config.edge_guard_min_pf
            and tp_ratio >= self.config.edge_guard_min_tp_ratio
            and sl_ratio <= self.config.edge_guard_max_sl_ratio
            and sample >= self.config.edge_guard_min_sample
        ):
            return ALLOW_PAPER, "edge_thresholds_met"
        if pf >= 1.1:
            return WATCH_ONLY, "pf_promising_but_quality_mixed"
        return SHADOW_ONLY, "edge_not_confirmed"

    def evaluate_signal(self, signal: Signal, market_regime: str, *, hours: int = 24) -> EdgeDecision:
        if not self.config.enable_edge_guard_paper_filter:
            return EdgeDecision(ALLOW_PAPER, "edge_guard_filter_disabled")
        report = self.build_edge_guard_report(hours=hours)
        score_bucket = _score_bucket(safe_int(getattr(signal, "confidence_score", 0)))
        candidates = [
            ("symbol", str(getattr(signal, "symbol", "")).upper()),
            ("regime", str(market_regime or "").upper()),
            ("score_bucket", score_bucket),
            ("side", str(getattr(signal, "side", "")).upper()),
        ]
        table = report.get("candidate_table", [])
        blocking: list[EdgeDecision] = []
        allowing = False
        for group_type, group_value in candidates:
            row = _find_group(table, group_type, group_value)
            if not row:
                continue
            decision = str(row.get("decision") or WATCH_ONLY)
            reason = str(row.get("reason") or "unknown")
            if decision in {BLOCK_PAPER, SHADOW_ONLY, WATCH_ONLY}:
                blocking.append(EdgeDecision(decision, reason, group_value, group_type))
            elif decision == ALLOW_PAPER:
                allowing = True
        if blocking:
            return blocking[0]
        if allowing:
            return EdgeDecision(ALLOW_PAPER, "edge_group_allowed")
        return EdgeDecision(WATCH_ONLY, "no_edge_group_evidence")


def build_edge_guard_report(config: BotConfig, db: Database, *, hours: int = 24) -> dict[str, Any]:
    return EdgeGuard(config, db).build_edge_guard_report(hours=hours)


def _select(groups: list[dict[str, Any]], decision: str) -> list[dict[str, Any]]:
    selected = [row for row in groups if row.get("decision") == decision]
    selected.sort(key=lambda row: (safe_float(row.get("profit_factor")), safe_float(row.get("tp_ratio"))), reverse=True)
    return selected[:12]


def _reason_counts(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for row in groups:
        reason = str(row.get("reason") or "unknown")
        counts[reason] = counts.get(reason, 0) + 1
    return [{"reason": reason, "count": count} for reason, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)]


def _overall_metrics(labels: dict[str, Any]) -> dict[str, float]:
    total = safe_float(labels.get("total_labels"))
    tp = safe_float(labels.get("tp1_count")) + safe_float(labels.get("tp2_count"))
    return {
        "labels": total,
        "profit_factor": safe_float(labels.get("profit_factor")),
        "time_ratio": safe_float(labels.get("time_count")) / max(total, 1.0) if total else 0.0,
        "sl_ratio": safe_float(labels.get("sl_count")) / max(total, 1.0) if total else 0.0,
        "tp_ratio": tp / max(total, 1.0) if total else 0.0,
    }


def _overall_line(metrics: dict[str, Any]) -> str:
    return (
        f"- labels={safe_int(metrics.get('labels'))} "
        f"PF={safe_float(metrics.get('profit_factor')):.2f} "
        f"TIME%={safe_float(metrics.get('time_ratio')) * 100:.1f} "
        f"SL%={safe_float(metrics.get('sl_ratio')) * 100:.1f} "
        f"TP%={safe_float(metrics.get('tp_ratio')) * 100:.1f}"
    )


def _candidate_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- {row.get('group_type')}:{row.get('group_value')} labels={safe_int(row.get('total_labels'))} "
            f"PF={safe_float(row.get('profit_factor')):.2f} "
            f"TIME%={safe_float(row.get('time_ratio')) * 100:.1f} "
            f"SL%={safe_float(row.get('sl_ratio')) * 100:.1f} "
            f"TP%={safe_float(row.get('tp_ratio')) * 100:.1f} "
            f"reason={row.get('reason')}"
        )
        for row in rows[:10]
    ]


def _score_bucket(score: int) -> str:
    if score >= 90:
        return "90-100"
    if score >= 80:
        return "80-89"
    if score >= 70:
        return "70-79"
    return "<70"


def _find_group(rows: list[dict[str, Any]], group_type: str, group_value: str) -> dict[str, Any] | None:
    for row in rows:
        if str(row.get("group_type") or "") == group_type and str(row.get("group_value") or "").upper() == group_value.upper():
            return row
    return None
