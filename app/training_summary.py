from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .config import BotConfig
from .database import Database
from .utils import safe_float, safe_int


SUMMARY_START = "TRAINING SUMMARY START"
SUMMARY_END = "TRAINING SUMMARY END"
PLAN_START = "ACCELERATION PLAN START"
PLAN_END = "ACCELERATION PLAN END"


class TrainingSummary:
    """Cheap aggregated research telemetry. No heavy reports, no model training."""

    def __init__(self, config: BotConfig, db: Database) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 6) -> str:
        window = self._window(hours)
        labels = self.db.get_signal_label_summary_since(window["since"])
        observations = self.db.get_training_observation_summary_since(
            window["since"],
            min_score=self.config.min_score_to_trade,
            limit=5,
        )
        paper = self.db.get_paper_trade_summary()
        events = self.db.get_event_type_counts_since(window["since"])
        high_score_labels = self.db.get_high_score_label_summary_since(
            window["since"],
            self.config.min_score_to_trade,
        )
        by_symbol = self.db.get_shadow_opportunity_group_summaries_since(
            window["since"],
            min_score=self.config.min_score_to_trade,
            group_key="symbol",
            limit=3,
        )
        by_regime = self.db.get_shadow_opportunity_group_summaries_since(
            window["since"],
            min_score=self.config.min_score_to_trade,
            group_key="market_regime",
            limit=3,
        )
        recommendation = _recommendation(labels, events)
        metrics = _label_metrics(labels)
        high_score_metrics = _label_metrics(high_score_labels)
        lines = [
            SUMMARY_START,
            f"now: {window['now']}",
            f"since: {window['since']}",
            f"hours: {window['hours']}",
            (
                "safety: "
                f"PAPER={self.config.paper_trading} LIVE={self.config.live_trading} "
                f"DRY={self.config.dry_run} LIGHTWEIGHT={self.config.worker_lightweight_mode}"
            ),
            (
                "observations: "
                f"total={safe_int(observations.get('total'))} "
                f"LONG={safe_int(observations.get('long_count'))} "
                f"SHORT={safe_int(observations.get('short_count'))} "
                f"NO_TRADE={safe_int(observations.get('no_trade_count'))} "
                f"high_score={safe_int(observations.get('high_score_count'))}"
            ),
            (
                "labels: "
                f"total={safe_int(labels.get('total_labels'))} "
                f"TIME={safe_int(labels.get('time_count'))} "
                f"SL={safe_int(labels.get('sl_count'))} "
                f"TP1={safe_int(labels.get('tp1_count'))} "
                f"TP2={safe_int(labels.get('tp2_count'))} "
                f"PF={safe_float(labels.get('profit_factor')):.2f} "
                f"TIME%={metrics['time_ratio'] * 100:.1f} "
                f"SL%={metrics['sl_ratio'] * 100:.1f} "
                f"TP%={metrics['tp_ratio'] * 100:.1f}"
            ),
            (
                "win_loss_time_balance: "
                f"TP={safe_int(labels.get('tp1_count')) + safe_int(labels.get('tp2_count'))} "
                f"SL={safe_int(labels.get('sl_count'))} "
                f"TIME={safe_int(labels.get('time_count'))}"
            ),
            (
                "high_score_performance: "
                f"labels={safe_int(high_score_labels.get('total_labels'))} "
                f"PF={safe_float(high_score_labels.get('profit_factor')):.2f} "
                f"TIME%={high_score_metrics['time_ratio'] * 100:.1f} "
                f"SL%={high_score_metrics['sl_ratio'] * 100:.1f} "
                f"TP%={high_score_metrics['tp_ratio'] * 100:.1f}"
            ),
            f"paper: open={safe_int(paper.get('open'))} closed={safe_int(paper.get('closed'))}",
            (
                "events: "
                f"slot_blocks={events.get('training_slot_block', 0)} "
                f"high_score_missed={events.get('training_high_score_missed', 0)} "
                f"api_429={events.get('training_api_429', 0)} "
                f"paper_reconcile={events.get('training_paper_reconcile', 0)}"
            ),
            "dominant_regimes:",
            *_rows_to_lines(observations.get("regimes", [])),
            "top_high_score_symbols:",
            *_rows_to_lines(observations.get("top_symbols", [])),
            "by_symbol_edge:",
            *_edge_rows_to_lines(by_symbol),
            "by_regime_edge:",
            *_edge_rows_to_lines(by_regime),
            f"recommendation: {recommendation}",
            "final_recommendation: NO LIVE",
            SUMMARY_END,
        ]
        return "\n".join(lines)

    def acceleration_plan(self, *, hours: int = 24) -> str:
        window = self._window(hours)
        labels = self.db.get_signal_label_summary_since(window["since"])
        events = self.db.get_event_type_counts_since(window["since"])
        observations = self.db.get_training_observation_summary_since(
            window["since"],
            min_score=self.config.min_score_to_trade,
            limit=5,
        )
        if hasattr(self.db, "get_signal_path_metrics_summary_since"):
            path_metrics = self.db.get_signal_path_metrics_summary_since(window["since"])
        else:
            path_metrics = {"total": 0, "coverage_pct": 0.0}
        if hasattr(self.db, "get_signal_path_metrics_source_summary_since"):
            path_sources = self.db.get_signal_path_metrics_source_summary_since(window["since"])
        else:
            path_sources = []
        candidate_groups = []
        for group_key in ("symbol", "market_regime", "score_bucket"):
            candidate_groups.extend(
                self.db.get_shadow_opportunity_group_summaries_since(
                    window["since"],
                    min_score=self.config.min_score_to_trade,
                    group_key=group_key,
                    limit=10,
                )
            )
        score_groups = [row for row in candidate_groups if str(row.get("group_value")) in {"70-79", "80-89", "90-100"}]
        score_not_monotonic = _score_not_monotonic(score_groups)
        policy_context = _policy_context(self.config, self.db, hours)
        biggest = _biggest_problem(self.config, labels, events, observations, candidate_groups, path_metrics, score_not_monotonic, path_sources, policy_context)
        lines = [
            PLAN_START,
            f"hours: {window['hours']}",
            f"biggest_problem: {biggest}",
            f"score_not_monotonic: {str(score_not_monotonic).lower()}",
            f"mfe_mae_coverage: {safe_float(path_metrics.get('coverage_pct')) * 100:.1f}%",
            f"policy_candidates: {safe_int(policy_context.get('paper_candidate_count'))}",
            f"global_news_risk: {policy_context.get('global_news_risk')}",
            f"catalyst_dependency: {safe_float(policy_context.get('catalyst_dependency')):.2f}",
            "GO_LIVE_GATES:",
            "- live_allowed=false",
            "- reason=paper/research only",
            "suggested_next_research:",
            *_plan_steps(biggest, path_metrics),
            "do_not_change:",
            "- LIVE_TRADING=false",
            "- DRY_RUN=true",
            "- PAPER_TRADING=true",
            "final_recommendation: NO LIVE",
            PLAN_END,
        ]
        return "\n".join(lines)

    @staticmethod
    def _window(hours: int) -> dict[str, Any]:
        hours = max(1, int(hours or 6))
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=hours)
        return {"now": now.isoformat(), "since": since.isoformat(), "hours": hours}


