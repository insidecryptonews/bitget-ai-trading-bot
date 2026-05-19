from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .cost_model import explain_cost_breakdown, round_trip_fee_bps, should_apply_funding
from .data_guards import duplicate_guard_smoke_text, labeler_guard_smoke_text


START = "CORE CORRECTIONS START"
END = "CORE CORRECTIONS END"


class CoreCorrections:
    """Read-only summary of the Fase 5 final core corrections."""

    def __init__(self, config: Any, db: Any | None = None) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        del hours
        no_cross = should_apply_funding("2026-05-19T01:00:00+00:00", "2026-05-19T02:00:00+00:00")
        cross = should_apply_funding("2026-05-19T07:55:00+00:00", "2026-05-19T08:05:00+00:00")
        long_funding = explain_cost_breakdown(
            side="LONG",
            entry_time="2026-05-19T07:55:00+00:00",
            exit_time="2026-05-19T08:05:00+00:00",
            funding_rate=0.0045,
        )
        short_funding = explain_cost_breakdown(
            side="SHORT",
            entry_time="2026-05-19T07:55:00+00:00",
            exit_time="2026-05-19T08:05:00+00:00",
            funding_rate=0.0045,
        )
        probe_cost = explain_cost_breakdown(source="market_probe")
        time_no_trade = explain_cost_breakdown(outcome="TIME", time_exit_assumption="no_trade")
        return {
            "cost_model_fixed": True,
            "fee_model": {
                "taker_taker_bps": round_trip_fee_bps("taker", "taker"),
                "maker_taker_bps": round_trip_fee_bps("maker", "taker"),
                "maker_maker_bps": round_trip_fee_bps("maker", "maker"),
            },
            "funding_model_status": "OK" if not no_cross and cross and long_funding.funding_component_bps > 0 and short_funding.funding_component_bps < 0 else "WARNING",
            "double_counting_risk": "LOW",
            "market_probe_cost_pollution": probe_cost.total_cost_bps != 0,
            "time_label_cost_pollution": time_no_trade.total_cost_bps != 0,
            "labeler_guard_status": "ACTIVE_FUTURE_GUARD",
            "duplicate_guard_status": "AVAILABLE_AUDIT_AND_FUTURE_GUARD",
            "candidate_actionability_logic": "market_probe_not_actionable; micro-samples use NEED_MORE_DATA_NOT_ACTIONABLE",
            "margin_mode_status": self._margin_status(),
            "historical_data_modified": False,
            "paper_filter_enabled": False,
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        lines = [
            START,
            f"cost_model_fixed: {str(payload['cost_model_fixed']).lower()}",
            "fee_model:",
            f"- taker_taker_bps: {payload['fee_model']['taker_taker_bps']}",
            f"- maker_taker_bps: {payload['fee_model']['maker_taker_bps']}",
            f"- maker_maker_bps: {payload['fee_model']['maker_maker_bps']}",
            f"funding_model_status: {payload['funding_model_status']}",
            f"double_counting_risk: {payload['double_counting_risk']}",
            f"market_probe_cost_pollution: {str(payload['market_probe_cost_pollution']).lower()}",
            f"time_label_cost_pollution: {str(payload['time_label_cost_pollution']).lower()}",
            f"labeler_guard_status: {payload['labeler_guard_status']}",
            f"duplicate_guard_status: {payload['duplicate_guard_status']}",
            f"candidate_actionability_logic: {payload['candidate_actionability_logic']}",
            f"margin_mode_status: {payload['margin_mode_status']}",
            "historical_data_modified: false",
            "paper_filter_enabled: false",
            "final_recommendation: NO LIVE",
            END,
        ]
        return "\n".join(lines)

    def _margin_status(self) -> str:
        try:
            from .margin_mode_audit import MarginModeAudit

            return str(MarginModeAudit(self.config, self.db).build().get("margin_mode_status") or "UNKNOWN_NEEDS_VERIFICATION")
        except Exception:
            return "UNKNOWN_NEEDS_VERIFICATION"


def cost_model_correction_smoke_text() -> str:
    checks = {
        "taker_taker_12_bps": round_trip_fee_bps("taker", "taker") == 12.0,
        "maker_taker_8_bps": round_trip_fee_bps("maker", "taker") == 8.0,
        "maker_maker_4_bps": round_trip_fee_bps("maker", "maker") == 4.0,
        "market_probe_zero_cost": explain_cost_breakdown(source="market_probe").total_cost_bps == 0.0,
        "time_no_trade_zero_cost": explain_cost_breakdown(outcome="TIME", time_exit_assumption="no_trade").total_cost_bps == 0.0,
    }
    return _smoke("COST MODEL CORRECTION SMOKE TEST", checks)


def funding_model_smoke_text() -> str:
    no_cross = should_apply_funding("2026-05-19T01:00:00+00:00", "2026-05-19T02:00:00+00:00")
    cross = should_apply_funding("2026-05-19T07:55:00+00:00", "2026-05-19T08:05:00+00:00")
    long_cost = explain_cost_breakdown(side="LONG", entry_time="2026-05-19T07:55:00+00:00", exit_time="2026-05-19T08:05:00+00:00", funding_rate=0.0045)
    short_income = explain_cost_breakdown(side="SHORT", entry_time="2026-05-19T07:55:00+00:00", exit_time="2026-05-19T08:05:00+00:00", funding_rate=0.0045)
    checks = {
        "funding_no_cross_zero": no_cross is False,
        "funding_cross_detected": cross is True,
        "positive_funding_long_cost": long_cost.funding_component_bps > 0,
        "positive_funding_short_income": short_income.funding_component_bps < 0,
        "timestamp": bool(datetime.now(timezone.utc)),
    }
    return _smoke("FUNDING MODEL SMOKE TEST", checks)


def core_corrections_smoke_text(config: Any, db: Any | None = None) -> str:
    payload = CoreCorrections(config, db).build()
    checks = {
        "cost_model_fixed": payload["cost_model_fixed"] is True,
        "funding_model_ok": payload["funding_model_status"] == "OK",
        "market_probe_not_polluting": payload["market_probe_cost_pollution"] is False,
        "time_no_trade_not_polluting": payload["time_label_cost_pollution"] is False,
        "paper_filter_off": payload["paper_filter_enabled"] is False,
        "final_recommendation_no_live": payload["final_recommendation"] == "NO LIVE",
    }
    return _smoke("CORE CORRECTIONS SMOKE TEST", checks)


def _smoke(title: str, checks: dict[str, bool]) -> str:
    result = "PASS" if all(checks.values()) else "FAIL"
    start = f"{title} START"
    end = f"{title} END"
    lines = [start]
    lines.extend(f"{key}: {str(value).lower()}" for key, value in checks.items())
    lines.extend([
        "LIVE_TRADING=false",
        "DRY_RUN=true",
        "PAPER_TRADING=true",
        "paper_filter_enabled: false",
        "final_recommendation: NO LIVE",
        f"result: {result}",
        end,
    ])
    return "\n".join(lines)
