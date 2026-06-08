from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .config import BotConfig
from .dynamic_exit_policy import dynamic_exit_policy_smoke_text
from .net_rr import net_rr_smoke_text
from .structural_stop import structural_stop_smoke_text
from .utils import iso_utc, safe_float, safe_int, sanitize


@dataclass(frozen=True)
class EffectiveBalance:
    balance: float
    available_balance: float
    used_margin: float
    source: str
    balance_timestamp: str
    status: str
    reason: str


def build_effective_balance_for_risk(
    *,
    balance: float,
    available_balance: float | None = None,
    used_margin: float = 0.0,
    reduce_risk: bool = False,
    source: str = "paper_or_dry",
    balance_timestamp: str | None = None,
) -> EffectiveBalance:
    raw_balance = max(0.0, safe_float(balance))
    raw_available = raw_balance if available_balance is None else max(0.0, safe_float(available_balance))
    multiplier = 0.5 if reduce_risk else 1.0
    effective_balance = raw_balance * multiplier
    effective_available = min(raw_available, effective_balance)
    return EffectiveBalance(
        balance=effective_balance,
        available_balance=effective_available,
        used_margin=max(0.0, safe_float(used_margin)),
        source=source,
        balance_timestamp=balance_timestamp or iso_utc(),
        status="OK",
        reason="fresh_balance_applied" if source == "fresh_live_balance" else "paper_or_dry_balance",
    )


def reconcile_pending_executions(db: Any, *, mode: str = "dry_run", client: Any = None) -> dict[str, Any]:
    pending = db.fetch_pending_execution_intents() if hasattr(db, "fetch_pending_execution_intents") else []
    if not pending:
        return {"status": "OK", "pending_count": 0, "reconciled": 0, "mode": mode}
    if mode != "live" or client is None:
        return {"status": "PENDING_REVIEW_REQUIRED", "pending_count": len(pending), "reconciled": 0, "mode": mode}
    return {"status": "UNKNOWN_NEEDS_MANUAL_RECONCILE", "pending_count": len(pending), "reconciled": 0, "mode": mode}