def _recommendation(labels: dict[str, Any], events: dict[str, int]) -> str:
    total = safe_float(labels.get("total_labels"))
    if total > 0:
        metrics = _label_metrics(labels)
        if safe_float(labels.get("profit_factor")) < 1.0 or metrics["tp_ratio"] < 0.05:
            return "NEED_RESEARCH_POOR_EDGE"
        if metrics["time_ratio"] > 0.80 or safe_float(labels.get("sl_count")) > safe_float(labels.get("tp1_count")) + safe_float(labels.get("tp2_count")):
            return "NEED_RESEARCH"
    if events.get("training_api_429", 0) > 0:
        return "CHECK_RATE_LIMIT"
    if events.get("training_slot_block", 0) > 0:
        return "CHECK_SLOT"
    return "PAPER ONLY"


def _biggest_problem(
    config: BotConfig,
    labels: dict[str, Any],
    events: dict[str, int],
    observations: dict[str, Any],
    candidate_groups: list[dict[str, Any]] | None = None,
    path_metrics: dict[str, Any] | None = None,
    score_not_monotonic: bool = False,
    path_sources: list[dict[str, Any]] | None = None,
    policy_context: dict[str, Any] | None = None,
) -> str:
    policy_context = policy_context or {}
    if config.live_trading:
        return "safety_live"
    if policy_context.get("global_news_risk") in {"NEWS_BLOCK_ALL_PAPER", "NEWS_RISK_OFF"}:
        return "news_risk_block"
    if safe_int(policy_context.get("paper_candidate_count")) > 0 and safe_float(policy_context.get("catalyst_dependency")) > 0.70:
        return "catalyst_dependency_unclear"
    if safe_int(policy_context.get("paper_candidate_count")) > 0 and safe_float(policy_context.get("walk_forward_stability")) < 60:
        return "need_walk_forward_validation"
    total = safe_float(labels.get("total_labels"))
    if total <= 0 and safe_int(observations.get("total")) == 0:
        return "no_data"
    metrics = _label_metrics(labels)
    path_metrics = path_metrics or {}
    path_sources = path_sources or []
    path_total = safe_float(path_metrics.get("total"))
    path_active = safe_float(path_metrics.get("active_count"))
    path_matured = safe_float(path_metrics.get("matured_count"))
    observations_total = safe_int(observations.get("total"))
    market_probe_active = sum(safe_int(row.get("active_count")) for row in path_sources if str(row.get("source")) == "market_probe")
    if total > 0 and safe_float(labels.get("profit_factor")) < 1.0:
        if any(
            safe_int(row.get("total_labels")) >= config.edge_guard_min_sample
            and safe_float(row.get("profit_factor")) > 1.2
            for row in (candidate_groups or [])
        ):
            return "poor_edge_but_candidates_exist"
        return "poor_edge"
    if total > 0 and metrics["tp_ratio"] < 0.05:
        return "low_tp_rate"
    if score_not_monotonic:
        return "score_not_monotonic"
    if total > 0 and metrics["time_ratio"] > 0.60:
        return "too_many_time"
    if total > 0 and metrics["sl_ratio"] > metrics["tp_ratio"] * 2:
        return "too_many_sl"
    if observations_total > 0 and path_total <= 0:
        return "mfe_mae_filtered_by_low_score"
    if market_probe_active > 0 and path_matured <= 0:
        return "mfe_mae_collecting_wait_maturity"
    if path_active > 0 and path_matured <= 0:
        return "mfe_mae_collecting_wait_maturity"
    if safe_float(path_metrics.get("total")) <= 0 or safe_float(path_metrics.get("coverage_pct")) < 0.30:
        if total > 0:
            return "insufficient_price_path_data"
    if events.get("training_slot_block", 0) > 0 and safe_float(labels.get("profit_factor")) >= 1.0 and metrics["tp_ratio"] >= 0.05:
        return "slot"
    if events.get("training_api_429", 0) > 0:
        return "rate_limit"
    if safe_int(observations.get("high_score_count")) == 0:
        return "no_strong_signals"
    return "paper_observation"


