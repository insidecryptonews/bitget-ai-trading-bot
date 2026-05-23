# Phase 7.4A-2 Hotfix Report

Status: research/shadow/read-only. No runtime trading changed. No `.env`
modified. No commit/push. No VPS touched. final_recommendation: **NO LIVE**.

Base commit: `5a358bb` (Phase 7.4A foundation).

## TL;DR

Three real production issues fixed:

1. **Worker Health Audit false BAD** — when the audit runs from a process
   DIFFERENT to the worker (dashboard report builder, CLI, cron), it saw
   `lock_status="blocked_duplicate"` and marked the worker as duplicate. It
   also counted tmux/bash wrappers whose ARGS mention `python -m app.main`
   as if they were real workers. Both fixed. Audit now only marks BAD when
   there are actually 2+ real Python interpreters running `app.main`.

2. **Dashboard short report PARTIAL_REPORT** — 8 heavy sections always
   timed out at the 3s budget against a 5GB DB. They now SKIP with status
   `skipped_heavy` in short mode. Report status stays `OK`. Caller can
   opt in to including heavy sections via `include_heavy=True`.

3. **OHLCV 5m on VPS** — backfill code already supports 5m. Documented
   exact commands to run on VPS after review.

## Files modified

| File | Change |
|---|---|
| `app/worker_health_audit.py` | New `_filter_real_python_workers()` drops tmux/bash; `_classify_duplicate_status()` rewritten to distinguish audit-from-non-worker from real duplicate. Health rollup no longer marks BAD just because lock is blocked. |
| `app/dashboard_pro.py` | New class const `SHORT_REPORT_HEAVY_SECTIONS` lists the 8 heavies. `build_short(include_heavy=False)` now SKIPS them via new `_skip_heavy_section()` helper with status `skipped_heavy`. Module-level helper passes the flag through. |

## Files new

| File | Purpose |
|---|---|
| `tests/test_phase_7_4a_2_hotfix.py` | 19 tests: process filtering, classification, e2e VPS scenario, dashboard skip semantics, OHLCV 5m CLI surface check. |
| `docs/PHASE_7_4A_2_HOTFIX.md` | This file. |

## Fix detail — Worker Health Audit

### Problem 1 (process counting)

`pgrep -af 'python.*app.main'` returned 3 lines on VPS:

```
12345 tmux new-session -d -s bot 'python -m app.main'
12346 bash -c 'python -m app.main'
12347 .venv/bin/python -m app.main
```

The regex `python.*app.main` matches the cmdline of all three because tmux
and bash both carry `python -m app.main` as a sub-argument.

**Fix**: new `_filter_real_python_workers(lines)` strips the leading PID,
extracts the first executable token of the cmdline, and only keeps lines
whose first token starts with `python` (covers `python`, `python3`,
`/usr/bin/python`, `.venv/bin/python`, `python.exe`). The tmux/bash
wrappers' first tokens are `tmux` and `bash`, so they're dropped.

The audit now reports both:

- `worker_process_count`: real Python workers only (= 1 on VPS).
- `worker_process_raw_count`: raw pgrep matches including wrappers (= 3 on VPS).

### Problem 2 (audit-from-non-worker classification)

`worker_lock_status_payload(config, db)` creates a fresh `WorkerLockManager`
with a NEW `instance_id`. When called from the dashboard report builder
(a different process than the actual worker), it sees the worker's lock
and reports `lock_status="blocked_duplicate"`. That's NOT a duplicate
worker — it's just the audit running from another process.

**Fix**: `_classify_duplicate_status()` rewritten with new outcome matrix:

```
distinct_pids > 1 AND blocked_duplicate     -> BAD  (real conflict)
distinct_pids > 1 AND lock missing/expired  -> WARNING (race risk)
distinct_pids > 1 AND lock owned/heartbeat  -> WARNING (suspicious)
distinct_pids == 1 AND blocked_duplicate    -> OK   ← VPS case
                                                     (audit-from-non-worker,
                                                      fresh known owner)
distinct_pids == 1 AND owned/acquired       -> OK
distinct_pids == 0 AND blocked_duplicate    -> WARNING (possible stale lock)
distinct_pids == 0 AND missing              -> OK   (e.g. tests)
```

The `active_worker_instance` + `lock_age_seconds` are now consumed to tell
"someone holds the lock fresh" vs "stale lock".

### Aggregate health

`worker_health_status` no longer takes `lock_status == "blocked_duplicate"`
as a direct BAD signal. It now follows `duplicate_status`:

```python
if duplicate_status == "BAD" or stale:
    health = "BAD"
elif api_error_status != "OK" or mismatch != "OK" or duplicate_status == "WARNING":
    health = "WARNING"
else:
    health = "OK"
```

## Fix detail — Dashboard short report

### Problem

`build_short(hours=24)` ran 22 sections with a 3s timeout each. 8 of them
query large tables (5GB+) and reliably timed out. Every short report
ended `report_status=PARTIAL_REPORT` with 8 `SECTION_TIMEOUT` warnings.

### Fix

Add explicit allow-list of HEAVY sections that are **skipped in short mode**:

```python
SHORT_REPORT_HEAVY_SECTIONS = (
    "Operational Intelligence",
    "Strategy Research Library",
    "Data Pipeline Diagnosis 24h",
    "Label Quality V2 24h",
    "Bitget Cost Model Audit 24h",
    "Edge Guard 24h",
    "Paper Policy Orchestrator 24h",
    "Time Death Autopsy 24h",
)
```

