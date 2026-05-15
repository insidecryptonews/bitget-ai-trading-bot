from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .config import BotConfig
from .database import Database
from .utils import safe_float, safe_int


START = "EVOLUTION SCORE START"
END = "EVOLUTION SCORE END"


class EvolutionScore:
    def __init__(self, config: BotConfig, db: Database) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 24) -> dict[str, Any]:
        hours = max(1, int(hours or 24))
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        labels = self.db.get_high_score_label_summary_since(since, self.config.min_score_to_trade)
        paths = self.db.get_signal_path_metrics_summary_since(since)
        path_sources = self.db.get_signal_path_metrics_source_summary_since(since)
        recent_since = (datetime.now(timezone.utc) - timedelta(hours=max(1, self.config.edge_guard_recent_hours))).isoformat()
        recent_labels = self.db.get_high_score_label_summary_since(recent_since, self.config.min_score_to_trade)
        coverage = _coverage_by_source(path_sources)
        data_quality = _data_quality(labels, paths, coverage)
        edge_quality = _edge_quality(labels, coverage)
        stability = _stability(labels, recent_labels)
        safety = _safety(self.config)
        policy_context = _policy_context(self.config, self.db, hours)
        operational_context = _operational_context(self.config, self.db, hours)
        final_status = _final_status(data_quality, edge_quality, stability, coverage, policy_context)
        return {
            "hours": hours,
            "data_quality": data_quality,
            "edge_quality": edge_quality,
            "policy_quality": policy_context["policy_quality"],
            "walk_forward_stability": policy_context["walk_forward_stability"],
            "catalyst_dependency": policy_context["catalyst_dependency"],
            "news_risk_score": policy_context["news_risk_score"],
            "paper_candidate_count": policy_context["paper_candidate_count"],
            "global_news_risk": policy_context["global_news_risk"],
            "time_death_risk": operational_context["time_death_risk"],
            "exit_policy_quality": operational_context["exit_policy_quality"],
            "latency_readiness": operational_context["latency_readiness"],
            "future_vps_readiness": operational_context["future_vps_readiness"],
            "data_vault_status": operational_context["data_vault_status"],
            "backup_recency": operational_context["backup_recency"],
            "migration_readiness": operational_context["migration_readiness"],
            "stability": stability,
            "safety": safety,
            "market_probe_coverage": coverage["market_probe_coverage"],
            "signal_path_coverage": coverage["signal_path_coverage"],
            "matured_signal_samples": coverage["matured_signal_samples"],
            "matured_probe_samples": coverage["matured_probe_samples"],
            "final_status": final_status,
            "labels": labels,
            "path_metrics": paths,
            "path_sources": path_sources,
            "go_live_gates": _go_live_gates(),
            "recommendation": _recommendations(final_status),
            "final_recommendation": "NO LIVE",
        }

    def to_text(self, *, hours: int = 24) -> str:
        payload = self.build(hours=hours)
        lines = [
            START,
            f"hours: {payload['hours']}",
            f"data_quality: {payload['data_quality']:.1f}",
            f"edge_quality: {payload['edge_quality']:.1f}",
            f"policy_quality: {payload['policy_quality']:.1f}",
            f"walk_forward_stability: {payload['walk_forward_stability']:.1f}",
            f"catalyst_dependency: {payload['catalyst_dependency']:.2f}",
            f"news_risk_score: {payload['news_risk_score']:.1f}",
            f"paper_candidate_count: {payload['paper_candidate_count']}",
            f"global_news_risk: {payload['global_news_risk']}",
            f"time_death_risk: {payload['time_death_risk']:.1f}",
            f"exit_policy_quality: {payload['exit_policy_quality']:.1f}",
            f"latency_readiness: {payload['latency_readiness']:.1f}",
            f"future_vps_readiness: {payload['future_vps_readiness']}",
            f"data_vault_status: {payload['data_vault_status']}",
            f"backup_recency: {payload['backup_recency']}",
            f"migration_readiness: {payload['migration_readiness']}",
            f"stability: {payload['stability']:.1f}",
            f"safety: {payload['safety']:.1f}",
            f"market_probe_coverage: {payload['market_probe_coverage'] * 100:.1f}%",
            f"signal_path_coverage: {payload['signal_path_coverage'] * 100:.1f}%",
            f"matured_signal_samples: {payload['matured_signal_samples']}",
            f"matured_probe_samples: {payload['matured_probe_samples']}",
            f"final_status: {payload['final_status']}",
            "GO_LIVE_GATES:",
            f"- live_allowed={str(payload['go_live_gates']['live_allowed']).lower()}",
            f"- reason={payload['go_live_gates']['reason']}",
            "recommendation:",
            *[f"- {item}" for item in payload["recommendation"]],
            "final_recommendation: NO LIVE",
            END,
        ]
        return "\n".join(lines)


