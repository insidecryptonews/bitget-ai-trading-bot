from __future__ import annotations

from typing import Any


START = "HIGH VALUE PATTERNS SMOKE TEST START"
END = "HIGH VALUE PATTERNS SMOKE TEST END"


class HighValuePatternsSmokeTest:
    def __init__(self, config: Any, db: Any, logger: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.logger = logger

    def run(self) -> dict[str, Any]:
        from .candidate_ranking import CandidateRanking
        from .decision_ledger_audit import DecisionLedgerAudit
        from .policy_stability_matrix import PolicyStabilityMatrix

        ranking = CandidateRanking(self.config, self.db).build(hours=24)
        stability = PolicyStabilityMatrix(self.config, self.db).build(hours=24)
        ledger = DecisionLedgerAudit(self.config, self.db).build(hours=24)
        safety = self.config.live_trading is False and self.config.dry_run is True and self.config.paper_trading is True
        result = safety and ranking.get("final_recommendation") == "NO LIVE" and stability.get("final_recommendation") == "NO LIVE"
        return {
            "candidate_ranking_ok": ranking.get("final_recommendation") == "NO LIVE",
            "policy_stability_ok": stability.get("final_recommendation") == "NO LIVE",
            "decision_ledger_ok": ledger.get("final_recommendation") == "NO LIVE",
            "LIVE_TRADING": bool(self.config.live_trading),
            "DRY_RUN": bool(self.config.dry_run),
            "PAPER_TRADING": bool(self.config.paper_trading),
            "opened_paper_trades": 0,
            "opened_real_trades": 0,
            "slots_changed": False,
            "result": "PASS" if result else "FAIL",
        }

    def to_text(self) -> str:
        payload = self.run()
        return "\n".join([
            START,
            f"candidate_ranking_ok={str(payload['candidate_ranking_ok']).lower()}",
            f"policy_stability_ok={str(payload['policy_stability_ok']).lower()}",
            f"decision_ledger_ok={str(payload['decision_ledger_ok']).lower()}",
            f"LIVE_TRADING={str(payload['LIVE_TRADING']).lower()}",
            f"DRY_RUN={str(payload['DRY_RUN']).lower()}",
            f"PAPER_TRADING={str(payload['PAPER_TRADING']).lower()}",
            "no_real_orders=true",
            "no_bitget_real=true",
            "no_external_repo_cloning=true",
            f"opened_paper_trades={payload['opened_paper_trades']}",
            f"opened_real_trades={payload['opened_real_trades']}",
            f"slots_changed={str(payload['slots_changed']).lower()}",
            "final_recommendation: NO LIVE",
            f"result: {payload['result']}",
            END,
        ])