New helper `_skip_heavy_section(name)` returns a `ReportSection` with
status `skipped_heavy` and text:

```
SKIPPED_HEAVY_SECTION: <name>
Heavy sections are excluded from the short report to keep latency under control.
Run the full report (build()) to include this section, or invoke the underlying
CLI directly via `python -m app.research_lab <command>`.
final_recommendation: NO LIVE
```

Key semantic change: `report_status` calculation:

```python
report_status = "PARTIAL_REPORT" if any(
    section.status in {"error", "timeout"} for section in rendered
) else "OK"
```

`skipped_heavy` is **NOT** in `{"error", "timeout"}`, so the short report
returns `OK` even when 8 sections are intentionally skipped.

### Opt-in via include_heavy=True

If you want the heavy sections in short mode (e.g. ad-hoc debugging):

```python
DashboardProReporter(config, db).build_short(hours=24, include_heavy=True)
# or
build_dashboard_short_report(config, db, hours=24, include_heavy=True)
```

Full report (`build()`) is unchanged and always includes everything.

## OHLCV 5m on VPS — commands (NOT executed here)

The backfill script already supports 5m. Before running on VPS, validate
that the dry-run path is healthy:

```bash
# Dry-run on a single symbol/24h to confirm the path works
python -m app.ohlcv_backfill \
  --symbols BTCUSDT \
  --timeframes 5m \
  --hours 24 \
  --dry-run

# Verify what the OHLCV replay loader sees (should still report 0 rows
# after a dry-run since dry-run does not write).
python -c "from app.config import load_config; from app.database import Database; \
  from app.ohlcv_replay_loader import ohlcv_replay_loader_audit_text; import logging; \
  c=load_config(); d=Database(c, logging.getLogger()); d.initialize(); \
  print(ohlcv_replay_loader_audit_text(c, d, hours=72))"
```

Then a small write run (30 days, 1 symbol) to confirm idempotency and
storage cost before going big:

```bash
python -m app.ohlcv_backfill \
  --symbols BTCUSDT \
  --timeframes 5m \
  --days 30
```

Inspect the result and DB growth before the big run:

```bash
# Should report 30 days * 288 candles/day = ~8640 candles for BTC 5m.
python -c "from app.config import load_config; from app.database import Database; \
  import logging; c=load_config(); d=Database(c, logging.getLogger()); d.initialize(); \
  print('BTCUSDT 5m rows:', d.count_ohlcv_rows('BTCUSDT', '5m'))"

# Disk usage:
du -sh bot_state.db bot_state.db-wal bot_state.db-shm 2>/dev/null
```

Once happy, the full canonical run:

```bash
python -m app.ohlcv_backfill \
  --symbols BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,BNBUSDT,LINKUSDT,AVAXUSDT,ADAUSDT,DOTUSDT \
  --timeframes 5m \
  --days 365
```

### Expected resource cost

- **API calls**: 365 days × 288 candles/day = ~105k candles per symbol.
  Bitget history-candles cap is 200 per call → ~530 calls per symbol × 10
  symbols = ~5 300 calls. At 8 calls/s rate-limit → ~11 minutes nominal.
  Realistic with backoff and idempotency skips: 15–25 minutes.
- **Storage**: each candle row ~120 bytes serialized. 10 symbols × 105k =
  1.05M rows × 120 B ≈ 130 MB before WAL/index overhead. With SQLite
  overhead expect ~250–400 MB net growth in `bot_state.db`.
- **Idempotency**: rerunning the same command is safe; existing rows are
  skipped via the composite PK `(symbol, timeframe, timestamp)`.

### After backfill

`real_strategy_backtester` will stop returning `NEED_DATA` and produce
real metrics on the 365-day window:

```bash
python -c "from app.config import load_config; from app.database import Database; \
  from app.real_strategy_backtester import real_strategy_backtest_text; import logging; \
  c=load_config(); d=Database(c, logging.getLogger()); d.initialize(); \
  print(real_strategy_backtest_text(c, d, hours=24*365))"
```

## Safety

- LIVE_TRADING=False
- DRY_RUN=True
- PAPER_TRADING=True
- ENABLE_PAPER_POLICY_FILTER=False
- ENABLE_CANDIDATE_SHADOW_MONITOR=False
- can_send_real_orders=False
- `.env` not touched
- No DB writes from tests (tmp_path fixtures only)
- No Bitget endpoints invoked from tests
- No order placed
- No commit/push performed by this hotfix

## What is NOT changed

- `app/main.py` runtime loop untouched.
- `app/execution_engine.py` untouched.
- `app/paper_trader.py` untouched.
- `app/signal_engine.py` untouched.
- `app/risk_manager.py` untouched.
- `app/bitget_client.py` untouched.
- `app/ohlcv_backfill.py` already supported 5m; no change needed.
- Full report (`build()`) behaviour unchanged.

## Pending heavy report sections (out of scope for this hotfix)

The 8 heavy sections are SKIPPED in short mode. They still **work in full
report mode** with the existing 60s budget (`build` uses `timeout_seconds=None`).

If short-mode users want the data, they can:
1. Invoke `python -m app.research_lab <command>` directly (each lab has
   its own CLI).
2. Use the full report endpoint when available.
3. Pass `include_heavy=True` to `build_short` (slow, but works).

Future Carril B work: precompute & cache heavy sections via a background
cron, then short report reads from cache. Out of scope here.

## FINAL_RECOMMENDATION

**NO LIVE**
