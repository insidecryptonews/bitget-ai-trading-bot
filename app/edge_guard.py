from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import BotConfig
from .database import Database
from .signal_engine import Signal
from .utils import safe_float, safe_int


ALLOW_PAPER = "ALLOW_PAPER"
GROSS_EDGE_ONLY = "GROSS_EDGE_ONLY"
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
        recent_since_iso = (now - timedelta(hours=max(1, int(self.config.edge_guard_recent_hours or 6)))).isoformat()
        overall = self.db.get_high_score_label_summary_since(since_iso, self.config.min_score_to_trade)
        strict_context = _strict_context(self.config, self.db, hours)
        groups: list[dict[str, Any]] = []
        for group_key, group_type in (
            ("symbol", "symbol"),
            ("market_regime", "regime"),
            ("side", "side"),
            ("score_bucket", "score_bucket"),
        ):
            recent_rows = self.db.get_shadow_opportunity_group_summaries_since(
                recent_since_iso,
                min_score=self.config.min_score_to_trade,
                group_key=group_key,
                limit=50,
            )
            recent_by_value = {str(row.get("group_value") or "").upper(): row for row in recent_rows}
            rows = self.db.get_shadow_opportunity_group_summaries_since(
                since_iso,
                min_score=self.config.min_score_to_trade,
                group_key=group_key,
                limit=50,
            )
            for row in rows:
                enriched = dict(row)
                enriched["group_type"] = group_type
                recent = recent_by_value.get(str(enriched.get("group_value") or "").upper(), {})
                enriched["recent_total_labels"] = safe_int(recent.get("total_labels"))
                enriched["recent_profit_factor"] = safe_float(recent.get("profit_factor"))
                enriched["recent_tp_ratio"] = safe_float(recent.get("tp_ratio"))
                enriched["recent_sl_ratio"] = safe_float(recent.get("sl_ratio"))
                enriched["stability_score"] = self._stability_score(enriched)
                enriched["sample_quality"] = min(safe_float(enriched.get("total_labels")) / max(1.0, float(self.config.edge_guard_min_sample)), 1.0)
                _apply_strict_context(enriched, group_type, strict_context, self.config)
                enriched["decision"], enriched["reason"] = self.classify_metrics(enriched)
                enriched["final_decision"] = _final_decision(enriched)
                groups.append(enriched)
        return {
            "hours": hours,
            "generated_at": now.isoformat(),
            "overall": _overall_metrics(overall),
            "allow_paper_candidates": _select(groups, ALLOW_PAPER),
            "gross_edge_only_candidates": _select(groups, GROSS_EDGE_ONLY),
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
            "gross_edge_only_candidates:",
            *_candidate_lines(report.get("gross_edge_only_candidates", [])),
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
        strict_reason = str(row.get("strict_block_reason") or "")
        if sample < self.config.edge_guard_min_sample:
            return WATCH_ONLY, "sample_too_small"
        if strict_reason:
            if pf >= 1.0:
                return GROSS_EDGE_ONLY, strict_reason
            return BLOCK_PAPER, strict_reason
        if self._recent_deteriorating(row):
            return WATCH_ONLY, "recent_deterioration"
        if time_ratio > safe_float(getattr(self.config, "net_edge_max_time_ratio", self.config.edge_guard_max_time_ratio)):
            return GROSS_EDGE_ONLY if pf >= 1.0 else BLOCK_PAPER, "high_time_death"
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

    def _recent_deteriorating(self, row: dict[str, Any]) -> bool:
        if not self.config.edge_guard_require_recent_stability:
            return False
        sample = safe_int(row.get("total_labels"))
        recent_sample = safe_int(row.get("recent_total_labels"))
        if sample < self.config.edge_guard_min_sample or recent_sample < max(25, self.config.edge_guard_min_sample // 10):
            return False
        pf = safe_float(row.get("profit_factor"))
        recent_pf = safe_float(row.get("recent_profit_factor"))
        if pf < self.config.edge_guard_min_pf or recent_pf <= 0:
            return False
        drop = (pf - recent_pf) / max(pf, 1.0)
        return drop > self.config.edge_guard_max_recent_pf_drop

    def _stability_score(self, row: dict[str, Any]) -> float:
        pf = safe_float(row.get("profit_factor"))
        recent_pf = safe_float(row.get("recent_profit_factor"))
        if pf <= 0 or recent_pf <= 0:
            return 0.0
        drop = abs(pf - recent_pf) / max(pf, 1.0)
        return max(0.0, min(1.0, 1.0 - drop))

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


def _strict_context(config: BotConfig, db: Database, hours: int) -> dict[str, Any]:
    try:
        from .candidate_ranking import CandidateRanking
        from .net_edge_lab import NetEdgeLab
        from .time_death_autopsy import TimeDeathAutopsyLab

        net = NetEdgeLab(config, db).build(hours=hours)
        autopsy = TimeDeathAutopsyLab(config, db).build(hours=hours)
        ranking = CandidateRanking(config, db).build(hours=hours)
    except Exception:
        return {"net": {}, "autopsy": {}, "ranking": {}}
    net_map = {
        (_guard_group_type(group), str(row.get("group_value") or "").upper()): row
        for group, rows in net.get("by_group", {}).items()
        for row in rows
    }
    autopsy_map = {
        (_guard_group_type(str(row.get("group_key") or "")), str(row.get("group_value") or "").upper()): row
        for row in autopsy.get("groups", [])
    }
    return {"net": net_map, "autopsy": autopsy_map, "ranking": ranking}


def _apply_strict_context(row: dict[str, Any], group_type: str, context: dict[str, Any], config: BotConfig) -> None:
    key = (group_type, str(row.get("group_value") or "").upper())
    net = context.get("net", {}).get(key, {})
    autopsy = context.get("autopsy", {}).get(key, {})
    ranking = context.get("ranking", {})
    if net:
        row["net_EV"] = safe_float(net.get("net_EV"))
        row["net_PF"] = safe_float(net.get("net_PF"))
    if autopsy:
        row["time_death_cause"] = autopsy.get("likely_cause")
        row["time_death_decision"] = autopsy.get("decision")
    reason = ""
    if net and safe_int(net.get("samples")) >= config.net_edge_min_samples:
        if safe_float(net.get("net_EV")) <= 0:
            reason = "net_ev_negative"
        elif safe_float(net.get("net_PF")) < config.net_edge_min_net_pf:
            reason = "net_pf_below_min"
    if autopsy and str(autopsy.get("decision")) == "REJECT":
        reason = str(autopsy.get("reason") or autopsy.get("likely_cause") or "high_time_death").lower()
    if autopsy and str(autopsy.get("decision")) == "WATCH_ONLY" and safe_int(autopsy.get("samples")) < config.edge_guard_min_sample:
        reason = "validation_sample_too_small"
    ranking_has_evidence = bool(
        ranking.get("top_candidates") or ranking.get("watch_list") or ranking.get("reject_list")
    )
    if ranking_has_evidence and ranking.get("status") == "NO_VALID_CANDIDATES":
        reason = "candidate_ranking_no_valid_candidates"
    group_value = str(row.get("group_value") or "").upper()
    if group_type == "score_bucket" and group_value in {"90-100", "90-94", "95-100"}:
        reason = reason or "generic_bucket_not_actionable"
    if group_type == "regime" and group_value == "RISK_OFF" and safe_float(row.get("time_ratio")) > 0.80:
        reason = "risk_off_high_time_death"
    if group_type == "side" and group_value == "LONG" and safe_float(row.get("tp_ratio")) <= 0.001 and safe_float(row.get("sl_ratio")) > 0.15:
        reason = "long_sl_high_tp_low"
    if group_type == "symbol" and group_value == "BNBUSDT" and (safe_float(row.get("profit_factor")) < 1.0 or safe_float(row.get("time_ratio")) >= 1.0):
        reason = "bnb_bad_symbol"
    if group_type == "score_bucket" and group_value.startswith("70-") and safe_float(row.get("time_ratio")) > 0.85:
        reason = "score_70_79_high_time_death"
    if reason:
        row["strict_block_reason"] = reason


def _guard_group_type(group: str) -> str:
    return {"market_regime": "regime", "strategy": "strategy", "policy_id": "policy_id"}.get(group, group)


def _final_decision(row: dict[str, Any]) -> str:
    decision = str(row.get("decision") or "")
    if decision == GROSS_EDGE_ONLY:
        return "BLOCKED_BY_NET_GATE"
    if decision == ALLOW_PAPER:
        return "ALLOW_PAPER"
    return decision


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
            f"recentPF={safe_float(row.get('recent_profit_factor')):.2f} "
            f"stability={safe_float(row.get('stability_score')):.2f} "
            f"decision={row.get('decision')} final_decision={row.get('final_decision')} reason={row.get('reason')}"
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
