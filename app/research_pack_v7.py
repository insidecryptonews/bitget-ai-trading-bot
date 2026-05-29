"""ResearchOps V7 — Research Pack V7 for ChatGPT.

Extends the V5 pack with V6 clean metrics and V7 data-pipeline root cause +
clean strategy lab summary + capital scaling.

Contract: no secrets, no DB dump, no env values, research_only=true.
"""

from __future__ import annotations

from typing import Any


def build_research_pack_v7(
    config: Any,
    db: Any,
    *,
    hours: int = 24,
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
    include_strategy_lab: bool = True,
    include_capital_scaling: bool = True,
) -> dict[str, Any]:
    """Build the V7 pack.

    Heavy sections delegate to the V5 builder; new V7 sections are added on
    top with their own CLI hint when skipped.
    """
    from .research_pack_v5 import build_research_pack_v5

    pack = dict(build_research_pack_v5(
        config, db,
        hours=hours, symbols=symbols, timeframes=timeframes,
        include_short_report=False,
        include_shadow=True,
        include_capital_leverage=False,
        include_fee_aware_exit=False,
    ))
    pack["pack_version"] = "v7"

    # V7 — Data Pipeline Root Cause.
    try:
        from .data_pipeline_root_cause import (
            render_data_pipeline_root_cause_text,
            run_data_pipeline_root_cause,
        )
        rc = run_data_pipeline_root_cause(db, hours=max(hours, 24), symbols=symbols, timeframes=timeframes)
        pack["data_pipeline_root_cause"] = rc.as_dict()
        pack["data_pipeline_root_cause_text"] = render_data_pipeline_root_cause_text(rc)
        if rc.biggest_problem and rc.biggest_problem != "clean_enough_for_research":
            pack.setdefault("known_issues", []).append(f"root_cause_biggest_problem={rc.biggest_problem}")
        if rc.duplicate_key_is_too_aggressive:
            pack.setdefault("known_issues", []).append("duplicate_key_too_aggressive_for_clean_view")
    except Exception as exc:
        pack["data_pipeline_root_cause"] = {"status": "unavailable", "error_type": type(exc).__name__}

    # V7 — Clean Strategy Lab summary.
    if include_strategy_lab:
        try:
            from .clean_strategy_lab import run_clean_strategy_lab
            lab = run_clean_strategy_lab(
                config, db,
                hours=min(int(hours), 24), timeframe="5m",
                symbols=symbols,
            )
            pack["clean_strategy_lab"] = {
                "summary": {
                    "data_quality_status": lab.data_quality_status,
                    "ohlcv_actionable": lab.ohlcv_freshness_overall_actionable,
                    "raw_sample_count": lab.raw_sample_count,
                    "clean_sample_count": lab.clean_sample_count,
                },
                "families": [
                    {
                        "strategy_family": f.strategy_family,
                        "decision": f.decision,
                        "confidence": f.confidence,
                        "samples_clean": f.samples_clean,
                        "net_ev_pct": f.net_ev_pct,
                        "net_pf": f.net_pf,
                        "why_not": f.why_not,
                    }
                    for f in lab.families
                ],
            }
        except Exception as exc:
            pack["clean_strategy_lab"] = {"status": "unavailable", "error_type": type(exc).__name__}
    else:
        pack["clean_strategy_lab"] = {
            "status": "skipped_by_caller",
            "command": "python -m app.research_lab clean-strategy-lab --hours 24",
        }

    # V7 — Capital Scaling Simulator (uses CLEAN EV as base).
    if include_capital_scaling:
        try:
            from .capital_scaling_simulator import run_capital_scaling_simulator
            clean = pack.get("clean_research_metrics") or {}
            base_ev = float(clean.get("clean_ev_pct") or 0.0)
            base_pf = float(clean.get("clean_pf") or 0.0)
            dq = str(clean.get("data_quality_status") or "UNKNOWN")
            matrix = pack.get("ohlcv_freshness_matrix") or {}
            ohlcv_actionable = bool(matrix.get("overall_actionable", False))
            sim = run_capital_scaling_simulator(
                base_clean_net_ev_pct=base_ev,
                base_clean_pf=base_pf,
                trades_per_window=100,
                data_quality_status=dq,
                ohlcv_actionable=ohlcv_actionable,
            )
            pack["capital_scaling_simulator"] = sim.as_dict()
        except Exception as exc:
            pack["capital_scaling_simulator"] = {"status": "unavailable", "error_type": type(exc).__name__}
    else:
        pack["capital_scaling_simulator"] = {
            "status": "skipped_by_caller",
            "command": "python -m app.research_lab capital-scaling-simulator",
        }

    # Suggested next CLI commands (consolidate the operator loop).
    pack["suggested_next_cli_commands"] = [
        "python -m app.research_lab data-pipeline-root-cause --hours 720",
        "python -m app.research_lab clean-research-metrics --hours 720",
        "python -m app.research_lab clean-strategy-lab --hours 24",
        "python -m app.research_lab ohlcv-freshness-status --symbols BTCUSDT,ETHUSDT,DOTUSDT --timeframes 5m,15m,1h",
        "python -m app.research_lab phase9-paper-readiness --hours 720 --symbols DOTUSDT",
        "python -m app.research_lab strategy-research-enhancer --hours 24",
    ]
    pack["paper_filter_enabled"] = False
    pack["can_send_real_orders"] = False
    pack["final_recommendation"] = "NO LIVE"
    pack["research_only"] = True
    pack["activation"] = "disabled"
    return pack


def render_research_pack_v7_text(payload: dict[str, Any]) -> str:
    lines = ["RESEARCH PACK V7 START"]
    lines.append(f"generated_at: {payload.get('generated_at')}")
    lines.append(f"pack_version: {payload.get('pack_version')}")
    lines.append(f"git_version: {payload.get('git_version')}")
    lines.append(f"final_recommendation: {payload.get('final_recommendation')}")
    lines.append(f"activation: {payload.get('activation')}")
    lines.append("safety:")
    for key, value in (payload.get("safety") or {}).items():
        lines.append(f"- {key}: {value}")
    rc = payload.get("data_pipeline_root_cause") or {}
    if isinstance(rc, dict) and "biggest_problem" in rc:
        lines.append(
            f"data_pipeline_root_cause: biggest_problem={rc.get('biggest_problem')} "
            f"recommended_fix={rc.get('recommended_fix')} can_use_for_strategy_eval={rc.get('can_use_for_strategy_eval')}"
        )
    lab = payload.get("clean_strategy_lab") or {}
    if isinstance(lab, dict) and "families" in lab:
        lines.append(f"clean_strategy_lab_families: {len(lab.get('families') or [])}")
    capital = payload.get("capital_scaling_simulator") or {}
    if isinstance(capital, dict) and "scenarios" in capital:
        lines.append(f"capital_scaling_scenarios: {len(capital.get('scenarios') or [])}")
    lines.append("suggested_next_cli_commands:")
    for command in payload.get("suggested_next_cli_commands") or []:
        lines.append(f"- {command}")
    lines.append("omissions:")
    for item in payload.get("omissions") or []:
        lines.append(f"- {item}")
    lines.extend([
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "RESEARCH PACK V7 END",
    ])
    return "\n".join(lines)
