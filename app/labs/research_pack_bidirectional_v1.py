"""V8.2 — Research Pack Bidirectional V1 (research-only).

Read-only ChatGPT-friendly summary that aggregates the V8.2 labs into one
payload. Never includes secrets, ``.env`` values or DB dumps.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import FINAL_RECOMMENDATION_NO_LIVE
from .bidirectional_forensic_lab import (
    build_funnel,
    failed_executed,
    good_not_monetized,
    missed_opportunities,
)
from .profit_lock_simulator import run_profit_lock_simulation
from .regime_router_simulator import simulate_router
from .score_asymmetry_audit import (
    audit,
    simulate_atr_softening,
    simulate_high_vol_directional,
    simulate_symmetric_regime,
)
from .trend_campaign_simulator import run_campaign_simulation


def build_pack(
    db: Any,
    *,
    hours: int = 168,
) -> dict[str, Any]:
    funnel = build_funnel(db, hours=hours).as_dict()
    asym = audit(db, hours=hours).as_dict()
    sym_sim = simulate_symmetric_regime(db, hours=hours).as_dict()
    atr_sim = simulate_atr_softening(db, hours=hours).as_dict()
    hv_sim = simulate_high_vol_directional(db, hours=hours).as_dict()
    router = simulate_router(db, hours=hours).as_dict()
    long_missed = missed_opportunities(db, side="LONG", hours=hours).as_dict()
    short_missed = missed_opportunities(db, side="SHORT", hours=hours).as_dict()
    long_failed = failed_executed(db, side="LONG", hours=hours).as_dict()
    short_failed = failed_executed(db, side="SHORT", hours=hours).as_dict()
    long_not_monetized = good_not_monetized(db, side="LONG", hours=hours).as_dict()
    short_not_monetized = good_not_monetized(db, side="SHORT", hours=hours).as_dict()
    long_campaign = run_campaign_simulation(db, side="LONG", hours=hours).as_dict()
    short_campaign = run_campaign_simulation(db, side="SHORT", hours=hours).as_dict()
    long_exits = run_profit_lock_simulation(db, side="LONG", hours=hours).as_dict()
    short_exits = run_profit_lock_simulation(db, side="SHORT", hours=hours).as_dict()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pack_version": "bidirectional_v1",
        "hours": int(hours),
        "funnel": funnel,
        "score_asymmetry": asym,
        "simulation_symmetric_regime": sym_sim,
        "simulation_atr_softening": atr_sim,
        "simulation_high_vol_directional": hv_sim,
        "regime_router_simulation": router,
        "long": {
            "missed_opportunities": long_missed,
            "failed_executed": long_failed,
            "good_not_monetized": long_not_monetized,
            "trend_campaign": long_campaign,
            "profit_lock": long_exits,
        },
        "short": {
            "missed_opportunities": short_missed,
            "failed_executed": short_failed,
            "good_not_monetized": short_not_monetized,
            "trend_campaign": short_campaign,
            "profit_lock": short_exits,
        },
        "research_only": True,
        "paper_filter_enabled": False,
        "can_send_real_orders": False,
        "no_private_endpoints_used": True,
        "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE,
    }


def render_pack_text(payload: dict[str, Any]) -> str:
    lines = ["RESEARCH PACK BIDIRECTIONAL V1 START"]
    lines.append(f"generated_at: {payload.get('generated_at')}")
    lines.append(f"pack_version: {payload.get('pack_version')}")
    lines.append(f"hours: {payload.get('hours')}")
    funnel = payload.get("funnel") or {}
    lines.append(f"funnel: total={funnel.get('total_signals', 0)} status={funnel.get('status')}")
    for side, count in (funnel.get("by_side") or {}).items():
        lines.append(f"funnel by_side {side}: {count}")
    asym = payload.get("score_asymmetry") or {}
    lines.append(
        f"score_asymmetry: median_long={asym.get('median_long', 0):.2f} "
        f"median_short={asym.get('median_short', 0):.2f} "
        f"gap={asym.get('gap_long_minus_short', 0):.2f} "
        f"long_pass%={asym.get('pct_long_pass_min_score', 0) * 100:.1f} "
        f"short_pass%={asym.get('pct_short_pass_min_score', 0) * 100:.1f}"
    )
    sym = payload.get("simulation_symmetric_regime") or {}
    lines.append(
        f"sim_symmetric_regime: delta_short_pass={sym.get('delta_short_pass', 0)} "
        f"delta_long_pass={sym.get('delta_long_pass', 0)}"
    )
    atr = payload.get("simulation_atr_softening") or {}
    lines.append(
        f"sim_atr_softening: delta_short_pass={atr.get('delta_short_pass', 0)} "
        f"delta_long_pass={atr.get('delta_long_pass', 0)}"
    )
    hv = payload.get("simulation_high_vol_directional") or {}
    lines.append(
        f"sim_high_vol_directional: delta_short_pass={hv.get('delta_short_pass', 0)} "
        f"delta_long_pass={hv.get('delta_long_pass', 0)}"
    )
    router = payload.get("regime_router_simulation") or {}
    lines.append(f"router: samples={router.get('samples', 0)} status={router.get('status')}")
    for state, count in (router.get("by_state") or {}).items():
        lines.append(f"router by_state {state}: {count}")
    for side in ("long", "short"):
        block = payload.get(side) or {}
        camp = block.get("trend_campaign") or {}
        plock = block.get("profit_lock") or {}
        lines.append(
            f"{side}.trend_campaign: samples={camp.get('samples', 0)} "
            f"optimal_adds={camp.get('optimal_adds')} status={camp.get('status')}"
        )
        lines.append(
            f"{side}.profit_lock: samples={plock.get('samples', 0)} "
            f"best_policy={plock.get('best_policy')} delta={plock.get('best_delta_pct', 0):.4f}"
        )
    lines.extend([
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "no_private_endpoints_used: true",
        f"final_recommendation: {payload.get('final_recommendation')}",
        "RESEARCH PACK BIDIRECTIONAL V1 END",
    ])
    return "\n".join(lines)
