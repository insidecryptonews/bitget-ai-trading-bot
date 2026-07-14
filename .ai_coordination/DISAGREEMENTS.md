# DISAGREEMENTS (never deleted)

## DIS-001 — Cluster accounting
- WORK/CODEX: LAST_SIGNAL_CLUSTER_ACCOUNTING silently ex-post-selects the last
  signal per cluster and is scientifically invalid.
- (historical V10.47 engine implicitly assumed it was harmless.)
- **Resolution:** FIRST_CAUSAL_SIGNAL_SINGLE_POSITION (see DECISIONS D001).
- Kept for the record even though resolved.
