# Dashboard Trader Read-Only — Data Contract (V10.2)

**Status: CONTRACT ONLY. No UI implemented in this phase.** This document
defines what a future *read-only* trader dashboard must display and the data
it consumes. It is a specification, not code.

> ⚠️ **Read-only by construction.** The dashboard described here:
> - has **NO order buttons**, **NO "go live" button**;
> - has **NO leverage / margin / sizing controls**;
> - **cannot** modify `.env`, flags, or any runtime config;
> - is **visualization only** — it reads research/paper state and never writes.
>
> It is unblocked only **after** long-history validation
> (`dashboard_next_phase: TRADER_READONLY_AFTER_LONG_HISTORY_VALIDATION`).

---

## A. Trading cockpit
Fields: `mode`, `open_positions`, `paper_shadow_live_blocked` (always shows
live = blocked in this phase), `daily_pnl`, `weekly_pnl`, `total_pnl`,
`equity_curve`, `drawdown`, `winrate`, `profit_factor`, `open_trades`,
`closed_trades`.
Source: paper/shadow research state (read-only). No write paths.

## B. Live operations table (per trade)
`timestamp`, `symbol`, `direction`, `entry`, `current_price`, `pnl_pct`,
`pnl_usd`, `stop`, `take_profit`, `time_in_trade`, `status`, `entry_reason`,
`exit_reason`, `strategy_bucket`.
Source: paper/shadow trade ledger (read-only).

## C. Price charts
OHLC candles; `entry_marker`; `exit_marker`; `stop_line`; `tp_line`;
`funding_overlay`; `oi_overlay`; `liquidation_markers`.
Source: `ohlcv_candles` (read) + external_data clean funding/OI/liquidations
(read). Overlays are visual only.

## D. Signal monitor
`active_candidates`, `blocked_signals`, `no_trade_reasons`, `bucket_status`,
`stability_status`, `oos_status`.
Source: diagnostics + stability reports (read-only). Shows why nothing trades.

## E. Risk / safety panel
`can_send_real_orders` (must show `false`), `LIVE_TRADING` (`false`),
`DRY_RUN` (`true`), `PAPER_TRADING` (`true`), `paper_filter_enabled` (`false`),
`worker_lock`, `duplicate_worker_warning`, `circuit_breaker`,
`daily_loss_limit`, `weekly_loss_limit`.
Source: config + worker health (read-only). Display only — no toggles.

## F. Edge panel (per active research bucket)
`active_bucket`, `net_ev`, `edge_vs_baseline`, `ci_low`, `stability_status`,
`cost_x2_status`, `missing_oi_risk`, `final_policy_status`.
Source: V10.1/V10.2 reports (read-only).

---

## Hard contract (must hold for the implementation phase)
- READ-ONLY. No order placement, no `place_order`, no execution calls.
- No "activate live" / "enable paper filter" controls.
- No leverage / margin / sizing / slot controls.
- No `.env` editing, no secret display.
- No DB writes from the dashboard.
- Live trading remains blocked; `can_send_real_orders` stays `false`.

**FINAL_RECOMMENDATION: NO LIVE.**
