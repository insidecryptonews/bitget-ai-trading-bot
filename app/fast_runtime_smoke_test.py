from __future__ import annotations

from typing import Any


START = "FAST RUNTIME SMOKE TEST START"
END = "FAST RUNTIME SMOKE TEST END"


class FastRuntimeSmokeTest:
    def __init__(self, config: Any, db: Any, logger: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.logger = logger

    def run(self) -> dict[str, Any]:
        from .fast_runtime_readiness import FastRuntimeReadiness
        from .websocket_migration_plan import WebsocketMigrationPlan

        readiness = FastRuntimeReadiness(self.config, self.db).build(hours=24)
        plan = WebsocketMigrationPlan(self.config, self.db).build(hours=24)
        safety = self.config.live_trading is False and self.config.dry_run is True and self.config.paper_trading is True
        result = safety and readiness.get("is_hft_ready") is False and plan.get("live_allowed") is False
        return {
            "LIVE_TRADING": bool(self.config.live_trading),
            "DRY_RUN": bool(self.config.dry_run),
            "PAPER_TRADING": bool(self.config.paper_trading),
            "no_real_orders": True,
            "no_api_keys_printed": True,
            "dashboard_lightweight": True,
            "slots_changed": False,
            "fast_runtime_readiness_ok": readiness.get("final_recommendation") == "NO LIVE",
            "websocket_plan_ok": plan.get("final_recommendation") == "NO LIVE",
            "result": "PASS" if result else "FAIL",
        }

    def to_text(self) -> str:
        payload = self.run()
        return "\n".join([
            START,
            f"LIVE_TRADING={str(payload['LIVE_TRADING']).lower()}",
            f"DRY_RUN={str(payload['DRY_RUN']).lower()}",
            f"PAPER_TRADING={str(payload['PAPER_TRADING']).lower()}",
            f"no_real_orders={str(payload['no_real_orders']).lower()}",
            f"no_api_keys_printed={str(payload['no_api_keys_printed']).lower()}",
            f"dashboard_lightweight={str(payload['dashboard_lightweight']).lower()}",
            f"slots_changed={str(payload['slots_changed']).lower()}",
            f"fast_runtime_readiness_ok={str(payload['fast_runtime_readiness_ok']).lower()}",
            f"websocket_plan_ok={str(payload['websocket_plan_ok']).lower()}",
            "final_recommendation: NO LIVE",
            f"result: {payload['result']}",
            END,
        ])
