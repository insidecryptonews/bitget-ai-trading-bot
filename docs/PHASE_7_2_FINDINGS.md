# Phase 7.2 — Findings Report (2026-05-21)

Session goal: replace the "wait for live collection" path with historical OHLCV
backfill from Bitget public endpoints, then run the real strategy backtester
over real data to determine whether the current strategy has edge.

This document is intended to be shared with the user, ChatGPT, or any future
session as the canonical summary of what we learned today.

## TL;DR — updated after vault import (137k labeled samples)

1. **5m timeframe has no net edge.** Three independent data sources confirm:
   VPS labels (8.7k at 24h, 137k at 7-day), local backtest (3.4k trades, 90d),
   exit policy lab (9 alternative exits). All net_EV ≈ -0.18% per trade.

2. **4h timeframe has real but fragile edge.** Honest multi-timeframe backtest
   (1 year of real OHLCV): BTC net_EV +0.19%, ETH +1.08%, SOL +1.28%.
   But regime-dependent — last 2 months negative across all symbols.

3. **At 137k VPS labels, FILTERED edge exists.** Score ≥85 AND regime in
   (TREND_DOWN, RISK_ON) → net +0.12% per trade on 46,664 samples. Some
   specific (symbol, side, regime) setups are strongly positive at 1.5k–7k
   samples each.

4. **The user's intuition is data-confirmed.** A single threshold doesn't work,
   but a multi-strategy / multi-filter ensemble that excludes losing setups
   and includes winning ones could be net positive. Months of work to build
   properly — but the foundation evidence is there.

5. **MAJOR caveat**: the 137k-sample data is 7 days. It may reflect a
   TREND_DOWN-heavy week where SHORT trades had tailwind. Independent
   confirmation on 1-year historical data is required before committing.

## What was built this session

