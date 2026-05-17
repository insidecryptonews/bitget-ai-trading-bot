from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import BotConfig
from .database import Database
from .edge_guard import ALLOW_PAPER, EdgeGuard
from .time_death_autopsy import TimeDeathAutopsyLab, decision_for


START = "TIME DEATH SMOKE TEST START"
END = "TIME DEATH SMOKE TEST END"


class TimeDeathSmokeTest:
    def __init__(self, config: Any, db: Any, logger: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.logger = logger

    def run(self) -> dict[str, Any]:
        checks = {
            "time_high_tp_low_reject": decision_for({"samples": 1000, "time_ratio": 0.85, "tp_ratio": 0.05, "avg_MFE": 0.05}, self.config) == "REJECT",
            "risk_off_high_time_reject": decision_for({"samples": 1000, "group_key": "market_regime", "group_value": "RISK_OFF", "time_ratio": 0.90, "tp_ratio": 0.04, "avg_MFE": 0.10}, self.config) == "REJECT",
            "btcusdt_high_time_not_allow": decision_for({"samples": 1000, "group_key": "symbol", "group_value": "BTCUSDT", "time_ratio": 0.90, "tp_ratio": 0.05, "avg_MFE": 0.10}, self.config) in {"REJECT", "WATCH_ONLY"},
            "bnb_pf_low_reject": decision_for({"samples": 1000, "group_key": "symbol", "group_value": "BNBUSDT", "gross_PF": 0.2, "time_ratio": 1.0, "tp_ratio": 0.0}, self.config) == "REJECT",
            "long_sl_high_tp_low_reject": decision_for({"samples": 1000, "group_key": "side", "group_value": "LONG", "time_ratio": 0.40, "tp_ratio": 0.0, "sl_ratio": 0.36}, self.config) == "REJECT",
            "net_ev_negative_blocks": decision_for({"samples": 1000, "time_ratio": 0.40, "tp_ratio": 0.20, "net_EV": -0.1}, self.config) != "PAPER_CANDIDATE_ONLY_IF_CONFIRMED",
            "sample_small_no_allow": decision_for({"samples": 10, "time_ratio": 0.10, "tp_ratio": 0.50, "net_EV": 1.0}, self.config) == "WATCH_ONLY",
        }
        edge_decision, _ = EdgeGuard(self.config, self.db).classify_metrics({
            "group_value": "BTCUSDT",
            "total_labels": 1000,
            "profit_factor": 2.0,
            "tp_ratio": 0.05,
            "sl_ratio": 0.02,
            "time_ratio": 0.90,
            "strict_block_reason": "high_time_death",
        })
        checks["edge_guard_no_allow_if_orchestrator_blocks"] = edge_decision != ALLOW_PAPER
        safety = self.config.live_trading is False and self.config.dry_run is True and self.config.paper_trading is True
        result = safety and all(checks.values())
        return {
            **checks,
            "LIVE_TRADING": bool(self.config.live_trading),
            "DRY_RUN": bool(self.config.dry_run),
            "PAPER_TRADING": bool(self.config.paper_trading),
            "opened_real_trades": 0,
            "slots_changed": False,
            "result": "PASS" if result else "FAIL",
        }

    def to_text(self) -> str:
        payload = self.run()
        lines = [START]
        for key, value in payload.items():
            if key == "result":
                continue
            if isinstance(value, bool):
                lines.append(f"{key}={str(value).lower()}")
            else:
                lines.append(f"{key}={value}")
        lines.extend(["final_recommendation: NO LIVE", f"result: {payload['result']}", END])
        return "\n".join(lines)
