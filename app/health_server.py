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
_DASHBOARD_SHORT_REPORT_CACHE: dict[str, dict[str, Any]] = {}
_ATI_REPORT_DIR = Path(__file__).resolve().parents[1] / "reports" / "research" / "ati"
_RESEARCH_DASHBOARD_V1043C = (
    Path(__file__).resolve().parents[1]
    / "reports" / "research" / "dashboard_v10_43c" / "dashboard_data_v10_43c.json"
)


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
                payload = state.payload()
                components = _research_components_status_payload(state)
                payload["status_scope"] = "http_liveness_only"
                payload["overall_status"] = components["overall_status"]
                payload["research_components"] = components["components"]
                payload["ati_shadow"] = components["components"]["ati_shadow"]
                self._send_json(payload)
                return
            if not _dashboard_enabled(config):
                self._send_status(404, "not found")
                return
            if path in {"/static/dashboard.css", "/static/dashboard.js"}:
                self._send_static(path)
                return
            if path in {
                "/dashboard",
                "/api/training/status",
                "/api/training/ati-shadow",
                "/api/research/ati-shadow",
                "/api/training/summary",
                "/api/training/acceleration-plan",
                "/api/training/shadow-opportunity",
                "/api/training/edge-guard",
                "/api/training/tp-sl-lab",
                "/api/training/exit-simulation",
                "/api/training/exit-label-calibration-v2",
                "/api/training/score-calibration",
                "/api/training/candidate-incubator",
                "/api/training/training-data-integrity",
                "/api/training/worker-health-audit",
                "/api/training/data-vault-audit",
                "/api/training/dashboard-data-binding-audit",
                "/api/training/data-pipeline-diagnosis",
                "/api/training/relation-repair-audit",
                "/api/training/label-quality-v2",
                "/api/training/bitget-cost-model-audit",
                "/api/training/cost-model-inventory",
                "/api/training/margin-mode-audit",
                "/api/training/core-corrections",
                "/api/training/execution-safety-audit",
                "/api/training/net-rr-audit",
                "/api/training/dynamic-exit-policy-audit",
                "/api/training/structural-stop-audit",
                "/api/training/operational-intelligence-audit",
                "/api/training/exit-policy-v3-backtest",
                "/api/training/sudden-move-detector",
                "/api/training/pre-move-v2",
                "/api/training/walk-forward-validator",
                "/api/training/anti-overfit-v2",
                "/api/training/candidate-promotion-v2",
                "/api/training/shadow-strategy-simulator",
                "/api/training/strategy-research-library",
                "/api/training/runtime-optimization-proposal",
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
                "/api/training/real-strategy-backtest",
                "/api/training/ohlcv-replay-loader-audit",
                "/api/training/duplicate-module-audit",
                "/api/training/research-cockpit",
                "/api/training/cost-stress",
                "/api/training/profit-lock-lab",
                "/api/training/fast-exit-lab",
                "/api/training/time-death-reducer-lab",
                "/api/training/trade-replay",
                "/api/training/final-policy-builder",
                "/api/training/time-exit-autopsy-v2",
                "/api/training/dynamic-hold-lab",
                "/api/training/entry-exhaustion-lab",
                "/api/training/reversal-candidate-lab",
                "/api/training/exit-policy-v2",
                "/api/training/phase8-candidate-validator",
                "/api/training/phase8-cost-stress",
                "/api/training/dot-regime-diagnosis",
                "/api/training/dot-regime-filter-lab",
                "/api/training/phase9-paper-readiness",
                "/api/training/net-profit-lock-lab",
                "/api/training/fast-signal-shadow",
                "/api/training/research-pack",
                "/api/training/research-pack-v5",
                "/api/training/ohlcv-freshness-status",
                "/api/training/ohlcv-freshness-refresh-dry",
                "/api/training/training-clean-view-audit",
                "/api/training/shadow-multi-trade-status",
                "/api/training/capital-leverage-sim",
                "/api/training/fee-aware-exit-trainer",
                "/api/time-exit-autopsy-v2",
                "/api/dynamic-hold-lab",
                "/api/entry-exhaustion-lab",
                "/api/reversal-candidate-lab",
                "/api/exit-policy-v2",
                "/api/phase8-candidate-validator",
                "/api/phase8-cost-stress",
                "/api/dot-regime-diagnosis",
                "/api/dot-regime-filter-lab",
                "/api/phase9-paper-readiness",
                "/api/net-profit-lock-lab",
                "/api/fast-signal-shadow",
                "/api/research-pack",
                "/api/research-pack-v5",
                "/api/research/ohlcv-freshness-status",
                "/api/research/ohlcv-freshness-refresh-dry",
                "/api/research/training-clean-view-audit",
                "/api/research/shadow-multi-trade-status",
                "/api/research/capital-leverage-sim",
                "/api/research/fee-aware-exit-trainer",
                "/api/research/strategy-research-enhancer",
                "/api/training/strategy-research-enhancer",
                "/api/research/clean-research-metrics",
                "/api/training/clean-research-metrics",
                "/api/research/data-pipeline-root-cause",
                "/api/training/data-pipeline-root-cause",
                "/api/research/clean-strategy-lab",
                "/api/training/clean-strategy-lab",
                "/api/research/capital-scaling-simulator",
                "/api/training/capital-scaling-simulator",
                "/api/research-pack-v7",
                "/api/training/research-pack-v7",
                "/api/research/duplicate-guard-hook-status",
                "/api/research/funding-cost-model",
                "/api/research/liquidation-model-bitget",
                "/api/research/walk-forward-v2",
                "/api/research-pack-v7-5",
                "/api/training/research-pack-v7-5",
                "/api/research/auto-data-enrichment-status",
                "/api/research/exit-intelligence-lab",
                "/api/research/strategy-experiment-registry",
                "/api/research/shadow-candidate-lifecycle",
                "/api/research/validation-gates-v9",
                "/api/research/bidirectional-funnel",
                "/api/research/score-asymmetry-audit",
                "/api/research/trend-campaign-sim",
                "/api/research/profit-lock-sim",
                "/api/research/research-pack-bidirectional-v1",
                "/api/research/counterfactual-training-export",
                "/api/research/counterfactual-training-download",
                "/api/research/counterfactual-training-summary",
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
                "/trader-terminal",
            } or path.startswith("/api/researchops/v104/"):
                if not _authorized(config, query, self.headers):
                    self._send_json({"error": "unauthorized"}, status=401)
                    return
            if path == "/dashboard":
                self._send_html(_dashboard_html(config))
                return
            # V10.4 — read-only Trader Terminal (GET only; no mutable routes).
            if path == "/trader-terminal":
                self._send_html(_v104_terminal_html(config, db, state, training_pulse))
                return
            if path.startswith("/api/researchops/v104/"):
                payload, status = _v104_api(path, config, db, state, training_pulse)
                self._send_json(payload, status=status)
                return
            if path == "/api/training/status":
                self._send_json(_training_status(config, db, training_pulse, telegram_notifier))
                return
            if path in {"/api/training/ati-shadow", "/api/research/ati-shadow"}:
                self._send_json(_ati_shadow_status_payload())
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
            if path == "/api/training/candidate-incubator":
                self._send_json(_candidate_incubator(config, db, query))
                return
            if path == "/api/training/training-data-integrity":
                self._send_json(_training_data_integrity(config, db, query))
                return
            if path == "/api/training/worker-health-audit":
                self._send_json(_worker_health_audit(config, db, query))
                return
            if path == "/api/training/data-vault-audit":
                self._send_json(_data_vault_audit(config, db, query))
                return
            if path == "/api/training/dashboard-data-binding-audit":
                self._send_json(_dashboard_data_binding_audit(config, db, query))
                return
            if path == "/api/training/data-pipeline-diagnosis":
                self._send_json(_data_pipeline_diagnosis(config, db, query))
                return
            if path == "/api/training/relation-repair-audit":
                self._send_json(_relation_repair_audit(config, db, query))
                return
            if path == "/api/training/label-quality-v2":
                self._send_json(_label_quality_v2(config, db, query))
                return
            if path == "/api/training/bitget-cost-model-audit":
                self._send_json(_bitget_cost_model_audit(config, db, query))
                return
            if path == "/api/training/cost-model-inventory":
                self._send_json(_cost_model_inventory(config, db, query))
                return
            if path == "/api/training/margin-mode-audit":
                self._send_json(_margin_mode_audit(config, db, query))
                return
            if path == "/api/training/core-corrections":
                self._send_json(_core_corrections(config, db, query))
                return
            if path == "/api/training/execution-safety-audit":
                self._send_json(_execution_safety_audit(config, db, query))
                return
            if path == "/api/training/net-rr-audit":
                self._send_json(_net_rr_audit(config, db, query))
                return
            if path == "/api/training/dynamic-exit-policy-audit":
                self._send_json(_dynamic_exit_policy_audit(config, db, query))
                return
            if path == "/api/training/structural-stop-audit":
                self._send_json(_structural_stop_audit(config, db, query))
                return
            if path == "/api/training/operational-intelligence-audit":
                self._send_json(_operational_intelligence_audit(config, db, query))
                return
            if path == "/api/training/exit-policy-v3-backtest":
                self._send_json(_exit_policy_v3_backtest(config, db, query))
                return
            if path == "/api/training/sudden-move-detector":
                self._send_json(_sudden_move_detector(config, db, query))
                return
            if path == "/api/training/pre-move-v2":
                self._send_json(_pre_move_v2(config, db, query))
                return
            if path == "/api/training/walk-forward-validator":
                self._send_json(_walk_forward_validator(config, db, query))
                return
            if path == "/api/training/anti-overfit-v2":
                self._send_json(_anti_overfit_v2(config, db, query))
                return
            if path == "/api/training/candidate-promotion-v2":
                self._send_json(_candidate_promotion_v2(config, db, query))
                return
            if path == "/api/training/shadow-strategy-simulator":
                self._send_json(_shadow_strategy_simulator(config, db, query))
                return
            if path == "/api/training/strategy-research-library":
                self._send_json(_strategy_research_library(config, db, query))
                return
            if path == "/api/training/real-strategy-backtest":
                self._send_json(_real_strategy_backtest(config, db, query))
                return
            if path == "/api/training/ohlcv-replay-loader-audit":
                self._send_json(_ohlcv_replay_loader_audit(config, db, query))
                return
            if path == "/api/training/duplicate-module-audit":
                self._send_json(_duplicate_module_audit(config, db, query))
                return
            if path == "/api/training/runtime-optimization-proposal":
                self._send_json(_runtime_optimization_proposal(config, db, query))
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
            if path == "/api/training/research-cockpit":
                self._send_json(_research_cockpit(config, db, query))
                return
            if path == "/api/training/cost-stress":
                self._send_json(_cost_stress(config, db, query))
                return
            if path == "/api/training/profit-lock-lab":
                self._send_json(_profit_lock_lab(config, db, query))
                return
            if path == "/api/training/fast-exit-lab":
                self._send_json(_fast_exit_lab(config, db, query))
                return
            if path == "/api/training/time-death-reducer-lab":
                self._send_json(_time_death_reducer_lab(config, db, query))
                return
            if path == "/api/training/trade-replay":
                self._send_json(_trade_replay(config, db, query))
                return
            if path == "/api/training/final-policy-builder":
                self._send_json(_final_policy_builder(config, db, query))
                return
            if path in {"/api/training/time-exit-autopsy-v2", "/api/time-exit-autopsy-v2"}:
                self._send_json(_phase8_research_endpoint(config, db, query, "time_exit_autopsy_v2"))
                return
            if path in {"/api/training/dynamic-hold-lab", "/api/dynamic-hold-lab"}:
                self._send_json(_phase8_research_endpoint(config, db, query, "dynamic_hold_lab"))
                return
            if path in {"/api/training/entry-exhaustion-lab", "/api/entry-exhaustion-lab"}:
                self._send_json(_phase8_research_endpoint(config, db, query, "entry_exhaustion_lab"))
                return
            if path in {"/api/training/reversal-candidate-lab", "/api/reversal-candidate-lab"}:
                self._send_json(_phase8_research_endpoint(config, db, query, "reversal_candidate_lab"))
                return
            if path in {"/api/training/exit-policy-v2", "/api/exit-policy-v2"}:
                self._send_json(_phase8_research_endpoint(config, db, query, "exit_policy_v2"))
                return
            if path in {"/api/training/phase8-candidate-validator", "/api/phase8-candidate-validator"}:
                self._send_json(_phase8_research_endpoint(config, db, query, "phase8_candidate_validator"))
                return
            if path in {"/api/training/phase8-cost-stress", "/api/phase8-cost-stress"}:
                self._send_json(_phase8_research_endpoint(config, db, query, "phase8_cost_stress"))
                return
            if path in {"/api/training/dot-regime-diagnosis", "/api/dot-regime-diagnosis"}:
                self._send_json(_phase9_research_endpoint(config, db, query, "dot_regime_diagnosis"))
                return
            if path in {"/api/training/dot-regime-filter-lab", "/api/dot-regime-filter-lab"}:
                self._send_json(_phase9_research_endpoint(config, db, query, "dot_regime_filter_lab"))
                return
            if path in {"/api/training/phase9-paper-readiness", "/api/phase9-paper-readiness"}:
                self._send_json(_phase9_research_endpoint(config, db, query, "phase9_paper_readiness"))
                return
            if path in {"/api/training/net-profit-lock-lab", "/api/net-profit-lock-lab"}:
                self._send_json(_phase9_research_endpoint(config, db, query, "net_profit_lock_lab"))
                return
            if path in {"/api/training/fast-signal-shadow", "/api/fast-signal-shadow"}:
                self._send_json(_phase9_research_endpoint(config, db, query, "fast_signal_shadow"))
                return
            if path in {"/api/training/research-pack", "/api/research-pack"}:
                payload = _research_pack_endpoint(config, db, query)
                fmt = (query.get("format") or ["json"])[0].lower()
                if fmt == "text":
                    self._send_text(str(payload.get("text") or ""))
                else:
                    self._send_json(payload)
                return
            if path in {"/api/training/research-pack-v5", "/api/research-pack-v5"}:
                payload = _research_pack_v5_endpoint(config, db, query)
                fmt = (query.get("format") or ["json"])[0].lower()
                if fmt == "text":
                    self._send_text(str(payload.get("text") or ""))
                else:
                    self._send_json(payload)
                return
            if path in {"/api/training/ohlcv-freshness-status", "/api/research/ohlcv-freshness-status"}:
                self._send_json(_v5_ohlcv_freshness_status(config, db, query))
                return
            if path in {"/api/training/ohlcv-freshness-refresh-dry", "/api/research/ohlcv-freshness-refresh-dry"}:
                self._send_json(_v5_ohlcv_freshness_refresh_dry(config, db, query))
                return
            if path in {"/api/training/training-clean-view-audit", "/api/research/training-clean-view-audit"}:
                self._send_json(_v5_training_clean_view_audit(config, db, query))
                return
            if path in {"/api/training/shadow-multi-trade-status", "/api/research/shadow-multi-trade-status"}:
                self._send_json(_v5_shadow_multi_trade_status(config, db, query))
                return
            if path in {"/api/training/capital-leverage-sim", "/api/research/capital-leverage-sim"}:
                self._send_json(_v5_capital_leverage_sim(config, db, query))
                return
            if path in {"/api/training/fee-aware-exit-trainer", "/api/research/fee-aware-exit-trainer"}:
                self._send_json(_v5_fee_aware_exit_trainer(config, db, query))
                return
            if path in {"/api/training/strategy-research-enhancer", "/api/research/strategy-research-enhancer"}:
                self._send_json(_v51_strategy_research_enhancer(config, db, query))
                return
            if path in {"/api/training/clean-research-metrics", "/api/research/clean-research-metrics"}:
                self._send_json(_v6_clean_research_metrics(config, db, query))
                return
            if path in {"/api/training/data-pipeline-root-cause", "/api/research/data-pipeline-root-cause"}:
                self._send_json(_v7_data_pipeline_root_cause(config, db, query))
                return
            if path in {"/api/training/clean-strategy-lab", "/api/research/clean-strategy-lab"}:
                self._send_json(_v7_clean_strategy_lab(config, db, query))
                return
            if path in {"/api/training/capital-scaling-simulator", "/api/research/capital-scaling-simulator"}:
                self._send_json(_v7_capital_scaling_simulator(config, db, query))
                return
            if path in {"/api/research-pack-v7", "/api/training/research-pack-v7"}:
                payload = _v7_research_pack(config, db, query)
                fmt = (query.get("format") or ["json"])[0].lower()
                if fmt == "text":
                    self._send_text(str(payload.get("text") or ""))
                else:
                    self._send_json(payload)
                return
            if path == "/api/research/duplicate-guard-hook-status":
                self._send_json(_v75_duplicate_guard_hook_status(config, db, query))
                return
            if path == "/api/research/funding-cost-model":
                self._send_json(_v75_funding_cost_model(config, db, query))
                return
            if path == "/api/research/liquidation-model-bitget":
                self._send_json(_v75_liquidation_model_bitget(config, db, query))
                return
            if path == "/api/research/walk-forward-v2":
                self._send_json(_v75_walk_forward_v2(config, db, query))
                return
            if path in {"/api/research-pack-v7-5", "/api/training/research-pack-v7-5"}:
                payload = _v75_research_pack(config, db, query)
                fmt = (query.get("format") or ["json"])[0].lower()
                if fmt == "text":
                    self._send_text(str(payload.get("text") or ""))
                else:
                    self._send_json(payload)
                return
            if path == "/api/research/auto-data-enrichment-status":
                self._send_json(_v8v9_auto_data_enrichment(config, db, query))
                return
            if path == "/api/research/exit-intelligence-lab":
                self._send_json(_v8v9_exit_intelligence(config, db, query))
                return
            if path == "/api/research/strategy-experiment-registry":
                self._send_json(_v8v9_strategy_experiment_registry(config, db, query))
                return
            if path == "/api/research/shadow-candidate-lifecycle":
                self._send_json(_v8v9_shadow_candidate_lifecycle(config, db, query))
                return
            if path == "/api/research/validation-gates-v9":
                self._send_json(_v8v9_validation_gates(config, db, query))
                return
            if path == "/api/research/bidirectional-funnel":
                self._send_json(_v82_bidirectional_funnel(config, db, query))
                return
            if path == "/api/research/score-asymmetry-audit":
                self._send_json(_v82_score_asymmetry(config, db, query))
                return
            if path == "/api/research/trend-campaign-sim":
                self._send_json(_v82_trend_campaign_sim(config, db, query))
                return
            if path == "/api/research/profit-lock-sim":
                self._send_json(_v82_profit_lock_sim(config, db, query))
                return
            if path == "/api/research/research-pack-bidirectional-v1":
                self._send_json(_v82_research_pack(config, db, query))
                return
            if path == "/api/research/counterfactual-training-export":
                self._send_json(_v824_counterfactual_training_export(config, db, query))
                return
            if path == "/api/research/counterfactual-training-download":
                payload = _v824_counterfactual_training_download(config, db, query)
                if payload.get("status") == "OK" and payload.get("zip_bytes") is not None:
                    self._send_zip(
                        payload["zip_bytes"],
                        filename=payload.get("zip_name", "counterfactual_training_exports_v1.zip"),
                    )
                else:
                    self._send_json(payload)
                return
            if path == "/api/research/counterfactual-training-summary":
                self._send_json(_v824_counterfactual_training_summary(config, db, query))
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

        def _send_zip(self, payload: bytes, status: int = 200, filename: str = "export.zip") -> None:
            """V8.2.4 — stream a sanitised ZIP to the client."""
            self.send_response(status)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_html(self, html: str, status: int = 200) -> None:
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_static(self, path: str) -> None:
            filename = path.rsplit("/", 1)[-1]
            if filename not in {"dashboard.css", "dashboard.js"}:
                self._send_status(404, "not found")
                return
            file_path = STATIC_DIR / filename
            try:
                body = file_path.read_bytes()
            except OSError:
                self._send_status(404, "not found")
                return
            content_type = "text/css; charset=utf-8" if filename.endswith(".css") else "application/javascript; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_status(self, status: int, message: str) -> None:
            self._send_json({"error": message}, status=status)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server_ready = threading.Event()
    server_box: dict[str, HTTPServer] = {}

    def run() -> None:
        try:
            httpd = HTTPServer(("0.0.0.0", port), Handler)
            server_box["server"] = httpd
            server_ready.set()
            httpd.serve_forever()
        except OSError as exc:
            server_ready.set()
            logger.warning("Health server no pudo iniciar en puerto %s: %s", port, exc)

    thread = threading.Thread(target=run, name="health-server", daemon=True)
    thread.start()
    setattr(thread, "server_ready", server_ready)
    setattr(thread, "server_box", server_box)
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


