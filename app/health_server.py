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
_DASHBOARD_LAB_CACHE: dict[str, dict[str, Any]] = {}
_DASHBOARD_FULL_REPORT_CACHE: dict[str, dict[str, Any]] = {}


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
                "/api/training/exit-label-calibration-v2",
                "/api/training/score-calibration",
                "/api/training/shadow-experiments",
                "/api/training/evolution-score",
                "/api/training/mfe-mae-diagnostic",
                "/api/training/catalyst-summary",
                "/api/training/news-risk-gate",
                "/api/training/paper-policy-lab",
                "/api/training/paper-policy-orchestrator",
                "/api/training/walk-forward",
                "/api/training/policy-backtest",
                "/api/training/exit-policy-backtest",
                "/api/training/time-death-autopsy",
                "/api/training/time-death-filter-proposal",
                "/api/training/exit-cause-backtest",
                "/api/training/pre-move-event-labeler",
                "/api/training/pre-move-feature-snapshot",
                "/api/training/pre-move-pattern-miner",
                "/api/training/pre-move-similarity-scanner",
                "/api/training/net-edge-lab",
                "/api/training/anti-overfit-gate",
                "/api/training/ev-slippage-calibration-gate",
                "/api/training/policy-stability-matrix",
                "/api/training/candidate-ranking",
                "/api/training/decision-ledger-audit",
                "/api/training/adaptive-exit-backtest",
                "/api/training/sizing-safety-lab",
                "/api/training/structured-output-guard-status",
                "/api/training/vps-runtime-health",
                "/api/training/post-migration-backup",
                "/api/training/data-restore-benchmark",
                "/api/training/fast-runtime-readiness",
                "/api/training/websocket-migration-plan",
                "/api/training/time-death-lab",
                "/api/training/adaptive-exit-policy",
                "/api/training/latency-audit",
                "/api/training/fast-execution-readiness",
                "/api/training/data-vault-status",
                "/api/training/data-export",
                "/api/training/data-upload-latest",
                "/api/training/data-download-latest",
                "/api/training/data-restore-latest",
                "/api/training/data-vault-prune",
                "/api/training/migration-readiness",
                "/api/training/migration-readiness-deep-check",
                "/api/training/vps-migration-guide",
                "/api/training/vps-preflight",
                "/api/training/fast-runtime-plan",
                "/api/training/worker-lock-status",
                "/api/training/full-report",
                "/api/training/export/full.txt",
                "/api/training/export/full.json",
                "/api/training/export/signals.csv",
                "/api/training/export/paper-trades.csv",
                "/api/training/export/labels.csv",
                "/api/training/export/latency.csv",
                "/api/training/export/pre-move-events.csv",
                "/api/training/export/candidates.csv",
                "/api/training/short-report",
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
            if path == "/api/training/exit-label-calibration-v2":
                self._send_json(_exit_label_calibration_v2(config, db, query))
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
            if path == "/api/training/paper-policy-orchestrator":
                self._send_json(_paper_policy_orchestrator(config, db, query))
                return
            if path == "/api/training/walk-forward":
                self._send_json(_walk_forward(config, db, query))
                return
            if path == "/api/training/policy-backtest":
                self._send_json(_policy_backtest(config, db, query))
                return
            if path == "/api/training/exit-policy-backtest":
                self._send_json(_exit_policy_backtest(config, db, query))
                return
            if path == "/api/training/time-death-autopsy":
                self._send_json(_time_death_autopsy(config, db, query))
                return
            if path == "/api/training/time-death-filter-proposal":
                self._send_json(_time_death_filter_proposal(config, db, query))
                return
            if path == "/api/training/exit-cause-backtest":
                self._send_json(_exit_cause_backtest(config, db, query))
                return
            if path == "/api/training/pre-move-event-labeler":
                self._send_json(_pre_move_event_labeler(config, db, query))
                return
            if path == "/api/training/pre-move-feature-snapshot":
                self._send_json(_pre_move_feature_snapshot(config, db, query))
                return
            if path == "/api/training/pre-move-pattern-miner":
                self._send_json(_pre_move_pattern_miner(config, db, query))
                return
            if path == "/api/training/pre-move-similarity-scanner":
                self._send_json(_pre_move_similarity_scanner(config, db, query))
                return
            if path == "/api/training/net-edge-lab":
                self._send_json(_net_edge_lab(config, db, query))
                return
            if path == "/api/training/anti-overfit-gate":
                self._send_json(_anti_overfit_gate(config, db, query))
                return
            if path == "/api/training/ev-slippage-calibration-gate":
                self._send_json(_ev_slippage_calibration_gate(config, db, query))
                return
            if path == "/api/training/policy-stability-matrix":
                self._send_json(_policy_stability_matrix(config, db, query))
                return
            if path == "/api/training/candidate-ranking":
                self._send_json(_candidate_ranking(config, db, query))
                return
            if path == "/api/training/decision-ledger-audit":
                self._send_json(_decision_ledger_audit(config, db, query))
                return
            if path == "/api/training/adaptive-exit-backtest":
                self._send_json(_adaptive_exit_backtest(config, db, query))
                return
            if path == "/api/training/sizing-safety-lab":
                self._send_json(_sizing_safety_lab(config, db, query))
                return
            if path == "/api/training/structured-output-guard-status":
                self._send_json(_structured_output_guard_status(config, db, query))
                return
            if path == "/api/training/vps-runtime-health":
                self._send_json(_vps_runtime_health(config, db, query))
                return
            if path == "/api/training/post-migration-backup":
                self._send_json(_post_migration_backup(config, db, query))
                return
            if path == "/api/training/data-restore-benchmark":
                self._send_json(_data_restore_benchmark(config, db, query))
                return
            if path == "/api/training/fast-runtime-readiness":
                self._send_json(_fast_runtime_readiness(config, db, query))
                return
            if path == "/api/training/websocket-migration-plan":
                self._send_json(_websocket_migration_plan(config, db, query))
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
            if path == "/api/training/data-upload-latest":
                self._send_json(_data_upload_latest(config, db, query))
                return
            if path == "/api/training/data-download-latest":
                self._send_json(_data_download_latest(config, db, query))
                return
            if path == "/api/training/data-restore-latest":
                self._send_json(_data_restore_latest(config, db, query))
                return
            if path == "/api/training/data-vault-prune":
                self._send_json(_data_vault_prune(config, db, query))
                return
            if path == "/api/training/migration-readiness":
                self._send_json(_migration_readiness(config, db, query))
                return
            if path == "/api/training/migration-readiness-deep-check":
                self._send_json(_migration_readiness_deep_check(config, db, query))
                return
            if path == "/api/training/vps-migration-guide":
                self._send_json(_vps_migration_guide(config, db, query))
                return
            if path == "/api/training/vps-preflight":
                self._send_json(_vps_preflight(config, db, query))
                return
            if path == "/api/training/fast-runtime-plan":
                self._send_json(_fast_runtime_plan(config, db, query))
                return
            if path == "/api/training/worker-lock-status":
                self._send_json(_worker_lock_status(config, db, query))
                return
            if path == "/api/training/full-report":
                payload = _dashboard_full_report(config, db, query)
                fmt = (query.get("format") or ["text"])[0].lower()
                if fmt == "json":
                    self._send_json(payload)
                else:
                    self._send_text(str(payload.get("text") or ""))
                return
            if path == "/api/training/short-report":
                payload = _dashboard_short_report(config, db, query)
                fmt = (query.get("format") or ["text"])[0].lower()
                if fmt == "json":
                    self._send_json(payload)
                else:
                    self._send_text(str(payload.get("text") or ""))
                return
            if path == "/api/training/export/full.txt":
                payload = _dashboard_full_report(config, db, query)
                self._send_text(str(payload.get("text") or ""), filename="bitget_training_full_report.txt")
                return
            if path == "/api/training/export/full.json":
                self._send_json(_dashboard_full_report(config, db, query))
                return
            if path.startswith("/api/training/export/") and path.endswith(".csv"):
                filename, csv_text = _dashboard_csv_export(config, db, path, query)
                self._send_csv(csv_text, filename=filename)
                return
            self._send_status(404, "not found")

        def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            try:
                from .dashboard_pro import sanitize_json_for_dashboard

                payload = sanitize_json_for_dashboard(payload)
            except Exception:
                pass
            body = json.dumps(payload, ensure_ascii=True, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, text: str, status: int = 200, filename: str | None = None) -> None:
            try:
                from .dashboard_pro import sanitize_text_for_dashboard

                text = sanitize_text_for_dashboard(text)
            except Exception:
                pass
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_csv(self, text: str, status: int = 200, filename: str = "export.csv") -> None:
            try:
                from .dashboard_pro import sanitize_text_for_dashboard

                text = sanitize_text_for_dashboard(text)
            except Exception:
                pass
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
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
    payload["worker_lock"] = _worker_lock_status_payload(config, db)
    payload["vps_migration"] = _vps_dashboard_summary(config, db, payload["worker_lock"])
    if "mfe_mae" not in payload:
        payload["mfe_mae"] = {}
    payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        from .dashboard_pro import _git_version

        payload["git_version"] = _git_version()
    except Exception:
        payload["git_version"] = "unknown"
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


