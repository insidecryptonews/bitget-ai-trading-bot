# BLOCKERS

## Certification blockers (from Work audit V10.47.14 — must clear to re-certify)
- BLK-C1 (P1): VALIDATION defined but never evaluated in any gate.
- BLK-C2 (P1): holdout not physically sealed — full series precomputed;
  `holdout_touched=False` is a hard-coded literal with no guard.
- BLK-C3 (P1): matched baseline does not preserve realised holding/censoring/
  single-position; lower bound is vs zero, not paired candidate−random.
- BLK-C4 (P1): deterministic strategies not spec-conforming (no real 4h→1h,
  fixed 2%/6%/2% stops instead of 2 ATR / 1R trailing).
- BLK-C5 (P1): manifest/seal stale + not bound to HEAD/tree/dataset/spec/registry.
- BLK-C6 (P2): 2896 pytest invocations vs 2895 unique nodeids (duplicate id).
- BLK-C7 (P2): `bars_to_events()` defaults to a 1m step for higher timeframes.

## Data blockers (unchanged)
- BLK-001: no ≥2y verified 1h/4h OHLCV → deterministic strategies NEEDS_DATA.
- BLK-002: no reproducible free historical real OI / funding-sign feed →
  canonical P08_OI_FUNDING_DIVERGENCE cannot be implemented (only the proxy).
- BLK-003: no free historical L2 order book → book-based costs stay MODELLED.

## Focused re-audit blockers (V10.47.18)
- BLK-R1 (P1): validation rejection does not prevent WALK_FORWARD execution.
- BLK-R2 (P1): holdout data is present in discovery memory and self-authorization
  plus path escape are possible.
- BLK-R3 (P1): baseline accepts mismatched holding/session/day/censoring/notional/
  funding/regime and does not apply a corrected p-value.
- BLK-R4 (P1): manifest/seal does not verify current Git/dataset/spec/policy state.
- BLK-R5 (P2): incomplete 4h buckets can become regime-ready; DET_* is smoke-only.
- BLK-R6 (P2): ATR and initial stop are not append-only ledger facts.

## V10.47.22 implementation disposition (not independent certification)
- BLK-R1: IMPLEMENTATION_REPAIRED - validation rejection now prevents all WF work.
- BLK-R2: IMPLEMENTATION_REPAIRED - discovery and sealed holdout are physically
  separate; no tournament imports the holdout loader or has an opening capability.
- BLK-R3: IMPLEMENTATION_REPAIRED - one exact baseline simulation per opportunity,
  immutable pair IDs, explicit incompatibilities and corrected paired gate.
- BLK-R4: IMPLEMENTATION_REPAIRED - deterministic real-state manifest hashes and
  re-reads Git plus every required evidence category.
- BLK-R5: IMPLEMENTATION_REPAIRED - complete causal 4h buckets and an independent
  `DETERMINISTIC_MTF_1H_4H` smoke; scientific status remains INSUFFICIENT_DATA.
- BLK-R6: IMPLEMENTATION_REPAIRED - SIGNAL/ENTRY/POSITION/CLOSE ATR risk records are
  append-only and initial stop is immutable.
- BLK-R7: OPEN - Work must independently attempt to falsify all six repairs and the
  final manifest/seal. Until then `CERTIFICATION=PENDING_WORK_REAUDIT`.

## V10.47.23 exact-pairing and campaign-FWER disposition
- BLK-R8: IMPLEMENTATION_REPAIRED - pairing now requires unique, explicit candidate,
  baseline and deterministic pair identities; any duplicate, missing, malformed or
  incompatible identity invalidates the whole pairing evaluation fail-closed.
- BLK-R9: IMPLEMENTATION_REPAIRED - promotion significance uses the preregistered
  campaign family of 4 symbols x 3 timeframes x 47 trials (`m_campaign=564`), not
  the per-tournament family alone. Ambiguous semantic dedup remains nominal.
- BLK-R10: OPEN - Work must independently re-audit exact bijection, campaign-wide
  correction, regenerated gates, evidence manifest and seal. Builder completion is
  not certification and cannot create a shadow candidate.
