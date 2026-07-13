# LIVE READINESS RUNBOOK — Bitget AI Trading Bot (V10.46)

> **THIS DOCUMENT IS NOT AN AUTHORISATION TO TRADE LIVE.**
> `LIVE_TRADING` remains **False** and `can_send_real_orders` remains **false**.
> The V10.46 integrated system is REPLAY / SIMULATION / SHADOW / PAPER RESEARCH
> ONLY. Enabling any live path is a deliberate human decision that requires the
> prerequisites and the **independent audit** described below. No code in the
> `app/labs/v10_46` package can place an order or reach a private endpoint; a
> test (`test_researchops_v10_46_1_blockers.py`) fails if that ever changes.

## 1. Purpose
Define, in advance and in writing, exactly what evidence and controls MUST
exist before anyone could responsibly consider a *micro-live* pilot — and how
to stop and roll back. This makes "should we go live?" a checklist, not a
feeling.

## 2. Promotion ladder (no LIVE executable state)
```
REPLAY_CANDIDATE -> SHADOW_CANDIDATE -> VALIDATED_SHADOW
-> PAPER_CHALLENGER -> PAPER_CHAMPION -> LIVE_READINESS_ONLY
```
`LIVE_READINESS_ONLY` produces a **report**, never an order path. The
deterministic Promotion Controller (`app/labs/v10_46/promotion.py`) is the ONLY
thing that can advance a policy; no AI/LLM can promote anything.

## 3. Minimum evidence before micro-live is even discussed
A single Paper Champion policy must, on data it never learned from:
- trial registry CLOSED; dataset DATASET_VERIFIED; no lookahead; embargo held;
- positive **net** PnL in euros under the *observed* cost scenario, and still
  non-negative under *conservative* and *stress*;
- paired lower bound (challenger vs champion, per event_cluster_id) > 0;
- beats **No-Trade** and **random exposure-matched** baselines;
- sufficient `n_eff` (distinct event clusters), not a handful of events;
- calibrated probabilities (Brier within bound); stability across time,
  symbol and regime; bounded drawdown and expected shortfall in euros;
- robust to top-3 event removal;
- survives an INDEPENDENT forward shadow window (not the training window);
- a sustained Paper Champion track record;
- an **independent audit** signing off (`independent_audit_ref`).

If any item is missing: **do not proceed.** Keep researching.

## 4. Prerequisites (technical, only if §3 is fully met)
These are NOT enabled by this repository and must be done by a human operator
outside this package:
- separate, minimal-scope API credentials stored in the operator's own secret
  manager (never in `.env` in this repo, never printed, never committed);
- a hard, externally-enforced position/notional cap (start at the 5 EUR
  scenario, 1x, no added margin, no martingale, no loss-DCA);
- an emergency **kill switch** and a **logical** cancel-all;
- health checks (data freshness, clock alignment, connectivity);
- monitoring + alerting on PnL, drawdown, error rate, fill anomalies;
- a written rollback path back to PAPER.

## 5. Flags and limits (must all be verified before any pilot)
- `PAPER_TRADING=True` while researching; a live pilot is a *separate*,
  operator-owned deployment, never this research process;
- `LIVE_TRADING=False` and `can_send_real_orders=false` in THIS system, always;
- leverage/margin/sizing of the production bot are **never** modified by V10.46;
- paper filter stays disabled in research.

## 6. Emergency stop / kill switch
- Trigger conditions: drawdown breach, data staleness, repeated fill anomalies,
  connectivity loss, or ANY unexpected order behaviour.
- Action: halt new decisions, flatten via the operator's own controls, and
  revert to PAPER. Preserve all logs and autopsies for review.

## 7. What to do on losses
Losses inside the pre-agreed euro exposure (≈5 EUR at 1x plus explicit costs)
are expected sampling noise, not a signal to add size. **Never** add margin,
average down, or increase exposure to "recover". If cumulative loss approaches
the pre-set stop, halt and return to PAPER for autopsy.

## 8. When to stop
- Any §3 gate fails on forward data.
- Realised behaviour diverges from simulation beyond tolerance.
- The independent audit is withdrawn or expires.

## 9. Return-to-PAPER procedure
1. Kill switch / flatten (operator controls). 2. Revert deployment to PAPER.
3. Freeze the policy hash and dataset lineage. 4. Run autopsies. 5. File a
blocker report. 6. Do not re-enable without a fresh audit.

## 10. Required audit before micro-live
An **independent** party (not the author of the policy) must review: data
provenance and no-lookahead proofs, the SimOMS cost model, the promotion gate
evidence, the paired/forward results, and this runbook's controls, and issue an
`independent_audit_ref`. Without it, `LIVE_READINESS_ONLY` promotion is HELD by
the controller.

---
**Standing statement:** `LIVE_TRADING=False`, `can_send_real_orders=false`,
`FINAL_RECOMMENDATION=NO LIVE`. This runbook documents the path; it does not
open it.
