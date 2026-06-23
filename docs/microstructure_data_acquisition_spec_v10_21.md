# Microstructure Data Acquisition Spec (V10.21)

Purpose: the exact, minimal data to acquire so the bot can search for a **real**
edge (not the beta/regime artifacts that public OHLCV produced across V10.13–20).
This is a procurement checklist for a HUMAN action. NO paid download or API-key
use is performed by the bot. RESEARCH ONLY. NO LIVE.

## Why (one line)
OHLCV (1m→1d, Bitget/Binance/Bybit) has no validated non-beta edge after costs.
The information that's missing — order flow, real spread, positioning — lives in
microstructure data. That is the only credible unlock left.

## What to request (priority order)

### Tier 1 — highest value
1. **Trades (tick) with aggressor side** — every trade: ts(ms), price, size, side
   (buy/sell aggressor), trade_id. Enables real order-flow imbalance + true fills.
2. **L2 orderbook snapshots** — top 10–25 levels at ≥1s (ideally 100ms) cadence:
   ts, bid/ask prices+sizes per level. Enables real spread, depth, queue, fill
   probability (the thing the flat 2bps proxy could not model).

### Tier 2 — strong context
3. **Historical Open Interest** — ts, OI, OI value, per symbol (≥1h cadence).
4. **Liquidations** — ts, side, price, qty, notional.
5. **Funding rate history** — ts, rate (we already have this from Bitget public).

## Scope
- Symbols: BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT, DOGEUSDT (USDT perps).
- Venue: Binance USDT-M futures and/or Bybit linear (where our 365d OHLCV already lives).
- Depth: **180 days minimum, 365 preferred**, spanning bull+bear+range (we have
  the matching OHLCV to align).
- Format: CSV or Parquet, UTC ms timestamps, one file per symbol per data-type.

## Candidate providers (verify license BEFORE any download — DO NOT auto-pay)
- **Tardis.dev** — best coverage (trades + L2 + derivatives), normalized history,
  paid sample. RECOMMENDED for a first 180/365d sample.
- **Amberdata / Kaiko** — institutional, broader, pricier.
- **Coinalyze / CoinGlass** — good for OI + liquidations + funding (cheaper), but
  NOT raw trades / full L2.
Pick ONE Tier-1 source (trades+L2) + optionally a cheap OI/liq source.

## Canonical schemas (already defined in the repo)
Match `app/labs/intraday_data_foundation_v10_13.py: canonical_intraday_schemas()`:
- ohlcv_intraday, **trades**, **orderbook**, open_interest, liquidations.
Place files in a staging dir; do NOT write to `external_data/raw` or any DB.

## Plug-and-play path once the sample arrives (all already built)
1. Stage the sample (flat files, canonical columns).
2. `intraday-data-readiness-v1013 --sample-dir <dir>` → must reach
   `MICROSTRUCTURE_PARTIAL`/`READY` (it currently returns NO_INTRADAY_DATA).
3. `intraday-sample-build-v1013` → manifest + dataset hash + quality audit.
4. `intraday-to-shadow-readiness-v1013` → must say `READY_FOR_MICRO_SCALP_REPLAY`.
5. Re-run the chain on REAL microstructure features:
   - V10.10 micro-scalp tournament, V10.11 pattern memory, V10.12 intelligent
     shadow scalper — now with **real spread + order-flow + fill modeling**.
   - V10.8 (now temporally honest: uniform_time full-period + coverage gate).
6. Only a candidate that: net_EV>0 after REAL costs, survives cost-stress ×2 at
   candidate level, multi-symbol, multi-regime, FDR not HIGH, beats baselines,
   and survives **weeks of forward-shadow** → becomes a paper candidate (future).
   Nothing is auto-promoted; NO LIVE.

## Hard guardrails (unchanged)
No paid download/activation by the bot, no API keys, no private endpoints, no
raw/DB writes, no live/paper. The human acquires + licenses the data; the bot
only validates and researches it.

research_only: true
shadow_only: true
paper_ready: false
live_ready: false
can_send_real_orders: false
final_recommendation: NO LIVE
