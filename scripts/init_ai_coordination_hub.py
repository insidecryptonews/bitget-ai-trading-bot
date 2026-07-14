"""V10.47.13 — scaffold the professional multi-agent coordination hub
.ai_coordination/ with REAL current state (no placeholders). Idempotent: only
writes a file if it is missing, except the always-refreshed CURRENT_STATE.md and
NEXT_ACTION.md. Research-coordination artifacts only; no live, no models, no APIs."""
import os
import sys

ROOT = r"C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot"
HUB = os.path.join(ROOT, ".ai_coordination")
for d in ("", "proposals", "reviews", "experiments"):
    os.makedirs(os.path.join(HUB, d), exist_ok=True)

SAFETY = ("PAPER_TRADING=True · LIVE_TRADING=False · DRY_RUN=True · "
          "can_send_real_orders=false · FINAL_RECOMMENDATION=NO LIVE")

FILES = {}

FILES["README.md"] = f"""# .ai_coordination — Multi-Agent Research Hub

A file-based coordination hub for a small research team of AI roles working on
the Bitget AI trading-bot **research** (no live trading). Everything here is
append-friendly documentation; it executes nothing and authorises nothing.

**Safety invariant (always):** {SAFETY}

## Roles
- **WORK** — proposes hypotheses, researches, defines falsification. Does NOT edit code.
- **FABLE** — reviews feasibility, implements APPROVED proposals, runs tests, saves evidence.
- **CODEX** — independent auditor; reproduces claims, logs contradictions. Does NOT edit production code.
- **COORDINATOR** — records decisions, sets the single NEXT_ACTION, resolves priorities.

## Flow
IDEA → REVIEW → SYNTHESIS → PREREGISTERED EXPERIMENT → IMPLEMENTATION → EVIDENCE
→ AUDIT → DECISION → NEXT ACTION

## Rules
- Exactly ONE NEXT_ACTION at a time (`NEXT_ACTION.md`).
- Decisions are append-only with an ID (`DECISIONS.md`).
- Disagreements are never deleted (`DISAGREEMENTS.md`).
- No experiment is rerun without stating what changed.
- Green tests are NOT evidence of edge.

## Status tool
`python scripts/ai_coordination_status.py` — prints the hub state and detects
incoherences (multiple NEXT_ACTION, broken links, experiments without evidence,
proposals without review, decisions without ID).
"""

FILES["PROJECT_CHARTER.md"] = """# PROJECT CHARTER

**Mission:** determine — honestly — whether any strategy/symbol/regime/timeframe
has a positive, stable, reproducible net edge on free/public data, using a single
causal SimOMS, an immutable ledger, closed pre-registration and sealed holdouts.

**Non-goals:** live trading, moving funds, promoting a paper champion, or calling
a proxy a real data feed.

**Definition of edge (must pass ALL):** pre-registered; causal single-position
ledger; sufficient cluster-aware n_eff; validation strictly later than selection;
walk-forward later still; corrected lower bound > 0; beats exposure-matched
random and no-trade; survives conservative costs; top-event removal keeps sign;
no proxy presented as real data; sealed holdout intact.

**Current standing fact:** no validated edge on free/public data.
"""

FILES["CURRENT_STATE.md"] = f"""# CURRENT STATE  (auto-refreshed by init_ai_coordination_hub.py)

**Branch:** local-v10-47-8-scientific-repair
**Result:** SCIENTIFIC REPAIR COMPLETE — NO CONFIRMED EDGE
**SHADOW_CANDIDATES = 0 · NO_CONFIRMED_EDGE · HOLD**
**Safety:** {SAFETY}

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
"""

FILES["NEXT_ACTION.md"] = """# NEXT ACTION

There must be exactly ONE next action.

- [ ] NEXT: Collect ≥ 2 years of verified 1h/4h OHLCV for BTC/ETH/XRP/DOGE, then
  run EXP-DET-EMA-ADX and EXP-DET-DONCHIAN under the closed registry with sealed
  holdout. Until that data exists, both deterministic strategies stay NEEDS_DATA
  and no candidate is promoted.
"""

FILES["DECISIONS.md"] = """# DECISIONS (append-only; every decision has an ID)

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
"""