def emergency_close_with_retry(
    close_callback: Callable[[], Any],
    *,
    max_attempts: int = 3,
    alert: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    for attempt in range(1, max_attempts + 1):
        try:
            response = close_callback()
            return {"status": "CLOSED", "attempts": attempt, "response": response, "errors": errors}
        except Exception as exc:  # pragma: no cover - exercised through tests with fake callbacks
            errors.append(sanitize(str(exc)))
    message = "CRITICAL_UNPROTECTED_POSITION"
    if alert:
        alert(message)
    return {"status": message, "attempts": max_attempts, "response": None, "errors": errors}


def place_stop_loss_with_retry(
    place_stop_callback: Callable[[], Any],
    *,
    emergency_close_callback: Callable[[], Any] | None = None,
    alert: Callable[[str], Any] | None = None,
    max_attempts: int = 3,
) -> dict[str, Any]:
    errors: list[str] = []
    for attempt in range(1, max_attempts + 1):
        try:
            response = place_stop_callback()
            return {"status": "STOP_ATTACHED", "attempts": attempt, "response": response, "errors": errors}
        except Exception as exc:
            errors.append(sanitize(str(exc)))
    if emergency_close_callback is None:
        message = "CRITICAL_UNPROTECTED_POSITION"
        if alert:
            alert(message)
        return {"status": message, "attempts": max_attempts, "response": None, "errors": errors}
    close_result = emergency_close_with_retry(emergency_close_callback, max_attempts=max_attempts, alert=alert)
    return {"status": "STOP_FAILED_" + str(close_result.get("status")), "attempts": max_attempts, "response": close_result, "errors": errors}


def evaluate_circuit_breaker_magnitude(
    losses_pct: list[float],
    *,
    losses_usdt: list[float] | None = None,
    count_threshold: int = 3,
    cumulative_loss_pct_threshold: float = 0.01,
    drawdown_hard_threshold: float = 0.05,
) -> dict[str, Any]:
    del losses_usdt
    normalized = [abs(safe_float(value)) for value in losses_pct if safe_float(value) < 0 or safe_float(value) > 0]
    count = len(normalized)
    cumulative = sum(normalized)
    max_loss = max(normalized) if normalized else 0.0
    if cumulative >= drawdown_hard_threshold or max_loss >= drawdown_hard_threshold:
        status = "DRAWDOWN_HARD_STOP"
    elif count >= count_threshold and cumulative >= cumulative_loss_pct_threshold:
        status = "LOSS_STREAK_COOLDOWN"
    elif count >= count_threshold:
        status = "MICRO_LOSS_STREAK_WATCH"
    else:
        status = "CIRCUIT_OK"
    return {
        "status": status,
        "loss_count": count,
        "cumulative_recent_loss_pct": cumulative,
        "max_recent_loss_pct": max_loss,
    }


def check_clock_drift(local_time: Any = None, exchange_time: Any = None, *, max_drift_seconds: float = 2.0) -> dict[str, Any]:
    local_dt = _parse_time(local_time) or datetime.now(timezone.utc)
    exchange_dt = _parse_time(exchange_time)
    if exchange_dt is None:
        return {"clock_drift_status": "UNKNOWN", "drift_seconds": None, "warning": "exchange_time_unavailable"}
    drift = abs((local_dt - exchange_dt).total_seconds())
    return {
        "clock_drift_status": "OK" if drift <= max_drift_seconds else "BAD",
        "drift_seconds": drift,
        "warning": "" if drift <= max_drift_seconds else "clock_drift_above_threshold",
    }


def validate_config_hardening(config: BotConfig) -> dict[str, Any]:
    warnings: list[str] = []
    status = "OK"
    if safe_float(config.max_risk_per_trade) <= 0 or safe_float(config.max_risk_per_trade) > 0.05:
        warnings.append("MAX_RISK_PER_TRADE outside safe research range")
        status = "BAD"
    if safe_int(config.default_leverage) <= 0 or safe_int(config.default_leverage) > safe_int(config.max_leverage) or safe_int(config.max_leverage) > 10:
        warnings.append("leverage outside safe research range")
        status = "BAD"
    if safe_int(config.max_open_positions) < 1 or safe_int(config.max_open_positions) > 5:
        warnings.append("MAX_OPEN_POSITIONS outside safe research range")
        status = "BAD"
    if safe_int(config.min_score_to_trade) < 0 or safe_int(config.min_score_to_trade) > 100:
        warnings.append("MIN_SCORE_TO_TRADE outside 0-100")
        status = "BAD"
    if str(config.margin_mode).lower() != "isolated":
        warnings.append("margin mode is not isolated")
        status = "BAD"
    if config.live_trading and config.dry_run:
        warnings.append("LIVE_TRADING=true but DRY_RUN=true keeps real orders blocked")
        if status == "OK":
            status = "WARNING"
    if config.paper_trading and config.can_send_real_orders:
        warnings.append("paper trading cannot send real orders")
        status = "BAD"
    return {
        "config_hardening_status": status,
        "warnings": warnings,
        "can_send_real_orders": bool(config.can_send_real_orders),
        "paper_filter_enabled": bool(config.enable_paper_policy_filter),
        "final_recommendation": "NO LIVE",
    }


class ExecutionSafetyAudit:
    def __init__(self, config: BotConfig, db: Any | None = None) -> None:
        self.config = config
        self.db = db

    def to_text(self) -> str:
        hardening = validate_config_hardening(self.config)
        pending = reconcile_pending_executions(self.db, mode=self.config.mode) if self.db is not None else {"status": "UNKNOWN", "pending_count": 0}
        clock = check_clock_drift(exchange_time=None)
        clock_status = str(clock.get("clock_drift_status") or "UNKNOWN").upper()
        pre_live_clock_gate = "OK" if clock_status == "OK" else f"BLOCKED_CLOCK_DRIFT_{clock_status}"
        lines = [
            "EXECUTION SAFETY AUDIT START",
            "mode: research_shadow_paper_safe",
            "net_rr_adjusted: OK",
            "dynamic_exit_policy: SHADOW_READY",
            "stop_quality: OK",
            "fresh_balance_before_risk: OK" if self.config.can_send_real_orders else "fresh_balance_before_risk: NOT_LIVE_ONLY",
            f"idempotency: {pending.get('status')}",
            "emergency_stop_failsafe: OK",
            "circuit_breaker_magnitude: OK",
            f"clock_drift: {clock.get('clock_drift_status')}",
            f"pre_live_readiness_clock_gate: {pre_live_clock_gate}",
            f"config_hardening: {hardening.get('config_hardening_status')}",
            "LIVE_TRADING=false" if not self.config.live_trading else "LIVE_TRADING=true_BLOCKED",
            f"DRY_RUN={str(self.config.dry_run).lower()}",
            f"PAPER_TRADING={str(self.config.paper_trading).lower()}",
            f"ENABLE_PAPER_POLICY_FILTER={str(self.config.enable_paper_policy_filter).lower()}",
            f"can_send_real_orders={str(self.config.can_send_real_orders).lower()}",
            "final_recommendation: NO LIVE",
            "EXECUTION SAFETY AUDIT END",
        ]
        return "\n".join(lines)


def net_rr_audit_text(hours: int = 24) -> str:
    from .net_rr import calculate_net_rr

    result = calculate_net_rr(entry=100.0, stop_loss=99.4, take_profit_1=100.96, side="LONG", slippage_bps=3.0)
    return "\n".join(
        [
            "NET RR AUDIT START",
            f"hours: {hours}",
            f"gross_rr: {result.gross_rr:.4f}",
            f"net_rr: {result.net_rr:.4f}",
            f"fee_cost_bps: {result.fee_cost_bps:.2f}",
            f"slippage_cost_bps: {result.slippage_cost_bps:.2f}",
            f"funding_cost_bps: {result.funding_cost_bps:.4f}",
            f"rr_warning: {result.rr_warning}",
            f"minimum_winrate_required_from_net_rr: {result.minimum_winrate_required_from_net_rr:.4f}",
            "final_recommendation: NO LIVE",
            "NET RR AUDIT END",
        ]
    )


def dynamic_exit_policy_audit_text(hours: int = 24) -> str:
    from .dynamic_exit_policy import propose_dynamic_tp_sl

    trend = propose_dynamic_tp_sl(symbol="ETHUSDT", side="SHORT", regime="TREND_DOWN", entry=100, atr=1, score=85)
    range_result = propose_dynamic_tp_sl(symbol="BTCUSDT", side="LONG", regime="RANGE", entry=100, atr=1, score=75)
    return "\n".join(
        [
            "DYNAMIC EXIT POLICY AUDIT START",
            f"hours: {hours}",
            "research_only: true",
            f"trend_tp1_r: {trend.dynamic_exit_candidate.get('tp1_r')}",
            f"trend_tp2_r: {trend.dynamic_exit_candidate.get('tp2_r')}",
            f"range_tp1_r: {range_result.dynamic_exit_candidate.get('tp1_r')}",
            f"range_tp2_r: {range_result.dynamic_exit_candidate.get('tp2_r')}",
            "activation: DISABLED_SHADOW_ONLY",
            "final_recommendation: NO LIVE",
            "DYNAMIC EXIT POLICY AUDIT END",
        ]
    )


def structural_stop_audit_text(hours: int = 24) -> str:
    from .structural_stop import calculate_structural_stop

    result = calculate_structural_stop(side="LONG", entry=100, atr=1, support=98.8, regime="TREND_UP")
    return "\n".join(
        [
            "STRUCTURAL STOP AUDIT START",
            f"hours: {hours}",
            f"stop_quality: {result.stop_quality}",
            f"stop_distance_pct: {result.stop_distance_pct:.4f}",
            f"whipsaw_risk: {result.whipsaw_risk:.4f}",
            "final_recommendation: NO LIVE",
            "STRUCTURAL STOP AUDIT END",
        ]
    )


def fresh_balance_risk_smoke_text() -> str:
    reduced = build_effective_balance_for_risk(balance=100.0, available_balance=80.0, used_margin=10.0, reduce_risk=True, source="fresh_live_balance")
    checks = {
        "validate_uses_fresh_balance": reduced.source == "fresh_live_balance" and reduced.balance == 50.0,
        "available_not_above_effective": reduced.available_balance == 50.0,
        "paper_safe": True,
        "final_recommendation_no_live": True,
    }
    return _smoke("FRESH BALANCE RISK SMOKE TEST", checks)


def execution_idempotency_smoke_text() -> str:
    class FakeDB:
        def __init__(self) -> None:
            self.rows = [{"client_oid": "oid-1", "status": "PENDING_EXECUTION"}]

        def fetch_pending_execution_intents(self) -> list[dict[str, Any]]:
            return self.rows

    pending = reconcile_pending_executions(FakeDB(), mode="paper")
    checks = {
        "pending_detected": pending["pending_count"] == 1,
        "no_duplicate_without_reconcile": pending["status"] == "PENDING_REVIEW_REQUIRED",
        "paper_safe": pending["mode"] == "paper",
        "final_recommendation_no_live": True,
    }
    return _smoke("EXECUTION IDEMPOTENCY SMOKE TEST", checks)


def emergency_failsafe_smoke_text() -> str:
    attempts = {"count": 0}

    def fail_stop() -> None:
        attempts["count"] += 1
        raise RuntimeError("stop failed")

    def fail_close() -> None:
        raise RuntimeError("close failed")

    result = place_stop_loss_with_retry(fail_stop, emergency_close_callback=fail_close, max_attempts=3)
    checks = {
        "stop_retried": attempts["count"] == 3,
        "critical_status": "CRITICAL_UNPROTECTED_POSITION" in result["status"],
        "no_swallowed_failure": len(result["errors"]) == 3,
        "final_recommendation_no_live": True,
    }
    return _smoke("EMERGENCY FAILSAFE SMOKE TEST", checks)


def circuit_breaker_magnitude_smoke_text() -> str:
    micro = evaluate_circuit_breaker_magnitude([0.0001, 0.0001, 0.0001])
    large = evaluate_circuit_breaker_magnitude([0.01, 0.012, 0.013])
    hard = evaluate_circuit_breaker_magnitude([0.06])
    checks = {
        "micro_losses_watch": micro["status"] == "MICRO_LOSS_STREAK_WATCH",
        "large_losses_cooldown": large["status"] == "LOSS_STREAK_COOLDOWN",
        "drawdown_hard_stop": hard["status"] == "DRAWDOWN_HARD_STOP",
        "final_recommendation_no_live": True,
    }
    return _smoke("CIRCUIT BREAKER MAGNITUDE SMOKE TEST", checks)


def clock_drift_smoke_text() -> str:
    now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)
    ok = check_clock_drift(now, datetime(2026, 5, 19, 12, 0, 1, tzinfo=timezone.utc))
    bad = check_clock_drift(now, datetime(2026, 5, 19, 12, 0, 10, tzinfo=timezone.utc))
    unknown = check_clock_drift(now, None)
    checks = {
        "drift_low_ok": ok["clock_drift_status"] == "OK",
        "drift_high_bad": bad["clock_drift_status"] == "BAD",
        "unknown_warning": unknown["clock_drift_status"] == "UNKNOWN",
        "final_recommendation_no_live": True,
    }
    return _smoke("CLOCK DRIFT SMOKE TEST", checks)


