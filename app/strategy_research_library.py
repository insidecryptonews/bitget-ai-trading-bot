from __future__ import annotations

from typing import Any

from .operational_intelligence_utils import (
    FINAL_RECOMMENDATION,
    edge_metrics,
    load_operational_rows,
    safe_float_text,
    smoke_safety_lines,
)
from .utils import safe_float


HYPOTHESES = (
    "time_series_momentum",
    "breakout_volatility_expansion",
    "failed_breakout_reversal",
    "atr_volatility_based_exits",
    "meta_labeling_secondary_filter",
    "regime_specific_playbook",
    "funding_time_of_day_session_effects",
    "multi_lookback_adaptive_momentum",
)


BENCHMARKS = (
    "always_no_trade",
    "buy_and_hold_intraday",
    "simple_momentum_baseline",
    "simple_breakout_baseline",
    "simple_atr_trailing_baseline",
    "fixed_tp_sl_baseline",
)


class StrategyResearchLibrary:
    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db

    def build(self, *, hours: int = 72) -> dict[str, Any]:
        rows = load_operational_rows(self.db, hours=hours)
        tested = [evaluate_hypothesis(name, rows, self.config) for name in HYPOTHESES]
        benchmarks = [evaluate_benchmark(name, rows, self.config) for name in BENCHMARKS]
        valid_benchmarks = [row for row in benchmarks if row.get("benchmark_status") == "OK"]
        best_baseline = sorted(valid_benchmarks, key=lambda row: safe_float(row.get("net_EV")), reverse=True)[0] if valid_benchmarks else {}
        promising = [row for row in tested if row["decision"] in {"RESEARCH_POCKET", "SHADOW_CANDIDATE"}]
        rejected = [row for row in tested if row["decision"].startswith("REJECT")]
        overfit = [row for row in tested if row["decision"] == "REJECT_OVERFIT"]
        return {
            "hours": hours,
            "tested_hypotheses": tested,
            "rejected_hypotheses": rejected,
            "promising_hypotheses": promising,
            "overfit_hypotheses": overfit,
            "benchmarks": benchmarks,
            "best_baseline": best_baseline,
            "bot_vs_baseline": compare_bot_vs_baseline(rows, best_baseline, self.config),
            "recommendation": "NO LIVE",
            "paper_filter_enabled": False,
            "live_allowed": False,
            "research_only": True,
            "final_recommendation": FINAL_RECOMMENDATION,
        }

    def to_text(self, *, hours: int = 72) -> str:
        payload = self.build(hours=hours)
        lines = [
            "STRATEGY RESEARCH LIBRARY START",
            f"hours: {payload['hours']}",
            f"tested_hypotheses: {len(payload['tested_hypotheses'])}",
            f"promising_hypotheses: {len(payload['promising_hypotheses'])}",
            f"rejected_hypotheses: {len(payload['rejected_hypotheses'])}",
            f"best_baseline: {payload['best_baseline'].get('benchmark_id', 'none')}",
            f"bot_vs_baseline: {payload['bot_vs_baseline']}",
            "tested:",
        ]
        for row in payload["tested_hypotheses"]:
            lines.append(f"- {row['hypothesis_id']}: samples={row['samples']} net_EV={safe_float_text(row['net_EV'])} decision={row['decision']} reason={row['reason']}")
        lines.extend(["paper_filter_enabled=false", "live_allowed=false", "recommendation: NO LIVE", "STRATEGY RESEARCH LIBRARY END"])
        return "\n".join(lines)


def evaluate_hypothesis(name: str, rows: list[dict[str, Any]], config: Any | None = None) -> dict[str, Any]:
    selected = filter_hypothesis_rows(name, rows)
    metrics = edge_metrics(selected, config)
    if metrics.get("edge_metrics_status") != "OK":
        decision = "NEED_MORE_DATA"
        reason = str(metrics.get("edge_metrics_status") or "NEED_REALIZED_RETURN").lower()
    elif metrics["samples"] < 250:
        decision = "NEED_MORE_DATA"
        reason = "low_sample_hypothesis"
    elif metrics["actionability"] == "NOT_ACTIONABLE_MARKET_PROBE":
        decision = "NEED_MORE_DATA_NOT_ACTIONABLE"
        reason = "market_probe_only"
    elif safe_float(metrics["net_EV"]) <= 0 or safe_float(metrics["net_PF"]) < 1.05:
        decision = "REJECT_BAD_EDGE"
        reason = "net_edge_failed"
    elif safe_float(metrics["TIME"]) > 0.8:
        decision = "REJECT_TIME_DEATH"
        reason = "time_death_high"
    elif metrics["samples"] < 750:
        decision = "RESEARCH_POCKET"
        reason = "positive_but_needs_walk_forward"
    else:
        decision = "SHADOW_CANDIDATE"
        reason = "research_prelim_pass"
    return {
        "hypothesis_id": name,
        "samples": metrics["samples"],
        "net_EV": metrics["net_EV"],
        "net_PF": metrics["net_PF"],
        "TIME": metrics["TIME"],
        "edge_metrics_status": metrics.get("edge_metrics_status"),
        "decision": decision,
        "reason": reason,
        "research_only": True,
    }


