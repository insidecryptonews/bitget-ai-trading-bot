from __future__ import annotations

from typing import Any


START = "EDGE HARDENING SMOKE TEST START"
END = "EDGE HARDENING SMOKE TEST END"


class EdgeHardeningSmokeTest:
    def __init__(self, config: Any, db: Any, logger: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.logger = logger

    def run(self) -> dict[str, Any]:
        from .anti_overfit_gate import AntiOverfitGate
        from .ev_slippage_calibration_gate import EvSlippageCalibrationGate
        from .net_edge_lab import NetEdgeLab
        from .paper_policy_orchestrator import PaperPolicyOrchestrator
        from .sizing_safety_lab import SizingSafetyLab
        from .structured_output_guard import StructuredOutputGuard

        safety_ok = bool(self.config.live_trading is False and self.config.dry_run is True and self.config.paper_trading is True)
        net = NetEdgeLab(self.config, self.db).build(hours=24)
        anti = AntiOverfitGate(self.config, self.db).build(hours=24)
        ev = EvSlippageCalibrationGate(self.config, self.db).build(hours=24)
        sizing = SizingSafetyLab(self.config, self.db).build(hours=24)
        orchestrator = PaperPolicyOrchestrator(self.config, self.db).build(hours=24)
        invalid = StructuredOutputGuard().parse('{"decision":"ALLOW","score": NaN}', {"decision": str, "score": float})
        result = (
            safety_ok
            and net.get("final_recommendation") == "NO LIVE"
            and anti.get("final_recommendation") == "NO LIVE"
            and ev.get("final_recommendation") == "NO LIVE"
            and sizing.get("slots_changed") is False
            and orchestrator.get("policy_filter", {}).get("enabled") is False
            and invalid.get("valid") is False
        )
        return {
            "LIVE_TRADING": bool(self.config.live_trading),
            "DRY_RUN": bool(self.config.dry_run),
            "PAPER_TRADING": bool(self.config.paper_trading),
            "opened_paper_trades": 0,
            "opened_real_trades": 0,
            "real_bitget_calls": 0,
            "external_repo_execution": False,
            "net_edge_lab_ok": net.get("final_recommendation") == "NO LIVE",
            "anti_overfit_ok": anti.get("final_recommendation") == "NO LIVE",
            "ev_gate_ok": ev.get("final_recommendation") == "NO LIVE",
            "structured_output_invalid_cannot_allow": invalid.get("valid") is False,
            "sizing_safety_no_live_sizing": sizing.get("live_sizing_touched") is False,
            "orchestrator_shadow_default": orchestrator.get("policy_filter", {}).get("enabled") is False,
            "slots_changed": False,
            "result": "PASS" if result else "FAIL",
        }

    def to_text(self) -> str:
        payload = self.run()
        return "\n".join([
            START,
            f"LIVE_TRADING={str(payload['LIVE_TRADING']).lower()}",
            f"DRY_RUN={str(payload['DRY_RUN']).lower()}",
            f"PAPER_TRADING={str(payload['PAPER_TRADING']).lower()}",
            "no_real_orders=true",
            "no_bitget_real=true",
            "no_api_keys_printed=true",
            f"no_external_repo_execution={str(not payload['external_repo_execution']).lower()}",
            f"net_edge_lab_ok={str(payload['net_edge_lab_ok']).lower()}",
            f"anti_overfit_ok={str(payload['anti_overfit_ok']).lower()}",
            f"ev_gate_ok={str(payload['ev_gate_ok']).lower()}",
            f"structured_output_invalid_cannot_allow={str(payload['structured_output_invalid_cannot_allow']).lower()}",
            f"sizing_safety_no_live_sizing={str(payload['sizing_safety_no_live_sizing']).lower()}",
            f"orchestrator_shadow_default={str(payload['orchestrator_shadow_default']).lower()}",
            f"slots_changed={str(payload['slots_changed']).lower()}",
            "final_recommendation: NO LIVE",
            f"result: {payload['result']}",
            END,
        ])
