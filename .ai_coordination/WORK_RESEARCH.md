# WORK — RESEARCH NOTES

Findings that drove this repair (all reproduced before fixing):
1. Fixed 1-minute EventClock step for all timeframes.
2. per_cluster overwrite → last signal per cluster selected ex post.
3. Multiple concurrent signals per cluster silently executed.
4. Unmatched random baseline (count/exposure mismatch).
5. Post-selection "OOS" mislabeled as out-of-sample.
6. n_eff == trade count (ignored dependence).
7. Ambiguous intrabar trailing sequence.
8. P08 proxy mislabeled as OI/Funding.
9. "observed" costs were fixed bps tables.

Falsification for any future candidate: it must fail if it cannot beat an
exposure-matched random baseline on a strictly-later validation window.

## Independent final audit of V10.47.14 (2026-07-14)

Verdict: **FAIL** for the claim `SCIENTIFIC REPAIR COMPLETE`; the conservative
operational conclusion remains **SHADOW_CANDIDATES=0 / NO_CONFIRMED_EDGE / NO
LIVE**. The DOGE/XRP sign flip, causal ledger accounting and 12 tournament
totals reproduce, but the final certification has material gaps:

1. `VALIDATION` is defined but never evaluated. The runner precomputes signals
   over the full dataset (including the nominal holdout) and returns the
   hard-coded flag `holdout_touched=False`; this is not a sealed holdout.
2. The matched random null preserves count, aggregate side mix, cluster,
   exposure and cost parameters, but not realised holding/censoring or the
   single-position path. Its lower bound is versus zero, not a paired
   candidate-minus-random lower bound.
3. The deterministic implementations do not implement the preregistered 4h
   regime + 1h pullback linkage, dynamic 2-ATR stop, or trailing from 1R;
   `DET_EXIT` uses fixed 2%/6%/2% fractions.
4. The output manifest is stale (`progress_checkpoint.md` SHA-256 mismatch), so
   the actual-files seal does not match the declared seal. The seal covers only
   output path/hash pairs and does not bind commit/tree/dataset/spec provenance.
5. The suite log is real and records 2896 passing invocations, but collection
   has 2895 unique nodeids: `%2E%2E` and `%2e%2e` collide under the same pytest
   nodeid. The 29 new tests independently pass.
6. `bars_to_events()` still defaults to a 1-minute close step even when its
   `timeframe` argument is 5m/15m/1h/4h unless the caller supplies an interval.

Full evidence, severity, matrix, reproductions and required next action:
`reviews/V10_47_14_WORK_FINAL_AUDIT.md`.
