# Paper/Demo Readiness Contract

This project does not move directly from research to live trading.

The allowed progression is:

1. Research/backtest with no lookahead.
2. Shadow validation.
3. Manual paper/demo readiness review.
4. Controlled paper/demo only after explicit human approval.
5. Live remains out of scope.

## Manual Readiness Label

`PAPER_DEMO_READY_MANUAL_REVIEW_ONLY` means:

- The candidate passed research gates.
- A human may consider a future controlled paper/demo phase.
- No switch is flipped automatically.
- `ENABLE_PAPER_POLICY_FILTER` stays false.
- `can_send_real_orders` stays false.
- `final_recommendation` stays `NO LIVE`.

## Blocking Conditions

Any of these blocks readiness:

- Data stale or missing.
- Cost stress WARN/FAIL/NEED_DATA.
- Walk-forward WARN/FAIL/NEED_DATA.
- Anti-overfit WARN/FAIL.
- Low sample size.
- Negative 720h net EV.
- 72h improvement contradicting 720h.
- Maker/maker audit scenario as the only positive case.
- Fold-level concentration or catastrophic fold.
- Paper allocator disabled or missing manual review.

## Leverage, Margin, Sizing

Phase 9 does not change leverage, margin mode, sizing, slots, or risk manager.
Those remain future manual topics only after robust paper evidence.
