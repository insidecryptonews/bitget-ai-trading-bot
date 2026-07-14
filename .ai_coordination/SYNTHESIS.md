# SYNTHESIS

The only V10.47 "leads" (P08_LONG 1m) were artifacts of a broken accounting rule;
under a causal single-position ledger they are net-negative, and the 12-combo
causal tournament produces zero shadow candidates. That conservative conclusion is
independently reproduced and STANDS.

However, Work's independent audit returned **CERTIFICATION=FAIL** for the claim
"scientific repair complete": VALIDATION is never evaluated, the holdout is not
physically sealed (the whole series is precomputed and `holdout_touched=False` is a
hard-coded literal), the matched baseline does not preserve realised holding/
censoring or the single-position path (its lower bound is versus zero, not paired
vs random), the deterministic strategies are not the pre-registered 4h→1h with
2-ATR stops / 1R trailing, the manifest/seal is stale and does not bind
commit/tree/dataset/spec provenance, and there is one duplicate pytest nodeid.

Net synthesis: the *conclusion* is sound but the *certification* is not. Work's
focused V10.47.18 re-audit demonstrated that V10.47.16-18 did not close the strong
contracts: WF still runs after validation rejection, holdout isolation is only an
in-memory convention, baseline pairs are permissive, MTF accepts incomplete 4h
buckets, ATR risk is missing from the ledger, and the manifest does not verify the
real current state. V10.47.19-22 must repair and regenerate all evidence before a
new independent Work re-audit. No confirmed edge; no candidate; no live.
