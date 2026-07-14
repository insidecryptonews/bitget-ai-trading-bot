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
