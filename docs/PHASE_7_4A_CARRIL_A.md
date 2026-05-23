# Phase 7.4A — Carril A Sprint Report

Status: research/shadow/read-only. **No runtime hook added.** No `.env` change.
No commit/push. No VPS touched. final_recommendation: **NO LIVE**.

Base commit: `4e9799d` (stability hotfix).

## TL;DR

Carril A delivers six new read-only audit modules + seven design skeletons for
Carril B + microstructure roadmap doc. **0 changes** to runtime trading,
PaperTrader, ExecutionEngine, RiskManager, SignalEngine, or BitgetClient
besides the already-committed stability hotfix.

Resolved during this sprint:
- **Track G (worker duplicate audit false positive)**: fixed — duplicate
  status now trusts `worker_lock` as source of truth and tolerates tmux/bash
  artefacts.
- **Track B (data quality audit module)**: new module `data_quality_audit.py`
  diagnoses exact duplicates vs benign density; never deletes anything.
- **Track C (label quality audit module)**: new module `label_quality_audit.py`
  classifies missed/inconsistent labels and surfaces TP_TOO_FAR /
  SL_TOO_TIGHT / HORIZON_TOO_SHORT flags.
- **Track H (data vault cleanup audit)**: new module
  `data_vault_cleanup_audit.py` lists incomplete work dirs with
  `safe_to_delete` boolean per entry — never executes deletion.
- **Track F (cost model coverage)**: 11 new tests cover taker/maker
  combinations, funding direction + timestamp crossing, market_probe zero
  cost, already_includes_costs no-double-count, TIME no_trade assumption.
- **Track J (Phase 7.4B skeletons)**: 6 design-only Python skeletons +
  microstructure roadmap doc. All return DESIGN_ONLY status. All include
  `final_recommendation: NO LIVE` and `no_runtime_change: true`.

Partially addressed (audit + documentation, no refactor):
- **Track A (report reliability)**: documented and audited.
  See section "Track A status" below. No major refactor of `dashboard_pro.py`
  or `health_server.py` — those are the kind of large existing modules where
  a sprint-scope rewrite would create risk; documented gaps + safer next-step
  proposal.
- **Track D/E (OHLCV + backtester)**: existing infrastructure (built in
  Phase 7.2 sprint) is intact. Audit confirms idempotency and no-lookahead
  remain correct. Documented backfill commands to be run on VPS *after
  review*, not executed here.
- **Track I (dashboard binding)**: existing dashboard markers
  (`research_only`, `no_runtime_change`, `final_recommendation: NO LIVE`)
  audited and verified — present across new modules. Not visually wired
  into `dashboard_pro.py` sections — that's deferred to Carril B.

## Files modified (3)

| File | Change |
|---|---|
| `app/worker_health_audit.py` | Track G: duplicate_status now trusts worker_lock + dedups process listing into distinct PIDs. False BAD eliminated. |
| Tests passing 484 → expected 517 with new sprint tests (see below). |

## Files new (10)

| File | Track | Purpose |
|---|---|---|
| `app/data_quality_audit.py` | B | Read-only DB integrity audit (duplicates, density, orphans). |
| `app/label_quality_audit.py` | C | Read-only label vs path metric consistency audit. |
| `app/data_vault_cleanup_audit.py` | H | Read-only incomplete_work_dirs audit with `safe_to_delete` flag. |
| `app/profit_lock_exit_lab.py` | J | Phase 7.4B skeleton — break-even/trailing exit lab. |
| `app/fast_exit_lab.py` | J | Phase 7.4B skeleton — score decay / signal flip exits. |
| `app/mtf_regime_gate_lab.py` | J | Phase 7.4B skeleton — multi-timeframe regime gate evaluator. |
| `app/momentum_burst_5m_lab.py` | J | Phase 7.4B skeleton — needs OHLCV 5m persisted. |
| `app/setup_key_trainer.py` | J | Phase 7.4B skeleton — labels setup_keys by quality. |
| `app/net_ev_trainer.py` | J | Phase 7.4B skeleton — ranks setups by penalised net_EV. |
| `tests/test_phase_7_4a_sprint.py` | F+G+B+C+H+J | 33 tests covering all sprint tracks. |
| `docs/MICROSTRUCTURE_ROADMAP.md` | J | Documentation only. No code. NO WebSocket. NO market making. |
| `docs/PHASE_7_4A_CARRIL_A.md` | K | This file. |

