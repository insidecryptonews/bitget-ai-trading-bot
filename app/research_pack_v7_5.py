"""ResearchOps V7.5 — Pack V7.5 para ChatGPT.

Extiende el pack V7 añadiendo:
  - duplicate_guard_hook_stats (audit / enforce)
  - funding_cost_model status
  - liquidation_model_bitget summary
  - walk_forward_v2 brief

Read-only. Sin secretos. Sin DB dump. Sin .env.
"""

from __future__ import annotations

from typing import Any


def build_research_pack_v7_5(
    config: Any,
    db: Any,
    *,
    hours: int = 24,
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
) -> dict[str, Any]:
    from .research_pack_v7 import build_research_pack_v7
    pack = dict(build_research_pack_v7(
        config, db,
        hours=hours, symbols=symbols, timeframes=timeframes,
        include_strategy_lab=True,
        include_capital_scaling=True,
    ))
    pack["pack_version"] = "v7_5"

    # Duplicate Guard Hook stats (audit por defecto).
    try:
        from .duplicate_guard_hook import (
            get_global_hook,
            render_duplicate_guard_hook_stats_text,
        )
        stats = get_global_hook().stats()
        pack["duplicate_guard_hook_stats"] = stats.as_dict()
        pack["duplicate_guard_hook_stats_text"] = render_duplicate_guard_hook_stats_text(stats)
    except Exception as exc:
        pack["duplicate_guard_hook_stats"] = {"status": "unavailable", "error_type": type(exc).__name__}

    # Funding cost model status.
    try:
        from .funding_cost_model import summarise_funding
        summary = summarise_funding(db, trades=[], symbols=symbols or [], hours=int(hours))
        pack["funding_cost_model"] = summary.as_dict()
        if not summary.table_present:
            pack.setdefault("known_issues", []).append("funding_rates_table_not_present")
    except Exception as exc:
        pack["funding_cost_model"] = {"status": "unavailable", "error_type": type(exc).__name__}

    # Liquidation model brief para DOTUSDT como muestra (capital=40, leverage=5).
    try:
        from .liquidation_model_bitget import evaluate_liquidation
        sample = evaluate_liquidation(
            symbol="DOTUSDT", leverage=5, capital_usdt=40.0,
            margin_per_trade_usdt=5.0,
        )
        pack["liquidation_model_sample"] = sample.as_dict()
        if sample.warnings:
            pack.setdefault("known_issues", []).extend(sample.warnings)
    except Exception as exc:
        pack["liquidation_model_sample"] = {"status": "unavailable", "error_type": type(exc).__name__}

    # V8/V9 Foundation enrichment — always safe / read-only.
    try:
        from .auto_data_enrichment import summarise_enrichment
        pack["auto_data_enrichment"] = summarise_enrichment(
            db, symbols=symbols or [], timeframe=(timeframes or ["5m"])[0], hours=int(hours)
        )
    except Exception as exc:
        pack["auto_data_enrichment"] = {"status": "unavailable", "error_type": type(exc).__name__}

    try:
        from .exit_intelligence_lab import run_exit_intelligence
        pack["exit_intelligence"] = run_exit_intelligence(
            [], hours=int(hours), timeframe=(timeframes or ["5m"])[0],
        ).as_dict()
    except Exception as exc:
        pack["exit_intelligence"] = {"status": "unavailable", "error_type": type(exc).__name__}

    try:
        from .strategy_experiment_registry import StrategyExperimentRegistry
        pack["strategy_experiment_registry"] = StrategyExperimentRegistry().snapshot()
    except Exception as exc:
        pack["strategy_experiment_registry"] = {"status": "unavailable", "error_type": type(exc).__name__}

    try:
        from .shadow_candidate_lifecycle import summarise_lifecycle
        pack["shadow_candidate_lifecycle"] = summarise_lifecycle([])
    except Exception as exc:
        pack["shadow_candidate_lifecycle"] = {"status": "unavailable", "error_type": type(exc).__name__}

    try:
        from .validation_gates_v9 import run_validation_gates_v9
        pack["validation_gates_v9"] = run_validation_gates_v9(
            strategy_id="placeholder",
            net_returns=[],
        ).as_dict()
    except Exception as exc:
        pack["validation_gates_v9"] = {"status": "unavailable", "error_type": type(exc).__name__}

    pack["pack_version"] = "v7_5_v8v9_foundation"
    pack["paper_filter_enabled"] = False
    pack["can_send_real_orders"] = False
    pack["final_recommendation"] = "NO LIVE"
    pack["activation"] = "disabled"
    pack["v10_design_note"] = "V10_FUTURE_MICRO_LIVE_PILOT_NOT_IMPLEMENTED"
    return pack


def render_research_pack_v7_5_text(payload: dict[str, Any]) -> str:
    lines = ["RESEARCH PACK V7.5 START"]
    lines.append(f"generated_at: {payload.get('generated_at')}")
    lines.append(f"pack_version: {payload.get('pack_version')}")
    lines.append(f"git_version: {payload.get('git_version')}")
    lines.append(f"final_recommendation: {payload.get('final_recommendation')}")
    lines.append(f"activation: {payload.get('activation')}")
    dg = payload.get("duplicate_guard_hook_stats") or {}
    if isinstance(dg, dict) and "mode" in dg:
        lines.append(
            f"duplicate_guard_hook: mode={dg.get('mode')} enabled={dg.get('enabled')} "
            f"would_block={dg.get('would_block_count')} actual_block={dg.get('actual_block_count')} "
            f"seen={dg.get('seen_count')}"
        )
    fund = payload.get("funding_cost_model") or {}
    if isinstance(fund, dict) and "funding_data_status" in fund:
        lines.append(
            f"funding_cost_model: status={fund.get('funding_data_status')} "
            f"table_present={fund.get('table_present')} trades_evaluated={fund.get('trades_evaluated')}"
        )
    liq = payload.get("liquidation_model_sample") or {}
    if isinstance(liq, dict) and "liquidation_distance_pct" in liq:
        lines.append(
            f"liquidation_model_sample: symbol={liq.get('symbol')} leverage={liq.get('leverage')} "
            f"distance_pct={liq.get('liquidation_distance_pct'):.4f} risk={liq.get('liquidation_risk')} "
            f"tier_source={liq.get('tier_source')}"
        )
    lines.append("known_issues:")
    for item in payload.get("known_issues") or []:
        lines.append(f"- {item}")
    lines.extend([
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "RESEARCH PACK V7.5 END",
    ])
    return "\n".join(lines)