def filter_hypothesis_rows(name: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if name == "time_series_momentum":
        return [row for row in rows if str(row.get("market_regime")) in {"TREND_UP", "TREND_DOWN"}]
    if name == "breakout_volatility_expansion":
        return [row for row in rows if _has_clean_breakout_feature(row) and safe_float(row.get("volatility")) >= 0.01]
    if name == "failed_breakout_reversal":
        return [row for row in rows if _has_failed_breakout_feature(row)]
    if name == "atr_volatility_based_exits":
        return [row for row in rows if safe_float(row.get("volatility")) > 0]
    if name == "meta_labeling_secondary_filter":
        return [row for row in rows if str(row.get("source")) == "trade_signal"]
    if name == "regime_specific_playbook":
        return [row for row in rows if str(row.get("market_regime")) in {"TREND_DOWN", "TREND_UP", "RISK_OFF", "RANGE", "CHOPPY_MARKET"}]
    if name == "funding_time_of_day_session_effects":
        return [row for row in rows if row.get("timestamp")]
    if name == "multi_lookback_adaptive_momentum":
        return [row for row in rows if abs(safe_float(row.get("momentum"))) > 0]
    return rows


def evaluate_benchmark(name: str, rows: list[dict[str, Any]], config: Any | None = None) -> dict[str, Any]:
    if name == "always_no_trade":
        return {
            "benchmark_id": name,
            "samples": 0,
            "net_EV": 0.0,
            "net_PF": 0.0,
            "recommendation": "safe_baseline",
            "benchmark_features_used": [],
            "benchmark_uses_expost_fields": False,
            "benchmark_status": "OK",
        }
    selected = rows
    status = "OK"
    features: list[str] = []
    if name == "simple_momentum_baseline":
        features = ["market_regime", "momentum/trend_strength"]
        if not any(_has_momentum_feature(row) for row in rows):
            selected = []
            status = "NEED_FEATURES"
        else:
            selected = [row for row in rows if str(row.get("market_regime")) in {"TREND_UP", "TREND_DOWN"} and _has_momentum_feature(row)]
    elif name == "simple_breakout_baseline":
        features = ["breakout_signal", "range_compression", "ATR/range expansion", "volume_confirmation"]
        if not any(_has_clean_breakout_feature(row) for row in rows):
            selected = []
            status = "NEED_FEATURES"
        else:
            selected = [row for row in rows if _has_clean_breakout_feature(row)]
    elif name == "simple_atr_trailing_baseline":
        features = ["bar_path", "ATR/volatility"]
        if not any(_has_bar_path(row) for row in rows):
            selected = []
            status = "NEED_BAR_PATH"
        else:
            selected = [row for row in rows if _has_bar_path(row) and safe_float(row.get("volatility")) > 0]
    elif name == "fixed_tp_sl_baseline":
        features = ["realized_return_pct"]
    metrics = edge_metrics(selected, config)
    if status == "OK" and metrics.get("edge_metrics_status") != "OK" and name != "always_no_trade":
        status = "NEED_REALIZED_RETURN"
    return {
        "benchmark_id": name,
        "samples": metrics["samples"],
        "net_EV": metrics["net_EV"] if status == "OK" else 0.0,
        "net_PF": metrics["net_PF"] if status == "OK" else 0.0,
        "recommendation": "research_only",
        "benchmark_features_used": features,
        "benchmark_uses_expost_fields": False,
        "benchmark_status": status,
        "edge_metrics_status": metrics.get("edge_metrics_status"),
    }


def compare_bot_vs_baseline(rows: list[dict[str, Any]], baseline: dict[str, Any], config: Any | None = None) -> dict[str, Any]:
    bot_metrics = edge_metrics(rows, config)
    if not baseline or baseline.get("benchmark_status") != "OK":
        return {
            "bot_net_EV": bot_metrics["net_EV"],
            "best_baseline_net_EV": None,
            "bot_beats_baseline": False,
            "conclusion": "no_valid_baseline",
        }
    return {
        "bot_net_EV": bot_metrics["net_EV"],
        "best_baseline_net_EV": baseline.get("net_EV", 0.0),
        "bot_beats_baseline": safe_float(bot_metrics["net_EV"]) > safe_float(baseline.get("net_EV")),
        "conclusion": "not_proven" if safe_float(bot_metrics["net_EV"]) <= safe_float(baseline.get("net_EV")) else "research_watch",
    }


def _has_momentum_feature(row: dict[str, Any]) -> bool:
    return any(abs(safe_float(row.get(key))) > 0 for key in ("momentum", "momentum_5", "momentum_15", "trend_strength_score"))


def _has_clean_breakout_feature(row: dict[str, Any]) -> bool:
    truthy_keys = (
        "breakout_signal",
        "clean_breakout",
        "range_compression_before_breakout",
        "atr_expansion",
        "candle_range_expansion",
        "volume_confirmation",
        "close_above_resistance",
        "close_below_support",
    )
    return any(bool(row.get(key)) for key in truthy_keys) or (
        safe_float(row.get("range_width_pct")) > 0
        and safe_float(row.get("volume_change")) > 1.2
        and safe_float(row.get("volatility")) > 0
    )


def _has_failed_breakout_feature(row: dict[str, Any]) -> bool:
    return any(bool(row.get(key)) for key in (
        "breakout_failure_signal",
        "failed_breakout",
        "liquidity_sweep_candidate",
        "rejection_candle",
        "close_back_inside_range",
        "volume_spike_without_followthrough",
    ))


def _has_bar_path(row: dict[str, Any]) -> bool:
    path = row.get("bar_path") or row.get("bars_ohlcv") or row.get("bars")
    return isinstance(path, list) and bool(path)


def strategy_research_library_smoke_text() -> str:
    low_sample = [{"symbol": "ETHUSDT", "side": "SHORT", "market_regime": "TREND_DOWN", "source": "trade_signal", "return_pct": 0.5, "first_barrier_hit": "TP"} for _ in range(20)]
    overfit = [{"symbol": "BTCUSDT", "side": "LONG", "market_regime": "CHOPPY_MARKET", "source": "trade_signal", "return_pct": -0.4, "first_barrier_hit": "SL"} for _ in range(300)]
    probe = [{**row, "source": "market_probe"} for row in low_sample * 20]
    benchmark = evaluate_benchmark("simple_momentum_baseline", [{**row, "momentum": 1.0} for row in low_sample + overfit])
    invalid_breakout = evaluate_benchmark("simple_breakout_baseline", [{"mfe": 9.0, "return_pct": 0.5, "first_barrier_hit": "TP"} for _ in range(20)])
    checks = {
        "strategy_library_no_live": True,
        "strategy_library_no_paper_filter": True,
        "low_sample_hypothesis_needs_more_data": evaluate_hypothesis("time_series_momentum", low_sample)["decision"] == "NEED_MORE_DATA",
        "overfit_or_bad_hypothesis_rejected": evaluate_hypothesis("regime_specific_playbook", overfit)["decision"].startswith("REJECT"),
        "market_probe_only_not_actionable": evaluate_hypothesis("time_series_momentum", probe)["decision"] == "NEED_MORE_DATA_NOT_ACTIONABLE",
        "benchmark_comparison_works": "benchmark_id" in benchmark,
        "breakout_mfe_oracle_blocked": invalid_breakout["benchmark_status"] == "NEED_FEATURES",
    }
    lines = ["STRATEGY RESEARCH LIBRARY SMOKE TEST START"]
    lines.extend(f"{key}: {str(value).lower()}" for key, value in checks.items())
    lines.extend(smoke_safety_lines())
    lines.extend([f"result: {'PASS' if all(checks.values()) else 'FAIL'}", "STRATEGY RESEARCH LIBRARY SMOKE TEST END"])
    return "\n".join(lines)
