from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from typing import Any


@dataclass
class HealthState:
    mode: str
    started_at: float = field(default_factory=time.time)
    open_positions: int = 0
    daily_pnl: float = 0.0
    last_scan: str = ""
    circuit_breaker: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "mode": self.mode,
            "uptime": f"{int(time.time() - self.started_at)}s",
            "open_positions": self.open_positions,
            "daily_pnl": self.daily_pnl,
            "last_scan": self.last_scan,
            "circuit_breaker": self.circuit_breaker,
            **self.extra,
        }


STATIC_DIR = Path(__file__).resolve().parent / "static"
DASHBOARD_PATH = STATIC_DIR / "dashboard.html"


def start_health_server(
    state: HealthState,
    port: int,
    logger,
    *,
    config: Any | None = None,
    db: Any | None = None,
    training_pulse: Any | None = None,
    telegram_notifier: Any | None = None,
) -> threading.Thread:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if path == "/health":
                self._send_json(state.payload())
                return
            if not _dashboard_enabled(config):
                self._send_status(404, "not found")
                return
            if path in {
                "/dashboard",
                "/api/training/status",
                "/api/training/summary",
                "/api/training/acceleration-plan",
                "/api/training/shadow-opportunity",
                "/api/training/edge-guard",
                "/api/training/tp-sl-lab",
                "/api/training/exit-simulation",
                "/api/training/score-calibration",
                "/api/training/shadow-experiments",
                "/api/training/evolution-score",
                "/api/training/mfe-mae-diagnostic",
                "/api/training/catalyst-summary",
                "/api/training/news-risk-gate",
                "/api/training/paper-policy-lab",
                "/api/training/walk-forward",
                "/api/training/policy-backtest",
                "/api/training/time-death-lab",
                "/api/training/adaptive-exit-policy",
                "/api/training/latency-audit",
                "/api/training/fast-execution-readiness",
                "/api/training/data-vault-status",
                "/api/training/data-export",
                "/api/training/migration-readiness",
            }:
                if not _authorized(config, query, self.headers):
                    self._send_json({"error": "unauthorized"}, status=401)
                    return
            if path == "/dashboard":
                self._send_html(_dashboard_html(config))
                return
            if path == "/api/training/status":
                self._send_json(_training_status(config, db, training_pulse, telegram_notifier))
                return
            if path == "/api/training/summary":
                self._send_json(_training_summary(config, db, query))
                return
            if path == "/api/training/acceleration-plan":
                self._send_json(_acceleration_plan(config, db, query))
                return
            if path == "/api/training/shadow-opportunity":
                self._send_json(_shadow_opportunity(config, db, query))
                return
            if path == "/api/training/edge-guard":
                self._send_json(_edge_guard(config, db, query))
                return
            if path == "/api/training/tp-sl-lab":
                self._send_json(_tp_sl_lab(config, db, query))
                return
            if path == "/api/training/exit-simulation":
                self._send_json(_exit_simulation(config, db, query))
                return
            if path == "/api/training/score-calibration":
                self._send_json(_score_calibration(config, db, query))
                return
            if path == "/api/training/shadow-experiments":
                self._send_json(_shadow_experiments(config, db, query))
                return
            if path == "/api/training/evolution-score":
                self._send_json(_evolution_score(config, db, query))
                return
            if path == "/api/training/mfe-mae-diagnostic":
                self._send_json(_mfe_mae_diagnostic(config, db, query))
                return
            if path == "/api/training/catalyst-summary":
                self._send_json(_catalyst_summary(config, db, query))
                return
            if path == "/api/training/news-risk-gate":
                self._send_json(_news_risk_gate(config, db, query))
                return
            if path == "/api/training/paper-policy-lab":
                self._send_json(_paper_policy_lab(config, db, query))
                return
            if path == "/api/training/walk-forward":
                self._send_json(_walk_forward(config, db, query))
                return
            if path == "/api/training/policy-backtest":
                self._send_json(_policy_backtest(config, db, query))
                return
            if path == "/api/training/time-death-lab":
                self._send_json(_time_death_lab(config, db, query))
                return
            if path == "/api/training/adaptive-exit-policy":
                self._send_json(_adaptive_exit_policy(config, db, query))
                return
            if path == "/api/training/latency-audit":
                self._send_json(_latency_audit(config, db, query))
                return
            if path == "/api/training/fast-execution-readiness":
                self._send_json(_fast_execution_readiness(config, db, query))
                return
            if path == "/api/training/data-vault-status":
                self._send_json(_data_vault_status(config, db, query))
                return
            if path == "/api/training/data-export":
                self._send_json(_data_export(config, db, query))
                return
            if path == "/api/training/migration-readiness":
                self._send_json(_migration_readiness(config, db, query))
                return
            self._send_status(404, "not found")

        def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=True, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str, status: int = 200) -> None:
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_status(self, status: int, message: str) -> None:
            self._send_json({"error": message}, status=status)

        def log_message(self, format: str, *args: Any) -> None:
            return

    def run() -> None:
        try:
            HTTPServer(("0.0.0.0", port), Handler).serve_forever()
        except OSError as exc:
            logger.warning("Health server no pudo iniciar en puerto %s: %s", port, exc)

    thread = threading.Thread(target=run, name="health-server", daemon=True)
    thread.start()
    logger.info("Health server listo en /health puerto %s", port)
    return thread