def _plan_steps(problem: str, path_metrics: dict[str, Any] | None = None) -> list[str]:
    if problem in {"catalyst_dependency_unclear", "news_risk_block", "need_walk_forward_validation"}:
        return [
            "1. catalyst-summary --hours 24",
            "2. news-risk-gate --hours 24",
            "3. paper-policy-lab --hours 24",
            "4. walk-forward --hours 24",
            "5. policy-backtest --hours 24",
            "6. no live y no ampliar slots",
        ]
    if problem == "mfe_mae_filtered_by_low_score":
        return [
            "1. activar market probes research-only",
            "2. mantener low score sampling controlado",
            "3. esperar maduracion MFE/MAE",
            "4. revisar exit-simulation por source",
            "5. no ampliar slots y NO LIVE",
        ]
    if problem == "mfe_mae_collecting_wait_maturity":
        return [
            "1. esperar a que las muestras active alcancen MFE_MAE_MAX_BARS",
            "2. revisar mfe-mae-diagnostic --hours 24",
            "3. ejecutar exit-simulation --hours 24 cuando haya matured > 0",
            "4. no ampliar slots y NO LIVE",
        ]
    if problem in {"insufficient_price_path_data", "need_exit_simulation"}:
        return [
            "1. collect_mfe_mae_data",
            "2. ejecutar exit-simulation --hours 24 cuando haya cobertura suficiente",
            "3. ejecutar score-calibration --hours 24",
            "4. ejecutar shadow-experiments --hours 24",
            "5. mantener NO LIVE",
        ]
    if problem == "slot":
        return [
            "1. revisar training-summary --hours 24 para high_score_missed",
            "2. ejecutar reconcile-paper si hay PAPER_OPEN antigua",
            "3. mantener slots reales/paper sin ampliar hasta edge validado",
        ]
    if problem == "rate_limit":
        return [
            "1. revisar frecuencia de escaneo y 429",
            "2. mantener backoff activo",
            "3. no lanzar research pesado en worker",
        ]
    if problem in {"poor_edge", "poor_edge_but_candidates_exist", "low_tp_rate", "too_many_time", "too_many_sl"}:
        return [
            "1. ejecutar exit-simulation --hours 24",
            "2. ejecutar score-calibration --hours 24",
            "3. ejecutar shadow-experiments --hours 24",
            "4. ejecutar evolution-score --hours 24",
            "5. ejecutar edge-guard --hours 24",
            "3. mantener paper slots igual; no ampliar slots hasta PF>1 y TP rate suficiente",
            "4. habilitar edge guard paper filter solo despues de revisar dashboard y tests, no automaticamente",
            "5. revisar scoring high_score porque muchos score altos no llegan a TP",
        ]
    if problem == "score_not_monotonic":
        return [
            "1. ejecutar score-calibration --hours 24",
            "2. no confiar ciegamente en score 90-100",
            "3. cruzar score con Edge Guard y regimen",
            "4. mantener NO LIVE",
        ]
    if problem in {"TIME", "SL"}:
        return [
            "1. ejecutar strategy-lab offline en local/Railway shell controlada",
            "2. ejecutar virtual-portfolio offline",
            "3. revisar daily-summary/training-summary antes de tocar filtros",
        ]
    return [
        "1. seguir acumulando paper labels",
        "2. revisar training-summary cada pocas horas",
        "3. no activar live sin edge validado",
    ]


