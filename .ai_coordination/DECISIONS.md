# DECISIONS (append-only; every decision has an ID)

### D001 — Accounting rule for concurrent same-cluster signals
- **Date (UTC):** 2026-07-14
- **Context:** V10.47 used per-cluster overwrite (last signal wins) → ex-post selection.
- **Options:** LAST_SIGNAL_CLUSTER_ACCOUNTING vs FIRST_CAUSAL_SIGNAL_SINGLE_POSITION.
- **Decision:** **FIRST_CAUSAL_SIGNAL_SINGLE_POSITION**.
- **Rationale:** only the first causal eligible signal may open a position; one
  open position at a time; later signals are POSITION_ALREADY_OPEN / CLUSTER_COOLDOWN;
  no retrospective replacement. Reproduction shows the flawed rule manufactured
  positive net that flips negative under this rule.
- **Owner:** COORDINATOR · **Status:** RESOLVED (binding).

### D002 — V10.47 shadow candidates invalidated
- **Date (UTC):** 2026-07-14
- **Decision:** DOGE 1m + XRP 1m P08_LONG are INVALIDATED (LAST_SIGNAL_CLUSTER_OVERWRITE),
  kept as history. SHADOW_CANDIDATES=0. No promotion.
- **Status:** RESOLVED.

### D003 — Deterministic 1h/4h strategies gated on data
- **Date (UTC):** 2026-07-14
- **Decision:** implementation on the canonical infra but SCIENTIFIC_EVALUATION=INSUFFICIENT_DATA
  (only ~90d verified; 2y required). Neither strategy is promoted; both NEEDS_DATA.
- **Status:** RESOLVED. Superseded in part by D005 (implementation was not yet spec-conforming).

### D004 — Independent certification of V10.47.14 FAILED
- **Date (UTC):** 2026-07-14
- **Context:** Work's independent audit (reviews/V10_47_14_WORK_FINAL_AUDIT.md) returned
  CERTIFICATION_VERDICT=FAIL for the claim "SCIENTIFIC REPAIR COMPLETE".
- **Decision:** revoke the "complete" certification. Official state =
  SCIENTIFIC_REPAIR_IMPLEMENTED_BUT_NOT_CERTIFIED. The conservative conclusion
  (NO_CONFIRMED_EDGE / SHADOW_CANDIDATES=0 / NO LIVE) STANDS and is supported by the audit.
- **Rationale:** VALIDATION unevaluated; holdout not physically sealed; baseline not
  fully matched/paired; deterministic strategies not spec-conforming; manifest/seal
  not bound to provenance; duplicate pytest nodeid. These are material certification gaps.
- **Owner:** COORDINATOR · **Status:** RESOLVED (accept the FAIL; repair before re-certifying).

### D005 — Deterministic strategies need implementation repair before data
- **Date (UTC):** 2026-07-14
- **Decision:** DET_EMA_ADX_PULLBACK / DET_DONCHIAN require real 4h→1h linkage and
  dynamic 2-ATR stops / 1R trailing (per their pre-registration) — status
  NEEDS_IMPLEMENTATION_REPAIR, then NEEDS_DATA. `IMPLEMENTATION_STATUS=COMPLETE` is retracted.
- **Owner:** COORDINATOR · **Status:** OPEN → repaired in V10.47.17.
