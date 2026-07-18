# Bitget Research Project Contract

## Purpose

This contract is the persistent safety memory for the local research stack. It
does not control trading and cannot promote a strategy. It protects the current
ATI, P11 and Cross-Venue boundaries, paper-account identity, active-policy
files, holdout access counters and safety flags across restarts.

## Fixed safety state

- `PAPER_TRADING=True`
- `LIVE_TRADING=False`
- `DRY_RUN=True`
- `ENABLE_PAPER_POLICY_FILTER=False`
- `ENABLE_CANDIDATE_SHADOW_MONITOR=False`
- `can_send_real_orders=false`
- `FINAL_RECOMMENDATION=NO LIVE`

The contract also forbids account resets, boundary regressions, active route or
policy changes, automatic promotion, `.env` changes and raw deletion without a
verified remote restoration.

## Runtime artifacts

The versioned contract is
`config/project/BITGET_RESEARCH_PROJECT_CONTRACT.json`. Runtime state and the
hash-chained decision ledger are written only below
`data/runtime/project_memory/`, which is ignored by Git.

The guard reads ATI and Cross-Venue SQLite ledgers with SQLite `mode=ro`. It
freezes account ID, initial balance and creation time, but not changing equity.
ATI/P11 boundaries and Cross-Venue initial offsets are immutable. Cross-Venue
current offsets are byte cursors in rotating `current.jsonl` files, so a
coordinated rollover may reduce them; the guard requires every cursor to remain
present and non-negative instead of incorrectly treating it as a global counter.
The `.env` file is never read by this guard: only existence, size and modification
timestamp are fingerprinted, and no secret value is logged.

## Operation

```powershell
python -m app.research_lab project-memory-contract-v1 --apply
python -m app.research_lab project-memory-status-v1
```

The first command creates the baseline only when every required source and
safety flag is valid. Later runs compare against that baseline. Any violation
sets `guardrails_status=FAIL` and prevents scheduled Challenger or demo work;
collectors and existing ledgers are not modified.

The decision ledger is append-only, hash chained, research-only and always marks
human approval as required. It must never be used as an order or execution log.

## Change control

Intentional changes to an active policy, account identity, boundary contract or
the versioned contract require a separate human-reviewed migration. Deleting the
runtime baseline to bypass a failure is not an approved migration.

SIMULATION ONLY. RESEARCH ONLY. NO LIVE.
