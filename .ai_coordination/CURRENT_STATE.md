# CURRENT STATE  (auto-refreshed)

**Branch:** local-v10-47-8-scientific-repair
**Certification:** **CERTIFICATION=FAIL** (independent Work audit of V10.47.14)
**Official state:** SCIENTIFIC_REPAIR_IMPLEMENTED_BUT_NOT_CERTIFIED
**SHADOW_CANDIDATES = 0 · NO_CONFIRMED_EDGE · HOLD**
**Safety:** PAPER_TRADING=True · LIVE_TRADING=False · DRY_RUN=True · can_send_real_orders=false · FINAL_RECOMMENDATION=NO LIVE
**Holdout:** SEALED (not opened; being physically sealed in the certification repair)

## Why certification FAILED (Work audit — kept verbatim in reviews/V10_47_14_WORK_FINAL_AUDIT.md)
- VALIDATION defined but never evaluated in any gate.
- The "sealed" holdout is precomputed with the whole series; `holdout_touched=False`
  is a hard-coded literal with no guard — not a verifiable seal.
- The matched random baseline does not preserve realised holding/censoring or the
  single-position path, and its lower bound is versus zero (not paired vs random).
- Deterministic strategies are not the pre-registered 4h→1h with 2 ATR stops / 1R trailing.
- The output manifest is stale and the seal binds only output path:hash, not
  HEAD/tree/dataset/spec/registry provenance.
- 2896 pytest invocations but only 2895 unique nodeids (one duplicate id).

## Conservative conclusion (unchanged, supported by the audit)
The DOGE/XRP sign flip, causal ledger and 12-tournament totals reproduce. No edge,
no candidate, no live. The FAIL is about the *certification*, not the conclusion.

## Repair in progress
V10.47.16 (validation + physical sealed holdout + paired baselines) →
V10.47.17 (real 4h→1h + ATR risk) → V10.47.18 (reproducible manifest/seal + unique
certification + regenerate the 12 tournaments without opening the holdout).