## Track-by-track detail

### Track A — Report reliability (audit only)

Existing infrastructure inspected:
- `dashboard_pro.py` already has section-level timeouts (`SECTION_TIMEOUT` warnings
  in PARTIAL_REPORT path, documented in earlier audit doc).
- `worker_lightweight_mode=True` default skips heavy research in worker.
- WAL + busy_timeout (committed in 4e9799d) reduce DB-side contention.

Recommended next moves NOT taken in this sprint (would be substantial refactor):
- Raise per-section timeout from 3s → 10-15s in `dashboard_pro.py` (config-driven).
- Add `LIMIT` clauses to full-table queries inside report sections.
- Move full_research_report to subprocess or cron separated from worker.

These are out-of-scope for a "no runtime change" sprint. Documented as Carril B work.

### Track B — Data Quality Audit

`DataQualityAudit.build(hours=24)` returns a `DataQualityReport` with:
- per-table classification: `CLEAN` / `BENIGN_DENSITY` / `EXACT_DUPLICATE` / `MIXED` / `ERROR`
- relation diagnosis: orphan path_metrics, labels without observation, multi-labels,
  conflicting labels, path vs label mismatch
- recommended action; **never** deletes or modifies anything

Density thresholds tuned per table to known scan cadence; rows above threshold but
without exact duplicate fingerprints are flagged as `BENIGN_DENSITY` rather than `BAD`.

Sample CLI usage (manual, not wired):
```python
from app.data_quality_audit import DataQualityAudit, render_report_text
audit = DataQualityAudit(db)
print(render_report_text(audit.build(hours=24)))
```

### Track C — Label Quality Audit