def _ati_shadow_status_payload(report_dir: Path | None = None) -> dict[str, Any]:
    """Read only the whitelisted ATI report contract; never runs a heavy lab."""
    declared_root = report_dir or _ATI_REPORT_DIR
    root_unsafe = declared_root.exists() and declared_root.is_symlink()
    root = declared_root.resolve()

    def read_object(name: str) -> dict[str, Any]:
        if root_unsafe:
            return {}
        path = root / name
        if path.parent.resolve() != root or not path.is_file() or path.is_symlink():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    health = read_object("ati_health.json")
    summary = read_object("ati_summary.json")
    forward = read_object("ati_forward_state.json")
    ran_at = health.get("last_run_at") or summary.get("generated_at")
    report_age_seconds = None
    if ran_at:
        try:
            parsed = datetime.fromisoformat(str(ran_at).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            report_age_seconds = max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())
        except ValueError:
            report_age_seconds = None
    dataset_available_at = (
        health.get("dataset_available_at")
        or summary.get("dataset_available_at")
        or health.get("dataset_last_bar_at")
    )
    dataset_age_seconds = None
    if dataset_available_at:
        try:
            parsed_data = datetime.fromisoformat(str(dataset_available_at).replace("Z", "+00:00"))
            if parsed_data.tzinfo is None:
                parsed_data = parsed_data.replace(tzinfo=timezone.utc)
            dataset_age_seconds = max(
                0.0,
                (datetime.now(timezone.utc) - parsed_data.astimezone(timezone.utc)).total_seconds(),
            )
        except ValueError:
            dataset_age_seconds = None
    status = str(health.get("status") or "NO_DATA")
    stale = dataset_age_seconds is None or dataset_age_seconds > 30 * 60
    if status == "HEALTHY" and stale:
        status = "DEGRADED"
    allowed_setup_fields = {
        "setup_id", "setup_variant", "trades", "net_ev", "gross_ev",
        "profit_factor", "win_rate", "max_drawdown", "average_mfe",
        "average_mae", "result_status", "ci95_lower", "ci95_upper",
        "gross_pnl", "net_pnl", "total_cost", "median_holding_bars",
        "top_3_profit_concentration",
    }
    by_setup = [
        {key: row.get(key) for key in allowed_setup_fields}
        for row in (summary.get("by_setup") or [])
        if isinstance(row, dict)
    ]
    allowed_group_fields = allowed_setup_fields | {"symbol", "regime", "policy"}

    def safe_group(name: str) -> list[dict[str, Any]]:
        return [
            {key: row.get(key) for key in allowed_group_fields}
            for row in (summary.get(name) or [])
            if isinstance(row, dict)
        ]

    source_receipts = []
    for row in summary.get("dataset_receipts") or []:
        if not isinstance(row, dict):
            continue
        source_receipts.append({
            "symbol": row.get("symbol"),
            "venue": row.get("venue"),
            "generation_id": row.get("generation_id"),
            "verification_status": row.get("verification_status"),
            "n_bars": row.get("n_bars") or row.get("actual_bars"),
            "coverage_ratio": row.get("coverage_ratio"),
            "source_file_mtime": row.get("source_file_mtime"),
            "source_file_age_seconds": row.get("source_file_age_seconds"),
        })
    overall = summary.get("overall_baseline") if isinstance(summary.get("overall_baseline"), dict) else {}
    metric_ran_at = summary.get("generated_at")
    metric_age_seconds = None
    if metric_ran_at:
        try:
            parsed_metric = datetime.fromisoformat(str(metric_ran_at).replace("Z", "+00:00"))
            if parsed_metric.tzinfo is None:
                parsed_metric = parsed_metric.replace(tzinfo=timezone.utc)
            metric_age_seconds = max(
                0.0,
                (datetime.now(timezone.utc) - parsed_metric.astimezone(timezone.utc)).total_seconds(),
            )
        except ValueError:
            metric_age_seconds = None
    return {
        "status": status,
        "result_status": summary.get("status") or forward.get("status") or "NO_DATA",
        "last_run_at": ran_at,
        "age_seconds": report_age_seconds,
        "report_age_seconds": report_age_seconds,
        "dataset_age_seconds": dataset_age_seconds,
        "stale": stale,
        "stale_reason": "dataset_missing_or_older_than_30m" if stale else None,
        "dataset_last_bar_at": health.get("dataset_last_bar_at"),
        "dataset_available_at": dataset_available_at,
        "dataset_snapshot_sha256": health.get("dataset_snapshot_sha256") or summary.get("dataset_snapshot_sha256"),
        "dataset_source_mode": summary.get("dataset_source_mode") or health.get("source_mode"),
        "dataset_source_paths": summary.get("dataset_source_paths") or health.get("source_paths") or {},
        "dataset_sources": source_receipts,
        "history_days": summary.get("history_days"),
        "policy_version": (summary.get("policy") or {}).get("policy_version"),
        "feature_version": (summary.get("policy") or {}).get("feature_version"),
        "signals_total": int(summary.get("signals_total") or 0),
        "historical_signals": int(summary.get("signals_total") or 0),
        "forward_signals": int(forward.get("signals_total") or health.get("signals_total") or 0),
        "shadow_candidates": int(summary.get("shadow_candidates") or 0),
        "open_positions": int(forward.get("open_positions") or health.get("open_positions") or 0),
        "historical_trades": int(summary.get("baseline_trades") or 0),
        "closed_shadow_trades": int(forward.get("closed_outcomes", 0) or 0),
        "net_ev": overall.get("net_ev"),
        "profit_factor": overall.get("profit_factor"),
        "win_rate": overall.get("win_rate"),
        "max_drawdown": overall.get("max_drawdown"),
        "average_mfe": overall.get("average_mfe"),
        "average_mae": overall.get("average_mae"),
        "median_holding_bars": overall.get("median_holding_bars"),
        "gross_pnl": overall.get("gross_pnl"),
        "net_pnl": overall.get("net_pnl"),
        "total_cost": overall.get("total_cost"),
        "fees": overall.get("fees"),
        "slippage": overall.get("slippage"),
        "funding": overall.get("funding"),
        "metric_ran_at": metric_ran_at,
        "metric_age_seconds": metric_age_seconds,
        "observer_last_cycle_at": health.get("observer_last_cycle_at") or forward.get("observer_last_cycle_at"),
        "observer_cycle_duration_seconds": health.get("observer_cycle_duration_seconds") or forward.get("observer_cycle_duration_seconds"),
        "observer_status": health.get("observer_status") or forward.get("observer_status"),
        "boundary_status": health.get("boundary_status") or forward.get("boundary_status"),
        "shadow_phase": health.get("shadow_phase") or forward.get("shadow_phase"),
        "reconciliation_status": health.get("reconciliation_status") or forward.get("reconciliation_status"),
        "cache_status": health.get("cache_status") or forward.get("cache_status") or "STALE_UNKNOWN",
        "by_setup": by_setup,
        "by_symbol": safe_group("by_symbol"),
        "by_regime": safe_group("by_regime"),
        "trailing_grid": safe_group("trailing_grid"),
        "blockers": [str(item) for item in (summary.get("blockers") or [])],
        "last_error": health.get("last_error"),
        "cli_replay": "python -m app.research_lab ati-shadow-replay-v2 --symbols BTCUSDT,ETHUSDT",
        "cli_forward": "python -m app.research_lab ati-shadow-forward-once-v2 --symbols BTCUSDT,ETHUSDT",
        "mode": "SHADOW_RESEARCH_ONLY",
        "research_only": True,
        "shadow_only": True,
        "paper_execution_used": False,
        "paper_filter_enabled": False,
        "live_trading": False,
        "can_send_real_orders": False,
        "activation": "disabled",
        "edge_validated": False,
        "paper_ready": False,
        "live_ready": False,
        "final_recommendation": "NO LIVE",
    }


