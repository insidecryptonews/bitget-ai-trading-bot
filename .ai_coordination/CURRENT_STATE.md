# CURRENT STATE  (auto-refreshed by init_ai_coordination_hub.py)

**Branch:** local-v10-47-8-scientific-repair
**Result:** SCIENTIFIC REPAIR COMPLETE — NO CONFIRMED EDGE
**SHADOW_CANDIDATES = 0 · NO_CONFIRMED_EDGE · HOLD**
**Safety:** PAPER_TRADING=True · LIVE_TRADING=False · DRY_RUN=True · can_send_real_orders=false · FINAL_RECOMMENDATION=NO LIVE

## What happened
- Work's read-only audit INVALIDATED the V10.47 DOGE/XRP 1m P08_LONG shadow
  candidates (reason LAST_SIGNAL_CLUSTER_OVERWRITE).
- Reproduced numerically: DOGE 1m P08_LONG flawed +0.6726€ → causal −0.7300€;
  XRP 1m P08_LONG flawed +0.3103€ → causal −0.5574€ (both sign-flipped).
- Repaired: timeframe-aware EventClock, causal trailing, causal immutable ledger
  (first causal signal, single position), conservative cluster-aware n_eff,
  exposure-matched random baseline, block-bootstrap LB, closed registry +
  multiple testing, TRAIN/VALIDATION/WALK-FORWARD/sealed-HOLDOUT split, P08 truth
  relabel, cost data-truth.
- Regenerated all 12 causal tournaments → **0 shadow candidates**, holdout sealed.
- Deterministic 1h/4h strategies implemented; data = ~90d < 2y ⇒ INSUFFICIENT_DATA.

## P08 truth
Canonical `P08_OI_FUNDING_DIVERGENCE` is NOT implemented. What ran is
`P08_FUNDING_HOUR_RETURN_REVERSAL_PROXY` (funding-hour timestamp only; no real
OI, no real funding sign/rate). It does not validate canonical P08.
