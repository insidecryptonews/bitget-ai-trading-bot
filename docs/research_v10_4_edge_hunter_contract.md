# ResearchOps V10.4 — Edge Hunter Contract (for V10.5)

**Status:** contract/gates only · NOT operational · NO LIVE
**Module:** `app/labs/edge_hunter_contract_v10_4.py`
**CLI:** `python -m app.research_lab edge-hunter-contract-v104`

The Edge Hunter is **not implemented** in V10.4. This contract freezes, ahead
of time, what V10.5 must satisfy so it cannot be quietly weakened later.

## Candidate definition

`candidate = symbol + side + regime + score_bucket + exit_policy + timeframe`

## Minimums and gates

- `minimum_samples: 150` per candidate.
- `minimum_history_days: 180` clean (and a valid acquisition manifest).
- Required metrics: net EV after costs, net PF, gross PF vs net PF,
  time-death rate, TP/SL/TIME distribution, cost sensitivity x1/x2/x3,
  max drawdown, exposure time, return by month, return by regime, worst
  streak, top-1/top-5 trade dependency.
- Validation: monthly + rolling walk-forward, train/test split, regime split,
  OOS validation, anti-overfit score, stability matrix.
- Anti-lookahead: entry on next bar after signal, same-bar SL/TP resolved as
  worst case, trailing-window features only, no labels in decisions.
- Cost model: maker/taker fees, slippage x1/x2/x3, funding, spread proxy.

## Gate evaluator (included, pure)

`evaluate_edge_hunter_gate(...)` orders blockers conservatively:

1. `clean_days < 180` → `NEED_LONG_HISTORY`
2. OI-dependent strategy with blocked OI → `MISSING_OI_RISK_BLOCK`
3. `samples < 150` → `NEED_MORE_SAMPLES`
4. net EV ≤ 0 or net PF < 1.30 → `REJECT`
5. cost x2 fail → `REJECT`; OOS fail → `REJECT`
6. one-trade dominance > 0.25 or time-death > 0.55 → `WATCH_ONLY`
7. everything passes → `SHADOW_ONLY` (the **ceiling**)

## Reject reasons (explicit)

fewer than minimum samples · PF positive only pre-cost · one-trade dominance ·
one-week/month dominance · OOS fail · cost x2 fail · drawdown too high ·
same-bar ambiguity too frequent · missing-OI dependency unresolved · edge
disappears outside ETH.

## Promotion ladder

`RESEARCH_ONLY → BACKTEST_CANDIDATE → WALK_FORWARD_CANDIDATE → SHADOW_ONLY →
PAPER_ELIGIBLE_FUTURE`

The evaluator can never output `paper_ready=true` or `live_ready=true`; the
fields are forced false after every evaluation. Auto-promotion is forbidden.
`final_recommendation: NO LIVE`.
