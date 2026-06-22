# ResearchOps V10.9 — Provider Gap Plan (OI history + liquidations)

> Research-only. NO paid download before offline sample validation. NO LIVE.

## What is missing
- long historical open interest (per-symbol series)
- historical liquidations
- full 365d OHLCV on low TFs

## Why Bitget public is insufficient
- candles endpoint caps a single request at a 90-day interval
- public history is materially under a year on some symbols/timeframes
- no public historical OI series
- no public historical liquidations

## Minimum required data
- OHLCV 365d
- funding 365d
- OI historical 365d
- liquidations 365d
- optional trades/orderbook

## Candidate providers
- Tardis.dev (preferred_sample_candidate): OHLCV/OI/funding/liquidations/trades; request a free sample first
- CoinGlass (fallback): OI/liquidations aggregated
- Coinalyze (limited): intraday retention cap ~84d
- Kaiko / CryptoCompare (evaluate): enterprise; verify ToS

## Provider checklist
- [ ] Bitget USDT-perp coverage
- [ ] 365d+ OHLCV
- [ ] OI history
- [ ] liquidations history
- [ ] license allows research
- [ ] sample before payment
- [ ] stable schema

## Sample request text

We need a 365-day historical SAMPLE for Bitget USDT perpetuals (BTC/ETH + 8 alts) at 1h/4h/6h: OHLCV, funding, OPEN INTEREST series and LIQUIDATIONS, UTC unix-ms timestamps, CSV/JSONL. Research/eval use; no payment before we validate the sample offline.

## Rejection criteria
- no Bitget perps
- no OI history
- no liquidations
- <365d
- license forbids research
- no sample
- unstable/garbled schema

## Safety
- no_paid_download_before_sample_validation: true
- research_only: true
- paper_ready: false
- live_ready: false
- FINAL_RECOMMENDATION: NO LIVE