FILES["IDEA_BOARD.md"] = """# IDEA BOARD

- IDEA: DET_EMA_ADX_PULLBACK_1H_4H — trend-regime pullback (see proposals/PROP-DET-EMA-ADX.md) — status: NEEDS_DATA
- IDEA: DET_DONCHIAN_BREAKOUT_4H — channel breakout with regime filter (see proposals/PROP-DET-DONCHIAN.md) — status: NEEDS_DATA
"""

FILES["REVIEW_QUEUE.md"] = """# REVIEW QUEUE

All current proposals have an initial review recorded.
- PROP-DET-EMA-ADX → reviews/REV-DET-EMA-ADX.md — status: REVIEWED (NEEDS_DATA)
- PROP-DET-DONCHIAN → reviews/REV-DET-DONCHIAN.md — status: REVIEWED (NEEDS_DATA)
"""

FILES["EXPERIMENT_REGISTRY.md"] = """# EXPERIMENT REGISTRY (pre-registered)

## EXP-DET-EMA-ADX
- proposal: proposals/PROP-DET-EMA-ADX.md
- spec: experiments/EXP-DET-EMA-ADX.md
- status: NEEDS_DATA
- evidence: reports/research/v10_47_8_scientific_repair/det_strategies_result.json (smoke only, INSUFFICIENT_DATA)

## EXP-DET-DONCHIAN
- proposal: proposals/PROP-DET-DONCHIAN.md
- spec: experiments/EXP-DET-DONCHIAN.md
- status: NEEDS_DATA
- evidence: reports/research/v10_47_8_scientific_repair/det_strategies_result.json (smoke only, INSUFFICIENT_DATA)

## EXP-CAUSAL-TOURNAMENT-12
- spec: experiments/EXP-CAUSAL-TOURNAMENT-12.md
- status: COMPLETE
- evidence: reports/research/v10_47_8_scientific_repair/causal_tournament_summary.json
- result: SHADOW_CANDIDATES=0 NO_CONFIRMED_EDGE
"""

FILES["SYNTHESIS.md"] = """# SYNTHESIS

The only V10.47 "leads" (P08_LONG 1m) were artifacts of a broken accounting rule.
Under a causal single-position ledger they are net-negative. The full 12-combo
causal tournament, with exposure-matched baselines and multiple-testing
correction, produces zero shadow candidates. The deterministic 1h/4h strategies
are implemented and causal but cannot be evaluated scientifically on ~90 days of
data. Net synthesis: **no confirmed edge; the next real step is data, not code.**
"""

FILES["DISAGREEMENTS.md"] = """# DISAGREEMENTS (never deleted)

## DIS-001 — Cluster accounting
- WORK/CODEX: LAST_SIGNAL_CLUSTER_ACCOUNTING silently ex-post-selects the last
  signal per cluster and is scientifically invalid.
- (historical V10.47 engine implicitly assumed it was harmless.)
- **Resolution:** FIRST_CAUSAL_SIGNAL_SINGLE_POSITION (see DECISIONS D001).
- Kept for the record even though resolved.
"""

FILES["REQUESTS.md"] = """# REQUESTS

- REQ-001 (open): obtain ≥2y verified 1h/4h OHLCV for BTC/ETH/XRP/DOGE from a
  reproducible free public source, with manifest + SHA + gap checks. Blocks
  EXP-DET-EMA-ADX and EXP-DET-DONCHIAN.
"""

FILES["IMPROVEMENT_ROADMAP.md"] = """# IMPROVEMENT ROADMAP

1. Data: 2y 1h/4h OHLCV (unblocks deterministic strategies).
2. Real funding sign/rate + real OI history (unblocks canonical P08).
3. Forward paper-shadow harness keyed on timestamp > selection_end_ms.
4. Broader regime tagging for regime-matched baselines.
"""

FILES["WORK_RESEARCH.md"] = """# WORK — RESEARCH NOTES

Findings that drove this repair (all reproduced before fixing):
1. Fixed 1-minute EventClock step for all timeframes.
2. per_cluster overwrite → last signal per cluster selected ex post.
3. Multiple concurrent signals per cluster silently executed.
4. Unmatched random baseline (count/exposure mismatch).
5. Post-selection "OOS" mislabeled as out-of-sample.
6. n_eff == trade count (ignored dependence).
7. Ambiguous intrabar trailing sequence.
8. P08 proxy mislabeled as OI/Funding.
9. "observed" costs were fixed bps tables.

Falsification for any future candidate: it must fail if it cannot beat an
exposure-matched random baseline on a strictly-later validation window.
"""

