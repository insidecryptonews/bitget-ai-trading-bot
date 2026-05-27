"""ResearchOps V5 — Research pack v5 builder.

Wraps the Phase 9 `research_pack.build_research_pack` and enriches it with
V5-specific summaries:

  - OHLCV freshness matrix (multi-symbol, multi-timeframe)
  - Training data clean view
  - Shadow multi-trade status summary
  - Capital/leverage scenario top rows
  - Fee-aware exit trainer per-symbol best

Hard contract (kept verbatim from Phase 9):
  - never expose .env, API keys, DB dumps
  - never call private endpoints
  - never open orders
  - final_recommendation: NO LIVE
"""

from __future__ import annotations

from typing import Any

from .research_pack import build_research_pack


def build_research_pack_v5(
    config: Any,
    db: Any,
    *,
    hours: int = 24,
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
    include_short_report: bool = True,
    include_shadow: bool = True,
    include_capital_leverage: bool = True,
    include_fee_aware_exit: bool = False,
) -> dict[str, Any]:
    """Compose the V5 pack on top of the existing v4 pack.

    The `include_*` flags let callers cheaply turn off heavy sections when
    they only need the cockpit summary. Heavy sections are SKIPPED with a
    `command` hint rather than executed in the pack itself.
    """
    base = build_research_pack(config, db, hours=hours, include_short_report=include_short_report)
    pack = dict(base)
    pack["pack_version"] = "v5"

    # OHLCV freshness matrix.
    try:
        from .ohlcv_freshness_manager import freshness_status

        matrix = freshness_status(
            db,
            symbols=symbols,
            timeframes=timeframes,
            config=config,
        )
        pack["ohlcv_freshness_matrix"] = matrix.as_dict()
    except Exception as exc:
        pack["ohlcv_freshness_matrix"] = {
            "status": "unavailable",
            "error_type": type(exc).__name__,
        }

    # Training data clean view.
    try:
        from .training_data_clean_view import run_training_data_clean_view

        clean = run_training_data_clean_view(db, hours=max(hours, 24))
        pack["training_data_clean_view"] = clean.as_dict()
    except Exception as exc:
        pack["training_data_clean_view"] = {
            "status": "unavailable",
            "error_type": type(exc).__name__,
        }

    # Shadow multi-trade quick replay (small window so the pack stays cheap).
    if include_shadow:
        try:
            from .shadow_multi_trade_learning import run_shadow_multi_trade

            shadow = run_shadow_multi_trade(
                config, db, hours=min(int(hours), 24), timeframe="5m",
                symbols=symbols,
            )
            shadow_summary = {
                "symbols": shadow.symbols,
                "summary": shadow.summary,
                "pnl_summary": shadow.pnl_summary,
                "no_db_writes": True,
                "research_only": True,
                "activation": "shadow_only",
                "trades_sample": [trade.as_dict() for trade in shadow.trades[:20]],
            }
            pack["shadow_multi_trade_summary"] = shadow_summary
        except Exception as exc:
            pack["shadow_multi_trade_summary"] = {
                "status": "unavailable",
                "error_type": type(exc).__name__,
            }
    else:
        pack["shadow_multi_trade_summary"] = {
            "status": "skipped_by_caller",
            "command": "python -m app.research_lab shadow-multi-trade-status --hours 24",
        }

    # Capital/leverage simulator — produce a top-N by net PnL.
    if include_capital_leverage:
        try:
            from .capital_leverage_simulator import run_capital_leverage_simulator

            cap = run_capital_leverage_simulator(
                config, db, hours=min(int(hours) * 4, 720), timeframe="5m",
                symbols=symbols,
            )
            top = sorted(cap.scenarios, key=lambda item: item.net_pnl_usdt, reverse=True)[:5]
            pack["capital_leverage_top"] = {
                "capital_total_usdt": cap.capital_total_usdt,
                "warning": cap.warning,
                "top": [scenario.as_dict() for scenario in top],
            }
        except Exception as exc:
            pack["capital_leverage_top"] = {
                "status": "unavailable",
                "error_type": type(exc).__name__,
            }
    else:
        pack["capital_leverage_top"] = {
            "status": "skipped_by_caller",
            "command": (
                "python -m app.research_lab capital-leverage-sim "
                "--hours 720 --symbols DOTUSDT --capital 40 --margins 5,10,20 "
                "--leverages 3,5,10,20,50"
            ),
        }

    # Fee-aware exit trainer. By default we skip this in the pack (heavy) and
    # surface the CLI hint; callers can enable it explicitly.
    if include_fee_aware_exit:
        try:
            from .fee_aware_exit_trainer import run_fee_aware_exit_trainer

            fee_report = run_fee_aware_exit_trainer(
                config, db, hours=min(int(hours) * 4, 720), timeframe="5m",
                symbols=symbols,
            )
            pack["fee_aware_exit_best_per_symbol"] = dict(fee_report.best_per_symbol)
        except Exception as exc:
            pack["fee_aware_exit_best_per_symbol"] = {
                "status": "unavailable",
                "error_type": type(exc).__name__,
            }
    else:
        pack["fee_aware_exit_best_per_symbol"] = {
            "status": "skipped_by_caller",
            "command": (
                "python -m app.research_lab fee-aware-exit-trainer "
                "--symbols BTCUSDT,ETHUSDT,DOTUSDT --hours 720 --timeframe 5m"
            ),
        }

    # Suggested next actions — surfaced for the human reviewer.
    pack["suggested_next_actions"] = _suggested_actions(pack)
    # Reinforce safety markers (research_only, NO LIVE etc).
    pack["paper_filter_enabled"] = False
    pack["can_send_real_orders"] = False
    pack["final_recommendation"] = "NO LIVE"
    pack["activation"] = "disabled"
    pack.setdefault("omissions", []).extend([
        "research_pack_v5_excludes_secrets_env_db_dump_and_credentials",
        "shadow_summary_excludes_full_trade_list_above_20_rows",
    ])
    return pack


