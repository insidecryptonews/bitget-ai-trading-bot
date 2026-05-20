from __future__ import annotations

from typing import Any

from .anti_overfit_matrix_v2 import AntiOverfitMatrixV2
from .candidate_promotion_v2 import CandidatePromotionV2
from .exit_policy_v3 import ExitPolicyV3
from .exit_policy_v3_backtest import ExitPolicyV3Backtest
from .operational_intelligence_utils import FINAL_RECOMMENDATION, safe_float_text
from .pre_move_intelligence_v2 import PreMoveIntelligenceV2
from .shadow_strategy_simulator import ShadowStrategySimulator
from .strategy_research_library import StrategyResearchLibrary
from .sudden_move_detector import SuddenMoveDetector
from .walk_forward_validator import WalkForwardValidator


class OperationalIntelligenceAudit:
    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        exit_policy = ExitPolicyV3(self.config, self.db).build(hours=hours)
        exit_backtest = ExitPolicyV3Backtest(self.config, self.db).build(hours=hours)
        sudden = SuddenMoveDetector(self.config, self.db).build(hours=hours)
        pre_move = PreMoveIntelligenceV2(self.config, self.db).build(hours=hours)
        walk = WalkForwardValidator(self.config, self.db).build(hours=max(hours, 72))
        anti = AntiOverfitMatrixV2(self.config, self.db).build(hours=max(hours, 72))
        promotion = CandidatePromotionV2(self.config, self.db).build(hours=hours)
        simulator = ShadowStrategySimulator(self.config, self.db).build(hours=max(hours, 72))
        strategy = StrategyResearchLibrary(self.config, self.db).build(hours=max(hours, 72))
        return {
            "hours": hours,
            "exit_policy_v3_status": exit_policy.get("exit_policy_v3_status"),
            "best_exit_policies": exit_backtest.get("best_exit_policies", [])[:10],
            "trailing_profit_lock_candidate_count": _count_exit_candidates(exit_backtest),
            "sudden_move_patterns_found": sudden.get("patterns_found"),
            "sudden_move_false_positive_risk": sudden.get("false_positive_risk_count"),
            "pre_move_patterns_found": pre_move.get("patterns_found"),
            "walk_forward_stable_candidates": _count_decision(walk.get("walk_forward_candidates", []), {"SHADOW_CANDIDATE", "RESEARCH_POCKET"}),
            "walk_forward_overfit_rejected": _count_decision(walk.get("walk_forward_candidates", []), {"OVERFIT_REJECT", "REJECT"}),
            "anti_overfit_status": anti.get("anti_overfit_status"),
            "anti_overfit_rejects": _count_decision(anti.get("anti_overfit_matrix", []), {"REJECT_OVERFIT"}),
            "candidate_promotion_state_counts": promotion.get("candidate_promotion_state_counts", {}),
            "shadow_simulator_best_policy": simulator.get("best_simulated_policy", {}),
            "strategy_research_summary": {
                "tested_hypotheses": len(strategy.get("tested_hypotheses", [])),
                "promising_hypotheses": len(strategy.get("promising_hypotheses", [])),
                "best_baseline": (strategy.get("best_baseline") or {}).get("benchmark_id", "none"),
                "bot_vs_baseline": strategy.get("bot_vs_baseline", {}),
            },
            "paper_filter_enabled": False,
            "live_allowed": False,
            "research_only": True,
            "final_recommendation": FINAL_RECOMMENDATION,
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        best = payload.get("shadow_simulator_best_policy") or {}
        lines = [
            "OPERATIONAL INTELLIGENCE AUDIT START",
            f"hours: {payload['hours']}",
            f"exit_policy_v3_status: {payload['exit_policy_v3_status']}",
            f"trailing_profit_lock_candidate_count: {payload['trailing_profit_lock_candidate_count']}",
            f"sudden_move_patterns_found: {payload['sudden_move_patterns_found']}",
            f"sudden_move_false_positive_risk: {payload['sudden_move_false_positive_risk']}",
            f"pre_move_patterns_found: {payload['pre_move_patterns_found']}",
            f"walk_forward_stable_candidates: {payload['walk_forward_stable_candidates']}",
            f"walk_forward_overfit_rejected: {payload['walk_forward_overfit_rejected']}",
            f"anti_overfit_status: {payload['anti_overfit_status']}",
            f"candidate_promotion_state_counts: {payload['candidate_promotion_state_counts']}",
            f"shadow_simulator_best_policy: {best.get('strategy_id', 'none')}",
            f"shadow_simulator_best_net_ev: {safe_float_text(best.get('net_ev'))}",
            f"strategy_research_summary: {payload['strategy_research_summary']}",
            "paper_filter_enabled=false",
            "live_allowed=false",
            "research_only: true",
            "final_recommendation: NO LIVE",
            "OPERATIONAL INTELLIGENCE AUDIT END",
        ]
        return "\n".join(lines)


def _count_exit_candidates(payload: dict[str, Any]) -> int:
    return sum(1 for row in payload.get("best_exit_policies", []) if row.get("decision") in {"SHADOW_EXIT_CANDIDATE", "RESEARCH_POCKET", "WATCH_ONLY"})


def _count_decision(rows: list[dict[str, Any]], decisions: set[str]) -> int:
    return sum(1 for row in rows if str(row.get("decision")) in decisions)
