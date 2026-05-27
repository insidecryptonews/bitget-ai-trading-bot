# ResearchOps V5 — Pre-Paper Readiness Suite

**Status:** research-only. **Never** activates live trading, paper filter, or
real orders. All modules ship with `final_recommendation: NO LIVE`,
`paper_filter_enabled: false`, `can_send_real_orders: false`.

## What was implemented

| Module | Purpose |
| --- | --- |
| `app/ohlcv_freshness_manager.py` | Multi-symbol/timeframe OHLCV freshness matrix; dry-run refresh that delegates to the existing public-only `app.ohlcv_backfill`. Auto-refresh runtime stays disabled via `config.enable_ohlcv_auto_refresh`. |
| `app/training_data_clean_view.py` | Read-only audit producing RAW vs CLEAN sample counts, `duplicate_rate`, `dedupe_ratio`, orphan metrics, and a recommended next action. |
| `app/shadow_multi_trade_learning.py` | Generates virtual trades from OHLCV replays. Never calls `PaperTrader.open_position`, never touches paper slots, never opens real orders. |
| `app/capital_leverage_simulator.py` | Pure math for capital / margin / notional / leverage scenarios with net PnL, ROE, break-even moves, and a conservative liquidation-distance estimate. |
| `app/fee_aware_exit_trainer.py` | Wraps `net_profit_lock_lab` and tries `net_profit_lock_pct` ∈ {0.40, 0.60, 0.80, 1.00, 1.20, 1.50, 2.00} per symbol, blocking promotion when `gross_green_net_negative` or `maker_maker_audit_only`. |
| `app/phase9_paper_readiness_validator.py` | Extended with V2 hard gates: `data_quality_status=BAD` → `REJECT_DATA_QUALITY`; `gross_green_net_negative=True` → `REJECT_NEGATIVE_NET`; catastrophic fold (≤ −10% EV) → `REJECT_CATASTROPHIC_FOLD`. Backward compatible: V5 inputs are additive (off by default). |
| `app/research_pack_v5.py` | Composes the V5 ChatGPT export pack on top of the existing v4 pack. No secrets, no DB dump, no .env values. |

## What was NOT activated

- **Live trading**, **paper filter**, **candidate shadow monitor runtime**,
  **real orders**: untouched.
- **Leverage / margin / sizing / slots config**: untouched.
- **VPS / .env / DB**: untouched.
- **OHLCV auto-refresh runtime**: hidden behind `config.enable_ohlcv_auto_refresh`
  (default `False`). Dashboard endpoints only run dry-run.

## Why DOT is still blocked

The Phase 8B walk-forward for DOTUSDT had only 3 of 4 folds passing
(`WARN`) and `anti_overfit_status=WARN`. Phase 9 V2 only emits
`PAPER_DEMO_READY_MANUAL_REVIEW_ONLY` when **all** gates pass — so DOT remains
`RESEARCH_PROMISING_NOT_ACTIONABLE`. Adding the V5 gates can only make
promotion harder, never easier.

## CLI quick reference

```bash
# OHLCV freshness matrix (status only)
python -m app.research_lab ohlcv-freshness-status \
  --symbols BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,BNBUSDT,LINKUSDT,AVAXUSDT,ADAUSDT,DOTUSDT \
  --timeframes 5m,15m,1h

# OHLCV freshness refresh — dry-run by default
python -m app.research_lab ohlcv-freshness-refresh \
  --symbols BTCUSDT,ETHUSDT,DOTUSDT --timeframes 5m,15m,1h --hours 120 --dry-run

# Apply (requires both --apply and --allow-real-writes)
python -m app.research_lab ohlcv-freshness-refresh \
  --symbols BTCUSDT,ETHUSDT,DOTUSDT --timeframes 5m,15m,1h --hours 120 --apply --allow-real-writes

# Clean view audit (RAW vs CLEAN samples, duplicate_rate)
python -m app.research_lab training-clean-view-audit --hours 720

# Shadow multi-trade replay (research-only)
python -m app.research_lab shadow-multi-trade-replay \
  --symbols BTCUSDT,ETHUSDT,DOTUSDT --hours 720 --timeframe 5m

# Capital / leverage scenarios
python -m app.research_lab capital-leverage-sim \
  --symbols DOTUSDT --hours 720 --timeframe 5m \
  --capital 40 --margins 5,10,20 --leverages 3,5,10,20,50

# Fee-aware exit trainer multi-symbol
python -m app.research_lab fee-aware-exit-trainer \
  --symbols BTCUSDT,ETHUSDT,DOTUSDT --hours 720 --timeframe 5m
```

## Reading capital / margin / notional / leverage

- `notional = margin × leverage`. Bigger notional → bigger fees → higher
  break-even price move.
- ROE ≠ edge. If `net_pnl_usdt ≤ 0`, the scenario is reported as
  `promotion_eligible: false` regardless of ROE.
- More capital does NOT convert a negative percentage EV into a positive
  one. It only scales the absolute PnL.
- `liquidation_distance_estimate_pct ≈ (1/L) × 0.95`. Informational only.

## Why live stays blocked

- `LIVE_TRADING=False`, `DRY_RUN=True`, `PAPER_TRADING=True`,
  `ENABLE_PAPER_POLICY_FILTER=False`,
  `ENABLE_CANDIDATE_SHADOW_MONITOR=False`, `can_send_real_orders=False`.
- The Phase 9 V2 validator returns `PAPER_DEMO_READY_MANUAL_REVIEW_ONLY` only
  when every gate passes, and even then the candidate dataclass hardcodes
  `paper_filter_enabled: false` and `can_send_real_orders: false`.
- The paper portfolio allocator is design-only and disabled.

## Dashboard

`/dashboard?token=…` now has a `ResearchOps V5` sidebar entry with five panels
(Freshness matrix / Clean view / Shadow multi-trade / Capital & leverage /
Fee-aware exits) plus a `GENERAR PACK PARA CHATGPT V5` button that returns
JSON/text via `/api/research-pack-v5`. Heavy endpoints respect `allow_heavy=0`
by default and return `SKIPPED_HEAVY` with a CLI hint.