def _exit_label_calibration_v2(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "exit label calibration v2 unavailable", ".exit_label_calibration_v2", "ExitLabelCalibrationV2")


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


def _paper_policy_orchestrator(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "paper policy orchestrator unavailable", ".paper_policy_orchestrator", "PaperPolicyOrchestrator")


def _walk_forward(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "walk-forward unavailable", ".walk_forward_validation", "WalkForwardValidation")


def _policy_backtest(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "policy backtest unavailable", ".policy_backtest", "PolicyBacktest")


def _exit_policy_backtest(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "exit policy backtest unavailable", ".exit_policy_backtest", "ExitPolicyBacktest")


def _time_death_autopsy(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "time death autopsy unavailable", ".time_death_autopsy", "TimeDeathAutopsyLab")


def _time_death_filter_proposal(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "time death filter proposal unavailable", ".time_death_filter_proposal", "TimeDeathFilterProposal")


def _exit_cause_backtest(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "exit cause backtest unavailable", ".exit_cause_backtest", "ExitCauseBacktest")


def _pre_move_event_labeler(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "pre-move event labeler unavailable", ".pre_move_event_labeler", "PreMoveEventLabeler")


def _pre_move_feature_snapshot(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "pre-move feature snapshot unavailable", ".pre_move_feature_snapshot", "PreMoveFeatureSnapshot")


