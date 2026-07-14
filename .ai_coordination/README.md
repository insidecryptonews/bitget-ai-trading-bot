# .ai_coordination — Multi-Agent Research Hub

A file-based coordination hub for a small research team of AI roles working on
the Bitget AI trading-bot **research** (no live trading). Everything here is
append-friendly documentation; it executes nothing and authorises nothing.

**Safety invariant (always):** PAPER_TRADING=True · LIVE_TRADING=False · DRY_RUN=True · can_send_real_orders=false · FINAL_RECOMMENDATION=NO LIVE

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
