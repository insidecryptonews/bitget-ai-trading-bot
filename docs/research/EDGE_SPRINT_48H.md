# 48H Edge Sprint

This sprint is a persistent local research coordinator. It does not trade,
change active policies, tune ATI/P11/Cross-Venue, or enable the paper filter.

## Cadence and truth contract

- The existing storage scheduler owns the process and its mutex.
- A lightweight cycle runs with the scheduler; snapshots are due every 6 hours.
- A changed verified-feature dataset hash is required before analysis can be
  eligible. The coordinator itself never reruns heavy analysis.
- The holdout is sealed at sprint creation and may be evaluated at most once,
  after 48 hours, only for a pre-existing `WATCH_ONLY` candidate.
- Diagnostic, validation, forward-demo, ATI, P11, and Cross-Venue populations
  remain separate.

## Demos

`OPERABILITY_DIAGNOSTIC_DEMO_50` is an isolated causal simulator ledger. It is
created empty and never fabricates a signal. Its PnL is labelled `NOT EDGE` and
cannot enter candidate metrics.

`EDGE_CANDIDATE_DEMO_50` remains uninitialized unless every strict research gate
passes and a human performs a later explicit review. There is no automatic start
or promotion path.

## Safety

- `PAPER_TRADING=True`
- `LIVE_TRADING=False`
- `DRY_RUN=True`
- `ENABLE_PAPER_POLICY_FILTER=False`
- `can_send_real_orders=false`
- `SIMULATION ONLY`
- `RESEARCH ONLY`
- `FINAL_RECOMMENDATION=NO LIVE`
