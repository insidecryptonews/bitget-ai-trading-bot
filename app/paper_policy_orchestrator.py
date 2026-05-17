from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .adaptive_exit_policy_lab import AdaptiveExitPolicyLab
from .anti_overfit_gate import AntiOverfitGate
from .candidate_ranking import CandidateRanking
from .catalyst_registry import CatalystRegistry
from .config import BotConfig
from .decision_ledger_audit import DecisionLedgerAudit
from .edge_guard import ALLOW_PAPER, BLOCK_PAPER, SHADOW_ONLY, WATCH_ONLY, EdgeGuard
from .ev_slippage_calibration_gate import EvSlippageCalibrationGate
from .exit_simulation_lab import ExitSimulationLab
from .latency_audit import LatencyAudit
from .net_edge_lab import NetEdgeLab
from .news_risk_gate import NewsRiskGate
from .paper_policy_lab import PaperPolicyLab
from .policy_stability_matrix import PolicyStabilityMatrix
from .policy_backtest import PolicyBacktest
from .score_calibration_lab import ScoreCalibrationLab
from .time_death_lab import TimeDeathLab
from .utils import safe_float, safe_int
from .walk_forward_validation import WalkForwardValidation


START = "PAPER POLICY ORCHESTRATOR START"
END = "PAPER POLICY ORCHESTRATOR END"
ALLOW_PAPER_CANDIDATE = "ALLOW_PAPER_CANDIDATE"
ORCH_WATCH_ONLY = "WATCH_ONLY"
ORCH_SHADOW_ONLY = "SHADOW_ONLY"
ORCH_BLOCK_PAPER = "BLOCK_PAPER"


@dataclass(frozen=True)
class PaperPolicyDecision:
    decision: str
    reason: str
    policy_id: str = ""

    @property
    def blocks_paper(self) -> bool:
        return self.decision == ORCH_BLOCK_PAPER