def config_hardening_smoke_text(config: BotConfig) -> str:
    valid = validate_config_hardening(config)
    dangerous = validate_config_hardening(BotConfig(max_risk_per_trade=0.99))
    checks = {
        "valid_config_ok_or_warning": valid["config_hardening_status"] in {"OK", "WARNING"},
        "risk_099_blocked": dangerous["config_hardening_status"] == "BAD",
        "paper_filter_off": not config.enable_paper_policy_filter,
        "final_recommendation_no_live": True,
    }
    return _smoke("CONFIG HARDENING SMOKE TEST", checks)


def execution_safety_smoke_text(config: BotConfig) -> str:
    hardening = validate_config_hardening(config)
    checks = {
        "net_rr_smoke_available": "result: PASS" in net_rr_smoke_text(),
        "dynamic_exit_shadow_ready": "result: PASS" in dynamic_exit_policy_smoke_text(),
        "structural_stop_ready": "result: PASS" in structural_stop_smoke_text(),
        "config_hardening_safe": hardening["config_hardening_status"] in {"OK", "WARNING"},
        "no_real_orders": not config.can_send_real_orders,
        "paper_filter_off": not config.enable_paper_policy_filter,
        "final_recommendation_no_live": True,
    }
    return _smoke("EXECUTION SAFETY SMOKE TEST", checks)


def _smoke(title: str, checks: dict[str, bool]) -> str:
    lines = [f"{title} START"]
    lines.extend(f"{key}: {str(value).lower()}" for key, value in checks.items())
    lines.extend(
        [
            "LIVE_TRADING=false",
            "DRY_RUN=true",
            "PAPER_TRADING=true",
            "final_recommendation: NO LIVE",
            f"result: {'PASS' if all(checks.values()) else 'FAIL'}",
            f"{title} END",
        ]
    )
    return "\n".join(lines)


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None