def _pre_move_pattern_miner(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "pre-move pattern miner unavailable", ".pre_move_pattern_miner", "PreMovePatternMiner")


def _pre_move_similarity_scanner(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "pre-move similarity scanner unavailable", ".pre_move_similarity_scanner", "PreMoveSimilarityScanner")


def _net_edge_lab(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "net edge lab unavailable", ".net_edge_lab", "NetEdgeLab")


def _anti_overfit_gate(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "anti overfit gate unavailable", ".anti_overfit_gate", "AntiOverfitGate")


def _ev_slippage_calibration_gate(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "ev slippage calibration gate unavailable", ".ev_slippage_calibration_gate", "EvSlippageCalibrationGate")


def _policy_stability_matrix(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "policy stability matrix unavailable", ".policy_stability_matrix", "PolicyStabilityMatrix")


def _candidate_ranking(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "candidate ranking unavailable", ".candidate_ranking", "CandidateRanking")


def _decision_ledger_audit(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "decision ledger audit unavailable", ".decision_ledger_audit", "DecisionLedgerAudit")


def _adaptive_exit_backtest(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "adaptive exit backtest unavailable", ".adaptive_exit_backtest", "AdaptiveExitBacktest")


def _sizing_safety_lab(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "sizing safety lab unavailable", ".sizing_safety_lab", "SizingSafetyLab")


