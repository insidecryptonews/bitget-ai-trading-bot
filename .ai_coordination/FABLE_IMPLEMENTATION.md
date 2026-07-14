# FABLE — IMPLEMENTATION LOG

Implemented on the canonical infra (no new engine):
- event_clock: interval_ms_for / cluster_id_tf / cluster_block_ms / session_id / day_id;
  bars_to_events now derives interval + cluster from the timeframe (P2.2).
- sim_oms: interval_ms threading + causal trailing (next-bar effect) +
  trailing_activate_frac (trailing from +1R).
- causal_ledger.drive_causal (immutable/deep-copy, first-causal, single-position,
  ATR-multiple dynamic exit).
- causal_stats (conservative n_eff, block bootstrap, matched_random_paired: exact
  paired baseline with coverage + paired LB).
- causal_tournament (closed registry, semantic dedup, TRAIN/VALIDATION/WALK-FORWARD/
  physically-sealed HOLDOUT, VALIDATION evaluated in the gate).
- sealed_holdout (guarded, deny-by-default, one-time token, access log, state machine).
- manifest_seal (provenance-bound payload + seal + disk verification).
- cost_truth, families.strategy_truth/strategy_matrix (P08 proxy truth).
- det_strategies (real 4h→1h MTF + 2-ATR / 1R risk; still INSUFFICIENT_DATA).

## Certification repair (V10.47.16–18) — responding to Work's FAIL
Reproduced all findings with failing tests first, then fixed each; regenerated the
twelve tournaments WITHOUT opening the holdout; bound the manifest/seal. Green tests
are correctness checks, NOT evidence of edge.

## Temporary official builder repair (Codex, V10.47.19-22)
- Preserved Work's V10.47.18 audit and notes byte-for-byte and committed the FAIL.
- Converted the focused falsifications into RED evidence before changing behavior.
- Added physical discovery/holdout partitions, neutral commitment helpers and a
  separate external loader with immutable, log-backed, one-use capability.
- Implemented validation admission before lazy WF evaluation.
- Replaced averaged/permissive baseline comparison with one exact immutable pair,
  conservative n_eff, block-bootstrap lower bound and Bonferroni gate.
- Added complete causal MTF aggregation and independent MTF experiment registry.
- Added ATR and stop facts to the append-only ledger with next-bar trailing.
- Added deterministic real-state manifest, SEAL.txt, certified test runner, evidence
  generator and mutation-sensitive tests.
- Regenerated 12 tournaments without importing or parsing holdout rows: zero shadow
  candidates. This is `IMPLEMENTATION_COMPLETE_FOR_WORK_REAUDIT`, not certification.