def _dashboard_enabled(config: Any | None) -> bool:
    return bool(config is not None and getattr(config, "enable_training_dashboard", False))


def _authorized(config: Any | None, query: dict[str, list[str]], headers: Any) -> bool:
    token = str(getattr(config, "dashboard_auth_token", "") or "")
    if not token:
        return True
    query_token = (query.get("token") or [""])[0]
    header_token = headers.get("X-Dashboard-Token", "")
    return query_token == token or header_token == token


def _dashboard_html(config: Any | None) -> str:
    try:
        html = DASHBOARD_PATH.read_text(encoding="utf-8")
    except OSError:
        html = "<!doctype html><title>Training Dashboard</title><h1>Training Dashboard</h1>"
    refresh = max(2, int(getattr(config, "dashboard_refresh_seconds", 10) or 10))
    return html.replace("__DASHBOARD_REFRESH_SECONDS__", str(refresh))


def _training_status(config: Any | None, db: Any | None, training_pulse: Any | None, telegram_notifier: Any | None) -> dict[str, Any]:
    if training_pulse is not None and config is not None:
        payload = training_pulse.to_dict(config)
    else:
        payload = {
            "safety": {
                "paper_trading": bool(getattr(config, "paper_trading", True)),
                "live_trading": bool(getattr(config, "live_trading", False)),
                "dry_run": bool(getattr(config, "dry_run", True)),
                "worker_lightweight_mode": bool(getattr(config, "worker_lightweight_mode", True)),
            },
            "health": {},
            "paper": {},
            "allocator": {},
            "signals": {},
            "labels": {},
            "regimes": {},
            "top_signals": [],
            "top_blocks": [],
            "diagnosis": ["PAPER ONLY: waiting for training pulse"],
            "next_action": "PAPER ONLY",
            "final_recommendation": "NO LIVE",
        }
    payload["telegram"] = telegram_notifier.status_dict() if telegram_notifier is not None else {
        "enabled": False,
        "configured": False,
        "last_sent_at": "",
        "last_error": "",
        "sent_count": 0,
    }
    payload["open_paper_positions_detail"] = _open_paper_positions_detail(db)
    payload["edge"] = _edge_summary(config, db)
    if "mfe_mae" not in payload:
        payload["mfe_mae"] = {}
    payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return payload


