# FABLE — IMPLEMENTATION LOG

Implemented on the canonical infra (no new engine):
- event_clock: interval_ms_for / cluster_id_tf / cluster_block_ms / session_id / day_id
- sim_oms: interval_ms threading + causal trailing (next-bar effect)
- causal_ledger.drive_causal (immutable, first-causal, single-position)
- causal_stats (n_eff, matched random null, block bootstrap)
- causal_tournament (closed registry, splits, gate)
- cost_truth, families.strategy_truth/strategy_matrix (P08 proxy truth)
- det_strategies (DET_EMA_ADX_PULLBACK_1H_4H, DET_DONCHIAN_BREAKOUT_4H)

Green tests are correctness checks, NOT evidence of edge.
