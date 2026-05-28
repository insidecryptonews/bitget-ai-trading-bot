# ResearchOps V6 — Clean Metrics + Operator Cockpit Dashboard Redesign

**Base commit:** `3b2faf3` (ResearchOps V5.1). **Research-only**: no live,
no paper filter, no real orders, no leverage/margin/sizing/slots changes,
no `.env` touches. `final_recommendation: NO LIVE`.

## What changed

### 1. Clean Metrics Enforcement

`app/clean_research_metrics.py` is the new central helper. Every research
decision must consume it instead of raw counts. The helper computes BOTH
`raw_*` and `clean_*` metrics (sample count, net EV, PF, win rate, TP/SL/TIME
distribution) and labels the result with:

- `data_quality_status` — OK / WARNING / BAD / UNKNOWN.
- `confidence` — HIGH / MEDIUM / LOW.
- `blocked_gate` — populated when promotion must be refused.
- `duplicate_impact_pct` — |raw_ev − clean_ev| (high values flag silently
  dangerous RAW-based decisions).

**Downstream:**

- `app/strategy_research_enhancer.py` now builds rankings on the CLEAN
  (de-duplicated) shadow trades. It also exports a *reference-only* RAW
  ranking the dashboard surfaces with a `DO NOT PROMOTE RAW` warning.
- `app/phase9_paper_readiness_validator.py` accepts `require_v6_clean_gate`
  (default `True`). When the central helper says BAD, the validator escalates
  to `data_quality_status=BAD`; when RAW is positive but CLEAN is negative,
  it forces `gross_green_net_negative=True` so promotion is rejected as
  cost-failure (`REJECT_NEGATIVE_NET`).
- `app/research_pack_v5.py` adds a `clean_research_metrics` section + extends
  `known_issues` with the new gates so ChatGPT can read them.
- `app/health_server.py` exposes `GET /api/research/clean-research-metrics`.

### 2. Strategy Research Hypotheses (research only)

The enhancer ships 13 hypotheses, each evaluated against CLEAN metrics:

1. `short_only_candidate`
2. `block_long`
3. `risk_off_short_filter`
4. `no_trade_in_choppy`
5. `minimum_expected_move_after_fees`
6. `entry_anti_late_filter`
7. `hold_while_direction_valid`
8. `profit_lock_mfe_aware`
9. `time_death_reducer`
10. `volatility_aware_stop_tp`
11. `score_calibration_net_aware`
12. `correlation_guard_shadow`
13. `session_time_of_day_filter`

Each one returns one of `RESEARCH_PROMISING / NEED_MORE_DATA /
REJECT_NEGATIVE_NET / REJECT_DATA_QUALITY / REJECT_OVERFIT_RISK /
REJECT_COSTS / SHADOW_ONLY` plus the data the operator should collect next.
There is **no path** from a hypothesis to PAPER_READY or LIVE_READY.

### 3. Dashboard V6 Operator Cockpit (visual redesign)

The Overview is **visibly different**. The old `hero-card` with the giant
"PAPER ONLY. NO LIVE. Keep research." title is hidden behind a legacy compat
marker (kept for the V3 smoke test). The new Overview is built around:

- **Command Status bar** at the top: SAFE PAPER ONLY / NO LIVE / DRY RUN
  badges + git + last scan + final recommendation + next action + Refresh
  / Pack-for-ChatGPT buttons.
- **6 Operator Cockpit cards**: Safety / Data Freshness / Data Quality /
  Learning · Shadow / Edge (CLEAN) / Readiness. Each card uses a coloured
  left bar (green/amber/red/blue/purple/red) so state reads in <10s.
- **"Qué está bloqueando el avance"** panel: list of blockers
  (DATA_QUALITY_BAD / OHLCV_STALE / LOW_SAMPLE / RAW_CLEAN_DIFF /
  NEGATIVE_NET / LIVE_DISABLED / PAPER_FILTER_OFF) each with severity,
  short explanation, and recommended next action.
- **"Mejores pistas actuales (CLEAN)"** table: top-10 leads ranked by clean
  net EV with `why not promoted` column. Never more than 10 rows on the
  first view.
- **"Peores cosas a evitar"** panel: worst side / worst symbols / cost-time
  traps.
- **RAW vs CLEAN** panel: raw sample / clean sample / duplicate rate /
  duplicate impact %. Pinned `DO NOT PROMOTE RAW` badge.

Legacy V5 detail (charts + KPI grid + main analysis refresh) is preserved
under a collapsible `<details>` so the V5/V5.1 panels keep working.

## What was NOT activated

- Live trading, paper filter, candidate shadow monitor runtime, real
  orders: untouched.
- Leverage / margin / sizing / slots config: untouched.
- VPS / `.env` / DB: untouched.
- `enable_ohlcv_auto_refresh` remains `False`. Dashboard cannot trigger real
  OHLCV writes.

## How to read the new dashboard

1. **Top bar**: badges + worker + git + last scan tell you whether the bot
   is safe and connected. If "next action" reads `Resolver bloqueos rojos`,
   the cockpit found hard blockers; address those first.
2. **6 cards**: glance at the left bars. Red = blocker, amber = warning,
   green = OK, purple = shadow-only research.
3. **Qué está bloqueando**: every blocker has a tag, a one-line explanation,
   and the exact CLI / config change needed to clear it.
4. **Mejores pistas**: top 10 leads with `clean EV / clean PF / sample / decision`.
   If the column "why not" says `research only`, the lead is good but must
   stay in research (no auto-promotion).
5. **RAW vs CLEAN**: if `RAW EV > 0` but `CLEAN EV < 0`, the dashboard pins
   the `RAW_CLEAN_DIFF` blocker. **Never promote RAW**.

## CLI quick reference (V6)

```bash
# Central clean metrics — RAW vs CLEAN snapshot.
python -m app.research_lab clean-research-metrics --hours 720 \
  --symbols BTCUSDT,ETHUSDT,DOTUSDT --timeframes 5m

# Strategy Research Enhancer — uses CLEAN metrics, surfaces 13 hypotheses.
python -m app.research_lab strategy-research-enhancer --hours 24 \
  --symbols BTCUSDT,ETHUSDT,DOTUSDT

# Phase 9 readiness with V6 gate on by default.
python -m app.research_lab phase9-paper-readiness --hours 720 \
  --symbols DOTUSDT --min-trades 250 --folds 4
```

## Honest read on edge

DOT remains `RESEARCH_PROMISING_NOT_ACTIONABLE`. With CLEAN metrics enforced,
LONG side stays negative and `data_quality_status` is BAD because the raw
duplicate_rate is ~48%. SHORT side may *look* promising on RAW; the V6
helper rejects the RAW reading and waits for clean samples to grow.

**There is no edge accionable** — V6 is a guardrail, not a green light.
Paper / demo / live continue blocked.

## Why live stays blocked

- `LIVE_TRADING=False`, `DRY_RUN=True`, `PAPER_TRADING=True`,
  `ENABLE_PAPER_POLICY_FILTER=False`,
  `ENABLE_CANDIDATE_SHADOW_MONITOR=False`, `can_send_real_orders=False`,
  `enable_ohlcv_auto_refresh=False`.
- Even when the validator emits `PAPER_DEMO_READY_MANUAL_REVIEW_ONLY`, the
  dataclass hardcodes `paper_filter_enabled=False` and
  `can_send_real_orders=False`. The validator returns *labels* — no
  activations.