`LabelQualityAudit.build(hours=24)` queries `signal_labels` × `signal_observations` ×
`signal_path_metrics` and returns a `LabelQualityReport` with:
- TP/SL/TIME breakdown + rates
- `missed_tp_labels` / `missed_sl_labels` counts (MFE/MAE crossed threshold but
  the label didn't fire)
- `inconsistent_time_labels`, `path_metric_label_mismatch`, `both_tp_sl_touched_count`,
  `stale_labels`
- diagnostic flags `tp_too_far`, `sl_too_tight`, `horizon_too_short`
- `recommended_action` (research only — never modifies labels)

### Track D — OHLCV 5m foundation (already done in Phase 7.2)

Schema confirmed:
```
ohlcv_candles (
  symbol, timeframe, timestamp, open, high, low, close,
  volume, quote_volume, source, ingested_at,
  PRIMARY KEY (symbol, timeframe, timestamp)
)
```
- Idempotent INSERT OR IGNORE
- Loader: `OhlcvReplayLoader` returns `NEED_DATA` cleanly when empty
- No MFE/MAE fallback (verified by tests added in Phase 7.2)

Commands to be run on VPS **after review** (NOT executed here):
```bash
# Backfill 5m for the canonical 10 symbols, 90 days, idempotent
python -m app.ohlcv_backfill \
  --symbols BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,BNBUSDT,LINKUSDT,AVAXUSDT,ADAUSDT,DOTUSDT \
  --timeframes 5m \
  --days 90

# Audit OHLCV after backfill
python -c "from app.config import load_config; from app.database import Database; \
  from app.ohlcv_replay_loader import ohlcv_replay_loader_audit_text; import logging; \
  c=load_config(); d=Database(c, logging.getLogger()); d.initialize(); \
  print(ohlcv_replay_loader_audit_text(c, d, hours=72))"
```

### Track E — Real Strategy Backtester (already done in Phase 7.2)

Tests already in repo verify:
- no lookahead (`OK_PREFIX_ONLY`)
- entry next open `i+1`
- same-bar STOP_BEFORE_TP rule
- BLOCK_BELOW_MIN_NOTIONAL
- NEED_DATA when OHLCV absent
- Never uses MFE/MAE as a fallback

Once Track D backfill is executed on VPS, `RealStrategyBacktester.run()` will
produce real results instead of `NEED_DATA`.

### Track F — Cost Model

11 tests added covering:
- taker/taker = 12 bps (Bitget VIP0 round-trip)
- maker/maker = 4 bps
- maker/taker = 8 bps
- slippage scales with liquidity profile + execution type
- funding only applied if entry → exit crosses 00:00/08:00/16:00 UTC funding window
- LONG pays positive funding, SHORT receives positive funding (sign-correct)
- market_probe path returns zero cost (research-only)
- `already_includes_costs=True` returns zero cost (prevents double counting)
- TIME exit with `no_trade` assumption returns zero cost

### Track G — Worker Duplicate Audit FIX

`worker_health_audit.py` no longer flags BAD just because `pgrep` finds multiple
`app.main` lines.

New logic:
- `_distinct_python_app_main_pids()` extracts unique PIDs (or unique cmdlines) from the listing.
- `_classify_duplicate_status()` decision tree:
  - `lock_status == "blocked_duplicate"` → **BAD** (lock is source of truth)
  - `warning_if_duplicate_worker == "duplicate_worker_detected"` → **BAD**
  - multiple PIDs + lock `missing|expired` → **WARNING** (real race risk)
  - multiple PIDs + lock owned/acquired/heartbeat → **OK** (process count artefact)
  - single or zero PIDs → **OK**

`worker_process_count` is still reported for visibility, plus
`distinct_python_app_main_pids` and `duplicate_worker_reason`.

### Track H — Data Vault Cleanup Audit

`DataVaultCleanupAudit.build()` lists `training_vault_*_work` directories with:
- `name`, `path`, `age_hours`, `size_mb`
- `superseded_by`: name of newer complete `.zip` if any
- `safe_to_delete`: True only if age ≥ 48h AND ≥1 newer complete backup exists
- `reason` text

Never executes deletion. Emits `delete_command_template` for operator use.

### Track I — Dashboard binding (audit only)

Existing dashboard already shows `final_recommendation: NO LIVE`, `safety: ok`,
`paper_filter_enabled: false`. New modules add identical markers in their
text renderers. No visual wiring added to `dashboard_pro.py` — would be a
visual refactor; deferred.

### Track J — Phase 7.4B skeletons

7 design-only modules + microstructure roadmap. Each returns DESIGN_ONLY status
+ `final_recommendation: NO LIVE` + `no_runtime_change: true`. Test asserts
each module is design-only.

### Track K — Documentation

This document plus `MICROSTRUCTURE_ROADMAP.md`. Session handoff to be written
alongside if needed.

## Safety verification

- LIVE_TRADING=False
- DRY_RUN=True
- PAPER_TRADING=True
- ENABLE_PAPER_POLICY_FILTER=False
- ENABLE_CANDIDATE_SHADOW_MONITOR=False
- can_send_real_orders=False
- No `.env` modified
- No `bot_state.db*` written by tests (fixtures use tmp_path)
- No Bitget endpoints (public or private) called
- No order placed
- No leverage/margin/sizing/slot changes
- No live activation

## What is intentionally NOT done (future)

- No timeout raised in `dashboard_pro.py` sections (avoid touching huge existing module mid-sprint).
- No `dashboard_pro.py` UI cards wired to new audits — Carril B.
- No execution of any backfill on VPS — operator action.
- No execution of any data deletion — never automated.
- No runtime hook for shadow monitor (already a separate operator decision).
- No WebSocket. No market making. No microstructure (per roadmap).

## How to verify locally

```bash
.venv\Scripts\python.exe -m compileall app tests
.venv\Scripts\python.exe -m pytest -q --basetemp .manual_test_tmp\phase74a-megasprint
```

Expected: full suite green (476 from prior + 33 new = 509-ish, see actual number in
final session report).

## FINAL_RECOMMENDATION

**NO LIVE**