def _structured_output_guard_status(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    try:
        from .structured_output_guard import smoke_test_text

        text = smoke_test_text()
    except Exception as exc:
        return {"error": str(exc)[:300], "final_recommendation": "NO LIVE"}
    return {"text": text, "status": "available", "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "final_recommendation": "NO LIVE"}


def _vps_runtime_health(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    if config is None or db is None:
        return {"error": "vps runtime health unavailable", "final_recommendation": "NO LIVE"}
    try:
        from .vps_runtime_health import VpsRuntimeHealth

        lab = VpsRuntimeHealth(config, db)
        payload = lab.build()
        text = lab.to_text()
    except Exception as exc:
        return {"error": str(exc)[:300], "final_recommendation": "NO LIVE"}
    payload = dict(payload)
    payload["text"] = text
    payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload["final_recommendation"] = "NO LIVE"
    return payload


def _post_migration_backup(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    hours = _query_int(query, "hours", 168)
    if config is None or db is None:
        return {"error": "post migration backup unavailable", "hours": hours, "final_recommendation": "NO LIVE"}
    try:
        from .post_migration_backup import PostMigrationBackup

        text = PostMigrationBackup(config, db).to_text(hours=hours)
    except Exception as exc:
        return {"error": str(exc)[:300], "hours": hours, "final_recommendation": "NO LIVE"}
    return {"text": text, "hours": hours, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "final_recommendation": "NO LIVE"}


def _data_restore_benchmark(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    if config is None or db is None:
        return {"error": "data restore benchmark unavailable", "final_recommendation": "NO LIVE"}
    try:
        from .data_restore_benchmark import DataRestoreBenchmark

        lab = DataRestoreBenchmark(config, db)
        payload = lab.build(dry_run=True)
        text = lab.to_text(dry_run=True)
    except Exception as exc:
        return {"error": str(exc)[:300], "final_recommendation": "NO LIVE"}
    payload = dict(payload)
    payload["text"] = text
    payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload["final_recommendation"] = "NO LIVE"
    return payload


def _fast_runtime_readiness(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "fast runtime readiness unavailable", ".fast_runtime_readiness", "FastRuntimeReadiness")


def _websocket_migration_plan(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "websocket migration plan unavailable", ".websocket_migration_plan", "WebsocketMigrationPlan")


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

        vault = DataVault(config, db)
        payload = vault.export(hours=hours, upload=True)
        text = _data_export_text(payload, config)
    except Exception as exc:
        return {"error": str(exc)[:300], "hours": hours, "final_recommendation": "NO LIVE"}
    payload = dict(payload)
    payload["text"] = text
    payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload["final_recommendation"] = "NO LIVE"
    return payload


def _data_upload_latest(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    if config is None or db is None:
        return {"error": "data upload unavailable", "final_recommendation": "NO LIVE"}
    try:
        from .data_vault import DataVault

        vault = DataVault(config, db)
        payload = vault.upload_latest()
        text = _data_upload_latest_text(payload)
    except Exception as exc:
        return {"error": str(exc)[:300], "final_recommendation": "NO LIVE"}
    payload = dict(payload)
    payload["text"] = text
    payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload["final_recommendation"] = "NO LIVE"
    return payload


def _data_download_latest(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    if config is None or db is None:
        return {"error": "data download unavailable", "final_recommendation": "NO LIVE"}
    try:
        from .data_vault import DataVault

        vault = DataVault(config, db)
        payload = vault.download_latest()
        text = _data_download_latest_text(payload)
    except Exception as exc:
        return {"error": str(exc)[:300], "final_recommendation": "NO LIVE"}
    payload = dict(payload)
    payload["text"] = text
    payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload["final_recommendation"] = "NO LIVE"
    return payload


def _data_restore_latest(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    if config is None or db is None:
        return {"error": "data restore unavailable", "final_recommendation": "NO LIVE"}
    apply = str((query.get("apply") or ["false"])[0]).lower() in {"1", "true", "yes"}
    try:
        from .data_vault import DataVault

        vault = DataVault(config, db)
        payload = vault.restore_latest(apply=apply)
        text = _data_restore_latest_text(payload)
    except Exception as exc:
        return {"error": str(exc)[:300], "final_recommendation": "NO LIVE"}
    payload = dict(payload)
    payload["text"] = text
    payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload["final_recommendation"] = "NO LIVE"
    return payload


def _data_vault_prune(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    if config is None or db is None:
        return {"error": "data vault prune unavailable", "final_recommendation": "NO LIVE"}
    apply = str((query.get("apply") or ["false"])[0]).lower() in {"1", "true", "yes"}
    try:
        from .data_vault import DataVault

        vault = DataVault(config, db)
        payload = vault.prune_local_backups(apply=apply)
        text = _data_vault_prune_text(payload)
    except Exception as exc:
        return {"error": str(exc)[:300], "final_recommendation": "NO LIVE"}
    payload = dict(payload)
    payload["text"] = text
    payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload["final_recommendation"] = "NO LIVE"
    return payload


def _vps_migration_guide(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    if config is None:
        return {"error": "vps migration guide unavailable", "final_recommendation": "NO LIVE"}
    try:
        from .vps_migration import build_vps_migration_guide

        text = build_vps_migration_guide(config)
    except Exception as exc:
        return {"error": str(exc)[:300], "final_recommendation": "NO LIVE"}
    return {"text": text, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "final_recommendation": "NO LIVE"}


def _vps_preflight(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    if config is None or db is None:
        return {"error": "vps preflight unavailable", "final_recommendation": "NO LIVE"}
    try:
        from .vps_migration import VpsPreflight

        lab = VpsPreflight(config, db)
        payload = lab.build()
        text = lab.to_text()
    except Exception as exc:
        return {"error": str(exc)[:300], "final_recommendation": "NO LIVE"}
    payload = dict(payload)
    payload["text"] = text
    payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload["final_recommendation"] = "NO LIVE"
    return payload


def _fast_runtime_plan(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    hours = _query_int(query, "hours", 24)
    if config is None:
        return {"error": "fast runtime plan unavailable", "final_recommendation": "NO LIVE"}
    try:
        from .fast_runtime_plan import FastRuntimePlan

        lab = FastRuntimePlan(config, db)
        payload = lab.build(hours=hours)
        text = lab.to_text(hours=hours)
    except Exception as exc:
        return {"error": str(exc)[:300], "final_recommendation": "NO LIVE"}
    payload = dict(payload)
    payload["text"] = text
    payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload["final_recommendation"] = "NO LIVE"
    return payload


def _worker_lock_status(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    payload = _worker_lock_status_payload(config, db)
    text = "\n".join([
        "WORKER LOCK STATUS START",
        f"enabled: {str(payload.get('enabled')).lower()}",
        f"current_instance_id: {payload.get('current_instance_id', '')}",
        f"lock_status: {payload.get('lock_status', '')}",
        f"active_worker_instance: {payload.get('active_worker_instance', '')}",
        f"lock_age_seconds: {payload.get('lock_age_seconds', 0)}",
        f"warning_if_duplicate_worker: {payload.get('warning_if_duplicate_worker', '') or 'none'}",
        "final_recommendation: NO LIVE",
        "WORKER LOCK STATUS END",
    ])
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


def _migration_readiness_deep_check(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    if config is None or db is None:
        return {"error": "migration readiness deep check unavailable", "final_recommendation": "NO LIVE"}
    run = str((query.get("run") or ["false"])[0]).lower() in {"1", "true", "yes"}
    if not run:
        text = "\n".join([
            "MIGRATION READINESS DEEP CHECK START",
            "status: run_from_cli_recommended",
            "reason: deep check can be heavy for large backups",
            "command: python -m app.research_lab migration-readiness-deep-check",
            "final_recommendation: NO LIVE",
            "MIGRATION READINESS DEEP CHECK END",
        ])
        return {
            "text": text,
            "status": "run_from_cli_recommended",
            "final_recommendation": "NO LIVE",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    try:
        from .data_vault import DataVault

        vault = DataVault(config, db)
        payload = vault.migration_readiness_deep_check()
        text = _migration_deep_check_text(payload)
    except Exception as exc:
        return {"error": str(exc)[:300], "final_recommendation": "NO LIVE"}
    payload = dict(payload)
    payload["text"] = text
    payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload["final_recommendation"] = "NO LIVE"
    return payload


def _data_export_text(payload: dict[str, Any], config: Any | None) -> str:
    external = payload.get("external_upload", {}) or {}
    provider = getattr(config, "data_vault_external_provider", "s3_compatible")
    return "\n".join([
        "DATA EXPORT START",
        f"hours: {payload.get('hours')}",
        f"file: {payload.get('file')}",
        f"manifest_valid: {str(payload.get('manifest_valid')).lower()}",
        f"checksums_created: {str(payload.get('checksums_created')).lower()}",
        f"secrets_excluded: {str(payload.get('secrets_excluded')).lower()}",
        f"streaming_export: {str(payload.get('streaming_export', True)).lower()}",
        f"memory_safe_export: {str(payload.get('memory_safe_export', True)).lower()}",
        "external_upload:",
        f"- enabled: {str(external.get('enabled', False)).lower()}",
        f"- provider: {external.get('provider', provider)}",
        f"- configured: {str(external.get('configured', False)).lower()}",
        f"- attempted: {str(external.get('attempted', False)).lower()}",
        f"- uploaded: {str(external.get('uploaded', False)).lower()}",
        f"- remote_key: {external.get('remote_key', '')}",
        f"- remote_size_bytes: {external.get('remote_size_bytes', 0)}",
        f"- local_size_bytes: {external.get('local_size_bytes', payload.get('local_size_bytes', 0))}",
        f"- checksum_sha256: {external.get('checksum_sha256', payload.get('checksum_sha256', ''))}",
        f"- verified: {str(external.get('verified', False)).lower()}",
        f"- sanitized_error: {external.get('sanitized_error', 'none') or 'none'}",
        "final_recommendation: NO LIVE",
        "DATA EXPORT END",
    ])


def _migration_deep_check_text(payload: dict[str, Any]) -> str:
    return "\n".join([
        "MIGRATION READINESS DEEP CHECK START",
        f"backup_source_checked: {payload.get('backup_source_checked', 'none')}",
        f"latest_backup: {payload.get('latest_backup') or 'none'}",
        f"manifest_valid: {str(payload.get('manifest_valid')).lower()}",
        f"checksum_valid: {str(payload.get('checksum_valid')).lower()}",
        f"import_dry_run_ok: {str(payload.get('import_dry_run_ok')).lower()}",
        f"cache_updated: {str(payload.get('cache_updated')).lower()}",
        f"ready_for_vps_migration: {str(payload.get('ready_for_vps_migration')).lower()}",
        f"error_sanitized: {payload.get('error_sanitized') or 'none'}",
        "final_recommendation: NO LIVE",
        "MIGRATION READINESS DEEP CHECK END",
    ])


def _data_upload_latest_text(payload: dict[str, Any]) -> str:
    return "\n".join([
        "DATA UPLOAD LATEST START",
        f"latest_local_backup: {payload.get('latest_local_backup') or 'none'}",
        f"manifest_valid: {str(payload.get('manifest_valid')).lower()}",
        f"checksum_valid: {str(payload.get('checksum_valid')).lower()}",
        f"external_enabled: {str(payload.get('external_enabled')).lower()}",
        f"external_configured: {str(payload.get('external_configured')).lower()}",
        f"uploaded: {str(payload.get('uploaded')).lower()}",
        f"remote_key: {payload.get('remote_key', '')}",
        f"verified: {str(payload.get('verified')).lower()}",
        f"sanitized_error: {payload.get('sanitized_error') or 'none'}",
        "DATA UPLOAD LATEST END",
    ])


def _data_download_latest_text(payload: dict[str, Any]) -> str:
    return "\n".join([
        "DATA DOWNLOAD LATEST START",
        f"external_enabled: {str(payload.get('external_enabled')).lower()}",
        f"external_configured: {str(payload.get('external_configured')).lower()}",
        f"latest_remote_backup: {payload.get('latest_remote_backup') or 'none'}",
        f"downloaded: {str(payload.get('downloaded')).lower()}",
        f"already_exists: {str(payload.get('already_exists', False)).lower()}",
        f"local_newer_warning: {str(payload.get('local_newer_warning', False)).lower()}",
        f"local_file: {payload.get('local_file') or 'none'}",
        f"manifest_valid: {str(payload.get('manifest_valid')).lower()}",
        f"checksum_valid: {str(payload.get('checksum_valid')).lower()}",
        "secrets_excluded: true",
        f"sanitized_error: {payload.get('sanitized_error') or 'none'}",
        "final_recommendation: NO LIVE",
        "DATA DOWNLOAD LATEST END",
    ])


def _data_restore_latest_text(payload: dict[str, Any]) -> str:
    return "\n".join([
        "DATA RESTORE LATEST START",
        f"mode: {payload.get('mode')}",
        f"latest_backup: {payload.get('latest_backup') or 'none'}",
        f"download_attempted: {str(payload.get('download_attempted')).lower()}",
        f"manifest_valid: {str(payload.get('manifest_valid')).lower()}",
        f"checksum_valid: {str(payload.get('checksum_valid')).lower()}",
        f"duplicates_skipped: {payload.get('duplicates_skipped', 0)}",
        f"rows_inserted: {payload.get('rows_inserted', 0)}",
        f"rows_updated: {payload.get('rows_updated', 0)}",
        "secrets_excluded: true",
        f"result: {payload.get('result')}",
        f"sanitized_error: {payload.get('sanitized_error') or 'none'}",
        "final_recommendation: NO LIVE",
        "DATA RESTORE LATEST END",
    ])


def _data_vault_prune_text(payload: dict[str, Any]) -> str:
    deleted = payload.get("deleted") or []
    kept = payload.get("kept") or []
    deleted_lines = [f"- {item}" for item in deleted[:20]] if deleted else ["- none"]
    kept_lines = [f"- {item}" for item in kept[:20]] if kept else ["- none"]
    return "\n".join([
        "DATA VAULT PRUNE START",
        f"mode: {payload.get('mode')}",
        f"local_before: {payload.get('local_before')}",
        f"local_after: {payload.get('local_after')}",
        "deleted:",
        *deleted_lines,
        "kept:",
        *kept_lines,
        f"never_deleted_latest_valid: {str(payload.get('never_deleted_latest_valid')).lower()}",
        "DATA VAULT PRUNE END",
    ])


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
    cache_key = f"{module_name}:{class_name}:{hours}"
    started = time.perf_counter()
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
        payload = {"error": str(exc)[:300], "hours": hours, "final_recommendation": "NO LIVE"}
        _cache_dashboard_lab(cache_key, payload, "error", int((time.perf_counter() - started) * 1000))
        return payload
    payload = dict(payload)
    payload["text"] = text
    payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload["cache"] = {
        "key": cache_key,
        "created_at": payload["generated_at"],
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "status": "ok",
    }
    payload["final_recommendation"] = "NO LIVE"
    _cache_dashboard_lab(cache_key, payload, "ok", payload["cache"]["duration_ms"])
    return payload


def _cache_dashboard_lab(key: str, payload: dict[str, Any], status: str, duration_ms: int) -> None:
    try:
        from .dashboard_pro import sanitize_json_for_dashboard

        clean = sanitize_json_for_dashboard(payload)
    except Exception:
        clean = payload
    _DASHBOARD_LAB_CACHE[key] = {
        "key": key,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "duration_ms": duration_ms,
        "status": status,
        "payload_json": clean,
    }
    while len(_DASHBOARD_LAB_CACHE) > 80:
        oldest = sorted(_DASHBOARD_LAB_CACHE.items(), key=lambda item: str(item[1].get("created_at") or ""))[0][0]
        _DASHBOARD_LAB_CACHE.pop(oldest, None)


def _dashboard_full_report(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    hours = _query_int(query, "hours", 24)
    if config is None or db is None:
        return {"error": "full report unavailable", "hours": hours, "final_recommendation": "NO LIVE"}
    cache_key = f"full:{hours}"
    force = (query.get("force") or ["0"])[0] in {"1", "true", "yes"}
    cached = _DASHBOARD_FULL_REPORT_CACHE.get(cache_key)
    if cached and not force and time.time() - float(cached.get("cached_at_epoch", 0.0) or 0.0) < 300:
        payload = dict(cached.get("payload") or {})
        payload["cache_hit"] = True
        return payload
    try:
        from .dashboard_pro import build_dashboard_full_report

        payload = build_dashboard_full_report(config, db, hours=hours)
        payload["cache_hit"] = False
        _DASHBOARD_FULL_REPORT_CACHE[cache_key] = {"cached_at_epoch": time.time(), "payload": payload}
        _cache_dashboard_lab(
            f"dashboard_full_report:{hours}",
            payload,
            "ok",
            int(sum(int(section.get("duration_ms") or 0) for section in payload.get("sections", []))),
        )
        return payload
    except Exception as exc:
        return {
            "error": str(exc)[:300],
            "text": f"DASHBOARD PRO FULL REPORT START\nERROR_SANITIZED: {type(exc).__name__}\nfinal_recommendation: NO LIVE\nDASHBOARD PRO FULL REPORT END",
            "hours": hours,
            "final_recommendation": "NO LIVE",
        }


def _dashboard_short_report(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    hours = _query_int(query, "hours", 24)
    if config is None or db is None:
        return {"error": "short report unavailable", "hours": hours, "final_recommendation": "NO LIVE"}
    try:
        from .dashboard_pro import build_dashboard_short_report

        return build_dashboard_short_report(config, db, hours=hours)
    except Exception as exc:
        return {
            "error": str(exc)[:300],
            "text": f"DASHBOARD PRO SHORT REPORT START\nERROR_SANITIZED: {type(exc).__name__}\nfinal_recommendation: NO LIVE\nDASHBOARD PRO SHORT REPORT END",
            "hours": hours,
            "final_recommendation": "NO LIVE",
        }


def _dashboard_csv_export(config: Any | None, db: Any | None, path: str, query: dict[str, list[str]]) -> tuple[str, str]:
    if config is None or db is None:
        return "export.csv", "error\nexport unavailable\n"
    hours = _query_int(query, "hours", 24)
    limit = _query_int(query, "limit", 1000)
    kind = path.rsplit("/", 1)[-1].removesuffix(".csv")
    try:
        from .dashboard_pro import export_csv

        return export_csv(config, db, kind, hours=hours, limit=limit)
    except Exception as exc:
        return f"{kind}.csv", f"error\n{str(exc)[:120]}\n"


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


def _worker_lock_status_payload(config: Any | None, db: Any | None) -> dict[str, Any]:
    if config is None or db is None:
        return {
            "enabled": False,
            "acquired": False,
            "current_instance_id": "",
            "active_worker_instance": "",
            "lock_status": "unavailable",
            "lock_age_seconds": 0.0,
            "warning_if_duplicate_worker": "",
        }
    try:
        from .worker_lock import WorkerLockManager

        return WorkerLockManager(config, db).status().to_dict()
    except Exception:
        return {
            "enabled": bool(getattr(config, "require_single_worker_lock", False)),
            "acquired": False,
            "current_instance_id": "",
            "active_worker_instance": "",
            "lock_status": "error",
            "lock_age_seconds": 0.0,
            "warning_if_duplicate_worker": "worker_lock_status_error",
        }


def _vps_dashboard_summary(config: Any | None, db: Any | None, worker_lock: dict[str, Any]) -> dict[str, Any]:
    if config is None or db is None:
        return {}
    try:
        from .data_vault import DataVault

        vault = DataVault(config, db)
        status = vault.status()
        readiness = vault.migration_readiness()
    except Exception:
        status = {}
        readiness = {}
    return {
        "migration_readiness": readiness.get("readiness_status", "unknown"),
        "latest_remote_backup": status.get("latest_remote_backup", ""),
        "latest_local_backup": status.get("latest_local_backup", ""),
        "r2_configured": bool(status.get("external_configured", False)),
        "r2_last_upload_verified": bool(status.get("last_upload_verified", False)),
        "vps_preflight_status": "not_run",
        "worker_lock_status": worker_lock.get("lock_status", "unknown"),
        "active_instance_id": worker_lock.get("active_worker_instance", ""),
        "current_runtime_profile": getattr(config, "training_runtime_profile", "railway_lightweight"),
        "fast_runtime_readiness": "NOT_HFT / RESEARCH_MODE_ONLY",
        "final_recommendation": "NO LIVE",
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
