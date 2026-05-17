from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import BotConfig, PROJECT_ROOT
from .utils import safe_float, safe_int, sanitize


REAL_ORDER_FUNCTIONS = [
    "BitgetClient.place_order",
    "BitgetClient.place_tpsl_order",
    "BitgetClient.close_position_market",
    "BitgetClient.set_leverage",
    "BitgetClient.ensure_isolated_margin",
]


def _bool(value: Any) -> str:
    return "true" if bool(value) else "false"


def _fmt(value: float, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return "0.00"


def _since(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours or 24)))).isoformat()


def _read_repo_file(path: str) -> str:
    try:
        return (PROJECT_ROOT / path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _pct(count: float, total: float) -> float:
    return (float(count) / max(float(total), 1.0)) * 100.0


def _safe_call(default: Any, func, *args, **kwargs) -> Any:
    try:
        return func(*args, **kwargs)
    except Exception:
        return default


def _label_metrics(summary: dict[str, Any]) -> dict[str, float]:
    total = safe_float(summary.get("total_labels"))
    tp = safe_float(summary.get("tp1_count")) + safe_float(summary.get("tp2_count"))
    sl = safe_float(summary.get("sl_count"))
    time_count = safe_float(summary.get("time_count"))
    return {
        "total": total,
        "tp": tp,
        "sl": sl,
        "time": time_count,
        "tp_pct": _pct(tp, total),
        "sl_pct": _pct(sl, total),
        "time_pct": _pct(time_count, total),
        "pf": safe_float(summary.get("profit_factor")),
    }


def _estimated_net_metrics(rows: list[dict[str, Any]], config: BotConfig) -> dict[str, float]:
    round_trip_cost = (
        (config.net_edge_taker_fee_bps * 2.0)
        + (config.net_edge_slippage_bps * 2.0)
        + config.net_edge_funding_bps_per_8h
    ) / 10000.0
    returns = [safe_float(row.get("realized_return_pct")) - round_trip_cost for row in rows]
    gains = sum(value for value in returns if value > 0)
    losses = abs(sum(value for value in returns if value < 0))
    net_pf = gains / losses if losses > 0 else (999.0 if gains > 0 else 0.0)
    return {
        "net_pf": net_pf,
        "net_ev": sum(returns) / max(len(returns), 1),
        "estimated_cost_pct": round_trip_cost,
    }


@dataclass
class SecurityAudit:
    config: BotConfig
    db: Any | None = None

    def to_text(self) -> str:
        execution_source = _read_repo_file("app/execution_engine.py")
        main_source = _read_repo_file("app/main.py")
        risk_source = _read_repo_file("app/risk_manager.py")
        live_path_exists = "place_order(" in execution_source and "can_send_real_orders" in execution_source
        currently_reachable = bool(self.config.can_send_real_orders and self.config.has_bitget_credentials)
        unsafe_flags: list[str] = []
        if self.config.can_send_real_orders:
            unsafe_flags.append("can_send_real_orders=true")
        if self.config.live_trading:
            unsafe_flags.append("LIVE_TRADING=true")
        if not self.config.dry_run:
            unsafe_flags.append("DRY_RUN=false")
        if not self.config.paper_trading:
            unsafe_flags.append("PAPER_TRADING=false")
        if currently_reachable:
            final_status = "UNSAFE"
        elif self.config.live_trading or not self.config.dry_run or not self.config.paper_trading:
            final_status = "WARNING"
        else:
            final_status = "SAFE_PAPER_ONLY"
        risks = [
            "La ruta live existe en ExecutionEngine y puede enviar ordenes si se activan simultaneamente live=true, paper=false y dry_run=false.",
            "Si WORKER_LIGHTWEIGHT_MODE se desactiva, desaparece una compuerta fuerte que fuerza paper/dry-run.",
            "Los tests de seguridad no demuestran rentabilidad ni ausencia de errores de estrategia.",
        ]
        if unsafe_flags:
            risks.insert(0, "Flags peligrosos detectados: " + ", ".join(unsafe_flags))
        return "\n".join(
            [
                "SECURITY_AUDIT START",
                f"can_send_real_orders: {_bool(self.config.can_send_real_orders)}",
                f"live_execution_path_exists: {_bool(live_path_exists)}",
                f"live_execution_currently_reachable: {_bool(currently_reachable)}",
                "real_order_function_names:",
                *[f"- {name}" for name in REAL_ORDER_FUNCTIONS],
                "config_gates:",
                f"- PAPER_TRADING={_bool(self.config.paper_trading)}",
                f"- LIVE_TRADING={_bool(self.config.live_trading)}",
                f"- DRY_RUN={_bool(self.config.dry_run)}",
                f"- WORKER_LIGHTWEIGHT_MODE={_bool(self.config.worker_lightweight_mode)}",
                f"- can_send_real_orders = live_trading and not paper_trading and not dry_run -> {_bool(self.config.can_send_real_orders)}",
                f"- has_bitget_credentials={_bool(self.config.has_bitget_credentials)}",
                "what_if_misconfigured:",
                f"- LIVE_TRADING=true alone: {'still blocked if DRY_RUN=true or PAPER_TRADING=true' if 'dry_run' in main_source else 'needs manual review'}",
                "- DRY_RUN=false alone: still blocked while PAPER_TRADING=true or LIVE_TRADING=false.",
                "- PAPER_TRADING=false alone: still blocked while DRY_RUN=true or LIVE_TRADING=false.",
                "- all three dangerous flags together plus credentials: live path can become reachable.",
                "emergency_stop/circuit_breaker:",
                f"- STOP_REQUESTED loop stop present: {_bool('STOP_REQUESTED' in main_source)}",
                f"- RiskManager preflight block present: {_bool('_preflight_block' in risk_source)}",
                f"- circuit breakers enabled: {_bool(self.config.enable_circuit_breakers)}",
                f"- daily_loss/weekly_loss gates present: {_bool('daily_loss' in risk_source and 'weekly_loss' in risk_source)}",
                f"- isolated margin required: {_bool(self.config.force_isolated_margin)}",
                "risks_found:",
                *[f"- {item}" for item in risks],
                f"final_security_status: {final_status}",
                "final_recommendation: NO LIVE",
                "SECURITY_AUDIT END",
            ]
        )


@dataclass
class SignalAudit:
    config: BotConfig
    db: Any | None = None

    def to_text(self, hours: int = 24) -> str:
        since = _since(hours)
        obs = _safe_call({}, getattr(self.db, "get_training_observation_summary_since", lambda *_a, **_k: {}), since, self.config.min_score_to_trade)
        high = _safe_call({}, getattr(self.db, "get_high_score_label_summary_since", lambda *_a, **_k: {}), since, self.config.min_score_to_trade)
        high_m = _label_metrics(high) if high else {"total": 0, "pf": 0, "tp_pct": 0, "sl_pct": 0, "time_pct": 0}
        return "\n".join(
            [
                "SIGNAL_AUDIT START",
                f"hours: {hours}",
                "generation_path:",
                "- main.py fetches snapshots, detects regime, then SignalEngine.generate_signal for each symbol.",
                "- StrategyEngine proposes LONG/SHORT/NO_TRADE from trend, breakout, momentum, reversal and support/resistance patterns.",
                "- SignalEngine converts the strategy decision into a score and only trades LONG/SHORT if score and confirmations pass thresholds.",
                "long_conditions:",
                "- bullish multi-timeframe bias, bullish breakout, bullish fast momentum, BTC/ETH controlled bullish reversal, support rejection, or bullish trend continuation.",
                "short_conditions:",
                "- bearish multi-timeframe bias, bearish breakdown, bearish fast momentum, BTC/ETH controlled bearish reversal, resistance rejection, or bearish trend continuation.",
                "no_trade_conditions:",
                "- insufficient data, missing price/ATR, regime blocks, BTC/ETH regime contradiction, low score, too few confirmations, invalid stop/TP, poor spread/liquidity or choppy/range penalties.",
                "symmetry_assessment:",
                "- LONG and SHORT share most scoring components, but regime allowed_direction, RSI ranges, BTC/ETH filters and empirical market behavior can create directional bias.",
                "- Direction must be trusted only after label/net-edge validation, not from raw score.",
                "recent_observations:",
                f"- total={safe_int(obs.get('total'))}",
                f"- LONG={safe_int(obs.get('long_count'))}",
                f"- SHORT={safe_int(obs.get('short_count'))}",
                f"- NO_TRADE={safe_int(obs.get('no_trade_count'))}",
                f"- high_score={safe_int(obs.get('high_score_count'))}",
                f"- operated={safe_int(obs.get('operated_count'))}",
                "high_score_performance:",
                f"- labels={safe_int(high_m['total'])}",
                f"- PF={_fmt(high_m['pf'])}",
                f"- TP%={_fmt(high_m['tp_pct'])}",
                f"- SL%={_fmt(high_m['sl_pct'])}",
                f"- TIME%={_fmt(high_m['time_pct'])}",
                "audit_conclusion:",
                "- A high score is a raw confluence score, not proof of predictive edge.",
                "- If high_score PF/net_EV is weak, score must be treated as diagnostic only.",
                "final_recommendation: NO LIVE",
                "SIGNAL_AUDIT END",
            ]
        )


@dataclass
class LabelTimeAudit:
    config: BotConfig
    db: Any | None = None

    def to_text(self, hours: int = 24) -> str:
        since = _since(hours)
        summary = _safe_call({}, getattr(self.db, "get_signal_label_summary_since", lambda *_a, **_k: {}), since)
        metrics = _label_metrics(summary) if summary else {"total": 0, "tp": 0, "sl": 0, "time": 0, "tp_pct": 0, "sl_pct": 0, "time_pct": 0, "pf": 0}
        path_summary = _safe_call({}, getattr(self.db, "get_signal_path_metrics_summary_since", lambda *_a, **_k: {}), since)
        source_summary = _safe_call([], getattr(self.db, "get_signal_path_metrics_source_summary_since", lambda *_a, **_k: []), since)
        probe_rows = sum(safe_int(row.get("total")) for row in source_summary if str(row.get("source")) == "market_probe")
        signal_rows = sum(safe_int(row.get("total")) for row in source_summary if str(row.get("source")) in {"trade_signal", "paper_signal", "paper_trade"})
        contamination_risk = probe_rows > 0 and signal_rows > 0
        return "\n".join(
            [
                "LABEL_TIME_AUDIT START",
                f"hours: {hours}",
                "time_definition:",
                "- TIME means the triple-barrier label did not hit TP or SL within max_holding_bars, or the observation was invalid/missing enough future data to hit a barrier.",
                f"- max_holding_bars={self.config.max_holding_bars}",
                f"- main_timeframe={self.config.main_timeframe}",
                f"- label_use_tp2={_bool(self.config.label_use_tp2)}",
                "recent_labels:",
                f"- labels={safe_int(metrics['total'])}",
                f"- TP={safe_int(metrics['tp'])} TP%={_fmt(metrics['tp_pct'])}",
                f"- SL={safe_int(metrics['sl'])} SL%={_fmt(metrics['sl_pct'])}",
                f"- TIME={safe_int(metrics['time'])} TIME%={_fmt(metrics['time_pct'])}",
                f"- PF={_fmt(metrics['pf'])}",
                "inflation_risks:",
                "- TIME can be inflated by TP too far from entry.",
                "- TIME can be inflated by a horizon that is too short for the signal's natural move.",
                "- TIME can also represent no-edge chop where price never moves enough; exits cannot fix that.",
                "- TIME can be misleading if NO_TRADE/market_probe/rejected signals are mixed with actionable trade_signal rows.",
                "path_metrics:",
                f"- active={safe_int(path_summary.get('active_count'))}",
                f"- matured={safe_int(path_summary.get('matured_count'))}",
                f"- insufficient={safe_int(path_summary.get('insufficient_count'))}",
                "source_mix:",
                *[
                    f"- {row.get('source')}: count={safe_int(row.get('total'))} active={safe_int(row.get('active_count'))} matured={safe_int(row.get('matured_count'))}"
                    for row in source_summary[:10]
                ],
                f"market_probe_mixed_with_signal_metrics: {_bool(contamination_risk)}",
                "audit_conclusion:",
                "- Labels use stored observation entry/SL/TP and later candles; correctness depends on market_data candle ordering and timestamp quality.",
                "- signal_path_metrics are useful for MFE/MAE, but market_probe rows must never be treated as actionable signal edge.",
                "final_recommendation: NO LIVE",
                "LABEL_TIME_AUDIT END",
            ]
        )


@dataclass
class PaperTradingAudit:
    config: BotConfig
    db: Any | None = None

    def to_text(self, hours: int = 24) -> str:
        summary = _safe_call({}, getattr(self.db, "get_paper_trade_summary", lambda: {}))
        open_rows = _safe_call([], getattr(self.db, "fetch_open_paper_trades", lambda: []))
        open_detail = _safe_call([], getattr(self.db, "get_open_paper_positions_summary", lambda *_a, **_k: []), 10)
        zombies = max(0, len(open_rows) - len(open_detail))
        return "\n".join(
            [
                "PAPER_TRADING_AUDIT START",
                f"hours: {hours}",
                f"paper_trading_enabled: {_bool(self.config.paper_trading)}",
                "paper_open_path:",
                "- main.py routes approved selected signals to PaperTrader.open_position when PAPER_TRADING=true.",
                "- ExecutionEngine live path is bypassed while paper_trading=true.",
                "paper_close_path:",
                "- PaperTrader.monitor closes by STOP_LOSS/TAKE_PROFIT_2 and marks TP1 by moving stop to breakeven.",
                "- PaperReconciler can close stale paper rows from labels/time without exchange calls.",
                "summary:",
                f"- total={safe_int(summary.get('total'))}",
                f"- open={safe_int(summary.get('open'))}",
                f"- closed={safe_int(summary.get('closed'))}",
                f"- open_rows_db={len(open_rows)}",
                f"- open_detail_rows={len(open_detail)}",
                f"- possible_zombie_mismatch={zombies}",
                "pnl_reliability:",
                "- Paper PnL is simulated from latest prices and DB trade fields; it is not exchange-real fill/slippage/funding truth.",
                "- Paper fees are approximated; net-edge labs should be used before trusting gross paper results.",
                "slot_blocks:",
                "- Slot blocks are real allocator/risk decisions, but they do not prove missed profit unless labels/path metrics show positive net edge.",
                "risks_found:",
                "- Open positions loaded from DB can become stale if the worker was down; reconcile-paper is the repair path.",
                "- Paper closes and label closes may differ if price monitoring and label windows use different timing.",
                "final_recommendation: NO LIVE",
                "PAPER_TRADING_AUDIT END",
            ]
        )


@dataclass
class ResearchModulesAudit:
    config: BotConfig
    db: Any | None = None

    def to_text(self, hours: int = 24) -> str:
        rows = [
            ("candidate_ranking", "implemented", "medium", "medium", "Useful as a final sanity gate; only as good as net edge, samples and source filtering."),
            ("edge_guard", "implemented", "medium", "medium", "Useful for research/paper gating, but historically could show gross-edge candidates that stricter ranking rejects."),
            ("paper_policy_orchestrator", "implemented", "medium", "medium", "Research-only aggregator; should remain shadow/off by default."),
            ("net_edge_lab", "implemented", "high", "low", "Conservative fee/slippage/funding estimates; still estimates, not real fills."),
            ("anti_overfit_gate", "implemented", "medium", "medium", "Good guard against small samples and deterioration; depends on available windows."),
            ("ev_slippage_calibration_gate", "implemented", "high", "low", "Important because gross PF can disappear after costs."),
            ("policy_stability_matrix", "implemented", "medium", "medium", "Useful for time-window consistency; weak if recent sample small."),
            ("time_death_autopsy", "implemented", "high", "low", "Directly addresses TIME death, but cause classification is heuristic."),
            ("exit_cause_backtest", "implemented", "medium", "medium", "Shadow-only exit research; should not be applied automatically."),
            ("exit_label_calibration_v2", "implemented", "medium", "medium", "Separates sources and exits; actionability still requires net/walk-forward validation."),
            ("pre_move_event_labeler", "implemented", "low", "high", "Can find historical moves, but not yet proof of tradable prediction."),
            ("pre_move_pattern_miner", "implemented", "low", "high", "Pattern mining is vulnerable to overfit and false pattern discovery."),
            ("pre_move_similarity_scanner", "implemented", "low", "high", "Useful watchlist only; must not become an allow signal."),
            ("data_vault", "implemented", "high", "low", "Backups/restore are operationally useful; restore speed and validation remain important."),
            ("dashboard_pro", "functional", "medium", "medium", "Functional exports/endpoints; visual quality is not proven by tests."),
        ]
        lines = [
            "RESEARCH_MODULES_AUDIT START",
            f"hours: {hours}",
            "module | status | useful_now | risk | notes",
        ]
        lines.extend(f"{name} | {status} | {useful} | {risk} | {notes}" for name, status, useful, risk, notes in rows)
        lines.extend(
            [
                "audit_conclusion:",
                "- Most modules are implemented as research diagnostics, not proof that the bot is profitable.",
                "- Smoke tests prove commands do not crash and safety strings exist; they do not prove edge.",
                "- Any module that aggregates market_probe with trade_signal can create false confidence unless source is separated.",
                "final_recommendation: NO LIVE",
                "RESEARCH_MODULES_AUDIT END",
            ]
        )
        return "\n".join(lines)


@dataclass
class DataAudit:
    config: BotConfig
    db: Any | None = None

    def to_text(self, hours: int = 24) -> str:
        lines = ["DATA_AUDIT START", f"requested_hours: {hours}"]
        if self.db is None:
            lines.extend(["db_available: false", "limitation: No local DB object available.", "DATA_AUDIT END"])
            return "\n".join(lines)
        for window in (6, 24, 72):
            since = _since(window)
            obs = _safe_call({}, getattr(self.db, "get_training_observation_summary_since", lambda *_a, **_k: {}), since, self.config.min_score_to_trade)
            labels = _safe_call({}, getattr(self.db, "get_signal_label_summary_since", lambda *_a, **_k: {}), since)
            rows = _safe_call([], getattr(self.db, "fetch_labeled_signal_rows_since", lambda *_a, **_k: []), since, 20000)
            metrics = _label_metrics(labels) if labels else {"total": 0, "tp": 0, "sl": 0, "time": 0, "tp_pct": 0, "sl_pct": 0, "time_pct": 0, "pf": 0}
            net = _estimated_net_metrics(rows, self.config) if rows else {"net_pf": 0.0, "net_ev": 0.0, "estimated_cost_pct": 0.0}
            source_summary = _safe_call([], getattr(self.db, "get_signal_path_metrics_source_summary_since", lambda *_a, **_k: []), since)
            symbol_groups = _safe_call([], getattr(self.db, "get_shadow_opportunity_group_summaries_since", lambda *_a, **_k: []), since, min_score=self.config.min_score_to_trade, group_key="symbol", limit=3)
            regime_groups = _safe_call([], getattr(self.db, "get_shadow_opportunity_group_summaries_since", lambda *_a, **_k: []), since, min_score=self.config.min_score_to_trade, group_key="market_regime", limit=3)
            lines.extend(
                [
                    f"window_{window}h:",
                    f"- observations={safe_int(obs.get('total'))}",
                    f"- LONG={safe_int(obs.get('long_count'))} SHORT={safe_int(obs.get('short_count'))} NO_TRADE={safe_int(obs.get('no_trade_count'))}",
                    f"- high_score={safe_int(obs.get('high_score_count'))}",
                    f"- labels={safe_int(metrics['total'])} TP={safe_int(metrics['tp'])} SL={safe_int(metrics['sl'])} TIME={safe_int(metrics['time'])}",
                    f"- gross_PF={_fmt(metrics['pf'])} net_PF_est={_fmt(net['net_pf'])} net_EV_est={_fmt(net['net_ev'], 5)}",
                    "- path_sources=" + (", ".join(f"{row.get('source')}:{safe_int(row.get('total'))}" for row in source_summary[:6]) or "none"),
                    "- top_symbols=" + (", ".join(f"{row.get('group_value')} PF={_fmt(safe_float(row.get('profit_factor')))} labels={safe_int(row.get('total_labels'))}" for row in symbol_groups) or "none"),
                    "- top_regimes=" + (", ".join(f"{row.get('group_value')} PF={_fmt(safe_float(row.get('profit_factor')))} labels={safe_int(row.get('total_labels'))}" for row in regime_groups) or "none"),
                ]
            )
        lines.extend(
            [
                "candidate_ranking_status:",
                _extract_status(_safe_module_text("candidate-ranking", self.config, self.db, hours), ["status=", "NO_VALID_CANDIDATES", "top_candidates"]),
                "orchestrator_status:",
                _extract_status(_safe_module_text("paper-policy-orchestrator", self.config, self.db, hours), ["no_actionable_candidates", "ALLOW_PAPER_CANDIDATE", "BLOCK_PAPER"]),
                "data_limitations:",
                "- Local DB may differ from VPS DB if this workspace is not the running VPS copy.",
                "- Net PF/EV here use conservative estimated costs unless real fees/funding/slippage are stored.",
                "final_recommendation: NO LIVE",
                "DATA_AUDIT END",
            ]
        )
        return "\n".join(lines)


def _safe_module_text(name: str, config: BotConfig, db: Any, hours: int) -> str:
    try:
        if name == "candidate-ranking":
            from .candidate_ranking import CandidateRanking

            return CandidateRanking(config, db).to_text(hours=hours)
        if name == "paper-policy-orchestrator":
            from .paper_policy_orchestrator import PaperPolicyOrchestrator

            return PaperPolicyOrchestrator(config, db).to_text(hours=hours)
    except Exception as exc:
        return f"ERROR_SANITIZED: {sanitize(type(exc).__name__)}"
    return "not_run"


def _extract_status(text: str, tokens: list[str]) -> str:
    if not text:
        return "- unavailable"
    lines = []
    for line in text.splitlines():
        if any(token in line for token in tokens):
            lines.append("- " + line[:240])
        if len(lines) >= 6:
            break
    return "\n".join(lines) if lines else "- no explicit status line found"


@dataclass
class DashboardAudit:
    config: BotConfig

    def to_text(self) -> str:
        html_path = PROJECT_ROOT / "app" / "static" / "dashboard.html"
        html = _read_repo_file("app/static/dashboard.html")
        has_pro = "Dashboard Pro" in html or "Training Dashboard Pro" in html
        has_tabs = "tab" in html.lower()
        has_canvas = "<canvas" in html.lower() or "<svg" in html.lower()
        has_export = "full-report" in html and "short-report" in html
        visual_real = has_pro and has_tabs and has_canvas
        return "\n".join(
            [
                "DASHBOARD_AUDIT START",
                f"dashboard_file: {html_path}",
                f"functional_changes_detected: {_bool(has_export)}",
                f"pro_layout_markers_detected: {_bool(has_pro)}",
                f"tabs_or_sections_detected: {_bool(has_tabs)}",
                f"simple_charts_detected: {_bool(has_canvas)}",
                "visual_quality_verified: false",
                "visual_status: FUNCTIONAL_NOT_VISUALLY_VERIFIED",
                "what_is_real:",
                "- API/full report/export endpoints exist in code.",
                "- HTML contains dashboard controls and sections.",
                "what_remains_unproven:",
                "- No screenshot/browser visual QA was performed in this audit.",
                "- Existing tests check strings/endpoints, not whether the dashboard feels professional.",
                "- A real redesign needs desktop/mobile screenshots, spacing/typography review, chart readability and user workflow testing.",
                f"dashboard_declares_visual_ok: {_bool(visual_real)}",
                "honest_conclusion:",
                "- The dashboard may be functional, but current automated evidence is not enough to call it beautiful/professional.",
                "final_recommendation: NO LIVE",
                "DASHBOARD_AUDIT END",
            ]
        )


@dataclass
class TestCoverageAudit:
    config: BotConfig

    def to_text(self) -> str:
        test_dir = PROJECT_ROOT / "tests"
        test_files = sorted(test_dir.glob("test_*.py"))
        test_functions = 0
        for file in test_files:
            try:
                test_functions += len(re.findall(r"^def test_", file.read_text(encoding="utf-8", errors="replace"), re.MULTILINE))
            except Exception:
                pass
        return "\n".join(
            [
                "TEST_COVERAGE_AUDIT START",
                f"test_files={len(test_files)}",
                f"test_functions_detected={test_functions}",
                "covered_areas:",
                "- config safety defaults",
                "- execution engine dry-run/paper behavior",
                "- risk manager gates",
                "- paper trader/reconciler basics",
                "- research labs command smoke and some decision rules",
                "- dashboard endpoint/string checks",
                "not_covered_or_weak:",
                "- no proof of profitability",
                "- no real visual dashboard QA",
                "- no real exchange execution validation, intentionally",
                "- no guarantee that mined patterns generalize out of sample",
                "- no full VPS runtime proof from this local audit unless commands run on VPS DB/environment",
                "smoke_test_false_confidence_risk:",
                "- Smoke tests can pass while the UI is ugly or the strategy has no edge.",
                "- Tests passing means safety/basic plumbing, not live readiness.",
                "final_recommendation: NO LIVE",
                "TEST_COVERAGE_AUDIT END",
            ]
        )


@dataclass
class BotIntegrityAudit:
    config: BotConfig
    db: Any | None = None

    def to_text(self, hours: int = 24) -> str:
        sections = [
            SecurityAudit(self.config, self.db).to_text(),
            SignalAudit(self.config, self.db).to_text(hours=hours),
            LabelTimeAudit(self.config, self.db).to_text(hours=hours),
            PaperTradingAudit(self.config, self.db).to_text(hours=hours),
            ResearchModulesAudit(self.config, self.db).to_text(hours=hours),
            DataAudit(self.config, self.db).to_text(hours=hours),
            DashboardAudit(self.config).to_text(),
            TestCoverageAudit(self.config).to_text(),
            self._confidence_matrix(),
            self._final_verdict(),
        ]
        return "\n\n".join(sections)

    def _confidence_matrix(self) -> str:
        safe = not self.config.can_send_real_orders and self.config.paper_trading and self.config.dry_run and not self.config.live_trading
        rows = [
            ("Seguridad/no live", 88 if safe else 20, "Compuertas fuertes paper/dry-run/lightweight; ruta live existe si se configura mal.", "Mantener tests de can_send_real_orders y revision manual antes de tocar live."),
            ("Paper trading", 62, "Simula entradas/cierres y reconcile existe; PnL no es fill real.", "Comparar paper closes con labels y detectar zombies regularmente."),
            ("Signal engine", 45, "Reglas claras pero score es confluencia bruta.", "Demostrar predictividad por source/symbol/side/regime."),
            ("Labels/TIME", 55, "Triple barrier claro; TIME puede mezclar no-edge con parametros malos.", "Separar fuentes y validar precios/timestamps."),
            ("Edge calculation", 45, "Net labs existen, pero dependen de costes estimados.", "Costes reales, slippage real y walk-forward."),
            ("Candidate ranking", 50, "Conservador y util como bloqueo.", "Validacion robusta por ventanas y muestras mayores."),
            ("Exit calibration", 40, "Shadow research existe.", "No aplicar hasta net EV positivo estable."),
            ("Pre-move intelligence", 30, "Detecta eventos, pero alto riesgo de patrones espurios.", "Validacion estricta y out-of-sample."),
            ("Data vault/backups", 75, "Backups R2/manifest/checksums implementados.", "Restores periodicos de prueba y tiempos de import."),
            ("Dashboard", 42, "Funcionalidad y exports existen.", "Visual QA real y rediseño profesional."),
            ("Readiness for live", 0, "No hay evidencia suficiente de edge neto estable.", "Meses/semanas de paper robusto, gates y revision humana."),
        ]
        lines = ["CONFIDENCE_MATRIX START", "Area | Confianza 0-100 | Motivo | Que falta"]
        lines.extend(f"{area} | {score} | {reason} | {missing}" for area, score, reason, missing in rows)
        lines.append("CONFIDENCE_MATRIX END")
        return "\n".join(lines)

    def _final_verdict(self) -> str:
        safe = not self.config.can_send_real_orders and self.config.paper_trading and self.config.dry_run and not self.config.live_trading
        return "\n".join(
            [
                "FINAL BOT AUDIT VERDICT START",
                f"security_status: {'SAFE_PAPER_ONLY' if safe else 'WARNING'}",
                "live_readiness: NO_LIVE",
                "paper_reliability: usable_for_research_but_not_exchange_truth",
                "data_quality: useful_but_must_separate_trade_signal_market_probe_and_rejected_sources",
                "strategy_quality: not_proven_profitable",
                "dashboard_quality: functional_but_not_visually_verified_professional",
                "main_risks:",
                "- Generic high score or gross PF can create false confidence.",
                "- TIME death may be no-edge, bad TP/SL/horizon, or contaminated source mix.",
                "- Market probes are good for path calibration but not actionable edge.",
                "- Smoke tests can overstate readiness.",
                "must_fix_before_live:",
                "- Positive net_EV/net_PF after costs on real trade_signal rows.",
                "- Walk-forward stability with sufficient recent samples.",
                "- Paper PnL reconciliation and zombie position checks.",
                "- Real dashboard visual QA and operational runbook.",
                "- Independent human review of live execution/risk gates.",
                "recommended_next_step: freeze features temporarily, run audit outputs on the VPS DB, then fix the highest-risk evidence gaps before any UI or strategy expansion.",
                "final_recommendation: NO LIVE",
                "FINAL BOT AUDIT VERDICT END",
            ]
        )


def security_audit_text(config: BotConfig, db: Any | None = None) -> str:
    return SecurityAudit(config, db).to_text()


def label_time_audit_text(config: BotConfig, db: Any | None = None, hours: int = 24) -> str:
    return LabelTimeAudit(config, db).to_text(hours=hours)


def paper_trading_audit_text(config: BotConfig, db: Any | None = None, hours: int = 24) -> str:
    return PaperTradingAudit(config, db).to_text(hours=hours)


def research_modules_audit_text(config: BotConfig, db: Any | None = None, hours: int = 24) -> str:
    return ResearchModulesAudit(config, db).to_text(hours=hours)


def bot_integrity_audit_text(config: BotConfig, db: Any | None = None, hours: int = 24) -> str:
    return BotIntegrityAudit(config, db).to_text(hours=hours)


class BotIntegrityAuditSmokeTest:
    def __init__(self, config: BotConfig, db: Any | None = None, logger: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.logger = logger

    def to_text(self) -> str:
        security = SecurityAudit(self.config, self.db).to_text()
        dashboard = DashboardAudit(self.config).to_text()
        audit = BotIntegrityAudit(self.config, self.db).to_text(hours=1)
        checks = {
            "security_audit_no_live_ok": "final_security_status: SAFE_PAPER_ONLY" in security,
            "audit_no_real_orders_ok": "live_execution_currently_reachable: false" in security,
            "live_readiness_no_live_ok": "live_readiness: NO_LIVE" in audit,
            "market_probe_not_actionable_ok": "market_probe rows must never be treated as actionable" in audit,
            "dashboard_not_overclaiming_visual_ok": "visual_status: FUNCTIONAL_NOT_VISUALLY_VERIFIED" in dashboard,
            "paper_filter_default_off_ok": not self.config.enable_paper_policy_filter,
            "LIVE_TRADING_false": not self.config.live_trading,
            "DRY_RUN_true": self.config.dry_run,
            "PAPER_TRADING_true": self.config.paper_trading,
        }
        result = "PASS" if all(checks.values()) else "FAIL"
        lines = ["BOT INTEGRITY AUDIT SMOKE TEST START"]
        lines.extend(f"{key}: {_bool(value)}" for key, value in checks.items())
        lines.extend(
            [
                f"can_send_real_orders={_bool(self.config.can_send_real_orders)}",
                "opened_real_trades: 0",
                f"result: {result}",
                "final_recommendation: NO LIVE",
                "BOT INTEGRITY AUDIT SMOKE TEST END",
            ]
        )
        return "\n".join(lines)
