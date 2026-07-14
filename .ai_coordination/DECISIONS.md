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
- **Decision:** implementation COMPLETE but SCIENTIFIC_EVALUATION=INSUFFICIENT_DATA
  (only ~90d verified; 2y required). Neither strategy is promoted; both NEEDS_DATA.
- **Status:** RESOLVED.