FILES["FABLE_IMPLEMENTATION.md"] = """# FABLE — IMPLEMENTATION LOG

Implemented on the canonical infra (no new engine):
- event_clock: interval_ms_for / cluster_id_tf / cluster_block_ms / session_id / day_id
- sim_oms: interval_ms threading + causal trailing (next-bar effect)
- causal_ledger.drive_causal (immutable, first-causal, single-position)
- causal_stats (n_eff, matched random null, block bootstrap)
- causal_tournament (closed registry, splits, gate)
- cost_truth, families.strategy_truth/strategy_matrix (P08 proxy truth)
- det_strategies (DET_EMA_ADX_PULLBACK_1H_4H, DET_DONCHIAN_BREAKOUT_4H)

Green tests are correctness checks, NOT evidence of edge.
"""

FILES["CODEX_AUDIT.md"] = """# CODEX — INDEPENDENT AUDIT

Reproduced Work's central claim independently:
- DOGE 1m P08_LONG: flawed +0.6726€ → causal −0.7300€ (FLIP).
- XRP 1m P08_LONG: flawed +0.3103€ → causal −0.5574€ (FLIP).
Confirmed: 12 causal tournaments → 0 shadow candidates; holdout untouched;
registry closed (m_unique=47). No contradictions found in the repaired pipeline.
Open audit item: deterministic strategies unevaluable until 2y data exists.
"""

FILES["EVIDENCE_INDEX.md"] = """# EVIDENCE INDEX

- reproduction_flip.json — the sign flip (DOGE/XRP)
- invalidation_manifest.json / INVALIDATION.md — invalidation record
- tournament/*.json (12) — causal tournament per-combo
- causal_tournament_summary.json — consolidated
- det_strategies_result.json — deterministic strategies (INSUFFICIENT_DATA smoke)
- reports/research/v10_47_8_scientific_repair/manifests/output_manifest.json — SHA-256 of every output + git identity + seal
- logs/ — reproduction, causal_tournament, det_strategies, full_suite
"""

FILES["SESSION_HANDOFF.md"] = """# SESSION HANDOFF

Resume point: hub + reports + manifest + dashboard + final suite done for the
scientific repair. Result: SCIENTIFIC REPAIR COMPLETE — NO CONFIRMED EDGE. The
single NEXT_ACTION is to acquire ≥2y 1h/4h data and then run the two
pre-registered deterministic experiments. No push; no live.
"""

FILES["COLLABORATION_PROTOCOL.md"] = """# COLLABORATION PROTOCOL

- One NEXT_ACTION. Decisions append-only with IDs. Disagreements preserved.
- IDEA → REVIEW → SYNTHESIS → PREREGISTERED EXPERIMENT → IMPLEMENTATION →
  EVIDENCE → AUDIT → DECISION → NEXT ACTION.
- Every experiment cites evidence; every proposal has a review; every claim is
  reproducible. No re-run without stating what changed. Green tests ≠ edge.
"""

FILES["BLOCKERS.md"] = """# BLOCKERS

- BLK-001: no ≥2y verified 1h/4h OHLCV → deterministic strategies NEEDS_DATA.
- BLK-002: no reproducible free historical real OI / funding-sign feed →
  canonical P08_OI_FUNDING_DIVERGENCE cannot be implemented (only the proxy).
- BLK-003: no free historical L2 order book → book-based costs stay MODELLED.
"""

FILES["MEETING_NOTES.md"] = """# MEETING NOTES

## 2026-07-14 — Scientific repair review
- Accepted Work's audit in full; reproduced before fixing.
- Adopted FIRST_CAUSAL_SIGNAL_SINGLE_POSITION (D001).
- Invalidated V10.47 shadow candidates (D002).
- Deterministic strategies gated on data (D003).
- Verdict: NO CONFIRMED EDGE. Next real step is data.
"""