def _data_quality(labels: dict[str, Any], paths: dict[str, Any], coverage: dict[str, Any]) -> float:
    label_score = min(safe_float(labels.get("total_labels")) / 5000.0, 1.0) * 55.0
    coverage_score = safe_float(paths.get("coverage_pct")) * 35.0
    path_sample_score = min(safe_float(paths.get("total")) / 2000.0, 1.0) * 10.0
    probe_bonus = min(safe_float(coverage.get("matured_probe_samples")) / 500.0, 1.0) * 10.0
    return max(0.0, min(100.0, label_score + coverage_score + path_sample_score + probe_bonus))


def _edge_quality(labels: dict[str, Any], coverage: dict[str, Any]) -> float:
    if safe_float(coverage.get("matured_signal_samples")) <= 0 and safe_float(coverage.get("matured_probe_samples")) > 0:
        return 0.0
    total = safe_float(labels.get("total_labels"))
    tp = safe_float(labels.get("tp1_count")) + safe_float(labels.get("tp2_count"))
    sl = safe_float(labels.get("sl_count"))
    time_count = safe_float(labels.get("time_count"))
    pf = safe_float(labels.get("profit_factor"))
    tp_ratio = tp / max(total, 1.0) if total else 0.0
    sl_ratio = sl / max(total, 1.0) if total else 0.0
    time_ratio = time_count / max(total, 1.0) if total else 0.0
    score = min(pf / 1.5, 1.0) * 45.0 + min(tp_ratio / 0.05, 1.0) * 30.0
    score -= min(sl_ratio / 0.20, 1.0) * 15.0
    score -= min(time_ratio / 0.90, 1.0) * 10.0
    return max(0.0, min(100.0, score))


def _stability(labels: dict[str, Any], recent_labels: dict[str, Any]) -> float:
    pf = safe_float(labels.get("profit_factor"))
    recent_pf = safe_float(recent_labels.get("profit_factor"))
    if pf <= 0 or recent_pf <= 0:
        return 20.0
    drop = max(0.0, pf - recent_pf) / max(pf, 1.0)
    return max(0.0, min(100.0, 80.0 - drop * 100.0))


def _safety(config: BotConfig) -> float:
    score = 0.0
    score += 30.0 if not config.live_trading else 0.0
    score += 25.0 if config.dry_run else 0.0
    score += 20.0 if config.paper_trading else 0.0
    score += 15.0 if config.worker_lightweight_mode else 0.0
    score += 10.0 if not config.enable_kronos_research and not config.enable_full_research_auto_report else 0.0
    return min(100.0, score)


def _final_status(data_quality: float, edge_quality: float, stability: float, coverage: dict[str, Any], policy_context: dict[str, Any]) -> str:
    if policy_context.get("global_news_risk") in {"NEWS_BLOCK_ALL_PAPER", "NEWS_RISK_OFF"}:
        return "NEWS_RISK_BLOCK"
    if safe_float(policy_context.get("catalyst_dependency")) > 0.70 and safe_int(policy_context.get("paper_candidate_count")) > 0:
        return "CATALYST_DEPENDENT_EDGE"
    if safe_int(policy_context.get("paper_candidate_count")) > 0 and safe_float(policy_context.get("walk_forward_stability")) < 60:
        return "NEED_WALK_FORWARD"
    if safe_int(policy_context.get("paper_candidate_count")) > 0 and safe_float(policy_context.get("walk_forward_stability")) >= 60:
        return "POLICY_CANDIDATES_FOUND"
    if safe_float(coverage.get("matured_probe_samples")) > 0 and safe_float(coverage.get("matured_signal_samples")) <= 0:
        return "COLLECTING_PROBES"
    if data_quality < 35:
        return "NEED_MORE_DATA"
    if edge_quality < 45:
        return "EDGE_NEGATIVE"
    if stability < 45:
        return "KEEP_TRAINING"
    return "PAPER_ONLY"


def _recommendations(status: str) -> list[str]:
    if status == "NEWS_RISK_BLOCK":
        return ["NO LIVE", "news risk activo; mantener solo research/paper", "no activar policies"]
    if status == "CATALYST_DEPENDENT_EDGE":
        return ["NO LIVE", "edge parece depender de catalyst", "validar walk-forward antes de tocar paper"]
    if status == "NEED_WALK_FORWARD":
        return ["NO LIVE", "validar policies con walk-forward y backtest", "mantener filtros desactivados por defecto"]
    if status == "POLICY_CANDIDATES_FOUND":
        return ["NO LIVE", "candidatos research encontrados", "revisar manualmente antes de cualquier cambio paper"]
    if status == "COLLECTING_PROBES":
        return ["NO LIVE", "probes calibran movimiento de mercado, no edge de entrada", "esperar muestras reales maduras"]
    if status == "NEED_MORE_DATA":
        return ["NO LIVE", "seguir capturando MFE/MAE", "revisar exit-simulation cuando haya cobertura"]
    if status == "EDGE_NEGATIVE":
        return ["NO LIVE", "no ampliar slots", "usar Edge Guard y shadow experiments"]
    return ["NO LIVE", "seguir paper/research", "validar estabilidad temporal antes de cualquier cambio"]


