# ATI Paper Simulation V1

## Scope

ATI Paper is a local, persistent simulation account. It consumes only new
forward records produced by `ATI_SHADOW_POLICY_V2` after the executor is
running. It does not use the productive `PaperTrader`, `ExecutionEngine`,
private Bitget endpoints, credentials, `.env`, leverage or margin.

Fixed safety contract:

- `ATI_MODE=PAPER_FORWARD_SIMULATION`
- `ATI_EXECUTION_MODE=SIMULATION_ONLY`
- `PAPER_TRADING=True`
- `LIVE_TRADING=False`
- `DRY_RUN=True`
- `ENABLE_PAPER_POLICY_FILTER=False`
- `can_send_real_orders=false`
- `FINAL_RECOMMENDATION=NO LIVE`

## Account And Sizing

The ledger account is `ATI_PAPER_50` and receives exactly 50 simulated USDT
once. A restart resumes the same balance, positions, orders, events and equity
curve; it never credits another 50 USDT.

The versioned sizing policy is `realized_equity_fraction` with a configured
fraction of `1.0`. Before each entry:

```text
requested_notional = realized_equity_before_entry * 1.0
```

The actual notional is rounded down to the public instrument quantity step and
reduced when necessary to reserve entry fee and adverse slippage without
borrowing. Unrealized PnL is never used for the next position. The configured
and effective fractions, equity before entry, risk distance, risk money,
quantity, notional and change from the previous closed trade are audited.

This is unlevered simulated allocation, not a risk-brake policy. There are no
daily-loss, drawdown, losing-streak, cooldown or invented trade-count stops.

## Execution Truth

- Only an exact ATI V2 forward `SHADOW_CANDIDATE` is eligible.
- Signals already present when the executor starts are rejected as not observed
  live; they are never filled retrospectively.
- Entry uses the first fresh public ticker obtained after live observation.
- A setup already invalidated by a gap is rejected.
- Fee and adverse slippage are separate from gross PnL and are never counted
  twice.
- Public closed 1-minute bars drive stops, targets, trailing and time exits.
- The entry partial minute is excluded because it contains pre-entry extremes.
- A stop and target in the same bar resolves as `STOP_BEFORE_TP`.
- A stop gap fills at the adverse open; a favorable target gap is capped at the
  target.
- Trailing levels use completed prior-bar information and apply from the next
  bar. Trailing is disabled in V1 unless the versioned config changes.
- Funding is `UNKNOWN` and zero until a reliable causal source is integrated;
  it remains separate from fees and slippage.
- Stale or unavailable public data blocks new entries and preserves positions.

## Persistence And Read-Only API

Runtime state lives under `data/runtime/ati_paper/` and is ignored by Git. The
SQLite ledger uses foreign keys, WAL, full synchronization, explicit
transactions, unique identifiers and startup reconciliation.

Read-only routes on the loopback research server:

- `/api/ati-paper/account`
- `/api/ati-paper/positions`
- `/api/ati-paper/trades`
- `/api/ati-paper/equity`
- `/api/ati-paper/events`
- `/api/ati-paper/signals`
- `/api/ati-paper/health`
- `/api/ati-paper/chart`
- `/api/ati-paper/performance`

There are no web routes for open, close, reset, sizing, leverage, paper-filter
or live changes. The offline reset command requires the exact confirmation
phrase and archives the prior ledger; normal operations never call it.

## Local Operations

```powershell
scripts\start_local_stack.ps1
scripts\status_local_stack.ps1
scripts\restart_local_stack.ps1
scripts\stop_local_stack.ps1
```

Dashboard:

```text
http://127.0.0.1:8765/research-dashboard
```

The fast dashboard watcher reads bounded artifacts only. Heavy research is a
separate six-hour scheduler. The scanner carries the isolated P11 observer
sidecar, avoiding a duplicate P11 process.

Administrative reset, not for routine execution and not exposed over HTTP:

```powershell
python -m app.labs.ati_paper.cli reset --confirmation "RESET ATI_PAPER_50 SIMULATION"
```

Do not execute reset unless a deliberate manual account reset has been
approved. No ATI Paper result is evidence of live readiness.
