# PROPOSAL — DET_DONCHIAN_BREAKOUT_4H
- mechanism: 20/55 Donchian channel EXCLUDING current bar + EMA/ADX/DI regime;
  block >1 ATR extended; next-bar-open entry; LONG/SHORT.
- hypothesis: regime-filtered channel breakouts have positive net expectancy.
- data needed: ≥2y verified 4h OHLCV (BTC/ETH/XRP/DOGE).
- preregistered params: Donchian 20/55, ADX≥20, extension≤1 ATR, stop 2 ATR,
  trailing baseline, time exit 24.
- baseline: exposure-matched random + no-trade.
- metric: net EUR, corrected block-bootstrap lower bound > 0.
- falsification: fails if it cannot beat matched random on strictly-later validation.
- split: 12m train / 4m validation / 4m walk-forward / 4m sealed holdout.
- overfit risk: one dimension per challenger; no grid search.
- status: NEEDS_DATA
- review: reviews/REV-DET-DONCHIAN.md
