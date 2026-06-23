# Intraday Equity -> Crypto Lead-Lag Readiness Plan (V10.22)

Prepares the study of the original hypothesis: **"NVDA/QQQ/tech sell off hard
during the US session and crypto reacts hours later."** V10.20 showed cross-asset
adds nothing at DAILY resolution (coincident, not leading). The only place the
idea could still live is **intraday**. This is the plan; the study is a later phase.

RESEARCH ONLY. NO LIVE. No paid download, no keys, no heavy dependency.

> Note: this plan is kept under `docs/` (committable, durable) rather than the
> gitignored `reports/research/v10_22/` so it is not lost. Generated study
> artifacts will go under `reports/research/v10_22/`.

## 0. Feasibility — CONFIRMED (free)
Yahoo chart intraday works from here (probed 2026-06-23):
- Equities (NVDA/QQQ/SPY): `interval=15m` and `1h`, `range=60d` -> ~60 days,
  US session 13:30–20:00 UTC (1561 bars @15m, 421 @1h).
- Crypto (BTC-USD/ETH-USD): `interval=1h`, `range=60d`, 24/7 (1429 bars).
- **Limitation: ~60 days only** at intraday granularity -> enough for a FIRST
  study, too short for strong OOS. Flag results as preliminary; a longer
  intraday history needs a provider (see V10.21 microstructure spec).

## 1. Data needed
- Equities/ETF: NVDA, QQQ, SPY, SMH, MSTR, COIN (Yahoo; MSTR/COIN to verify).
- Macro: VIX intraday (verify `^VIX` intraday availability; daily as fallback).
- Crypto: BTCUSDT, ETHUSDT, SOLUSDT (Yahoo BTC-USD... or reuse cross-exchange 1h).
- Source: Yahoo chart public GET (no keys). Polygon/IEX/Alpaca/Nasdaq Data Link
  are candidates ONLY — not used without explicit licensing (no keys, no pay).

## 2. Timeframes
- 5m, 15m, 1h. Start with 15m (best resolution within the 60d window).

## 3. Sessions / timezone
- All timestamps normalized to **UTC**.
- US regular session ~13:30–20:00 UTC; after-hours separate. Crypto 24/7.
- Tag each crypto bar with whether US equities are OPEN/AFTERHOURS/CLOSED at that
  time (so we can separate "reaction during session" vs "overnight").

## 4. No-lookahead alignment (critical)
- An equity bar is usable only AFTER its close. To predict a crypto move over
  [t, t+H], use equity bars with close_time <= t.
- Test explicit lags: crypto reaction at +1h / +2h / +4h / +8h after an equity
  shock bar. The feature timestamp must strictly precede the crypto label window.
- Equities have gaps (nights/weekends); forward-fill only with PAST values.

## 5. Labels (no-lookahead in features)
- `FUTURE_DRAWDOWN`: crypto draws down > X% over next 1h/4h/8h/24h (X by vol).
- `BOUNCE_AFTER_EQUITY_SHOCK`: after a big red equity bar, crypto recovers > X%.
- `SHORT_BIAS_AFTER_TECH_BREAKDOWN`: short EV > long EV after NVDA/QQQ breakdown.
- `NO_TRADE_WHEN_MIXED`: expected move < cost x2 -> abstain.

## 6. Baselines (must beat to be interesting)
- BTC-only trend, QQQ-only, NVDA-only, VIX-only, random, and
  "always risk-off after a big red equity candle" (the naive version of the idea).

## 7. Success criteria
- OOS improvement vs BTC-only; risk-off precision/recall above base rate;
  net_EV > 0 after costs; no-lookahead verified; FDR not HIGH; holds on >1 crypto.
- Otherwise classify `REJECTED_NO_INTRADAY_LEADLAG` / `WEAK` / `NEEDS_MORE_DATA`.

## 8. Engineering guardrails
- Reuse the V10.15/V10.7 public-GET allowlist pattern (add Yahoo host) + staging
  path safety; staging-only; no raw/DB; bounded requests; deterministic.
- Likely a new module `intraday_leadlag_v10_23.py` + CLIs + tests in a later phase.

research_only: true
shadow_only: true
paper_ready: false
live_ready: false
can_send_real_orders: false
final_recommendation: NO LIVE