def _label_metrics(labels: dict[str, Any]) -> dict[str, float]:
    total = safe_float(labels.get("total_labels"))
    tp = safe_float(labels.get("tp1_count")) + safe_float(labels.get("tp2_count"))
    sl = safe_float(labels.get("sl_count"))
    time_count = safe_float(labels.get("time_count"))
    return {
        "time_ratio": time_count / max(total, 1.0) if total else 0.0,
        "sl_ratio": sl / max(total, 1.0) if total else 0.0,
        "tp_ratio": tp / max(total, 1.0) if total else 0.0,
    }


def _score_not_monotonic(rows: list[dict[str, Any]]) -> bool:
    by_bucket = {str(row.get("group_value")): row for row in rows}
    pf_80 = safe_float(by_bucket.get("80-89", {}).get("profit_factor"))
    pf_90 = safe_float(by_bucket.get("90-100", {}).get("profit_factor"))
    return bool(pf_80 > 0 and pf_90 > 0 and pf_80 > pf_90)


def _policy_context(config: BotConfig, db: Database, hours: int) -> dict[str, Any]:
    context = {
        "paper_candidate_count": 0,
        "walk_forward_stability": 0.0,
        "catalyst_dependency": 0.0,
        "global_news_risk": "NEWS_ALLOW",
    }
    try:
        from .paper_policy_lab import PaperPolicyLab
        from .walk_forward_validation import WalkForwardValidation
        from .news_risk_gate import NewsRiskGate

        policies = PaperPolicyLab(config, db).build(hours=hours)
        context["paper_candidate_count"] = len([
            row for row in policies.get("candidate_policies", [])
            if row.get("decision") in {"PAPER_CANDIDATE", "SHADOW_VALIDATE"}
        ])
        walk = WalkForwardValidation(config, db).build(hours=hours)
        if walk.get("policies"):
            context["walk_forward_stability"] = max(safe_float(row.get("stability")) for row in walk["policies"]) * 100.0
            context["catalyst_dependency"] = max(safe_float(row.get("catalyst_dependency")) for row in walk["policies"])
        news = NewsRiskGate(config, db).build(hours=hours)
        context["global_news_risk"] = news.get("global_decision", "NEWS_ALLOW")
    except Exception:
        pass
    return context


def _rows_to_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    lines: list[str] = []
    for row in rows[:5]:
        key = row.get("key") or row.get("group_value") or "NA"
        count = row.get("count") or row.get("total_labels") or 0
        extra = f" max_score={safe_int(row.get('max_score'))}" if "max_score" in row else ""
        lines.append(f"- {key}: {safe_int(count)}{extra}")
    return lines


def _edge_rows_to_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        (
            f"- {row.get('group_value') or 'NA'} labels={safe_int(row.get('total_labels'))} "
            f"PF={safe_float(row.get('profit_factor')):.2f} "
            f"TIME%={safe_float(row.get('time_ratio')) * 100:.1f} "
            f"SL%={safe_float(row.get('sl_ratio')) * 100:.1f} "
            f"TP%={safe_float(row.get('tp_ratio')) * 100:.1f}"
        )
        for row in rows[:3]
    ]
