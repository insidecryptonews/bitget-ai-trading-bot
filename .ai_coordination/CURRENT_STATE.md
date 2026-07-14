# CURRENT STATE  (auto-refreshed)

**Branch:** local-v10-47-8-scientific-repair
**Certification:** REPAIRED (V10.47.16–18) — re-audit pending
**Official state:** CERTIFICATION_REPAIR_IMPLEMENTED — AWAITING RE-AUDIT
**SHADOW_CANDIDATES = 0 · NO_CONFIRMED_EDGE · HOLD**
**Safety:** PAPER_TRADING=True · LIVE_TRADING=False · DRY_RUN=True · can_send_real_orders=false · FINAL_RECOMMENDATION=NO LIVE
**Holdout:** SEALED in all 12 combos (never opened)

## What the certification repair fixed (Work audit V10.47.14 FAIL → addressed)
- P1.1 VALIDATION now evaluated in the gate; holdout physically SEALED (guarded
  object, deny-by-default, one-time token, access log, commitment hash, state
  machine) and EXCLUDED from feature precompute.
- P1.2 exactly-paired exposure/holding-matched baseline with explicit pairs,
  coverage and paired lower bound; incomplete match → GATE_FAIL.
- P1.3 real 4h→1h regime + 2-ATR stops / 1R trailing (still INSUFFICIENT_DATA).
- P1.4 provenance-bound manifest + seal with independent disk verification.
- P2.1 unique pytest ids (2912 invocations = 2912 nodeids); P2.2 timeframe-derived
  bars_to_events; P2.3 semantic dedup; P2.4 justified bootstrap block; P3.1 deep-copy ledger.

## Regenerated evidence (holdout never opened)
12 tournaments: 564 nominal runs, NO_GROSS 389 / COST_KILLED 154 / NET_POSITIVE 21,
SHADOW_CANDIDATES=0, holdout SEALED everywhere. Manifest seal binds HEAD/tree/
dataset/spec/registry/holdout-commitment.

## Conclusion (unchanged, now certifiably supported)
No confirmed edge on free/public data. No paper champion, no shadow candidate, no
live. Next real step remains ≥2 years of 1h/4h data — after re-audit passes.