FILES[os.path.join("proposals", "PROP-DET-EMA-ADX.md")] = """# PROPOSAL — DET_EMA_ADX_PULLBACK_1H_4H
- mechanism: 4h EMA50/EMA200 + ADX/DI regime; 1h causal pullback to EMA50
  (ATR-normalised) with RSI recovery; next-bar-open entry.
- hypothesis: trend-regime pullbacks have positive net expectancy after costs.
- data needed: ≥2y verified 1h + 4h OHLCV (BTC/ETH/XRP/DOGE).
- preregistered params: EMA50/EMA200, ADX≥20, pullback≤1 ATR, RSI recover ~45,
  stop 2 ATR, trailing from 1R, time exit 24.
- baseline: exposure-matched random + no-trade.
- metric: net EUR, corrected block-bootstrap lower bound > 0.
- falsification: fails if it cannot beat matched random on strictly-later validation.
- split: 12m train / 4m validation / 4m walk-forward / 4m sealed holdout.
- overfit risk: one dimension per challenger; no grid search.
- status: NEEDS_DATA
- review: reviews/REV-DET-EMA-ADX.md
"""

FILES[os.path.join("proposals", "PROP-DET-DONCHIAN.md")] = """# PROPOSAL — DET_DONCHIAN_BREAKOUT_4H
- mechanism: 20/55 Donchian channel EXCLUDING current bar + EMA/ADX/DI regime;
  block >1 ATR extended; next-bar-open entry; LONG/SHORT.
- hypothesis: regime-filtered channel breakouts have positive net expectancy.
- data needed: ≥2y verified 4h OHLCV (BTC/ETH/XRP/DOGE).
- preregistered params: Donchian 20/55, ADX≥20, extension≤1 ATR, stop 2 ATR,
  trailing baseline, time exit 24.
- baseline: exposure-matched random + no-trade.
- metric: net EUR, corrected block-bootstrap lower bound > 0.
- falsification: fails if it cannot beat matched random on strictly-later validation.
- split: 12m train / 4m validation / 4m walk-forward / 4m sealed holdout.
- overfit risk: one dimension per challenger; no grid search.
- status: NEEDS_DATA
- review: reviews/REV-DET-DONCHIAN.md
"""

FILES[os.path.join("reviews", "REV-DET-EMA-ADX.md")] = """# REVIEW — PROP-DET-EMA-ADX
- feasibility: implemented + causally smoke-tested on 90d-resampled bars.
- blocker: data < 2y ⇒ cannot evaluate scientifically.
- verdict: APPROVED-PENDING-DATA (status NEEDS_DATA). No promotion.
"""

FILES[os.path.join("reviews", "REV-DET-DONCHIAN.md")] = """# REVIEW — PROP-DET-DONCHIAN
- feasibility: implemented + causally smoke-tested on 90d-resampled bars.
- blocker: data < 2y ⇒ cannot evaluate scientifically.
- verdict: APPROVED-PENDING-DATA (status NEEDS_DATA). No promotion.
"""

FILES[os.path.join("experiments", "EXP-DET-EMA-ADX.md")] = """# EXPERIMENT — EXP-DET-EMA-ADX
status: NEEDS_DATA
proposal: proposals/PROP-DET-EMA-ADX.md
evidence: reports/research/v10_47_8_scientific_repair/det_strategies_result.json
notes: smoke only (INSUFFICIENT_DATA). Run under closed registry once ≥2y data exists.
"""

FILES[os.path.join("experiments", "EXP-DET-DONCHIAN.md")] = """# EXPERIMENT — EXP-DET-DONCHIAN
status: NEEDS_DATA
proposal: proposals/PROP-DET-DONCHIAN.md
evidence: reports/research/v10_47_8_scientific_repair/det_strategies_result.json
notes: smoke only (INSUFFICIENT_DATA). Run under closed registry once ≥2y data exists.
"""

FILES[os.path.join("experiments", "EXP-CAUSAL-TOURNAMENT-12.md")] = """# EXPERIMENT — EXP-CAUSAL-TOURNAMENT-12
status: COMPLETE
evidence: reports/research/v10_47_8_scientific_repair/causal_tournament_summary.json
result: 12 combos, m_unique=47, holdout sealed, SHADOW_CANDIDATES=0, NO_CONFIRMED_EDGE.
"""

ALWAYS = {"CURRENT_STATE.md", "NEXT_ACTION.md"}
written = 0
for rel, content in FILES.items():
    p = os.path.join(HUB, rel)
    if os.path.basename(rel) in ALWAYS or not os.path.exists(p):
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(content)
        written += 1
print(f"hub scaffolded at {HUB}: {written} files written, {len(FILES)} total")