def _suggested_actions(pack: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    matrix = pack.get("ohlcv_freshness_matrix") or {}
    if isinstance(matrix, dict):
        if matrix.get("stale_count", 0) > 0:
            actions.append(
                "OHLCV stale rows present — run "
                "`python -m app.research_lab ohlcv-freshness-refresh "
                "--symbols ... --timeframes 5m,15m,1h --hours 120` "
                "(dry-run by default; pass --apply --allow-real-writes for a write)."
            )
        if matrix.get("need_data_count", 0) > 0:
            actions.append(
                "OHLCV missing rows present — run "
                "`python -m app.ohlcv_backfill --symbols ... --timeframes 5m,15m,1h --hours 720`"
            )
    clean = pack.get("training_data_clean_view") or {}
    if isinstance(clean, dict) and clean.get("overall_status") == "BAD":
        actions.append(
            "Data Pipeline duplicate_rate>=10% — use the clean view for EV/PF "
            "instead of raw counts: `python -m app.research_lab training-clean-view-audit --hours 720`"
        )
    shadow = pack.get("shadow_multi_trade_summary") or {}
    if isinstance(shadow, dict) and "pnl_summary" in shadow:
        pnl = shadow.get("pnl_summary") or {}
        if pnl.get("net_pnl_pct_sum", 0.0) < 0:
            actions.append(
                "Shadow multi-trade net PnL is negative — do NOT promote any "
                "candidate to paper/demo. Iterate filters in the regime filter lab."
            )
    if not actions:
        actions.append("no_specific_action_suggested_keep_research_only")
    return actions


def render_research_pack_v5_text(payload: dict[str, Any]) -> str:
    lines = ["RESEARCH PACK V5 START"]
    lines.append(f"generated_at: {payload.get('generated_at')}")
    lines.append(f"pack_version: {payload.get('pack_version')}")
    lines.append(f"git_version: {payload.get('git_version')}")
    lines.append(f"current_phase: {payload.get('current_phase')}")
    lines.append(f"final_recommendation: {payload.get('final_recommendation')}")
    lines.append(f"activation: {payload.get('activation')}")
    lines.append("safety:")
    for key, value in (payload.get("safety") or {}).items():
        lines.append(f"- {key}: {value}")
    matrix = payload.get("ohlcv_freshness_matrix") or {}
    if isinstance(matrix, dict):
        lines.append(
            f"ohlcv_freshness_matrix: stale={matrix.get('stale_count')} "
            f"need_data={matrix.get('need_data_count')} gap={matrix.get('gap_count')} "
            f"ok={matrix.get('ok_count')} actionable={matrix.get('overall_actionable')}"
        )
    clean = payload.get("training_data_clean_view") or {}
    if isinstance(clean, dict):
        lines.append(
            f"training_data_clean_view: status={clean.get('overall_status')} "
            f"duplicate_rate={clean.get('duplicate_rate')} "
            f"raw={clean.get('raw_sample_count')} clean={clean.get('clean_sample_count')}"
        )
    shadow = payload.get("shadow_multi_trade_summary") or {}
    if isinstance(shadow, dict) and "summary" in shadow:
        lines.append(f"shadow_multi_trade_summary: {shadow.get('summary')}")
    capital = payload.get("capital_leverage_top") or {}
    if isinstance(capital, dict) and "top" in capital:
        lines.append(f"capital_leverage_top_count: {len(capital.get('top') or [])}")
    fee = payload.get("fee_aware_exit_best_per_symbol") or {}
    if isinstance(fee, dict):
        lines.append(f"fee_aware_exit_best_per_symbol_keys: {list(fee.keys())}")
    lines.append("suggested_next_actions:")
    for action in payload.get("suggested_next_actions") or []:
        lines.append(f"- {action}")
    lines.append("omissions:")
    for item in payload.get("omissions") or []:
        lines.append(f"- {item}")
    lines.extend([
        "research_only: true",
        "paper_filter_enabled: false",
        "can_send_real_orders: false",
        "RESEARCH PACK V5 END",
    ])
    return "\n".join(lines)
