# ResearchOps V10.5.5 — Perplexity / Quant Research Review (analysis-only)

**Status:** review & roadmap ONLY · no runtime/.env/data change · NO LIVE
Treat the external (Perplexity) research as a **checklist, not ground truth**.
Nothing here is executed; it informs the gated roadmap.

## C1. Valid ideas from the external research
- Clean the data **before** optimizing anything (no tuning on contaminated/
  duplicated rows).
- Treat OHLCV freshness and `training_data_clean_view=BAD` as hard blockers.
- Build research-engine reports/exports **only on the clean view**.
- Bucketed analysis (by strategy / symbol / regime / RSI / volume / ATR /
  spread / EMA / score) is the right lens for edge discovery.
- Never promote a shadow strategy with negative net PnL.
- Require PF, win-rate, sample size, drawdown AND net EV together — not one
  metric in isolation.

## C2. What is outdated / incomplete in that research
- V10.5.x has moved past "tune the strategy"; the live constraint is
  **evidence integrity** (manifest + pipeline gates), now hardened.
- There is still **no verified 180/365d provider**. Coinalyze ~63d is
  insufficient; Tardis.dev / CoinGlass remain unverified candidates.
- No operational backtester on long real data; no demonstrated edge; paper
  readiness low; live readiness zero.

## C3. What would be dangerous to apply now
- Editing `.env` with contaminated/incomplete data.
- Hunting for more trades without a demonstrated net-EV edge.
- Running refresh **writes** (`--apply`) without backup/phase.
- Optimizing strategies while `duplicate_rate` is high.
- Enabling the paper filter or live; raising leverage/slots/sizing.
- Using TimesFM as a direct signal.
- Confusing gross PF with net EV (the recurring trap).

## C4. The bot's real pipeline (high level, read-only)
`OHLCV → indicators → strategy_engine → signal_engine → regime_detector →
score/R:R → portfolio_allocator → risk_manager → trade/NO_TRADE`.
- `signal_engine.py` / `strategy_engine.py`: produce raw signals + score.
- `regime_detector.py`: RISK_ON/OFF/TREND/RANGE context.
- `portfolio_allocator.py`: selects by adjusted score + correlation.
- `risk_manager.py`: the strong gate — blocks on net R:R (e.g. 1.33<1.40),
  isolated-margin preflight, circuit breakers. This is where most signals die.
- `research_engine.py`: reporting/export over the clean view (research-only).
- **Block points:** confluence/score threshold, net R:R gate, allocator
  ("all mediocre/NO_TRADE"), and the V10.5 evidence gates above it.
- **What validates a change:** net EV after x2 costs, PF, samples≥150,
  TIME<80%, OOS, walk-forward — never a single raw metric.
- **What NOT to touch now:** execution/sizing/leverage/margin/slots, the
  risk_manager thresholds, and `.env`.

## C5. Aggressive-but-safe roadmap to edge
1. Close V10.5.5 + Codex APTO.
2. Deploy research-only to VPS.
3. Human/provider verification: Tardis.dev sample → CoinGlass fallback →
   Bitget cross-check.
4. Offline sample validator: timestamps, gaps, duplicates, OHLCV coherence,
   OI/funding/liquidations completeness, checksums, **content** validation,
   `dataset_hash`.
5. Acquire 180/365d verified data (manifest + structured inventory + human auth).
6. Real bar-by-bar backtester (no lookahead, worst-case same-bar, real costs).
7. Research Engine over the clean view.
8. Edge Hunter: net EV, net PF, drawdown, samples, TIME-death, slippage/
   funding, fees x2 stress.
9. Walk-forward / OOS / anti-overfit.
10. Shadow → 11. paper (human-gated) → 12. micro-live only if everything passes.

## C6. Honest readiness (maturity/evidence, not time)
- research/safety infra: **80–85%**
- data foundation: **~20%**
- real backtester: **~25%**
- edge discovery: **~10%**
- paper readiness: **~5%**
- live readiness: **0%**

## C7. Next 7 / 30 days
- **7 days:** finish V10.5.5; Codex re-audit; deploy research-only if APTO;
  contact Tardis.dev/CoinGlass; prepare the sample-validation plan. No runtime
  or `.env` change.
- **30 days:** if a sample passes offline validation → controlled data
  acquisition → content validator → real backtester → first serious Edge
  Hunter. Zero live. If no verified sample arrives, do not fabricate progress.

## C8. TimesFM
Stays a **future, offline, shadow-only** candidate: forecast of volatility /
expected range / volume / funding / OI / uncertainty as a **NO_TRADE gate** —
never a direct signal, never in runtime, no dependency added. Earns a place
only if it beats ATR/EWMA/naive OOS **and** improves net EV under realistic
costs. Not implemented now.

FINAL_RECOMMENDATION: **NO LIVE**