def _research_components_status_payload(state: HealthState) -> dict[str, Any]:
    """Build a cheap, artifact-only component view for `/health`."""
    now = datetime.now(timezone.utc)

    def read_dashboard() -> dict[str, Any]:
        path = _RESEARCH_DASHBOARD_V1043C
        root = path.parent.resolve()
        if path.parent.resolve() != root or not path.is_file() or path.is_symlink():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def age(value: Any) -> float | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (now - parsed.astimezone(timezone.utc)).total_seconds())

    dashboard = read_dashboard()
    collector = dashboard.get("health") if isinstance(dashboard.get("health"), dict) else {}
    persistent = (
        dashboard.get("persistent_health")
        if isinstance(dashboard.get("persistent_health"), dict) else {}
    )
    sources = (
        dashboard.get("source_compare_3way")
        if isinstance(dashboard.get("source_compare_3way"), dict) else {}
    )
    watcher = (
        dashboard.get("dashboard_watch")
        if isinstance(dashboard.get("dashboard_watch"), dict) else {}
    )
    slow = dashboard.get("slow_metrics") if isinstance(dashboard.get("slow_metrics"), dict) else {}
    watcher_age = age(watcher.get("last_refresh_at"))
    watcher_stale = watcher_age is None or watcher_age > max(
        120.0, 3.0 * float(watcher.get("interval_seconds") or 30.0),
    )
    ati = _ati_shadow_status_payload()
    collector_status = str(collector.get("status") or "NO_DATA")
    persistent_status = str(persistent.get("status") or "NO_DATA")
    dataset_ready = bool(sources.get("ready_for_shadow_forward"))
    heavy_stale = bool(slow.get("strategy_stale", True) or slow.get("exit_stale", True))
    components = {
        "mode": {
            "status": "PAPER_RESEARCH" if str(state.mode).lower() != "live" else "ERROR",
            "mode": state.mode,
        },
        "safety": {
            "status": "HEALTHY" if str(state.mode).lower() != "live" else "ERROR",
            "paper_trading": True,
            "live_trading": False,
            "dry_run": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "final_recommendation": "NO LIVE",
        },
        "bot": {
            "status": "ERROR" if state.circuit_breaker else "HEALTHY",
            "http_liveness": "ok",
            "circuit_breaker": bool(state.circuit_breaker),
            "open_positions": int(state.open_positions),
        },
        "collectors": {
            "status": "HEALTHY" if collector_status == "HEALTHY" and persistent_status == "HEALTHY" else "DEGRADED",
            "rest_collector_status": collector_status,
            "persistent_ws_status": persistent_status,
            "persistent_ws_age_seconds": persistent.get("age_seconds"),
            "uses_api_keys": False,
            "can_send_real_orders": False,
        },
        "datasets": {
            "status": "HEALTHY" if dataset_ready else "DEGRADED",
            "recommended_source": sources.get("recommended_source"),
            "ready_for_shadow_forward": dataset_ready,
            "rest": sources.get("rest") or {},
            "ws": sources.get("ws") or {},
            "ws_persistent": sources.get("ws_persistent") or {},
            "metric_ran_at": sources.get("ran_at"),
            "metric_age_seconds": age(sources.get("ran_at")),
        },
        "dashboard_watcher": {
            "status": "DEGRADED" if watcher_stale else "HEALTHY",
            "watcher_status": watcher.get("watcher_status") or "NO_DATA",
            "last_cycle_at": watcher.get("last_refresh_at"),
            "age_seconds": watcher_age,
            "last_error": watcher.get("last_error"),
        },
        "heavy_research": {
            "status": "DEGRADED" if heavy_stale else "HEALTHY",
            "strategy_age_seconds": slow.get("strategy_age_seconds"),
            "exit_age_seconds": slow.get("exit_age_seconds"),
            "strategy_stale": bool(slow.get("strategy_stale", True)),
            "exit_stale": bool(slow.get("exit_stale", True)),
            "cache_status": slow.get("source_metrics_cache") or "STALE_UNKNOWN",
        },
        "ati_shadow": ati,
    }
    statuses = [str(item.get("status") or "NO_DATA") for item in components.values()]
    if any(item == "ERROR" for item in statuses):
        overall = "ERROR"
    elif any(item not in {"HEALTHY", "PAPER_RESEARCH"} for item in statuses):
        overall = "DEGRADED"
    else:
        overall = "HEALTHY"
    return {
        "overall_status": overall,
        "components": components,
        "status_scope": "ARTIFACT_AND_RUNTIME_COMPONENTS",
        "generated_at": now.isoformat(),
    }


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
    return _lab_payload(config, db, query, "score calibration unavailable", ".score_calibration", "ScoreCalibration")


def _candidate_incubator(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "candidate incubator unavailable", ".candidate_incubator", "CandidateIncubator")


def _training_data_integrity(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "training data integrity unavailable", ".training_data_integrity", "TrainingDataIntegrity")


def _worker_health_audit(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "worker health audit unavailable", ".worker_health_audit", "WorkerHealthAudit")


def _data_vault_audit(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "data vault audit unavailable", ".data_vault_audit", "DataVaultAudit")


def _dashboard_data_binding_audit(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "dashboard data binding audit unavailable", ".dashboard_data_binding_audit", "DashboardDataBindingAudit")


def _data_pipeline_diagnosis(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "data pipeline diagnosis unavailable", ".data_pipeline_diagnosis", "DataPipelineDiagnosis")


def _relation_repair_audit(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "relation repair audit unavailable", ".relation_repair_audit", "RelationRepairAudit")


def _label_quality_v2(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "label quality v2 unavailable", ".label_quality_v2", "LabelQualityV2")


def _bitget_cost_model_audit(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "bitget cost model audit unavailable", ".bitget_cost_model_audit", "BitgetCostModelAudit")


