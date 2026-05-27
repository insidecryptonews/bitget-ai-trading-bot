# Phase 9 Pre-Paper Readiness

Phase 9 is a research-only readiness layer. It does not enable paper filter,
candidate shadow monitor, live trading, leverage changes, sizing changes, or
orders.

## What It Adds

- Data freshness gate for OHLCV-driven actionability.
- DOT regime diagnosis and DOT regime filter labs.
- Phase 9 paper readiness validator on top of Phase 8B gates.
- Net profit lock lab with fee-aware break-even and cost stress scenarios.
- Fast signal shadow panel that remains non-executable.
- Research pack endpoint for sharing a compact, secret-free state snapshot.
- Design-only paper portfolio allocator, disabled by default.

## Required Manual Gate

`PAPER_DEMO_READY_MANUAL_REVIEW_ONLY` can only be a manual label. It does not
change configuration and does not place trades. A candidate must pass:

- Positive net EV and acceptable net PF.
- Minimum sample size.
- Cost stress PASS, including 0.22% and 0.25%.
- Walk-forward PASS.
- Anti-overfit PASS.
- Stability PASS.
- Data freshness OK for all required symbols.
- Validation window of at least 720h.

If any gate is WARN/FAIL/NEED_DATA/STALE, the candidate remains research-only.

## DOTUSDT Current Interpretation

DOTUSDT with `late_entry_block_plus_dynamic_hold` is promising but remains
blocked until Phase 9 validates cost, walk-forward, anti-overfit, sample size,
fold dominance, and data freshness. A weak or negative fold must remain visible
and cannot be hidden by aggregate performance.

## CLI

```powershell
python -m app.research_lab dot-regime-diagnosis --symbols DOTUSDT --hours 720 --timeframe 5m --folds 4
python -m app.research_lab dot-regime-filter-lab --symbols DOTUSDT --hours 720 --timeframe 5m --folds 4
python -m app.research_lab phase9-paper-readiness --symbols DOTUSDT --hours 720 --timeframe 5m --min-trades 250 --folds 4
python -m app.research_lab net-profit-lock-lab --symbols DOTUSDT --hours 720 --timeframe 5m
python -m app.research_lab fast-signal-shadow --symbols BTCUSDT,ETHUSDT,DOTUSDT --hours 72 --timeframe 5m
python -m app.research_lab research-pack --hours 24
```

## Safety

Final recommendation remains `NO LIVE`. Paper filter remains off. Candidate
shadow monitor remains off. The allocator is design-only and disabled.