class PaperPolicyOrchestrator:
    """Research-only policy merger. It proposes paper gates but never enables them."""

    def __init__(self, config: BotConfig, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        hours = max(1, int(hours or 24))
        edge = _safe_build(lambda: EdgeGuard(self.config, self.db).build_edge_guard_report(hours=hours), {})
        policy_lab = _safe_build(lambda: PaperPolicyLab(self.config, self.db).build(hours=hours), {})
        walk = _safe_build(lambda: WalkForwardValidation(self.config, self.db).build(hours=hours), {})
        backtest = _safe_build(lambda: PolicyBacktest(self.config, self.db).build(hours=hours), {})
        score = _safe_build(lambda: ScoreCalibrationLab(self.config, self.db).build(hours=hours), {})
        exit_sim = _safe_build(lambda: ExitSimulationLab(self.config, self.db).build(hours=hours), {})
        time_death = _safe_build(lambda: TimeDeathLab(self.config, self.db).build(hours=hours), {})
        exit_policy = _safe_build(lambda: AdaptiveExitPolicyLab(self.config, self.db).build(hours=hours), {})
        news = _safe_build(lambda: NewsRiskGate(self.config, self.db).build(hours=hours), {})
        catalyst = _safe_build(lambda: CatalystRegistry(self.config, self.db).build_summary(hours=hours), {})
        latency = _safe_build(lambda: LatencyAudit(self.config, self.db).build(hours=hours), {})
        net_edge = _safe_build(lambda: NetEdgeLab(self.config, self.db).build(hours=hours), {})
        anti_overfit = _safe_build(lambda: AntiOverfitGate(self.config, self.db).build(hours=hours), {})
        ev_gate = _safe_build(lambda: EvSlippageCalibrationGate(self.config, self.db).build(hours=hours), {})
        stability = _safe_build(lambda: PolicyStabilityMatrix(self.config, self.db).build(hours=hours), {})
        ranking = _safe_build(lambda: CandidateRanking(self.config, self.db).build(hours=hours), {})
        ledger = _safe_build(lambda: DecisionLedgerAudit(self.config, self.db).build(hours=hours), {})
        policy_candidates = self._merge(
            edge,
            policy_lab,
            walk,
            backtest,
            score,
            exit_sim,
            time_death,
            exit_policy,
            news,
            catalyst,
            net_edge,
            anti_overfit,
            ev_gate,
            stability,
            ranking,
        )
        blocked = self._blocked(edge, news, time_death)
        no_actionable_candidates = ranking.get("status") == "NO_VALID_CANDIDATES"
        next_action = [
            "keep paper/research",
            "do not activate live",
            "review policy candidates before enabling any paper filter",
            "keep ENABLE_PAPER_POLICY_FILTER=false until validation is stable",
        ]
        if no_actionable_candidates:
            next_action = [
                "keep_research",
                "do not activate live",
                "do not enable paper filter",
                "wait for concrete validated symbol+side+regime candidates",
            ]
        return {
            "hours": hours,
            "global_status": "PAPER_ONLY",
            "live_allowed": False,
            "policy_filter": {
                "enabled": bool(self.config.enable_paper_policy_filter),
                "mode": self.config.paper_policy_filter_mode,
                "live_unaffected": True,
            },
            "policy_candidates": policy_candidates,
            "blocked": blocked,
            "no_actionable_candidates": no_actionable_candidates,
            "module_status": {
                "edge_guard": bool(edge),
                "paper_policy_lab": bool(policy_lab),
                "walk_forward": bool(walk),
                "policy_backtest": bool(backtest),
                "score_calibration": bool(score),
                "exit_simulation": bool(exit_sim),
                "time_death": bool(time_death),
                "adaptive_exit_policy": bool(exit_policy),
                "news_risk_gate": bool(news),
                "catalyst_summary": bool(catalyst),
                "latency_audit": bool(latency),
                "net_edge_lab": bool(net_edge),
                "anti_overfit_gate": bool(anti_overfit),
                "ev_slippage_calibration_gate": bool(ev_gate),
                "policy_stability_matrix": bool(stability),
                "candidate_ranking": bool(ranking),
                "decision_ledger_audit": bool(ledger),
            },
            "recommended_next_action": next_action,
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        return "\n".join([
            START,
            f"hours: {payload['hours']}",
            f"global_status: {payload['global_status']}",
            f"live_allowed: {str(payload['live_allowed']).lower()}",
            f"no_actionable_candidates: {str(payload.get('no_actionable_candidates')).lower()}",
            "paper_filter:",
            f"- enabled={str(payload['policy_filter']['enabled']).lower()} mode={payload['policy_filter']['mode']} live_unaffected=true",
            "policy_candidates:",
            *_policy_lines(payload["policy_candidates"]),
            "blocked:",
            *_blocked_lines(payload["blocked"]),
            "recommended_next_action:",
            *[f"- {item}" for item in payload["recommended_next_action"]],
            "final_recommendation: NO LIVE",
            END,
        ])

    def evaluate_signal(self, symbol: str, side: str, market_regime: str, score_bucket: str, *, hours: int = 24) -> PaperPolicyDecision:
        if not self.config.enable_paper_policy_filter:
            return PaperPolicyDecision(ALLOW_PAPER_CANDIDATE, "paper_policy_filter_disabled")
        payload = self.build(hours=hours)
        matched = _match_policy(payload.get("policy_candidates", []), symbol, side, market_regime, score_bucket)
        if matched:
            decision = str(matched.get("decision") or ORCH_WATCH_ONLY)
            if self.config.paper_policy_filter_mode == "shadow":
                return PaperPolicyDecision(decision, "shadow_mode_no_block", str(matched.get("id") or ""))
            return PaperPolicyDecision(decision, str(matched.get("reason") or "matched_policy"), str(matched.get("id") or ""))
        blocked = _match_block(payload.get("blocked", []), symbol, side, market_regime)
        if blocked:
            if self.config.paper_policy_filter_mode == "shadow":
                return PaperPolicyDecision(ORCH_BLOCK_PAPER, "shadow_mode_no_block", str(blocked.get("group") or ""))
            return PaperPolicyDecision(ORCH_BLOCK_PAPER, str(blocked.get("reason") or "blocked_policy"), str(blocked.get("group") or ""))
        return PaperPolicyDecision(ORCH_WATCH_ONLY, "no_orchestrator_evidence")

    def _merge(
        self,
        edge: dict[str, Any],
        policy_lab: dict[str, Any],
        walk: dict[str, Any],
        backtest: dict[str, Any],
        score: dict[str, Any],
        exit_sim: dict[str, Any],
        time_death: dict[str, Any],
        exit_policy: dict[str, Any],
        news: dict[str, Any],
        catalyst: dict[str, Any],
        net_edge: dict[str, Any],
        anti_overfit: dict[str, Any],
        ev_gate: dict[str, Any],
        stability: dict[str, Any],
        ranking: dict[str, Any],
    ) -> list[dict[str, Any]]:
        walk_by_policy = {str(row.get("policy_id") or ""): row for row in walk.get("policies", [])}
        net_by_policy = {str(row.get("group_value") or ""): row for row in net_edge.get("by_group", {}).get("policy_id", [])}
        net_by_group = {
            (group, str(row.get("group_value") or "").upper()): row
            for group, rows in net_edge.get("by_group", {}).items()
            for row in rows
        }
        anti_by_policy = {str(row.get("group_value") or ""): row for row in anti_overfit.get("candidates", [])}
        ev_by_policy = {str(row.get("group_value") or ""): row for row in ev_gate.get("candidates", [])}
        stability_by_policy = {str(row.get("policy_id") or ""): row for row in stability.get("matrix", [])}
        ranking_by_policy = {}
        for key in ("top_candidates", "watch_list", "reject_list"):
            for row in ranking.get(key, []):
                ranking_by_policy[str(row.get("group_value") or "")] = row
        news_blocked = _news_blocked_symbols(news)
        time_risk = _time_risk_groups(time_death)
        candidates: list[dict[str, Any]] = []
        rows = edge.get("candidate_table", [])
        for row in rows:
            group_type = str(row.get("group_type") or "")
            group_value = str(row.get("group_value") or "")
            decision = _decision_from_edge(row, news_blocked, time_risk, self.config)
            policy_id = f"policy_{group_type}_{group_value}".replace(" ", "_")
            net_row = net_by_policy.get(policy_id, {}) or net_by_group.get((group_type if group_type != "regime" else "market_regime", group_value.upper()), {})
            anti_row = anti_by_policy.get(policy_id, {})
            ev_row = ev_by_policy.get(policy_id, {})
            stability_row = stability_by_policy.get(policy_id, {})
            rank_row = ranking_by_policy.get(policy_id, {})
            walk_row = walk_by_policy.get(policy_id, {})
            if decision == ALLOW_PAPER_CANDIDATE and walk_row:
                if str(walk_row.get("decision")) != "PAPER_CANDIDATE":
                    decision = ORCH_WATCH_ONLY
            if decision == ALLOW_PAPER_CANDIDATE and group_type == "score_bucket":
                decision = ORCH_WATCH_ONLY
            if decision == ALLOW_PAPER_CANDIDATE and ranking.get("status") == "NO_VALID_CANDIDATES":
                decision = ORCH_WATCH_ONLY
            decision = _stricten_decision(decision, net_row, anti_row, ev_row, stability_row, rank_row)
            if decision == ALLOW_PAPER_CANDIDATE and safe_float(row.get("stability_score")) < 0.50:
                decision = ORCH_WATCH_ONLY
            if decision == ALLOW_PAPER_CANDIDATE and safe_int(row.get("total_labels")) < self.config.paper_policy_min_samples:
                decision = ORCH_WATCH_ONLY
            reason = _reason(row, decision, walk_row, catalyst, time_risk)
            candidates.append({
                "id": policy_id,
                "decision": decision,
                "group_type": group_type,
                "group_value": group_value,
                "symbol_allowlist": group_value if group_type == "symbol" else "",
                "side_allowlist": group_value if group_type == "side" else "",
                "regime_allowlist": group_value if group_type == "regime" else "",
                "score_bucket_allowlist": group_value if group_type == "score_bucket" else "",
                "min_pf": safe_float(row.get("profit_factor")),
                "train_pf": safe_float(anti_row.get("train_pf")),
                "validation_pf": safe_float(walk_row.get("validation_pf")),
                "recent_pf": safe_float(anti_row.get("recent_pf")),
                "net_pf": safe_float(net_row.get("net_PF") or ev_row.get("net_PF")),
                "net_EV": safe_float(ev_row.get("net_EV") or net_row.get("net_EV")),
                "walk_forward_stability": safe_float(walk_row.get("stability")),
                "stability": stability_row.get("trend_status", "unknown"),
                "exit_policy": _best_exit_policy(exit_policy),
                "news_gate": _news_gate_for_group(news, group_type, group_value),
                "time_death_risk": time_risk.get((group_type, group_value.upper()), "unknown"),
                "drawdown_proxy": safe_float(row.get("max_drawdown_proxy")),
                "sample_size": safe_int(row.get("total_labels")),
                "tp_ratio": safe_float(row.get("tp_ratio")),
                "sl_ratio": safe_float(row.get("sl_ratio")),
                "time_ratio": safe_float(row.get("time_ratio")),
                "reason": reason,
            })
        candidates.sort(key=lambda item: (_decision_rank(item["decision"]), item["min_pf"], item["tp_ratio"]), reverse=True)
        return candidates[:20]

    @staticmethod
    def _blocked(edge: dict[str, Any], news: dict[str, Any], time_death: dict[str, Any]) -> list[dict[str, Any]]:
        blocked: list[dict[str, Any]] = []
        for row in edge.get("block_paper_candidates", [])[:16]:
            blocked.append({"group": row.get("group_value"), "type": row.get("group_type"), "reason": row.get("reason") or "edge_negative"})
        for row in news.get("blocked", [])[:16]:
            blocked.append({"group": row.get("symbol"), "type": "news", "reason": row.get("reason") or "news_risk"})
        for row in time_death.get("worst_time_groups", [])[:8]:
            if safe_float(row.get("time_ratio")) >= 0.80:
                blocked.append({"group": row.get("group_value"), "type": row.get("group_key"), "reason": "time_death_or_low_pf"})
        return _dedupe_blocked(blocked)


def _safe_build(fn, fallback):
    try:
        return fn()
    except Exception:
        return fallback


def _decision_from_edge(row: dict[str, Any], news_blocked: set[str], time_risk: dict[tuple[str, str], str], config: BotConfig) -> str:
    group_type = str(row.get("group_type") or "")
    group_value = str(row.get("group_value") or "").upper()
    if group_type == "symbol" and group_value in news_blocked:
        return ORCH_BLOCK_PAPER
    if group_type == "side" and group_value == "SHORT" and config.paper_policy_block_short_if_negative and safe_float(row.get("profit_factor")) < 1.0:
        return ORCH_BLOCK_PAPER
    if group_type == "regime" and group_value == "RISK_OFF" and config.paper_policy_block_risk_off and safe_float(row.get("profit_factor")) < 1.0:
        return ORCH_BLOCK_PAPER
    if group_type == "regime" and group_value == "RANGE" and config.paper_policy_block_range and safe_float(row.get("profit_factor")) < 1.0:
        return ORCH_BLOCK_PAPER
    if time_risk.get((group_type, group_value)) == "high_time_death":
        return ORCH_SHADOW_ONLY
    edge_decision = str(row.get("decision") or WATCH_ONLY)
    if edge_decision == ALLOW_PAPER:
        return ALLOW_PAPER_CANDIDATE
    if edge_decision == BLOCK_PAPER:
        return ORCH_BLOCK_PAPER
    if edge_decision == SHADOW_ONLY:
        return ORCH_SHADOW_ONLY
    return ORCH_WATCH_ONLY


def _stricten_decision(
    decision: str,
    net_row: dict[str, Any],
    anti_row: dict[str, Any],
    ev_row: dict[str, Any],
    stability_row: dict[str, Any],
    rank_row: dict[str, Any],
) -> str:
    strict_votes = {
        str(anti_row.get("final_decision") or ""),
        str(ev_row.get("final_decision") or ""),
        str(rank_row.get("decision") or ""),
    }
    if "REJECT" in strict_votes:
        return ORCH_BLOCK_PAPER
    net_samples = safe_int(net_row.get("samples") or ev_row.get("samples"))
    if net_samples >= 500 and safe_float(net_row.get("net_EV") or ev_row.get("net_EV")) <= 0 and (net_row or ev_row):
        return ORCH_BLOCK_PAPER
    if net_samples >= 500 and safe_float(net_row.get("net_PF") or ev_row.get("net_PF")) and safe_float(net_row.get("net_PF") or ev_row.get("net_PF")) < 1.2:
        return ORCH_BLOCK_PAPER
    if str(stability_row.get("trend_status") or "") == "deteriorating":
        return ORCH_WATCH_ONLY
    if "SHADOW_CANDIDATE" in strict_votes and decision == ALLOW_PAPER_CANDIDATE:
        return ORCH_SHADOW_ONLY
    return decision


def _reason(row: dict[str, Any], decision: str, walk_row: dict[str, Any], catalyst: dict[str, Any], time_risk: dict[tuple[str, str], str]) -> str:
    if decision == ORCH_BLOCK_PAPER:
        return str(row.get("reason") or "blocked_by_orchestrator")
    if decision == ORCH_SHADOW_ONLY:
        return "time_death_or_quality_risk" if time_risk else str(row.get("reason") or "shadow_validate")
    if decision == ORCH_WATCH_ONLY:
        if str(row.get("group_type") or "") == "score_bucket":
            return "generic_bucket_not_actionable"
        if safe_int(row.get("total_labels")) < 500:
            return "sample_too_small"
        if walk_row and str(walk_row.get("decision")) != "PAPER_CANDIDATE":
            return str(walk_row.get("reason") or "walk_forward_not_confirmed")
        if "catalyst_dependent_edge" in catalyst.get("risk_flags", []):
            return "catalyst_dependency_unclear"
        return str(row.get("reason") or "watch_only")
    return "multi_module_confirmation"


def _time_risk_groups(time_death: dict[str, Any]) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    mapping = {"market_regime": "regime", "symbol": "symbol", "side": "side", "score_bucket": "score_bucket"}
    for row in time_death.get("worst_time_groups", []):
        if safe_float(row.get("time_ratio")) >= 0.80:
            out[(mapping.get(str(row.get("group_key")), str(row.get("group_key"))), str(row.get("group_value") or "").upper())] = "high_time_death"
    return out


def _news_blocked_symbols(news: dict[str, Any]) -> set[str]:
    return {
        str(row.get("symbol") or "").upper()
        for row in news.get("blocked", [])
        if str(row.get("symbol") or "").upper()
    }


def _news_gate_for_group(news: dict[str, Any], group_type: str, group_value: str) -> str:
    if group_type != "symbol":
        return str(news.get("global_decision") or "NEWS_ALLOW")
    for row in news.get("symbol_decisions", []):
        if str(row.get("symbol") or "").upper() == group_value.upper():
            return str(row.get("decision") or "NEWS_WATCH")
    return str(news.get("global_decision") or "NEWS_ALLOW")


def _best_exit_policy(exit_policy: dict[str, Any]) -> str:
    rows = exit_policy.get("candidate_exit_policies", [])
    if not rows:
        return "current_exit"
    row = rows[0]
    return f"max_hold={row.get('max_hold_bars')} early_exit={row.get('early_exit_after_bars')} min_mfe={row.get('min_mfe_required')}"


def _decision_rank(decision: str) -> int:
    return {
        ALLOW_PAPER_CANDIDATE: 4,
        ORCH_WATCH_ONLY: 3,
        ORCH_SHADOW_ONLY: 2,
        ORCH_BLOCK_PAPER: 1,
    }.get(decision, 0)


def _dedupe_blocked(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    out = []
    for row in rows:
        key = (row.get("type"), row.get("group"), row.get("reason"))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out[:20]


def _match_policy(rows: list[dict[str, Any]], symbol: str, side: str, regime: str, bucket: str) -> dict[str, Any] | None:
    for row in rows:
        if row.get("symbol_allowlist") and str(row["symbol_allowlist"]).upper() != symbol.upper():
            continue
        if row.get("side_allowlist") and str(row["side_allowlist"]).upper() != side.upper():
            continue
        if row.get("regime_allowlist") and str(row["regime_allowlist"]).upper() != regime.upper():
            continue
        if row.get("score_bucket_allowlist") and str(row["score_bucket_allowlist"]).upper() != bucket.upper():
            continue
        return row
    return None


def _match_block(rows: list[dict[str, Any]], symbol: str, side: str, regime: str) -> dict[str, Any] | None:
    for row in rows:
        group_type = str(row.get("type") or "")
        group = str(row.get("group") or "").upper()
        if group_type == "symbol" and group == symbol.upper():
            return row
        if group_type == "side" and group == side.upper():
            return row
        if group_type == "regime" and group == regime.upper():
            return row
        if group_type == "news" and group in {"GLOBAL", symbol.upper()}:
            return row
    return None


def _policy_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- id={row.get('id')} decision={row.get('decision')} "
            f"symbol_allowlist={row.get('symbol_allowlist')} side_allowlist={row.get('side_allowlist')} "
            f"regime_allowlist={row.get('regime_allowlist')} score_bucket_allowlist={row.get('score_bucket_allowlist')} "
            f"min_pf={safe_float(row.get('min_pf')):.2f} validation_pf={safe_float(row.get('validation_pf')):.2f} "
            f"recent_pf={safe_float(row.get('recent_pf')):.2f} net_pf={safe_float(row.get('net_pf')):.2f} "
            f"net_EV={safe_float(row.get('net_EV')):.4f} stability={row.get('stability')} "
            f"walk_forward_stability={safe_float(row.get('walk_forward_stability')):.2f} news_gate={row.get('news_gate')} "
            f"time_death_risk={row.get('time_death_risk')} reason={row.get('reason')}"
        )
        for row in rows[:12]
    ]


def _blocked_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [f"- {row.get('type')} {row.get('group')} reason={row.get('reason')}" for row in rows[:16]]
