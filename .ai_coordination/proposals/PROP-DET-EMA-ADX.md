# PROPOSAL — DET_EMA_ADX_PULLBACK_1H_4H
- mechanism: 4h EMA50/EMA200 + ADX/DI regime; 1h causal pullback to EMA50
  (ATR-normalised) with RSI recovery; next-bar-open entry.
- hypothesis: trend-regime pullbacks have positive net expectancy after costs.
- data needed: ≥2y verified 1h + 4h OHLCV (BTC/ETH/XRP/DOGE).
- preregistered params: EMA50/EMA200, ADX≥20, pullback≤1 ATR, RSI recover ~45,
  stop 2 ATR, trailing from 1R, time exit 24.
- baseline: exposure-matched random + no-trade.
- metric: net EUR, corrected block-bootstrap lower bound > 0.
- falsification: fails if it cannot beat matched random on strictly-later validation.
- split: 12m train / 4m validation / 4m walk-forward / 4m sealed holdout.
- overfit risk: one dimension per challenger; no grid search.
- status: NEEDS_DATA
- review: reviews/REV-DET-EMA-ADX.md