| Component | Path | Status |
|---|---|---|
| `ohlcv_candles` table | [app/database.py:745](app/database.py#L745) | Schema + indexes + idempotent batch insert + range queries |
| `get_history_candles` API method | [app/bitget_client.py:194](app/bitget_client.py#L194) | Bitget v2 history-candles endpoint (public, no auth) |
| OHLCV backfill CLI | [app/ohlcv_backfill.py](app/ohlcv_backfill.py) | Bidirectional resume, idempotent, rate-limited |
| Honest multi-tf backtest | [app/multi_tf_backtest.py](app/multi_tf_backtest.py) | Real higher-timeframe data instead of aliasing |
| Plan doc | [docs/OHLCV_BACKFILL_PLAN.md](docs/OHLCV_BACKFILL_PLAN.md) | Approved by user before implementation |
| This findings doc | [docs/PHASE_7_2_FINDINGS.md](docs/PHASE_7_2_FINDINGS.md) | — |
| Tests added | [tests/test_ohlcv_candles_table.py](tests/test_ohlcv_candles_table.py), [tests/test_ohlcv_backfill.py](tests/test_ohlcv_backfill.py) | 18 new tests, all pass |

Full test suite: **428 passing** (was 408 — added 20 without breaking any).

## Data collected this session

Local SQLite `bot_state.db` now contains:

| symbol | 5m (90d) | 1h (1y) | 4h (1y) |
|---|---|---|---|
| BTCUSDT | 25 919 | 8 759 | 2 189 |
| ETHUSDT | 25 920 | 8 759 | 2 189 |
| SOLUSDT | 25 920 | 8 759 | 2 189 |

Plus the VPS training vault `training_vault_20260520_223640.zip` (95 MB)
imported partially (background continuing). Contains:

- 316,380 signal_observations (7-day window)
- 138,209 signal_labels
- 39,043 signal_path_metrics
- 20,520 events
- 7 trades

## Backtest results

### 5m × 90 days (local backtester, full feature path)
| Symbol | Trades | gross_EV | net_EV | win_rate | TIME% |
|---|---|---|---|---|---|
| BTCUSDT | 943 | +0.005% | **-0.175%** | 33.8% | 71.0% |
| ETHUSDT | 1154 | -0.005% | **-0.185%** | 34.4% | 69.7% |
| SOLUSDT | 1319 | +0.043% | **-0.137%** | 37.8% | 69.2% |

### 1h × 365 days
| Symbol | Trades | net_EV | net_PF |
|---|---|---|---|
| BTCUSDT | 1191 | -0.16% | 0.84 |
| ETHUSDT | 1066 | +0.033% | 1.03 |
| SOLUSDT | 1064 | +0.050% | 1.04 |

### 4h × 365 days HONEST multi-TF (real 1h confluence)
| Symbol | Trades | net_EV | net_PF | win_rate | Max DD |
|---|---|---|---|---|---|
| **BTCUSDT** | **204** | **+0.191%** | **1.14** | 46.6% | 57.2 |
| ETHUSDT | 84 | +1.076% | 1.79 | 56.0% | 54.3 |
| SOLUSDT | 40 | +1.278% | 2.04 | 60.0% | 37.4 |

### Walk-forward (BTCUSDT 4h honest, 12 monthly buckets)

12 months: 6 positive, 6 negative. Cumulative sum +38.99% over 204 trades.
Recent 4-of-5 months negative (Dec 2025, Apr/May 2026) — possible regime shift.
Best months: Aug 2025 (+42%), Jan 2026 (+48%).

## VAULT IMPORT — Score / Regime / Setup breakdown (137k labels, 7-day window)

Cost assumption: ~0.18% round-trip (12 bps fees + 6 bps slippage).

### By score bucket
```
bucket    samples     gross%       net%    win   TP%   SL%  TIME%
70-74     30 044    -0.0315    -0.2115  44.5%   2.9   7.0   90.0  ← loses
75-79     18 164    +0.0688    -0.1112  46.5%   9.2  14.6   76.2  ← loses
80-84     20 352    +0.0719    -0.1081  42.8%   7.0  14.1   78.8  ← loses
85-89     31 611    +0.2037    +0.0237  48.2%  18.8  10.8   70.3  ← WINS
90-94     16 685    +0.0764    -0.1036  46.8%   7.7   9.7   82.6  ← loses (anomaly)
95-100    20 307    +0.2290    +0.0490  52.2%  16.3   8.9   74.7  ← WINS
```

### By regime
```
RANGE         4 253  gross=-0.335%  net=-0.515%  win=30.0%  ← worst
RISK_OFF     41 498  gross=-0.041%  net=-0.221%  win=40.2%  ← loses
RISK_ON       5 811  gross=+0.392%  net=+0.212%  win=53.5%  ← WINS
TREND_DOWN   59 666  gross=+0.271%  net=+0.091%  win=55.5%  ← WINS
TREND_UP     25 550  gross=-0.041%  net=-0.221%  win=39.2%  ← loses (counterintuitive)
```

### Top setups (score ≥85, samples ≥100, by net_pct)
```
symbol    side  regime       samples  net%
LINKUSDT  SHORT TREND_DOWN     3 373   +0.446%   ← STRONG
AVAXUSDT  SHORT TREND_DOWN     1 609   +0.440%
DOGEUSDT  LONG  RISK_ON        1 557   +0.402%
DOGEUSDT  SHORT TREND_DOWN     4 006   +0.272%
ADAUSDT   SHORT TREND_DOWN     3 893   +0.252%
DOTUSDT   SHORT TREND_DOWN     1 277   +0.237%
SOLUSDT   SHORT TREND_DOWN     7 169   +0.148%
XRPUSDT   SHORT TREND_DOWN     6 343   +0.124%
ETHUSDT   SHORT TREND_DOWN     6 011   +0.115%
BNBUSDT   SHORT TREND_DOWN       715   +0.072%
                          -- below 0 --
BTCUSDT   SHORT TREND_DOWN     5 864   -0.066%
ADAUSDT   LONG  RISK_ON          140   -0.146%
DOTUSDT   LONG  RISK_ON          164   -0.261%
BTCUSDT   LONG  TREND_DOWN       452   -0.287%
ETHUSDT   LONG  TREND_DOWN       478   -0.363%
BTCUSDT   LONG  RISK_ON          538   -0.380%
... all other LONG TREND_DOWN: net -0.40% to -0.63%
```

**Pattern**:
- **SHORT in TREND_DOWN works for almost all altcoins** (10 of 11 are net positive).
- **BTCUSDT is the consistent exception** — its SHORT in TREND_DOWN loses slightly. Most liquid → tightest spreads → least edge extractable.
- **LONG in TREND_DOWN consistently loses** (counter-trend trying to catch reversals — doesn't work).
- **DOGE/XRP LONG in RISK_ON are positive** — high beta in risk-on periods.

## CRITICAL caveats on the 137k finding

1. **7 days is one week.** Could be a TREND_DOWN-heavy week (BTC sold off, altcoins followed). The apparent edge in "SHORT TREND_DOWN" might just be "the market actually went down during this week, so SHORT bias won". This is regime tailwind, not necessarily strategy edge.

2. **Source mixing.** The 137k includes `trade_signal`, `shadow_signal`, and `market_probe` sources. Trade_signal is what the bot would actually trade; shadow and probe are variants/dummies that don't reflect real execution.

3. **Cost assumption 0.18% is approximate.** Real Bitget cost depends on taker/maker, symbol-specific spreads, slippage at fill. Altcoins typically have higher slippage than BTC. If real cost is 0.22%, several "winning" setups become neutral.

4. **No drawdown analysis at this level.** Even if average per-trade is positive, the equity curve might have 50%+ drawdowns that wipe a leveraged account.

5. **Walk-forward not done at this level.** A single 7-day sum doesn't tell us if the edge is stable month-over-month.

## What this changes vs the earlier "no edge" verdict

The earlier conclusion ("strategy has no edge") was based on:
- 24h dashboard score-bucket data (small sample, noisy)
- 90-day 5m backtest (5m timeframe is fee-toxic regardless of strategy quality)

The updated picture at 137k labels:
- **Unfiltered net EV is still negative** (≈ -0.08%). Most score buckets lose.
- **Filtered subset (score ≥85 AND regime in TREND_DOWN/RISK_ON) is net positive** (+0.12%).
- **Specific setups (LINK/AVAX/DOGE SHORT in TREND_DOWN) are strongly positive** (+0.27 to +0.44%).

The "filtered edge" is what the project's `paper_policy_filter` was designed to capture. With 137k samples we now have evidence that **the filter has a real job to do** — it's not just gating against a strategy that has nothing.

## Recommendations — what comes next

### Tier 1 — Immediate (next 1–2 sessions)

**T1.1 Confirm setups against 1-year historical OHLCV.** The 4h data for BTC/ETH/SOL exists in `ohlcv_candles`. Backfill 1h for the other symbols (LINK, AVAX, DOGE, ADA, DOT, XRP, BNB). Run the same RealStrategyBacktester filtered to the top setups. If 1-year backtest also shows positive net EV on these (symbol, side, regime) combos, the edge is structural. If only 7-day shows it, it's regime tailwind from this week.

**T1.2 Walk-forward each winning setup.** Split each (symbol, side, regime) combo into monthly buckets and check stability. A combo with +0.4% avg but only 1 month carrying the year is fragile.

**T1.3 Cost stress.** Re-run filtered analysis with cost 0.22% and 0.25%. If most setups survive, the edge is robust; if they collapse, the margin is too thin.

### Tier 2 — Paper filter design (Phase 7.3, days)

**T2.1 Build the filter** as a config-driven allowlist of (symbol, side, regime, score_min) tuples. Activate in shadow mode first.

**T2.2 Run 30 days forward in paper-mode.** Compare actual forward results to backtest expectation. If they match within 1 sigma, edge is real and forward-tradable.

### Tier 3 — Multi-strategy architecture (Phase 7.4, weeks)

Same as original: regime-aware ensemble, per-strategy paper-filter gating,
risk budget split. This is what the user described as "mega algoritmo" and
the data now supports it as the correct path.

### Tier 4 — Live readiness, micro-live (months out)

Same as before. No timeline change.

## Files for the next session

- `bot_state.db` — local SQLite with 89 k OHLCV candles + ~64k+ imported observations (background import may still be running).
- `C:/Users/Adrian/Downloads/bitget-ai-trading-bot_training_training_vault_*.zip` — 8 vault zips covering May 15–20. Latest one (20260520_223640) imported.
- `docs/OHLCV_BACKFILL_PLAN.md` — approved technical plan.
- `docs/PHASE_7_2_FINDINGS.md` — this file.
- `app/ohlcv_backfill.py` — ready to extend to more symbols.
- `app/multi_tf_backtest.py` — honest-multi-tf variant.

## Safety status (unchanged)

- LIVE_TRADING: false
- DRY_RUN: true
- PAPER_TRADING: true
- ENABLE_PAPER_POLICY_FILTER: false
- can_send_real_orders: false
- Real orders sent today: 0
- Private endpoints touched: 0
- VPS state changed: no (all work was local against SQLite)
- final_recommendation: **NO LIVE**

## Reminder for user

- The dashboard token pasted in chat (`adrian-dashboard-2026-superseguro-9281`)
  should be rotated when convenient (change `DASHBOARD_AUTH_TOKEN` in VPS env).
- Downloaded vaults in `C:/Users/Adrian/Downloads/` are 800+ MB total. Move
  to a permanent location or delete after we've extracted what's needed.
- Background import (b1l4u14y6) of the vault into local SQLite continues. Not
  blocking anything; when done you'll have ~316k observations + 138k labels
  locally for next session's analysis without re-importing from zip.
