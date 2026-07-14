# V10.47.24 Local Invariant Validation Catalog

Scope: local code, synthetic fixtures and read-only inspection. The real holdout was not opened.

## Required fail-closed invariants

1. Campaign authority is loaded from a tracked, versioned root and cannot be replaced by caller data.
2. Campaign correction is fixed to 564 hypotheses, alpha 0.05 and Bonferroni for the official 4 x 3 x 47 family.
3. Every evaluated tournament must match exactly one of twelve authority entries.
4. Pairing requires complete, finite, canonical evidence and is independent of input row order.
5. Repeated economic evidence cannot increase effective sample size.
6. Validation must be finite and policy identity must remain unchanged before walk-forward is callable.
7. Discovery partitions must contain valid finite OHLCV and strictly ordered, disjoint timestamps.
8. SimOMS rejects malformed sides, prices, fractions, times and scenarios without raising unexpected arithmetic errors.
9. Holdout bars remain physically absent from selection and only sealed commitment metadata is accepted.
10. Any authority, dataset or policy mismatch produces no promotion and no shadow candidate.

## Round-one conclusion

The current implementation does not satisfy invariants 1 through 8. Existing holdout separation remains subject to regression verification. All affected statistical outputs, reports, dashboard, manifest and seal must be recomputed after repair.

Status: `IMPLEMENTATION_IN_PROGRESS`

Final recommendation: `NO LIVE`
