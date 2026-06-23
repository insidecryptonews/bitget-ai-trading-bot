# Forward-Shadow VPS Runbook (V10.22)

How to deploy the V10.21/V10.22 forward-shadow regime overlay on the VPS **safely
and READ-ONLY**. This overlay sends NO orders, touches NO money, writes NO DB. It
only fetches public OHLCV, classifies the current regime, and journals it.

DO NOT execute this on the VPS in this phase unless the user explicitly instructs
it later. This document is the procedure, not an action.

## Pre-conditions (must all be true before starting)
- `LIVE_TRADING=false`, `DRY_RUN=true`, `PAPER_TRADING=true` in the VPS env.
- `can_send_real_orders=false`, `final_recommendation=NO LIVE`.
- You have a current Data Vault / R2 backup.

## Mandatory protocol (in order — stop on any failure)
1. **Backup** the Data Vault to R2 (existing backup tooling).
2. **Verify the backup**: confirm `uploaded=true` AND `verified=true`. If not, STOP.
3. **Stop the bot** (graceful shutdown; confirm `open_positions=0`).
4. **`git pull`** to the target commit (>= 801a308 / V10.21, or the V10.22 commit).
5. **`python -m compileall app scripts tests`** — must succeed.
6. **Targeted tests** (fast, must all pass):
   - `python -m pytest tests/test_researchops_v10_21_forward_shadow_regime.py -q`
   - `python -m pytest tests/test_researchops_v10_15_cross_exchange_public_ohlcv.py -q`
   - `python -m app.research_lab security-audit`  -> expect `SAFE_PAPER_ONLY` / `NO LIVE`
7. **Start the bot** (still paper/dry-run; the overlay is independent of runtime).
8. **Check `/health`** read-only endpoint responds and shows safe flags.
9. **Run the overlay (read-only)**:
   `python -m app.research_lab forward-shadow-regime-run-v1021 --sample-dir <staged_dir> --symbols BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT --timeframe 1d`
   (To refresh data first, use the allowlisted public collector
   `cross-exchange-ohlcv-fetch-v1015 --apply` into a staging dir, then point here.)
10. **Run the report**:
   `python -m app.research_lab forward-shadow-regime-report-v1021 --last-n 10`
11. **Confirm the safety invariants** in the output and env:
   - `LIVE_TRADING=false`
   - `DRY_RUN=true`
   - `PAPER_TRADING=true`
   - `can_send_real_orders=false`
   - `open_positions=0`
   - `final_recommendation=NO LIVE`

## Scheduling (optional, read-only)
A daily cron MAY run steps 9–10 to grow the journal. It must:
- never pass `--apply` to anything that writes outside staging,
- never call any execution/paper/live path,
- write only to the gitignored journal (`reports/research/v10_21/regime_journal/`).

## Rollback
If anything fails: stop the bot, `git checkout` the previous commit, restart in
paper/dry-run, verify `/health`. The overlay has no state that can corrupt runtime
(read-only, separate journal).

## Hard guardrails
No live, no paper filter activation, no orders, no leverage, no `.env` edits, no
keys, no private endpoints, no DB/raw writes, no auto-promotion. VPS/SSH steps only
under the full safe protocol above and explicit user instruction.

research_only: true
shadow_only: true
paper_ready: false
live_ready: false
can_send_real_orders: false
final_recommendation: NO LIVE
