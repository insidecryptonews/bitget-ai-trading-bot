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

## V10.47.22 bounded repair synthesis

The implementation now enforces the contracts that V10.47.18 falsified. Validation
short-circuits WF, discovery never receives holdout rows, the external capability is
one-use and unavailable to discovery, baseline matching is exact and corrected for
the global registry, incomplete MTF buckets fail closed, and ATR risk evolution is
hash-bound in the ledger. Real-state evidence is deterministic and re-verifies Git,
datasets, manifests, specs, registry, commitment, policies, outputs, reports,
dashboard, audits, hub, collection, execution and nodeids from disk.

This repairs implementation, not market edge. The regenerated result remains zero
shadow candidates and all apparent positive selection rows fail validation and/or
the exact baseline contract. MTF still lacks two years of data. The only honest next
step is `WORK_REAUDIT_V10_47_22`; certification remains pending and live remains off.

## V10.47.23 synthesis

Work's final V10.47.22 re-audit correctly found that baseline reuse was blocked but
candidate reuse was not. The production gate could therefore overcount dependent
rows. V10.47.23 closes that path with a fail-closed identity registry and a
deterministic pair identity. It also corrects the promotion family from one local
tournament (`m=47`) to the complete campaign (`m=564`). Local corrected p-values
remain diagnostic only.

The repair does not create market edge. All prior positive TRAIN labels remain
subject to validation, exact baseline, costs, lower bounds and campaign FWER. The
expected recomputation remains 0 shadow candidates, sealed holdout and NO LIVE.
Only Work may decide whether this implementation clears the independent FAIL.
