# Phase 7.4B — Learning Quality & Exit Intelligence Plan

Status: **design only**. None of these modules has runtime hooks. Nothing here
operates with real money, paper filter remains OFF, and no live decision is
automated.

## Dependencies (must happen first, in order)

1. **Phase 7.4A** (this sprint) audits and documents the data quality and label
   quality state.
2. **OHLCV 5m backfill** on VPS for all 10 symbols × 90 days. Until this runs,
   labs that depend on bar_path return `NEED_DATA`.
3. **Data quality repair plan** (separate session) decides whether to clean
   duplicates/orphans by marking, ignoring, or sanitising them. NO blind delete.
4. **Label quality remediation** (separate session) reviews the labeler if
   `tp_too_far` / `sl_too_tight` flags are confirmed on real data.

## Lab modules (design only in this phase)

### profit_lock_exit_lab

Compares baseline TP/SL/TIME vs:
- break-even after MFE ≥ 0.50%
- break-even after MFE ≥ 0.80%
- trailing ATR×1.2
- signal-decay exit (close when score < N during hold)

Metrics: net_EV, net_PF, TP/SL/TIME mix, failed_winners, MFE capture ratio,
max drawdown.

**Requires**: ohlcv_5m_persisted, signal_path_metrics with bar_path, cost_model.

### fast_exit_lab

Studies offline whether closing positions on:
- score_decay_below_threshold
- opposite_side_signal_present
- no_follow_through_after_N_bars
- spread_widening
- btc_eth_alignment_flip

improves edge vs holding.

### mtf_regime_gate_lab

Evaluates which regime filters (BTC 15m alignment, ETH 1h alignment, block
RISK_OFF longs, block RANGE/CHOPPY, restrict TREND_DOWN to SHORT) improve
net_EV without killing sample size.

### momentum_burst_5m_lab

Wraps the existing `app/momentum_burst_lab.py` features (return_1m/3m/5m/8m/15m,
acceleration, volume_spike, etc.) onto 5m OHLCV when the table is populated.

Until OHLCV 5m is loaded, this lab returns `NEED_DATA`. Once loaded, exposes:
- per-day signal frequency
- net_EV with cost stress at 0.18% / 0.22% / 0.25%
- expected_move_to_cost ratio guard ≥ 3×

### setup_key_trainer

Labels each setup_key (symbol+side+regime+score_bucket+source+exit_policy) as:
`BASURA` / `INSUFFICIENT_DATA` / `WATCH` / `SHADOW_CANDIDATE` /
`PAPER_CANDIDATE_BLOCKED` / `MARKET_PROBE_ONLY`.

Rules:
- market_probe NEVER promoted to actionable (already enforced in
  candidate_incubator_v2 in Phase 7.3)
- Minimum samples per setup
- Monthly stability check
- Cost sensitivity at 0.22% and 0.25%
- expected_move_to_cost_ratio minimum

### net_ev_trainer

Ranks setups by realistic net_EV with penalties for:
- high TIME%
- small sample
- market_probe origin
- cost sensitivity failure
- single-month carry > 60%

Outputs a ranked list. NEVER auto-activates anything.

### microstructure_roadmap

See `docs/MICROSTRUCTURE_ROADMAP.md`. NO implementation in 7.4B.

## Roadmap (when to actually build each lab)

| Step | Lab | Trigger |
|---|---|---|
| 1 | `momentum_burst_5m_lab` | Once OHLCV 5m × 10 symbols × 90 days is loaded on VPS |
| 2 | `setup_key_trainer` | Right after step 1, uses signal_outcomes table |
| 3 | `net_ev_trainer` | Right after step 2 |
| 4 | `mtf_regime_gate_lab` | Once net_ev_trainer surfaces any setup with positive net_EV |
| 5 | `profit_lock_exit_lab` | Once any setup demonstrates edge for >30 days in shadow |
| 6 | `fast_exit_lab` | After profit_lock_exit_lab informs which exits help |
| 7 | microstructure | Only after a candidate is validated end-to-end (years out) |

## Strict invariants for ALL Phase 7.4B work

- NO runtime hook.
- NO `.env` change to enable.
- NO automatic paper filter activation.
- NO automatic live activation.
- NO modification of leverage/margin/sizing/slots.
- NO order placement.
- NO market making.
- NO WebSocket runtime.
- ALL final reports include `final_recommendation: NO LIVE`.

If any decision implies risk to real money, **stop and propose** rather than
execute.

FINAL_RECOMMENDATION: **NO LIVE**
