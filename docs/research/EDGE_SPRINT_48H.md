# 48H Edge Sprint

This sprint is a persistent local research coordinator. It does not trade,
change active policies, tune ATI/P11/Cross-Venue, or enable the paper filter.

## Cadence and truth contract

- The existing storage scheduler owns the process and its mutex.
- A lightweight cycle runs with the scheduler; snapshots are due every 6 hours
  of qualified active runtime.
- The target is 172,800 accumulated seconds. PC-off, suspended, stale collector,
  unhealthy scheduler, and no-data-growth intervals add exactly zero seconds.
- Heartbeat gaps over 15 minutes start a new session and are never inferred as
  active time. The original wall-clock end is retained only as provenance.
- A changed verified-feature dataset hash is required before analysis can be
  eligible. The coordinator itself never reruns heavy analysis.
- The holdout is sealed at sprint creation and may be evaluated at most once,
  after 48 qualified active hours, only for a pre-existing `WATCH_ONLY` candidate.
- Diagnostic, validation, forward-demo, ATI, P11, and Cross-Venue populations
  remain separate.

## Safe session controls and review

- `scripts/START_RESEARCH_SESSION.ps1` verifies the backup branch, safety audit,
  project-memory contract, and disk guard before resuming.
- `scripts/STOP_RESEARCH_SESSION.ps1` pauses accounting before cooperatively
  stopping the managed local research stack. Checkpoints and ledgers remain intact.
- `scripts/EXPORT_REVIEW_SNAPSHOT.ps1` creates a sanitized provisional ZIP. It
  does not access the holdout and does not stop collectors.
- `edge-sprint-final-handoff-v1 --apply` is fail-closed until accumulated active
  runtime, the final snapshot, reconciliation, and final report all pass. The
  scheduler creates it once after genuine completion; no fixed calendar alarm
  can finalize the sprint.

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