def _cost_model_inventory(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    del query
    if config is None or db is None:
        return {"error": "cost model inventory unavailable", "final_recommendation": "NO LIVE"}
    started = time.perf_counter()
    try:
        from .bitget_cost_model_audit import BitgetCostModelAudit

        lab = BitgetCostModelAudit(config, db)
        payload = lab.inventory()
        payload["text"] = lab.inventory_text()
    except Exception as exc:
        return {"error": str(exc)[:300], "final_recommendation": "NO LIVE"}
    payload = dict(payload)
    payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload["cache"] = {
        "key": "cost_model_inventory",
        "created_at": payload["generated_at"],
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "status": "ok",
    }
    payload["final_recommendation"] = "NO LIVE"
    return payload


def _margin_mode_audit(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "margin mode audit unavailable", ".margin_mode_audit", "MarginModeAudit")


def _core_corrections(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "core corrections unavailable", ".core_corrections", "CoreCorrections")


def _execution_safety_audit(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    del query
    if config is None:
        return {"error": "execution safety audit unavailable", "final_recommendation": "NO LIVE"}
    try:
        from .execution_safety import ExecutionSafetyAudit

        text = ExecutionSafetyAudit(config, db).to_text()
    except Exception as exc:
        return {"error": str(exc)[:300], "final_recommendation": "NO LIVE"}
    return _text_payload("execution_safety_audit", text)


def _net_rr_audit(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    del config, db
    from .execution_safety import net_rr_audit_text

    return _text_payload("net_rr_audit", net_rr_audit_text(hours=_query_int(query, "hours", 24)))


def _dynamic_exit_policy_audit(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    del config, db
    from .execution_safety import dynamic_exit_policy_audit_text

    return _text_payload("dynamic_exit_policy_audit", dynamic_exit_policy_audit_text(hours=_query_int(query, "hours", 24)))


def _structural_stop_audit(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    del config, db
    from .execution_safety import structural_stop_audit_text

    return _text_payload("structural_stop_audit", structural_stop_audit_text(hours=_query_int(query, "hours", 24)))


def _operational_intelligence_audit(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "operational intelligence unavailable", ".operational_intelligence", "OperationalIntelligenceAudit")


def _exit_policy_v3_backtest(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "exit policy v3 backtest unavailable", ".exit_policy_v3_backtest", "ExitPolicyV3Backtest")


def _sudden_move_detector(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "sudden move detector unavailable", ".sudden_move_detector", "SuddenMoveDetector")


def _pre_move_v2(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "pre-move v2 unavailable", ".pre_move_intelligence_v2", "PreMoveIntelligenceV2")


def _walk_forward_validator(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "walk-forward validator unavailable", ".walk_forward_validator", "WalkForwardValidator")


def _anti_overfit_v2(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "anti overfit v2 unavailable", ".anti_overfit_matrix_v2", "AntiOverfitMatrixV2")


def _candidate_promotion_v2(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "candidate promotion v2 unavailable", ".candidate_promotion_v2", "CandidatePromotionV2")


def _shadow_strategy_simulator(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "shadow strategy simulator unavailable", ".shadow_strategy_simulator", "ShadowStrategySimulator")


def _strategy_research_library(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "strategy research library unavailable", ".strategy_research_library", "StrategyResearchLibrary")


def _real_strategy_backtest(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    from .real_strategy_backtester import real_strategy_backtest_text

    hours = _query_int(query, "hours", 72)
    text = real_strategy_backtest_text(config, db, hours=hours)
    return _text_payload("real_strategy_backtester", text)


def _ohlcv_replay_loader_audit(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    from .ohlcv_replay_loader import ohlcv_replay_loader_audit_text

    hours = _query_int(query, "hours", 72)
    text = ohlcv_replay_loader_audit_text(config, db, hours=hours)
    return _text_payload("ohlcv_replay_loader_audit", text)


def _duplicate_module_audit(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    del config, db, query
    from .duplicate_module_audit import duplicate_module_audit_text

    return _text_payload("duplicate_module_audit", duplicate_module_audit_text())


def _runtime_optimization_proposal(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _lab_payload(config, db, query, "runtime optimization proposal unavailable", ".runtime_optimization_proposal", "RuntimeOptimizationProposal")


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


def _text_payload(key: str, text: str) -> dict[str, Any]:
    payload = {
        "text": text,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "final_recommendation": "NO LIVE",
    }
    payload["cache"] = {"key": key, "created_at": payload["generated_at"], "duration_ms": 0, "status": "ok"}
    _cache_dashboard_lab(key, payload, "ok", 0)
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


def _research_cockpit(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    """Phase 7B — Research Cockpit JSON payload."""
    del query
    if config is None or db is None:
        return {
            "error": "research cockpit unavailable",
            "final_recommendation": "NO LIVE",
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
        }
    try:
        from .research_cockpit import build_cockpit_state, export_cockpit_json, render_cockpit_text

        state = build_cockpit_state(config, db, mode="paper")
        payload = state.as_dict()
        payload["text"] = render_cockpit_text(state)
        payload["json"] = export_cockpit_json(state)
        payload["final_recommendation"] = "NO LIVE"
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        return payload
    except Exception as exc:
        return {
            "error": str(exc)[:300],
            "final_recommendation": "NO LIVE",
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
        }


def _cost_stress(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    """Phase 7B — Cost Stress evaluation under multiple fee scenarios."""
    hours = _query_int(query, "hours", 720)
    timeframe = (query.get("timeframe") or ["5m"])[0]
    symbols_arg = (query.get("symbols") or [""])[0]
    symbols = [s.strip().upper() for s in symbols_arg.split(",") if s.strip()] or None
    if config is None or db is None:
        return {
            "error": "cost stress unavailable",
            "final_recommendation": "NO LIVE",
            "research_only": True,
            "can_send_real_orders": False,
        }
    try:
        from .backtest_breakdown import collect_trade_records
        from .cost_stress import evaluate_cost_stress, render_cost_stress_text

        records = collect_trade_records(config, db, hours=hours, symbols=symbols, timeframe=timeframe)
        grosses = [r.gross_return_pct for r in records]
        report = evaluate_cost_stress(grosses)
        return {
            "hours": hours,
            "timeframe": timeframe,
            "symbols": symbols or [],
            "trades": int(report.trades),
            "scenarios": [
                {
                    "name": s.name,
                    "cost_pct": s.cost_pct,
                    "net_ev": s.net_ev,
                    "net_pf": s.net_pf,
                    "win_rate": s.win_rate,
                    "trades": s.trades,
                }
                for s in report.scenarios
            ],
            "cost_stress_status": report.cost_stress_status,
            "reasons": list(report.reasons),
            "text": render_cost_stress_text(report),
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "final_recommendation": "NO LIVE",
        }
    except Exception as exc:
        return {
            "error": str(exc)[:300],
            "final_recommendation": "NO LIVE",
            "research_only": True,
            "can_send_real_orders": False,
        }


def _exit_lab_run(
    config: Any | None,
    db: Any | None,
    query: dict[str, list[str]],
    runner: str,
) -> dict[str, Any]:
    hours = _query_int(query, "hours", 720)
    timeframe = (query.get("timeframe") or ["5m"])[0]
    symbol = (query.get("symbol") or ["BTCUSDT"])[0].upper()
    if config is None or db is None:
        return {
            "error": f"{runner} unavailable",
            "final_recommendation": "NO LIVE",
            "research_only": True,
            "can_send_real_orders": False,
        }
    try:
        from . import exit_labs as exit_labs_mod

        func = getattr(exit_labs_mod, runner)
        report = func(config, db, symbol=symbol, hours=hours, timeframe=timeframe)
        best_policy = ""
        best_delta = 0.0
        for c in report.comparisons:
            if c.policy_name == "baseline":
                continue
            if c.decision == "IMPROVES_BASELINE" and c.delta_ev_vs_baseline > best_delta:
                best_policy = c.policy_name
                best_delta = c.delta_ev_vs_baseline
        return {
            "lab_name": report.lab_name,
            "symbol": report.symbol,
            "hours": report.hours,
            "timeframe": report.timeframe,
            "baseline_trades": report.baseline_trades,
            "baseline_net_ev": report.baseline_net_ev,
            "comparisons": [c.as_dict() for c in report.comparisons],
            "best_policy": best_policy,
            "best_delta_ev": best_delta,
            "no_lookahead_status": report.no_lookahead_status,
            "stop_tp_same_bar_rule": report.stop_tp_same_bar_rule,
            "text": exit_labs_mod.render_exit_lab_text(report),
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "final_recommendation": "NO LIVE",
        }
    except Exception as exc:
        return {
            "error": str(exc)[:300],
            "final_recommendation": "NO LIVE",
            "research_only": True,
            "can_send_real_orders": False,
        }


def _profit_lock_lab(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _exit_lab_run(config, db, query, "run_profit_lock_lab")


def _fast_exit_lab(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _exit_lab_run(config, db, query, "run_fast_exit_lab")


def _time_death_reducer_lab(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    return _exit_lab_run(config, db, query, "run_time_death_reducer_lab")


def _trade_replay(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    """Phase 7B — Trade Replay JSON for the dashboard chart view."""
    hours = _query_int(query, "hours", 72)
    timeframe = (query.get("timeframe") or ["5m"])[0]
    symbol = (query.get("symbol") or ["BTCUSDT"])[0].upper()
    max_candles = _query_int(query, "max_candles", 600)
    max_trades = _query_int(query, "max_trades", 200)
    if config is None or db is None:
        return {
            "error": "trade replay unavailable",
            "final_recommendation": "NO LIVE",
            "research_only": True,
            "can_send_real_orders": False,
        }
    try:
        from .trade_replay_export import build_replay_payload, render_replay_summary

        payload_obj = build_replay_payload(
            config, db,
            symbol=symbol, hours=hours, timeframe=timeframe,
            max_candles=max_candles, max_trades=max_trades,
        )
        payload = payload_obj.as_dict()
        payload["text"] = render_replay_summary(payload_obj)
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return {
            "error": str(exc)[:300],
            "final_recommendation": "NO LIVE",
            "research_only": True,
            "can_send_real_orders": False,
        }


def _final_policy_builder(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    """Phase 7B — final research policy builder with optional enriched gates."""
    hours = _query_int(query, "hours", 720)
    timeframe = (query.get("timeframe") or ["5m"])[0]
    enriched_flag = (query.get("enriched") or ["0"])[0] in {"1", "true", "yes"}
    include_cost_stress = (query.get("include_cost_stress") or [str(int(enriched_flag))])[0] in {"1", "true", "yes"} or enriched_flag
    if config is None or db is None:
        return {
            "error": "final policy builder unavailable",
            "final_recommendation": "NO LIVE",
            "research_only": True,
            "can_send_real_orders": False,
        }
    try:
        from .backtest_breakdown import build_breakdown, collect_trade_records
        from .final_research_policy_builder import (
            PolicyBuildInput,
            build_policy,
            render_policy_text,
        )

        records = collect_trade_records(config, db, hours=hours, timeframe=timeframe)
        breakdown = build_breakdown(records, hours=hours, timeframe=timeframe)
        inputs = PolicyBuildInput(
            breakdown=breakdown,
            data_quality_status="UNKNOWN",
            label_quality_status="UNKNOWN",
            walk_forward_status="NOT_RUN",
            time_exit_autopsy_status="UNKNOWN",
            dynamic_hold_status="UNKNOWN",
            profit_protection_status="UNKNOWN",
            entry_exhaustion_status="UNKNOWN",
            anti_overfit_status="UNKNOWN",
            reversal_lab_status="RESEARCH_ONLY",
            phase8_candidate_validator_status="UNKNOWN",
            validation_hours=hours,
        )
        if include_cost_stress:
            try:
                from .cost_stress import evaluate_cost_stress

                grosses = [r.gross_return_pct for r in records]
                stress = evaluate_cost_stress(grosses)
                inputs.cost_stress_status = stress.cost_stress_status
                inputs.cost_stress_reasons = list(stress.reasons)
            except Exception:
                pass
        policy = build_policy(inputs)
        return {
            "hours": hours,
            "timeframe": timeframe,
            "enriched": bool(enriched_flag),
            "decision": policy.decision,
            "candidate_policy_id": policy.candidate_policy_id,
            "reasons": list(policy.reasons),
            "data_quality_status": policy.data_quality_status,
            "walk_forward_status": policy.walk_forward_status,
            "cost_stress_status": inputs.cost_stress_status,
            "cost_stress_reasons": list(inputs.cost_stress_reasons),
            "time_exit_autopsy_status": inputs.time_exit_autopsy_status,
            "dynamic_hold_status": inputs.dynamic_hold_status,
            "profit_protection_status": inputs.profit_protection_status,
            "entry_exhaustion_status": inputs.entry_exhaustion_status,
            "anti_overfit_status": inputs.anti_overfit_status,
            "phase8_candidate_validator_status": inputs.phase8_candidate_validator_status,
            "validation_hours": inputs.validation_hours,
            "net_ev": policy.net_ev,
            "net_pf": policy.net_pf,
            "tp_pct": policy.tp_pct,
            "sl_pct": policy.sl_pct,
            "time_pct": policy.time_pct,
            "text": render_policy_text(policy),
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "final_recommendation": "NO LIVE",
        }
    except Exception as exc:
        return {
            "error": str(exc)[:300],
            "final_recommendation": "NO LIVE",
            "research_only": True,
            "can_send_real_orders": False,
        }


def _phase8_research_endpoint(config: Any | None, db: Any | None, query: dict[str, list[str]], lab_name: str) -> dict[str, Any]:
    hours = _query_int(query, "hours", 72)
    timeframe = (query.get("timeframe") or ["5m"])[0]
    symbols = _query_symbols(query)
    allow_heavy = _query_bool(query, "allow_heavy", False)
    symbol_count = len(symbols or [])
    if not allow_heavy and (hours > 168 or symbol_count > 2):
        cli_command = _phase8_cli_command(lab_name)
        symbol_arg = ",".join(symbols or [])
        command = f"python -m app.research_lab {cli_command} --hours {hours} --timeframe {timeframe}"
        if symbol_arg:
            command += f" --symbols {symbol_arg}"
        return {
            "status": "HEAVY_RESEARCH_SKIPPED",
            "skipped_heavy": True,
            "reason": "phase8_endpoint_heavy_run_blocked_use_cli_or_allow_heavy",
            "lab_name": lab_name,
            "requested_hours": hours,
            "requested_timeframe": timeframe,
            "requested_symbols": symbols or [],
            "cli_command": command,
            "text": (
                "PHASE 8 HEAVY RESEARCH SKIPPED\n"
                f"lab: {lab_name}\n"
                f"requested_hours: {hours}\n"
                f"requested_symbols: {symbol_arg or 'default'}\n"
                f"run_cli: {command}\n"
                "research_only: true\n"
                "final_recommendation: NO LIVE"
            ),
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "final_recommendation": "NO LIVE",
        }
    if config is None or db is None:
        return {
            "error": f"{lab_name} unavailable",
            "final_recommendation": "NO LIVE",
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
        }
    try:
        if lab_name == "time_exit_autopsy_v2":
            from .time_exit_autopsy_v2 import render_time_exit_autopsy_v2_text, run_time_exit_autopsy_v2
            report = run_time_exit_autopsy_v2(config, db, hours=hours, timeframe=timeframe, symbols=symbols)
            payload = report.as_dict()
            payload["text"] = render_time_exit_autopsy_v2_text(report)
        elif lab_name == "dynamic_hold_lab":
            from .dynamic_hold_lab import render_dynamic_hold_lab_text, run_dynamic_hold_lab
            report = run_dynamic_hold_lab(config, db, hours=hours, timeframe=timeframe, symbols=symbols)
            payload = report.as_dict()
            payload["text"] = render_dynamic_hold_lab_text(report)
        elif lab_name == "entry_exhaustion_lab":
            from .entry_exhaustion_lab import render_entry_exhaustion_lab_text, run_entry_exhaustion_lab
            report = run_entry_exhaustion_lab(config, db, hours=hours, timeframe=timeframe, symbols=symbols)
            payload = report.as_dict()
            payload["text"] = render_entry_exhaustion_lab_text(report)
        elif lab_name == "reversal_candidate_lab":
            from .reversal_candidate_lab import render_reversal_candidate_lab_text, run_reversal_candidate_lab
            report = run_reversal_candidate_lab(config, db, hours=hours, timeframe=timeframe, symbols=symbols)
            payload = report.as_dict()
            payload["text"] = render_reversal_candidate_lab_text(report)
        elif lab_name == "exit_policy_v2":
            from .exit_policy_v2 import render_exit_policy_v2_text, run_exit_policy_v2
            report = run_exit_policy_v2(config, db, hours=hours, timeframe=timeframe, symbols=symbols)
            payload = report.as_dict()
            payload["text"] = render_exit_policy_v2_text(report)
        elif lab_name == "phase8_candidate_validator":
            from .phase8_candidate_validator import render_phase8_validator_text, run_phase8_candidate_validator
            report = run_phase8_candidate_validator(config, db, hours=hours, timeframe=timeframe, symbols=symbols)
            payload = report.as_dict()
            payload["text"] = render_phase8_validator_text(report)
        elif lab_name == "phase8_cost_stress":
            from .phase8_candidate_validator import phase8_cost_stress_text
            policy = (query.get("policy") or ["late_entry_block_plus_dynamic_hold"])[0]
            text = phase8_cost_stress_text(config, db, hours=hours, timeframe=timeframe, symbols=symbols, policy=policy)
            payload = {
                "status": "OK",
                "policy_name": policy,
                "text": text,
            }
        else:
            payload = {"error": "unknown phase8 lab"}
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return {
            "error": str(exc)[:300],
            "lab_name": lab_name,
            "final_recommendation": "NO LIVE",
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
        }


def _phase8_cli_command(lab_name: str) -> str:
    mapping = {
        "time_exit_autopsy_v2": "time-exit-autopsy-v2",
        "dynamic_hold_lab": "dynamic-hold-lab",
        "entry_exhaustion_lab": "entry-exhaustion-lab",
        "reversal_candidate_lab": "reversal-candidate-lab",
        "exit_policy_v2": "exit-policy-v2",
        "phase8_candidate_validator": "phase8-candidate-validator",
        "phase8_cost_stress": "phase8-cost-stress",
    }
    return mapping.get(lab_name, lab_name.replace("_", "-"))


def _phase9_research_endpoint(config: Any | None, db: Any | None, query: dict[str, list[str]], lab_name: str) -> dict[str, Any]:
    hours = _query_int(query, "hours", 72)
    timeframe = (query.get("timeframe") or ["5m"])[0]
    symbols = _query_symbols(query)
    allow_heavy = _query_bool(query, "allow_heavy", False)
    folds = _query_int(query, "folds", 4)
    min_trades = _query_int(query, "min_trades", 250)
    symbol_count = len(symbols or [])
    heavy_symbol_limit = 3 if lab_name == "fast_signal_shadow" else 2
    if not allow_heavy and (hours > 168 or symbol_count > heavy_symbol_limit):
        cli_command = _phase9_cli_command(lab_name)
        symbol_arg = ",".join(symbols or [])
        command = f"python -m app.research_lab {cli_command} --hours {hours} --timeframe {timeframe}"
        if symbol_arg:
            command += f" --symbols {symbol_arg}"
        if lab_name in {"dot_regime_diagnosis", "dot_regime_filter_lab", "phase9_paper_readiness"}:
            command += f" --folds {folds}"
        if lab_name == "phase9_paper_readiness":
            command += f" --min-trades {min_trades}"
        return {
            "status": "HEAVY_RESEARCH_SKIPPED",
            "skipped_heavy": True,
            "reason": "phase9_endpoint_heavy_run_blocked_use_cli_or_allow_heavy",
            "lab_name": lab_name,
            "requested_hours": hours,
            "requested_timeframe": timeframe,
            "requested_symbols": symbols or [],
            "cli_command": command,
            "text": (
                "PHASE 9 HEAVY RESEARCH SKIPPED\n"
                f"lab: {lab_name}\n"
                f"requested_hours: {hours}\n"
                f"requested_symbols: {symbol_arg or 'default'}\n"
                f"run_cli: {command}\n"
                "research_only: true\n"
                "paper_filter_enabled: false\n"
                "can_send_real_orders: false\n"
                "activation: disabled\n"
                "final_recommendation: NO LIVE"
            ),
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "activation": "disabled",
            "final_recommendation": "NO LIVE",
        }
    if config is None or db is None:
        return {
            "error": f"{lab_name} unavailable",
            "final_recommendation": "NO LIVE",
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "activation": "disabled",
        }
    try:
        if lab_name == "dot_regime_diagnosis":
            from .dot_regime_diagnosis import render_dot_regime_diagnosis_text, run_dot_regime_diagnosis
            report = run_dot_regime_diagnosis(config, db, hours=hours, timeframe=timeframe, symbols=symbols, folds=folds)
            payload = report.as_dict()
            payload["text"] = render_dot_regime_diagnosis_text(report)
        elif lab_name == "dot_regime_filter_lab":
            from .dot_regime_filter_lab import render_dot_regime_filter_lab_text, run_dot_regime_filter_lab
            report = run_dot_regime_filter_lab(config, db, hours=hours, timeframe=timeframe, symbols=symbols, folds=folds)
            payload = report.as_dict()
            payload["text"] = render_dot_regime_filter_lab_text(report)
        elif lab_name == "phase9_paper_readiness":
            from .phase9_paper_readiness_validator import render_phase9_paper_readiness_text, run_phase9_paper_readiness
            report = run_phase9_paper_readiness(
                config, db, hours=hours, timeframe=timeframe, symbols=symbols, min_trades=min_trades, folds=folds,
            )
            payload = report.as_dict()
            payload["text"] = render_phase9_paper_readiness_text(report)
        elif lab_name == "net_profit_lock_lab":
            from .net_profit_lock_lab import render_net_profit_lock_text, run_net_profit_lock_lab
            report = run_net_profit_lock_lab(config, db, hours=hours, timeframe=timeframe, symbols=symbols)
            payload = report.as_dict()
            payload["text"] = render_net_profit_lock_text(report)
        elif lab_name == "fast_signal_shadow":
            from .fast_signal_shadow import render_fast_signal_shadow_text, run_fast_signal_shadow
            report = run_fast_signal_shadow(config, db, hours=hours, timeframe=timeframe, symbols=symbols)
            payload = report.as_dict()
            payload["text"] = render_fast_signal_shadow_text(report)
        else:
            payload = {"error": "unknown phase9 lab"}
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["activation"] = "disabled"
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return {
            "error": str(exc)[:300],
            "lab_name": lab_name,
            "final_recommendation": "NO LIVE",
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "activation": "disabled",
        }


def _phase9_cli_command(lab_name: str) -> str:
    mapping = {
        "dot_regime_diagnosis": "dot-regime-diagnosis",
        "dot_regime_filter_lab": "dot-regime-filter-lab",
        "phase9_paper_readiness": "phase9-paper-readiness",
        "net_profit_lock_lab": "net-profit-lock-lab",
        "fast_signal_shadow": "fast-signal-shadow",
    }
    return mapping.get(lab_name, lab_name.replace("_", "-"))


def _research_pack_endpoint(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    hours = _query_int(query, "hours", 24)
    if config is None or db is None:
        return {
            "error": "research pack unavailable",
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "final_recommendation": "NO LIVE",
        }
    try:
        from .research_pack import build_research_pack, render_research_pack_text
        payload = build_research_pack(config, db, hours=min(hours, 24))
        payload["text"] = render_research_pack_text(payload)
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return {
            "error": str(exc)[:300],
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "final_recommendation": "NO LIVE",
        }


# ----- ResearchOps V5 endpoints --------------------------------------------


def _v5_no_op_safety_payload(error: str) -> dict[str, Any]:
    return {
        "error": error,
        "research_only": True,
        "paper_filter_enabled": False,
        "can_send_real_orders": False,
        "final_recommendation": "NO LIVE",
    }


def _research_pack_v5_endpoint(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    hours = _query_int(query, "hours", 24)
    symbols_arg = (query.get("symbols") or [""])[0]
    timeframes_arg = (query.get("timeframes") or [""])[0]
    symbols = [s.strip().upper() for s in symbols_arg.split(",") if s.strip()] or None
    timeframes = [t.strip().lower() for t in timeframes_arg.split(",") if t.strip()] or None
    include_shadow = (query.get("include_shadow") or ["1"])[0] in {"1", "true", "yes"}
    include_capital = (query.get("include_capital_leverage") or ["1"])[0] in {"1", "true", "yes"}
    include_fee = (query.get("include_fee_aware_exit") or ["0"])[0] in {"1", "true", "yes"}
    if config is None or db is None:
        return _v5_no_op_safety_payload("research pack v5 unavailable")
    try:
        from .research_pack_v5 import build_research_pack_v5, render_research_pack_v5_text
        payload = build_research_pack_v5(
            config, db,
            hours=min(hours, 24),
            symbols=symbols,
            timeframes=timeframes,
            include_shadow=include_shadow,
            include_capital_leverage=include_capital,
            include_fee_aware_exit=include_fee,
        )
        payload["text"] = render_research_pack_v5_text(payload)
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v5_ohlcv_freshness_status(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    symbols_arg = (query.get("symbols") or [""])[0]
    timeframes_arg = (query.get("timeframes") or [""])[0]
    symbols = [s.strip().upper() for s in symbols_arg.split(",") if s.strip()] or None
    timeframes = [t.strip().lower() for t in timeframes_arg.split(",") if t.strip()] or None
    if db is None:
        return _v5_no_op_safety_payload("freshness status unavailable")
    try:
        from .ohlcv_freshness_manager import freshness_status, render_freshness_matrix_text
        report = freshness_status(db, symbols=symbols, timeframes=timeframes, config=config)
        payload = report.as_dict()
        payload["text"] = render_freshness_matrix_text(report)
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v5_ohlcv_freshness_refresh_dry(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    symbols_arg = (query.get("symbols") or [""])[0]
    timeframes_arg = (query.get("timeframes") or [""])[0]
    hours = _query_int(query, "hours", 120)
    symbols = [s.strip().upper() for s in symbols_arg.split(",") if s.strip()] or None
    timeframes = [t.strip().lower() for t in timeframes_arg.split(",") if t.strip()] or None
    if db is None:
        return _v5_no_op_safety_payload("freshness refresh dry-run unavailable")
    try:
        from .ohlcv_freshness_manager import refresh, render_refresh_report_text
        # Dashboard endpoints NEVER trigger a real write. dry_run=True only.
        report = refresh(
            db,
            config=config,
            symbols=symbols,
            timeframes=timeframes,
            hours=hours,
            dry_run=True,
            allow_real_writes=False,
        )
        payload = report.as_dict()
        payload["text"] = render_refresh_report_text(report)
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v5_training_clean_view_audit(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    hours = _query_int(query, "hours", 24)
    if db is None:
        return _v5_no_op_safety_payload("training clean view unavailable")
    try:
        from .training_data_clean_view import run_training_data_clean_view, render_training_data_clean_view_text
        report = run_training_data_clean_view(db, hours=hours)
        payload = report.as_dict()
        payload["text"] = render_training_data_clean_view_text(report)
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v5_shadow_multi_trade_status(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    hours = _query_int(query, "hours", 24)
    timeframe = (query.get("timeframe") or ["5m"])[0]
    symbols_arg = (query.get("symbols") or [""])[0]
    symbols = [s.strip().upper() for s in symbols_arg.split(",") if s.strip()] or None
    allow_heavy = (query.get("allow_heavy") or ["0"])[0] in {"1", "true", "yes"}
    if not allow_heavy and hours > 72:
        return {
            "status": "SKIPPED_HEAVY",
            "reason": "shadow_multi_trade_request_hours_above_72_pass_allow_heavy_1",
            "cli_command": (
                f"python -m app.research_lab shadow-multi-trade-replay --hours {hours} "
                f"--timeframe {timeframe} --symbols {','.join(symbols or [])}"
            ),
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "final_recommendation": "NO LIVE",
        }
    if db is None:
        return _v5_no_op_safety_payload("shadow multi-trade unavailable")
    try:
        from .shadow_multi_trade_learning import run_shadow_multi_trade, render_shadow_multi_trade_text
        report = run_shadow_multi_trade(
            config, db, hours=hours, timeframe=timeframe, symbols=symbols,
        )
        payload = report.as_dict()
        payload["text"] = render_shadow_multi_trade_text(report)
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["activation"] = "shadow_only"
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v5_capital_leverage_sim(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    hours = _query_int(query, "hours", 168)
    timeframe = (query.get("timeframe") or ["5m"])[0]
    symbols_arg = (query.get("symbols") or [""])[0]
    capital = float((query.get("capital") or ["40"])[0])
    symbols = [s.strip().upper() for s in symbols_arg.split(",") if s.strip()] or None
    allow_heavy = (query.get("allow_heavy") or ["0"])[0] in {"1", "true", "yes"}
    if not allow_heavy and hours > 168:
        return {
            "status": "SKIPPED_HEAVY",
            "reason": "capital_leverage_sim_request_hours_above_168_pass_allow_heavy_1",
            "cli_command": (
                f"python -m app.research_lab capital-leverage-sim --hours {hours} "
                f"--timeframe {timeframe} --symbols {','.join(symbols or [])} --capital {capital}"
            ),
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "final_recommendation": "NO LIVE",
        }
    if db is None:
        return _v5_no_op_safety_payload("capital leverage simulator unavailable")
    try:
        from .capital_leverage_simulator import (
            run_capital_leverage_simulator,
            render_capital_leverage_text,
        )
        report = run_capital_leverage_simulator(
            config, db,
            hours=hours, timeframe=timeframe, symbols=symbols,
            capital_total_usdt=capital,
        )
        payload = report.as_dict()
        payload["text"] = render_capital_leverage_text(report)
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v75_duplicate_guard_hook_status(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    """V7.5 — Stats del duplicate guard hook (audit por defecto)."""
    del db, query
    try:
        from .duplicate_guard_hook import (
            get_global_hook,
            render_duplicate_guard_hook_stats_text,
        )
        stats = get_global_hook().stats()
        payload = stats.as_dict()
        payload["text"] = render_duplicate_guard_hook_stats_text(stats)
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v75_funding_cost_model(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    """V7.5 — Resumen del modelo de funding (read-only)."""
    hours = _query_int(query, "hours", 720)
    symbols_arg = (query.get("symbols") or [""])[0]
    symbols = [s.strip().upper() for s in symbols_arg.split(",") if s.strip()] or None
    if db is None:
        return _v5_no_op_safety_payload("funding cost model unavailable")
    try:
        from .funding_cost_model import render_funding_summary_text, summarise_funding
        summary = summarise_funding(db, trades=[], symbols=symbols or [], hours=hours)
        payload = summary.as_dict()
        payload["text"] = render_funding_summary_text(summary)
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v75_liquidation_model_bitget(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    """V7.5 — Liquidation model Bitget (read-only)."""
    del db
    symbol = (query.get("symbol") or ["DOTUSDT"])[0].upper()
    try:
        leverage = int((query.get("leverage") or ["5"])[0])
    except Exception:
        leverage = 5
    try:
        capital = float((query.get("capital") or ["40"])[0])
    except Exception:
        capital = 40.0
    try:
        margin = float((query.get("margin") or ["5"])[0])
    except Exception:
        margin = 5.0
    try:
        from .liquidation_model_bitget import (
            evaluate_liquidation,
            render_liquidation_text,
        )
        verdict = evaluate_liquidation(
            symbol=symbol, leverage=leverage,
            capital_usdt=capital, margin_per_trade_usdt=margin,
        )
        payload = verdict.as_dict()
        payload["text"] = render_liquidation_text(verdict)
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v75_walk_forward_v2(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    """V7.5 — Walk-forward V2 con bootstrap. Por defecto allow_heavy=false."""
    hours = _query_int(query, "hours", 720)
    timeframe = (query.get("timeframe") or ["5m"])[0]
    symbols_arg = (query.get("symbols") or [""])[0]
    symbols = [s.strip().upper() for s in symbols_arg.split(",") if s.strip()] or None
    allow_heavy = (query.get("allow_heavy") or ["0"])[0] in {"1", "true", "yes"}
    if not allow_heavy and hours > 168:
        return {
            "status": "SKIPPED_HEAVY",
            "reason": "walk_forward_v2_heavy_pass_allow_heavy_1",
            "cli_command": (
                f"python -m app.research_lab walk-forward-v2 --hours {hours} "
                f"--timeframe {timeframe} --symbols {','.join(symbols or [])}"
            ),
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "final_recommendation": "NO LIVE",
        }
    if db is None or config is None:
        return _v5_no_op_safety_payload("walk forward v2 unavailable")
    try:
        from .backtest_breakdown import collect_trade_records
        from .walk_forward_runner_v2 import (
            render_walk_forward_v2_text,
            run_walk_forward_v2,
        )
        records = collect_trade_records(config, db, hours=hours, symbols=symbols, timeframe=timeframe)
        trades = [
            {
                "entry_time": getattr(r, "entry_time", "") or "",
                "net_return_pct": getattr(r, "net_return_pct", 0.0) or 0.0,
            }
            for r in records
        ]
        report = run_walk_forward_v2(
            trades=trades, symbols=symbols or [], timeframe=timeframe,
        )
        payload = report.as_dict()
        payload["text"] = render_walk_forward_v2_text(report)
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v75_research_pack(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    """V7.5 — Pack ChatGPT V7.5."""
    hours = _query_int(query, "hours", 24)
    if config is None or db is None:
        return _v5_no_op_safety_payload("research pack v7.5 unavailable")
    try:
        from .research_pack_v7_5 import (
            build_research_pack_v7_5,
            render_research_pack_v7_5_text,
        )
        payload = build_research_pack_v7_5(config, db, hours=min(hours, 24))
        payload["text"] = render_research_pack_v7_5_text(payload)
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v8v9_auto_data_enrichment(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    """V8/V9 — Auto Data Enrichment. Read-only."""
    hours = _query_int(query, "hours", 24)
    timeframe = (query.get("timeframe") or ["5m"])[0]
    symbols_arg = (query.get("symbols") or [""])[0]
    symbols = [s.strip().upper() for s in symbols_arg.split(",") if s.strip()] or None
    if db is None:
        return _v5_no_op_safety_payload("auto data enrichment unavailable")
    try:
        from .auto_data_enrichment import summarise_enrichment
        from .phase8_research_utils import parse_symbols
        sym_list = parse_symbols(symbols, config) or ["BTCUSDT", "ETHUSDT", "DOTUSDT"]
        payload = summarise_enrichment(db, symbols=sym_list, timeframe=timeframe, hours=hours)
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v8v9_exit_intelligence(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    """V8/V9 — Exit Intelligence Lab. Read-only stub when no trades present."""
    hours = _query_int(query, "hours", 24)
    timeframe = (query.get("timeframe") or ["5m"])[0]
    try:
        from .exit_intelligence_lab import run_exit_intelligence
        report = run_exit_intelligence([], hours=hours, timeframe=timeframe)
        payload = report.as_dict()
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v8v9_strategy_experiment_registry(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    """V8/V9 — Strategy Experiment Registry snapshot. Read-only."""
    try:
        from .strategy_experiment_registry import StrategyExperimentRegistry
        snap = StrategyExperimentRegistry().snapshot()
        snap["research_only"] = True
        snap["paper_filter_enabled"] = False
        snap["can_send_real_orders"] = False
        snap["final_recommendation"] = "NO LIVE"
        return snap
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v8v9_shadow_candidate_lifecycle(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    """V8/V9 — Shadow Candidate Lifecycle summary. Read-only."""
    try:
        from .shadow_candidate_lifecycle import summarise_lifecycle
        out = summarise_lifecycle([])
        out["research_only"] = True
        out["paper_filter_enabled"] = False
        out["can_send_real_orders"] = False
        out["final_recommendation"] = "NO LIVE"
        return out
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v8v9_validation_gates(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    """V8/V9 — Validation Gates V9 status (no sample yet)."""
    try:
        from .validation_gates_v9 import run_validation_gates_v9
        report = run_validation_gates_v9(strategy_id="placeholder", net_returns=[])
        payload = report.as_dict()
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v82_bidirectional_funnel(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    """V8.2 — Bidirectional funnel. Read-only.

    V8.2.1: protected by ``allow_heavy=false`` when ``hours > 168``.
    """
    hours = _query_int(query, "hours", 168)
    side = (query.get("side") or [""])[0] or None
    allow_heavy = (query.get("allow_heavy") or ["0"])[0] in {"1", "true", "yes"}
    if not allow_heavy and hours > 168:
        return {
            "status": "SKIPPED_HEAVY",
            "reason": "bidirectional_funnel_hours_above_168",
            "hint": "pass allow_heavy=true to compute",
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "final_recommendation": "NO LIVE",
        }
    try:
        from .labs.bidirectional_forensic_lab import build_funnel
        payload = build_funnel(db, hours=hours, side_filter=side).as_dict()
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v82_score_asymmetry(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    """V8.2 — Score asymmetry audit. Read-only.

    V8.2.1: protected by ``allow_heavy=false`` when ``hours > 168``.
    """
    hours = _query_int(query, "hours", 168)
    allow_heavy = (query.get("allow_heavy") or ["0"])[0] in {"1", "true", "yes"}
    if not allow_heavy and hours > 168:
        return {
            "status": "SKIPPED_HEAVY",
            "reason": "score_asymmetry_audit_hours_above_168",
            "hint": "pass allow_heavy=true to compute",
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "final_recommendation": "NO LIVE",
        }
    try:
        from .labs.score_asymmetry_audit import audit
        payload = audit(db, hours=hours).as_dict()
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v82_trend_campaign_sim(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    """V8.2 — Trend campaign simulator. Read-only."""
    hours = _query_int(query, "hours", 168)
    side = (query.get("side") or ["SHORT"])[0].upper()
    allow_heavy = (query.get("allow_heavy") or ["0"])[0] in {"1", "true", "yes"}
    if not allow_heavy and hours > 168:
        return {
            "status": "SKIPPED_HEAVY",
            "reason": "trend_campaign_hours_above_168",
            "hint": "pass allow_heavy=true to compute",
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "final_recommendation": "NO LIVE",
        }
    try:
        from .labs.trend_campaign_simulator import run_campaign_simulation
        payload = run_campaign_simulation(db, side=side, hours=hours).as_dict()
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v82_profit_lock_sim(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    """V8.2 — Profit lock simulator. Read-only."""
    hours = _query_int(query, "hours", 168)
    side = (query.get("side") or ["SHORT"])[0].upper()
    allow_heavy = (query.get("allow_heavy") or ["0"])[0] in {"1", "true", "yes"}
    if not allow_heavy and hours > 168:
        return {
            "status": "SKIPPED_HEAVY",
            "reason": "profit_lock_hours_above_168",
            "hint": "pass allow_heavy=true to compute",
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "final_recommendation": "NO LIVE",
        }
    try:
        from .labs.profit_lock_simulator import run_profit_lock_simulation
        payload = run_profit_lock_simulation(db, side=side, hours=hours).as_dict()
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v824_counterfactual_training_export(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    """V8.2.4 — generate the counterfactual training dataset and ZIP it."""
    hours = _query_int(query, "hours", 168)
    limit = _query_int(query, "limit", 50000)
    allow_heavy = (query.get("allow_heavy") or ["0"])[0] in {"1", "true", "yes"}
    if not allow_heavy and (hours > 168 or limit > 50000):
        return {
            "status": "SKIPPED_HEAVY",
            "reason": "counterfactual_training_export_heavy",
            "hint": "pass allow_heavy=true to compute",
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "final_recommendation": "NO LIVE",
        }
    try:
        from .labs.counterfactual_training_dataset import build_dataset, export_dataset
        dataset, summary = build_dataset(db, hours=hours, limit=limit)
        manifest = export_dataset(dataset, summary)
        payload = {
            "status": "OK",
            "hours": hours,
            "limit": limit,
            "summary": summary.as_dict(),
            "manifest": manifest,
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "final_recommendation": "NO LIVE",
        }
        return payload
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v824_counterfactual_training_summary(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    """V8.2.4 — counterfactual training dataset summary (no export)."""
    hours = _query_int(query, "hours", 168)
    limit = _query_int(query, "limit", 50000)
    allow_heavy = (query.get("allow_heavy") or ["0"])[0] in {"1", "true", "yes"}
    if not allow_heavy and (hours > 168 or limit > 50000):
        return {
            "status": "SKIPPED_HEAVY",
            "reason": "counterfactual_training_summary_heavy",
            "hint": "pass allow_heavy=true to compute",
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "final_recommendation": "NO LIVE",
        }
    try:
        from .labs.counterfactual_training_dataset import build_dataset
        _dataset, summary = build_dataset(db, hours=hours, limit=limit)
        payload = summary.as_dict()
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v824_counterfactual_training_download(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    """V8.2.4 — return the latest exported ZIP as bytes for streaming.

    The handler refuses to serve any file outside the
    ``training_exports/research_v8_2_4/`` directory (path-traversal guard).
    """
    try:
        from .labs.counterfactual_training_dataset import EXPORT_SUBDIR, find_latest_zip
        zip_path = find_latest_zip()
        if zip_path is None:
            return {
                "status": "NEED_DATA",
                "reason": "no_export_available_yet",
                "hint": "call /api/research/counterfactual-training-export first",
                "research_only": True,
                "paper_filter_enabled": False,
                "can_send_real_orders": False,
                "final_recommendation": "NO LIVE",
            }
        # Path-traversal guard: ensure the resolved path is inside EXPORT_SUBDIR.
        from pathlib import Path
        base = Path(EXPORT_SUBDIR).resolve()
        resolved = zip_path.resolve()
        try:
            resolved.relative_to(base)
        except ValueError:
            return _v5_no_op_safety_payload("zip_path_outside_export_dir")
        with resolved.open("rb") as f:
            data = f.read()
        return {
            "status": "OK",
            "zip_name": resolved.name,
            "zip_bytes": data,
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "final_recommendation": "NO LIVE",
        }
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v82_research_pack(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    """V8.2 — Research Pack Bidirectional V1. Read-only."""
    hours = _query_int(query, "hours", 168)
    allow_heavy = (query.get("allow_heavy") or ["0"])[0] in {"1", "true", "yes"}
    if not allow_heavy and hours > 168:
        return {
            "status": "SKIPPED_HEAVY",
            "reason": "research_pack_bidirectional_hours_above_168",
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "final_recommendation": "NO LIVE",
        }
    try:
        from .labs.research_pack_bidirectional_v1 import build_pack
        payload = build_pack(db, hours=hours)
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v7_data_pipeline_root_cause(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    """V7 — Data pipeline root cause audit (read-only)."""
    hours = _query_int(query, "hours", 24)
    symbols_arg = (query.get("symbols") or [""])[0]
    timeframes_arg = (query.get("timeframes") or [""])[0]
    symbols = [s.strip().upper() for s in symbols_arg.split(",") if s.strip()] or None
    timeframes = [t.strip().lower() for t in timeframes_arg.split(",") if t.strip()] or None
    if db is None:
        return _v5_no_op_safety_payload("data pipeline root cause unavailable")
    try:
        from .data_pipeline_root_cause import (
            render_data_pipeline_root_cause_text,
            run_data_pipeline_root_cause,
        )
        report = run_data_pipeline_root_cause(
            db, hours=hours, symbols=symbols, timeframes=timeframes,
        )
        payload = report.as_dict()
        payload["text"] = render_data_pipeline_root_cause_text(report)
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v7_clean_strategy_lab(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    """V7 — Clean Strategy Lab. Research-only."""
    hours = _query_int(query, "hours", 24)
    timeframe = (query.get("timeframe") or ["5m"])[0]
    symbols_arg = (query.get("symbols") or [""])[0]
    symbols = [s.strip().upper() for s in symbols_arg.split(",") if s.strip()] or None
    allow_heavy = (query.get("allow_heavy") or ["0"])[0] in {"1", "true", "yes"}
    if not allow_heavy and (hours > 168 or (symbols and len(symbols) > 3)):
        return {
            "status": "SKIPPED_HEAVY",
            "reason": "clean_strategy_lab_hours_above_168_or_more_than_3_symbols",
            "cli_command": (
                f"python -m app.research_lab clean-strategy-lab --hours {hours} "
                f"--timeframe {timeframe} --symbols {','.join(symbols or [])}"
            ),
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "final_recommendation": "NO LIVE",
        }
    if db is None:
        return _v5_no_op_safety_payload("clean strategy lab unavailable")
    try:
        from .clean_strategy_lab import (
            render_clean_strategy_lab_text,
            run_clean_strategy_lab,
        )
        report = run_clean_strategy_lab(
            config, db,
            hours=hours, timeframe=timeframe, symbols=symbols,
        )
        payload = report.as_dict()
        payload["text"] = render_clean_strategy_lab_text(report)
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v7_capital_scaling_simulator(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    """V7 — Capital scaling simulator. Read-only."""
    hours = _query_int(query, "hours", 24)
    if db is None:
        return _v5_no_op_safety_payload("capital scaling simulator unavailable")
    try:
        from .clean_research_metrics import get_clean_research_metrics
        from .capital_scaling_simulator import (
            render_capital_scaling_text,
            run_capital_scaling_simulator,
        )
        cm = get_clean_research_metrics(db, hours=hours)
        report = run_capital_scaling_simulator(
            base_clean_net_ev_pct=float(cm.clean_ev_pct),
            base_clean_pf=float(cm.clean_pf),
            trades_per_window=100,
            data_quality_status=cm.data_quality_status,
            ohlcv_actionable=False,
        )
        payload = report.as_dict()
        payload["text"] = render_capital_scaling_text(report)
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v7_research_pack(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    """V7 — Pack for ChatGPT V7."""
    hours = _query_int(query, "hours", 24)
    symbols_arg = (query.get("symbols") or [""])[0]
    timeframes_arg = (query.get("timeframes") or [""])[0]
    symbols = [s.strip().upper() for s in symbols_arg.split(",") if s.strip()] or None
    timeframes = [t.strip().lower() for t in timeframes_arg.split(",") if t.strip()] or None
    if config is None or db is None:
        return _v5_no_op_safety_payload("research pack v7 unavailable")
    try:
        from .research_pack_v7 import build_research_pack_v7, render_research_pack_v7_text
        payload = build_research_pack_v7(
            config, db,
            hours=min(hours, 24),
            symbols=symbols, timeframes=timeframes,
        )
        payload["text"] = render_research_pack_v7_text(payload)
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v6_clean_research_metrics(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    """ResearchOps V6 — Clean research metrics (RAW vs CLEAN)."""
    hours = _query_int(query, "hours", 24)
    symbols_arg = (query.get("symbols") or [""])[0]
    timeframes_arg = (query.get("timeframes") or ["5m"])[0]
    symbols = [s.strip().upper() for s in symbols_arg.split(",") if s.strip()] or None
    timeframes = [t.strip().lower() for t in timeframes_arg.split(",") if t.strip()] or None
    if db is None:
        return _v5_no_op_safety_payload("clean research metrics unavailable")
    try:
        from .clean_research_metrics import (
            get_clean_research_metrics,
            render_clean_metrics_text,
        )
        report = get_clean_research_metrics(
            db, hours=hours, symbols=symbols, timeframes=timeframes,
        )
        payload = report.as_dict()
        payload["text"] = render_clean_metrics_text(report)
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v51_strategy_research_enhancer(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    """ResearchOps V5.1 — Strategy Research Enhancer (read-only)."""
    hours = _query_int(query, "hours", 24)
    timeframe = (query.get("timeframe") or ["5m"])[0]
    symbols_arg = (query.get("symbols") or [""])[0]
    symbols = [s.strip().upper() for s in symbols_arg.split(",") if s.strip()] or None
    data_quality_status = (query.get("data_quality_status") or [""])[0] or None
    allow_heavy = (query.get("allow_heavy") or ["0"])[0] in {"1", "true", "yes"}
    if not allow_heavy and (hours > 168 or (symbols and len(symbols) > 3)):
        return {
            "status": "SKIPPED_HEAVY",
            "reason": "strategy_research_enhancer_hours_above_168_or_more_than_3_symbols",
            "cli_command": (
                f"python -m app.research_lab strategy-research-enhancer --hours {hours} "
                f"--timeframe {timeframe} --symbols {','.join(symbols or [])}"
            ),
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "final_recommendation": "NO LIVE",
        }
    if db is None:
        return _v5_no_op_safety_payload("strategy research enhancer unavailable")
    try:
        from .strategy_research_enhancer import (
            render_strategy_research_enhancer_text,
            run_strategy_research_enhancer,
        )
        report = run_strategy_research_enhancer(
            config, db,
            hours=hours, timeframe=timeframe, symbols=symbols,
            data_quality_status=data_quality_status,
        )
        payload = report.as_dict()
        payload["text"] = render_strategy_research_enhancer_text(report)
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["activation"] = "disabled"
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


def _v5_fee_aware_exit_trainer(config: Any | None, db: Any | None, query: dict[str, list[str]]) -> dict[str, Any]:
    hours = _query_int(query, "hours", 168)
    timeframe = (query.get("timeframe") or ["5m"])[0]
    symbols_arg = (query.get("symbols") or [""])[0]
    symbols = [s.strip().upper() for s in symbols_arg.split(",") if s.strip()] or None
    allow_heavy = (query.get("allow_heavy") or ["0"])[0] in {"1", "true", "yes"}
    symbol_count = len(symbols or [])
    if not allow_heavy and (hours > 168 or symbol_count > 2):
        return {
            "status": "SKIPPED_HEAVY",
            "reason": "fee_aware_exit_request_heavy_pass_allow_heavy_1",
            "cli_command": (
                f"python -m app.research_lab fee-aware-exit-trainer --hours {hours} "
                f"--timeframe {timeframe} --symbols {','.join(symbols or [])}"
            ),
            "research_only": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "final_recommendation": "NO LIVE",
        }
    if db is None:
        return _v5_no_op_safety_payload("fee aware exit trainer unavailable")
    try:
        from .fee_aware_exit_trainer import (
            run_fee_aware_exit_trainer,
            render_fee_aware_exit_text,
        )
        report = run_fee_aware_exit_trainer(
            config, db,
            hours=hours, timeframe=timeframe, symbols=symbols,
        )
        payload = report.as_dict()
        payload["text"] = render_fee_aware_exit_text(report)
        payload["research_only"] = True
        payload["paper_filter_enabled"] = False
        payload["can_send_real_orders"] = False
        payload["final_recommendation"] = "NO LIVE"
        return payload
    except Exception as exc:
        return _v5_no_op_safety_payload(str(exc)[:300])


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
    cache_key = f"short:{hours}"
    force = (query.get("force") or ["0"])[0] in {"1", "true", "yes"}
    cached = _DASHBOARD_SHORT_REPORT_CACHE.get(cache_key)
    if cached and not force and time.time() - float(cached.get("cached_at_epoch", 0.0) or 0.0) < 120:
        payload = dict(cached.get("payload") or {})
        payload["cache_hit"] = True
        return payload
    try:
        from .dashboard_pro import build_dashboard_short_report

        payload = build_dashboard_short_report(config, db, hours=hours)
        payload["cache_hit"] = False
        _DASHBOARD_SHORT_REPORT_CACHE[cache_key] = {"cached_at_epoch": time.time(), "payload": payload}
        while len(_DASHBOARD_SHORT_REPORT_CACHE) > 20:
            oldest = sorted(_DASHBOARD_SHORT_REPORT_CACHE.items(), key=lambda item: float(item[1].get("cached_at_epoch", 0.0) or 0.0))[0][0]
            _DASHBOARD_SHORT_REPORT_CACHE.pop(oldest, None)
        return payload
    except Exception as exc:
        return {
            "error": str(exc)[:300],
            "text": f"DASHBOARD PRO SHORT REPORT START\nERROR_SANITIZED: {type(exc).__name__}\nfinal_recommendation: NO LIVE\nDASHBOARD PRO SHORT REPORT END",
            "hours": hours,
            "report_status": "ERROR",
            "warnings": [f"SHORT_REPORT_ERROR: {type(exc).__name__}"],
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


def _query_bool(query: dict[str, list[str]], key: str, default: bool = False) -> bool:
    raw = (query.get(key) or [default])[0]
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _query_symbols(query: dict[str, list[str]]) -> list[str] | None:
    raw = (query.get("symbols") or [""])[0]
    symbols = [part.strip().upper() for part in str(raw or "").split(",") if part.strip()]
    return symbols or None


def _public_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return re.sub(
        r"(?i)\b(API[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSPHRASE|PRIVATE[_-]?KEY)\s*[:=]\s*([^\s,;]+)",
        lambda match: f"{match.group(1)}=***",
        value,
    )


# ---------------------------------------------------------------------------
# ResearchOps V10.4 — read-only Trader Terminal endpoints (GET only).
# Additive layer: every handler is lazy-imported and wrapped in try/except so
# a labs failure can never break /health or the existing dashboard. There are
# NO mutable routes: nothing here writes to the DB, config, .env or files.
#
# V10.4.2 (Codex P1 hardening):
# - public error payloads are sanitized (no paths, no stack traces, no
#   exception text); internal logs contain only the generic error code and
#   exception class, never the exception message;
# - heavy HTTP endpoints (data-readiness, candidates, net-edge) are cache-peek
#   only. They never execute labs, disk scans, or bulk reads in the HTTP
#   request. Heavy reports are refreshed from the CLI/runbook;
# - the 7s polling endpoint (dashboard-state) NEVER computes heavy work: it
#   composes from existing caches only, so /health stays responsive on the
#   single-threaded HTTPServer.
# ---------------------------------------------------------------------------

import logging as _logging

_V104_LOG = _logging.getLogger("app.health_server.v104")
_V104_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_V104_ERROR_UNAVAILABLE = "component_unavailable"
_V104_ERROR_DATA = "data_temporarily_unavailable"
_V104_ERROR_ENDPOINT = "research_endpoint_error"
_V104_PENDING = "STALE_OR_PENDING"

# TTLs (seconds). Conservative on purpose: polling must never trigger these.
_V104_TTL_DATA_READINESS = 300.0
_V104_TTL_PROVIDERS = 600.0
_V104_TTL_LABS = 600.0


def _v104_sanitize_error_for_public(public_message: str) -> str:
    """Return one of the fixed public error codes; never echo caller input."""
    allowed = {
        _V104_ERROR_UNAVAILABLE,
        _V104_ERROR_DATA,
        _V104_ERROR_ENDPOINT,
    }
    return public_message if public_message in allowed else _V104_ERROR_ENDPOINT


def _v104_sanitize_error_for_log(exc: Exception | None) -> str:
    """Return safe metadata without reading the exception message."""
    if exc is None:
        return "exception_type=unknown detail=redacted"
    error_type = re.sub(r"[^A-Za-z0-9_.-]", "", type(exc).__name__)[:80]
    return f"exception_type={error_type or 'Exception'} detail=redacted"


def _v104_safe_error(public_message: str, exc: Exception | None = None,
                     component: str = "") -> dict[str, Any]:
    """Return and log a sanitized error without exposing exception text."""
    safe_message = _v104_sanitize_error_for_public(public_message)
    if exc is not None:
        _V104_LOG.warning(
            "v104 component=%s error=%s %s",
            component or "unknown",
            safe_message,
            _v104_sanitize_error_for_log(exc),
        )
    return {"error": safe_message, "final_recommendation": "NO LIVE"}


def _v104_sanitize(payload: dict[str, Any], component: str = "") -> dict[str, Any]:
    """Replace any raw error text coming from shared helpers with a generic
    message so internal paths/details never reach the client."""
    if isinstance(payload, dict) and payload.get("error"):
        _V104_LOG.warning(
            "v104 component=%s error=%s source_error=redacted",
            component or "unknown",
            _V104_ERROR_UNAVAILABLE,
        )
        return {
            "error": _V104_ERROR_UNAVAILABLE,
            "final_recommendation": "NO LIVE",
        }
    return payload


def _v104_cached(key: str, ttl_seconds: float, builder,
                 error_message: str = _V104_ERROR_UNAVAILABLE) -> dict[str, Any]:
    """TTL cache for LIGHT builders (no disk/DB heavy work)."""
    now = time.time()
    hit = _V104_CACHE.get(key)
    if hit is not None and (now - hit[0]) < ttl_seconds:
        return hit[1]
    try:
        value = _v104_sanitize(builder(), key)
    except Exception as exc:
        value = _v104_safe_error(error_message, exc, key)
    _V104_CACHE[key] = (now, value)
    return value


def _v104_cache_peek(key: str) -> dict[str, Any]:
    """Read-only cache peek for the polling endpoint: NEVER computes. Returns
    the cached payload (marked STALE when expired) or a pending placeholder."""
    hit = _V104_CACHE.get(key)
    if hit is None:
        return {"data_status": _V104_PENDING, "final_recommendation": "NO LIVE"}
    payload = dict(hit[1])
    ttl = {"data_readiness": _V104_TTL_DATA_READINESS,
           "provider_readiness": _V104_TTL_PROVIDERS,
           "provider_verification": _V104_TTL_PROVIDERS,
           "learning_counts": _V104_TTL_DATA_READINESS,
           "paper_monitor": _V104_TTL_DATA_READINESS,
           "candidates": _V104_TTL_LABS,
           "net_edge": _V104_TTL_LABS}.get(key, _V104_TTL_LABS)
    if (time.time() - hit[0]) >= ttl:
        payload["data_status"] = "STALE"
    return payload


def _v104_heavy_snapshot(key: str, recommended_cli: str) -> dict[str, Any]:
    """Return cached heavy research without computing inside HTTP."""
    payload = _v104_cache_peek(key)
    status = str(payload.get("data_status") or "").upper()
    payload["needs_manual_refresh"] = status in {"", "STALE", _V104_PENDING}
    payload["refresh_mode"] = "CLI_ONLY"
    payload["recommended_cli"] = recommended_cli
    payload["http_computation_disabled"] = True
    payload["final_recommendation"] = "NO LIVE"
    return payload


def _v104_as_dict(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "as_dict"):
        return obj.as_dict()
    import dataclasses

    return dataclasses.asdict(obj)


def _v104_safety(config: Any | None, db: Any | None, state: Any | None) -> dict[str, Any]:
    """V10.4.3 — worker_lock truth fix: the dashboard reuses the worker_lock
    payload the bot itself publishes in ``state.payload()`` (same source as
    /health). It must NOT build a new WorkerLockManager here: a fresh manager
    gets its own instance_id and falsely reports ``blocked_duplicate`` against
    the real running worker. If the payload has no worker_lock, the dashboard
    honestly reports unknown instead of recomputing."""
    del db  # kept in the signature for call-site stability; not used (truth fix)
    health = state.payload() if state is not None else {}
    raw = {
        "mode": str(health.get("mode") or "paper"),
        "live_trading": bool(getattr(config, "live_trading", False)),
        "dry_run": bool(getattr(config, "dry_run", True)),
        "paper_trading": bool(getattr(config, "paper_trading", True)),
        "paper_filter_enabled": bool(getattr(config, "enable_paper_policy_filter", False)),
        "open_positions": int(health.get("open_positions") or 0),
        "circuit_breaker": bool(health.get("circuit_breaker") or False),
        "uptime": str(health.get("uptime") or ""),
        "worker_lock": health.get("worker_lock"),  # dict from /health, or None
    }
    try:
        from .labs.trader_dashboard_v104 import derive_safety_view

        view = derive_safety_view(raw)
    except Exception as exc:
        _V104_LOG.warning("v104 safety view failed: %s", type(exc).__name__)
        view = raw
        view["worker_lock"] = "unknown"
        view["worker_acquired"] = "unknown"
        view["duplicate_worker"] = "UNKNOWN"
    view["final_recommendation"] = "NO LIVE"
    return view


def _v104_data_readiness() -> dict[str, Any]:
    """Cache-peek only. Heavy data audits are refreshed through the CLI."""
    return _v104_heavy_snapshot(
        "data_readiness",
        "python -m app.research_lab external-data-source-audit-v103 --hours 8760",
    )


def _v104_provider_readiness() -> dict[str, Any]:
    """LIGHT (pure in-memory registry)."""
    def build() -> dict[str, Any]:
        from .labs.external_data_provider_registry_v10_3 import run_provider_readiness

        return _v104_as_dict(run_provider_readiness())

    return _v104_cached("provider_readiness", _V104_TTL_PROVIDERS, build)


def _v104_provider_verification() -> dict[str, Any]:
    """LIGHT (pure in-memory registry)."""
    def build() -> dict[str, Any]:
        from .labs.external_provider_verification_v10_4 import run_provider_verification

        return _v104_as_dict(run_provider_verification())

    return _v104_cached("provider_verification", _V104_TTL_PROVIDERS, build)


def _v104_candidates(config: Any | None, db: Any | None) -> dict[str, Any]:
    """Cache-peek only. Candidate ranking is refreshed through the CLI."""
    return _v104_heavy_snapshot(
        "candidates",
        "python -m app.research_lab candidate-ranking --hours 24",
    )


def _v104_net_edge(config: Any | None, db: Any | None) -> dict[str, Any]:
    """Cache-peek only. Net-edge research is refreshed through the CLI."""
    return _v104_heavy_snapshot(
        "net_edge",
        "python -m app.research_lab net-edge-lab --hours 24",
    )


def _v104_paper_monitor_cache_peek(state: Any | None) -> dict[str, Any]:
    """V10.5.2 (Codex P1-1) — PURE cache peek for the polling path. Zero
    database access: the only fresh values come from the in-memory
    HealthState (paper PnL counter). Cold cache => STALE_OR_PENDING."""
    payload = _v104_cache_peek("paper_monitor")
    if payload.get("error"):
        payload = {"data_status": "ERROR_STALE", "error": _V104_ERROR_UNAVAILABLE}
    payload["paper_pnl_runtime"] = float(getattr(state, "daily_pnl", 0.0) or 0.0)
    payload["paper_pnl_is_real_money"] = False
    payload["note"] = "paper/shadow only — NOT real money"
    payload["final_recommendation"] = "NO LIVE"
    return payload


# V10.5.2 (Codex P1-1): the old _v104_paper_monitor() helper performed
# synchronous DB reads (get_signal_label_summary_since /
# get_open_paper_positions_summary) and was removed from the v104 HTTP
# surface entirely. Paper-monitor data now flows snapshot-only.


def _v104_signal_monitor(config: Any | None, db: Any | None, training_pulse: Any | None) -> dict[str, Any]:
    top_signals: list[Any] = []
    top_blocks: list[Any] = []
    try:
        if training_pulse is not None and config is not None:
            pulse = training_pulse.to_dict(config)
            top_signals = list(pulse.get("top_signals") or [])[:10]
            top_blocks = list(pulse.get("top_blocks") or [])[:10]
    except Exception:
        pass
    return {
        "top_signals": top_signals,
        "top_blocks": top_blocks,
        "final_recommendation": "NO LIVE",
    }


def _v104_overview(config: Any | None, db: Any | None, state: Any | None,
                   training_pulse: Any | None) -> dict[str, Any]:
    """LIGHT: safety flags + cached data-readiness peek (never computes)."""
    safety = _v104_safety(config, db, state)
    dr = _v104_cache_peek("data_readiness")
    return {
        "banner": "NO LIVE — RESEARCH ONLY",
        "read_only": True,
        "mode": safety.get("mode", "PAPER"),
        "security_status": safety.get("security_status", ""),
        "data_classification": dr.get("data_classification", dr.get("data_status", "")),
        "backtester_readiness": dr.get("backtester_readiness", ""),
        "oi_bucket_policy": dr.get("oi_bucket_policy", ""),
        "paper_ready": False,
        "live_ready": False,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "final_recommendation": "NO LIVE",
    }


def _v105_learning_snapshot() -> dict[str, Any]:
    """V10.5.2 (Codex P1-2) — snapshot-only /learning endpoint, consistent
    with the V10.4.2 design: NO computation inside HTTP, no database access.
    Learning counts are produced by the CLI; this endpoint only serves the
    cached snapshot or an honest STALE_OR_PENDING with the recommended CLI."""
    return _v104_heavy_snapshot(
        "learning_counts",
        "python -m app.research_lab learning-edge-diagnostic-v104",
    )


def _v105_learning_status_cache_peek() -> dict[str, Any]:
    """V10.5.1 (Codex P1-1) — PURE cache peek for the polling path. Zero
    database access of any kind: cold cache answers STALE_OR_PENDING; a
    cached error answers ERROR_STALE (sanitized)."""
    payload = _v104_cache_peek("learning_counts")
    if payload.get("error"):
        return {"data_status": "ERROR_STALE", "error": _V104_ERROR_UNAVAILABLE,
                "final_recommendation": "NO LIVE"}
    return payload


def _v104_edge_focus(candidates_peek: dict[str, Any],
                     data_peek: dict[str, Any]) -> dict[str, Any]:
    """V10.4.3 — LIGHT 'what is blocking edge / next best action' summary,
    composed from cached dicts only (no computation, no IO)."""
    blocking: list[str] = []
    cand_status = str(candidates_peek.get("status")
                      or candidates_peek.get("data_status") or "")
    if cand_status == "NO_VALID_CANDIDATES":
        blocking.append("no candidate with positive net EV (net_EV<=0 after costs)")
        blocking.append("samples too small / TIME-death too high in watchlist")
    elif cand_status in ("STALE_OR_PENDING", "STALE", ""):
        blocking.append("no cached candidate snapshot; run CLI report")
    data_status = str(data_peek.get("backtester_readiness")
                      or data_peek.get("data_status") or "")
    if data_status in ("NEED_LONG_HISTORY", "STALE_OR_PENDING", "STALE", ""):
        blocking.append("clean history < 180d; OI buckets blocked until audited")
    next_action = ("verify Tardis.dev/CoinGlass manually, then acquire 180/365d "
                   "clean history (manifest + checksums + human authorization)")
    return {
        "what_is_blocking_edge": blocking,
        "next_best_research_action": next_action,
        "final_recommendation": "NO LIVE",
    }


def _v104_dashboard_state(config: Any | None, db: Any | None, state: Any | None,
                          training_pulse: Any | None) -> dict[str, Any]:
    """Ultra-light polling payload. Heavy sections are cache-peek only.
    V10.5.1 (Codex P1-1): NO synchronous DB reads anywhere in this path —
    learning is a pure cache peek; counts are computed only by the on-demand
    /learning endpoint or the CLIs."""
    candidates_peek = _v104_cache_peek("candidates")
    data_peek = _v104_cache_peek("data_readiness")
    net_edge_peek = _v104_cache_peek("net_edge")
    safety = _v104_safety(config, db, state)
    signal_monitor = _v104_signal_monitor(config, db, training_pulse)
    try:
        from .labs.trader_dashboard_v104 import derive_pipeline_stages

        pipeline = derive_pipeline_stages(
            safety=safety, candidates=candidates_peek,
            net_edge=net_edge_peek, signal_monitor=signal_monitor)
    except Exception as exc:
        _V104_LOG.warning("v105 pipeline derivation failed: %s", type(exc).__name__)
        pipeline = []
    return {
        "banner": "NO LIVE — RESEARCH ONLY",
        "read_only": True,
        "live_allowed": False,
        "safety": safety,
        "data_readiness": data_peek,
        "provider_readiness": _v104_provider_readiness(),  # light, pure
        "provider_verification_v105": _v105_provider_verification_light(),
        "candidates": candidates_peek,
        "net_edge": net_edge_peek,
        "paper_monitor": _v104_paper_monitor_cache_peek(state),
        "signal_monitor": signal_monitor,
        "edge_focus": _v104_edge_focus(candidates_peek, data_peek),
        "learning": _v105_learning_status_cache_peek(),
        "pipeline": pipeline,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "final_recommendation": "NO LIVE",
    }


def _v105_provider_verification_light() -> dict[str, Any]:
    """V10.5.1 (Codex P2-3) — LIGHT pure in-memory V10.5 scorecards summary
    (clearly distinct from the legacy V10.3 registry)."""
    def build() -> dict[str, Any]:
        from .labs.provider_verification_v10_5 import run_provider_verification_v105

        rep = run_provider_verification_v105()
        return {
            "title": "V10.5 Provider Verification",
            "providers": [
                {"name": p["provider_name"], "role": p["role"],
                 "status": p["status"],
                 "paid_download_authorized": p["paid_download_authorized"]}
                for p in rep.providers
            ],
            "any_paid_download_authorized": rep.any_paid_download_authorized,
            "final_recommendation": "NO LIVE",
        }

    return _v104_cached("provider_verification_v105", _V104_TTL_PROVIDERS, build)


def _v104_api(path: str, config: Any | None, db: Any | None, state: Any | None,
              training_pulse: Any | None) -> tuple[dict[str, Any], int]:
    """Returns (payload, http_status). Unknown endpoints → 404 (Codex P2)."""
    kind = path.rsplit("/", 1)[-1]
    try:
        if kind == "overview":
            return _v104_overview(config, db, state, training_pulse), 200
        if kind == "safety":
            return _v104_safety(config, db, state), 200
        if kind == "data-readiness":
            return _v104_data_readiness(), 200
        if kind == "provider-readiness":
            return _v104_provider_readiness(), 200
        if kind == "provider-verification":
            return _v104_provider_verification(), 200
        if kind == "candidates":
            return _v104_candidates(config, db), 200
        if kind == "net-edge":
            return _v104_net_edge(config, db), 200
        if kind == "paper-monitor":
            # V10.5.2 — snapshot-only: no DB reads inside HTTP, ever.
            payload = _v104_heavy_snapshot(
                "paper_monitor", "python -m app.research_lab daily-summary")
            payload["paper_pnl_runtime"] = float(getattr(state, "daily_pnl", 0.0) or 0.0)
            payload["paper_pnl_is_real_money"] = False
            return payload, 200
        if kind == "signal-monitor":
            return _v104_signal_monitor(config, db, training_pulse), 200
        if kind == "learning":
            # V10.5.2 — snapshot-only; never computed in HTTP, never polled.
            return _v105_learning_snapshot(), 200
        if kind == "dashboard-state":
            return _v104_dashboard_state(config, db, state, training_pulse), 200
    except Exception as exc:
        return _v104_safe_error(_V104_ERROR_ENDPOINT, exc, kind), 200
    return ({"error": "unknown_researchops_v104_endpoint",
             "final_recommendation": "NO LIVE"}, 404)


def _v104_terminal_html(config: Any | None, db: Any | None, state: Any | None,
                        training_pulse: Any | None) -> str:
    try:
        from .labs.trader_dashboard_v104 import (
            build_dashboard_view_model,
            render_dashboard_html,
        )

        snapshot = _v104_dashboard_state(config, db, state, training_pulse)
        vm = build_dashboard_view_model(
            safety=snapshot.get("safety"),
            data_readiness=snapshot.get("data_readiness"),
            provider_readiness=snapshot.get("provider_readiness"),
            candidates=snapshot.get("candidates"),
            net_edge=snapshot.get("net_edge"),
            paper_monitor=snapshot.get("paper_monitor"),
            signal_monitor=snapshot.get("signal_monitor"),
        )
        refresh = max(3, int(getattr(config, "dashboard_refresh_seconds", 7) or 7))
        return render_dashboard_html(vm, refresh_seconds=refresh)
    except Exception as exc:
        # Even the failure page is read-only, sanitized and explicit: no
        # internal paths or exception details reach the client or logs.
        _V104_LOG.warning(
            "v104 component=terminal error=%s %s",
            _V104_ERROR_UNAVAILABLE,
            _v104_sanitize_error_for_log(exc),
        )
        return (
            "<!doctype html><title>Trader Terminal</title>"
            "<h1>NO LIVE — RESEARCH ONLY</h1>"
            "<p>component_unavailable</p>"
        )