def _training_summary(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    hours = _query_int(query, "hours", 6)
    if config is None or db is None:
        return {"error": "training summary unavailable", "hours": hours, "final_recommendation": "NO LIVE"}
    try:
        from .training_summary import TrainingSummary

        text = TrainingSummary(config, db).build(hours=hours)
    except Exception as exc:
        return {"error": str(exc)[:300], "hours": hours, "final_recommendation": "NO LIVE"}
    return {
        "text": text,
        "hours": hours,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "final_recommendation": "NO LIVE",
    }


def _acceleration_plan(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    hours = _query_int(query, "hours", 24)
    if config is None or db is None:
        return {"error": "acceleration plan unavailable", "hours": hours, "final_recommendation": "NO LIVE"}
    try:
        from .training_summary import TrainingSummary

        text = TrainingSummary(config, db).acceleration_plan(hours=hours)
    except Exception as exc:
        return {"error": str(exc)[:300], "hours": hours, "final_recommendation": "NO LIVE"}
    biggest_problem = ""
    for line in text.splitlines():
        if line.startswith("biggest_problem:"):
            biggest_problem = line.split(":", 1)[1].strip()
            break
    return {
        "text": text,
        "hours": hours,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "biggest_problem": biggest_problem,
        "final_recommendation": "NO LIVE",
    }


def _shadow_opportunity(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    hours = _query_int(query, "hours", 24)
    if config is None or db is None:
        return {"error": "shadow opportunity unavailable", "hours": hours, "final_recommendation": "NO LIVE"}
    try:
        from .shadow_opportunity_lab import ShadowOpportunityLab

        lab = ShadowOpportunityLab(config, db)
        payload = lab.build(hours=hours)
        text = lab.to_text(hours=hours)
    except Exception as exc:
        return {"error": str(exc)[:300], "hours": hours, "final_recommendation": "NO LIVE"}
    return {
        "text": text,
        "hours": hours,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "overall": payload.get("overall", {}),
        "best_candidates": payload.get("best_candidates", []),
        "worst_candidates": payload.get("worst_candidates", []),
        "final_recommendation": "NO LIVE",
    }


def _edge_guard(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    hours = _query_int(query, "hours", 24)
    if config is None or db is None:
        return {"error": "edge guard unavailable", "hours": hours, "final_recommendation": "NO LIVE"}
    try:
        from .edge_guard import EdgeGuard

        guard = EdgeGuard(config, db)
        payload = guard.build_edge_guard_report(hours=hours)
        text = guard.to_text(hours=hours)
    except Exception as exc:
        return {"error": str(exc)[:300], "hours": hours, "final_recommendation": "NO LIVE"}
    payload = dict(payload)
    payload["text"] = text
    payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload["final_recommendation"] = "NO LIVE"
    return payload


def _tp_sl_lab(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    hours = _query_int(query, "hours", 24)
    if config is None or db is None:
        return {"error": "tp/sl lab unavailable", "hours": hours, "final_recommendation": "NO LIVE"}
    try:
        from .tp_sl_horizon_lab import TpSlHorizonLab

        lab = TpSlHorizonLab(config, db)
        payload = lab.build(hours=hours)
        text = lab.to_text(hours=hours)
    except Exception as exc:
        return {"error": str(exc)[:300], "hours": hours, "final_recommendation": "NO LIVE"}
    payload = dict(payload)
    payload["text"] = text
    payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload["final_recommendation"] = "NO LIVE"
    return payload


def _exit_simulation(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "exit simulation unavailable", ".exit_simulation_lab", "ExitSimulationLab")


def _score_calibration(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "score calibration unavailable", ".score_calibration_lab", "ScoreCalibrationLab")


def _shadow_experiments(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "shadow experiments unavailable", ".shadow_experiments", "ShadowExperimentsLab")


def _evolution_score(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "evolution score unavailable", ".evolution_score", "EvolutionScore")


def _mfe_mae_diagnostic(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "mfe/mae diagnostic unavailable", ".mfe_mae_diagnostic", "MfeMaeDiagnostic")


def _catalyst_summary(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "catalyst summary unavailable", ".catalyst_registry", "CatalystRegistry")


def _news_risk_gate(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "news risk gate unavailable", ".news_risk_gate", "NewsRiskGate")


def _paper_policy_lab(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "paper policy lab unavailable", ".paper_policy_lab", "PaperPolicyLab")


def _walk_forward(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "walk-forward unavailable", ".walk_forward_validation", "WalkForwardValidation")


def _policy_backtest(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "policy backtest unavailable", ".policy_backtest", "PolicyBacktest")


def _time_death_lab(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "time death lab unavailable", ".time_death_lab", "TimeDeathLab")


def _adaptive_exit_policy(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "adaptive exit policy unavailable", ".adaptive_exit_policy_lab", "AdaptiveExitPolicyLab")


def _latency_audit(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "latency audit unavailable", ".latency_audit", "LatencyAudit")


def _fast_execution_readiness(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "fast execution readiness unavailable", ".fast_execution_readiness", "FastExecutionReadiness")


def _data_vault_status(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    if config is None or db is None:
        return {"error": "data vault unavailable", "final_recommendation": "NO LIVE"}
    try:
        from .data_vault import DataVault

        payload = DataVault(config, db).status()
        text = DataVault(config, db).status_text()
    except Exception as exc:
        return {"error": str(exc)[:300], "final_recommendation": "NO LIVE"}
    payload = dict(payload)
    payload["text"] = text
    payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload["final_recommendation"] = "NO LIVE"
    return payload


def _data_export(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    hours = _query_int(query, "hours", 168)
    if config is None or db is None:
        return {"error": "data export unavailable", "hours": hours, "final_recommendation": "NO LIVE"}
    try:
        from .data_vault import DataVault

        payload = DataVault(config, db).export(hours=hours, upload=False)
        text = "\n".join([
            "DATA EXPORT START",
            f"hours: {payload.get('hours')}",
            f"file: {payload.get('file')}",
            f"manifest_valid: {str(payload.get('manifest_valid')).lower()}",
            f"checksums_created: {str(payload.get('checksums_created')).lower()}",
            f"secrets_excluded: {str(payload.get('secrets_excluded')).lower()}",
            "DATA EXPORT END",
        ])
    except Exception as exc:
        return {"error": str(exc)[:300], "hours": hours, "final_recommendation": "NO LIVE"}
    payload = dict(payload)
    payload["text"] = text
    payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload["final_recommendation"] = "NO LIVE"
    return payload


def _migration_readiness(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    if config is None or db is None:
        return {"error": "migration readiness unavailable", "final_recommendation": "NO LIVE"}
    try:
        from .data_vault import DataVault

        payload = DataVault(config, db).migration_readiness()
        text = DataVault(config, db).migration_readiness_text()
    except Exception as exc:
        return {"error": str(exc)[:300], "final_recommendation": "NO LIVE"}
    payload = dict(payload)
    payload["text"] = text
    payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload["final_recommendation"] = "NO LIVE"
    return payload


def _lab_payload(
    config: Any | None,
    db: Any | None,
    query: dict[str, list[str]],
    error_message: str,
    module_name: str,
    class_name: str,
) -> dict[str, Any]:
    hours = _query_int(query, "hours", 24)
    if config is None or db is None:
        return {"error": error_message, "hours": hours, "final_recommendation": "NO LIVE"}
    try:
        import importlib

        module = importlib.import_module(module_name, package=__package__)
        lab = getattr(module, class_name)(config, db)
        if hasattr(lab, "build"):
            payload = lab.build(hours=hours)
        else:
            payload = lab.build_summary(hours=hours)
        if hasattr(lab, "to_text"):
            text = lab.to_text(hours=hours)
        else:
            text = lab.to_summary_text(hours=hours)
    except Exception as exc:
        return {"error": str(exc)[:300], "hours": hours, "final_recommendation": "NO LIVE"}
    payload = dict(payload)
    payload["text"] = text
    payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload["final_recommendation"] = "NO LIVE"
    return payload


def _open_paper_positions_detail(db: Any | None) -> list[dict[str, Any]]:
    if db is None:
        return []
    try:
        rows = db.get_open_paper_positions_summary(limit=5)
    except Exception:
        return []
    allowed = {
        "symbol",
        "side",
        "entry_price",
        "opened_at",
        "strategy",
        "score",
        "stop_loss",
        "take_profit_1",
        "take_profit_2",
        "status",
        "realized_pnl",
        "unrealized_pnl",
        "reason",
    }
    return [
        {key: _public_value(row.get(key)) for key in allowed if key in row}
        for row in rows
    ]


def _edge_summary(config: Any | None, db: Any | None) -> dict[str, Any]:
    if config is None or db is None:
        return {}
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        labels = db.get_signal_label_summary_since(since)
    except Exception:
        return {}
    total = float(labels.get("total_labels") or 0.0)
    tp = float(labels.get("tp1_count") or 0.0) + float(labels.get("tp2_count") or 0.0)
    sl = float(labels.get("sl_count") or 0.0)
    time_count = float(labels.get("time_count") or 0.0)
    return {
        "profit_factor": float(labels.get("profit_factor") or 0.0),
        "time_ratio": time_count / max(total, 1.0) if total else 0.0,
        "sl_ratio": sl / max(total, 1.0) if total else 0.0,
        "tp_ratio": tp / max(total, 1.0) if total else 0.0,
        "total_labels": total,
    }


def _query_int(query: dict[str, list[str]], key: str, default: int) -> int:
    try:
        return max(1, int((query.get(key) or [default])[0]))
    except (TypeError, ValueError):
        return default


def _public_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return re.sub(
        r"(?i)\b(API[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSPHRASE|PRIVATE[_-]?KEY)\s*[:=]\s*([^\s,;]+)",
        lambda match: f"{match.group(1)}=***",
        value,
    )