def _coverage_by_source(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = sum(safe_float(row.get("total")) for row in rows)
    probe_total = sum(safe_float(row.get("total")) for row in rows if str(row.get("source")) == "market_probe")
    signal_total = max(0.0, total - probe_total)
    matured_probe = sum(safe_float(row.get("matured_count")) for row in rows if str(row.get("source")) == "market_probe")
    matured_signal = sum(safe_float(row.get("matured_count")) for row in rows if str(row.get("source")) != "market_probe")
    return {
        "market_probe_coverage": probe_total / max(total, 1.0) if total else 0.0,
        "signal_path_coverage": signal_total / max(total, 1.0) if total else 0.0,
        "matured_probe_samples": int(matured_probe),
        "matured_signal_samples": int(matured_signal),
    }


def _go_live_gates() -> dict[str, Any]:
    return {
        "live_allowed": False,
        "reason": "paper/research only",
        "future_minimums": [
            "500 operaciones paper cerradas o muestras equivalentes de senales reales maduras",
            "PF paper >= 1.25",
            "TP ratio suficiente y SL ratio controlado",
            "estabilidad 7 dias sin OOM ni 429 graves",
            "Edge Guard y Exit Simulation estables",
        ],
    }


def _policy_context(config: BotConfig, db: Database, hours: int) -> dict[str, Any]:
    context = {
        "policy_quality": 0.0,
        "walk_forward_stability": 0.0,
        "catalyst_dependency": 0.0,
        "news_risk_score": 100.0,
        "paper_candidate_count": 0,
        "global_news_risk": "NEWS_ALLOW",
    }
    try:
        from .paper_policy_lab import PaperPolicyLab
        from .walk_forward_validation import WalkForwardValidation
        from .news_risk_gate import NewsRiskGate

        policies = PaperPolicyLab(config, db).build(hours=hours)
        candidates = [row for row in policies.get("candidate_policies", []) if row.get("decision") in {"PAPER_CANDIDATE", "SHADOW_VALIDATE"}]
        context["paper_candidate_count"] = len(candidates)
        context["policy_quality"] = min(100.0, len(candidates) * 20.0)
        walk = WalkForwardValidation(config, db).build(hours=hours)
        walk_rows = walk.get("policies", [])
        if walk_rows:
            context["walk_forward_stability"] = max(0.0, min(100.0, max(safe_float(row.get("stability")) for row in walk_rows) * 100.0))
            context["catalyst_dependency"] = max(safe_float(row.get("catalyst_dependency")) for row in walk_rows)
        news = NewsRiskGate(config, db).build(hours=hours)
        context["global_news_risk"] = news.get("global_decision", "NEWS_ALLOW")
        if context["global_news_risk"] in {"NEWS_BLOCK_ALL_PAPER", "NEWS_RISK_OFF"}:
            context["news_risk_score"] = 10.0
    except Exception:
        pass
    return context


def _operational_context(config: BotConfig, db: Database, hours: int) -> dict[str, Any]:
    context = {
        "time_death_risk": 0.0,
        "exit_policy_quality": 0.0,
        "latency_readiness": 100.0,
        "future_vps_readiness": "STAY_ON_RAILWAY_FOR_NOW",
        "data_vault_status": "unknown",
        "backup_recency": "none",
        "migration_readiness": "not_ready",
    }
    try:
        from .time_death_lab import TimeDeathLab

        time_report = TimeDeathLab(config, db).build(hours=hours)
        time_ratio = safe_float(time_report.get("overall", {}).get("time_ratio"))
        context["time_death_risk"] = min(100.0, time_ratio * 100.0)
        context["exit_policy_quality"] = max(0.0, 100.0 - context["time_death_risk"])
    except Exception:
        pass
    try:
        from .latency_audit import LatencyAudit

        latency = LatencyAudit(config, db).build(hours=hours)
        cycle = latency.get("metrics", {}).get("cycle_total_ms", {})
        p95 = safe_float(cycle.get("p95_ms"))
        context["latency_readiness"] = max(0.0, 100.0 - min(p95 / 1000.0, 100.0))
    except Exception:
        pass
    try:
        from .data_vault import DataVault

        readiness = DataVault(config, db).migration_readiness()
        status = DataVault(config, db).status()
        context["data_vault_status"] = "backup_exists" if readiness.get("backup_exists") else "no_recent_backup"
        context["backup_recency"] = f"{safe_float(status.get('latest_backup_age_hours')):.1f}h" if status.get("latest_backup_age_hours") is not None else "none"
        context["migration_readiness"] = "ready" if readiness.get("ready_for_vps_migration") else "not_ready"
    except Exception:
        pass
    return context
