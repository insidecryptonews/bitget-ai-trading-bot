# ResearchOps V10.2 — Long-History Extension Runbook (VPS)

**Status: RESEARCH-ONLY. NO LIVE. Do NOT auto-run. Operator executes manually on the VPS.**

This runbook extends the Coinalyze BTC/ETH history to 180 (then optionally 365)
days, re-ingests, re-runs the diagnostics + stability + missing-OI audit, and
produces a consolidated validation. It downloads market DATA only — never
places orders, never enables paper filter, never touches `.env`.

> ⚠️ **Hard rules during this runbook**
> - NO LIVE. NO paper filter. NO real orders. NO leverage/margin/sizing changes.
> - Do NOT commit CSV/JSON data to Git (`external_data/**` data is git-ignored).
> - Do NOT print or store the API key. It lives only in the `COINALYZE_API_KEY` env var.
> - Do NOT touch `.env`. Do NOT write to the DB.

---

## 0. Preconditions

- VPS healthy, worker_lock OK, no duplicate worker, `open_positions=0`.
- Flags: `LIVE_TRADING=False`, `DRY_RUN=True`, `PAPER_TRADING=True`,
  `ENABLE_PAPER_POLICY_FILTER=False`, `can_send_real_orders=false`.
- `COINALYZE_API_KEY` exported in the shell env (NOT in `.env`, NOT in Git).

## 1. Backup first (R2 / Data Vault) — but DO NOT archive/delete raw yet

```bash
# Back up current state BEFORE any change (use the project's existing vault tooling).
python -m app.research_lab data-vault-status
python -m app.research_lab post-migration-backup    # or your standard vault backup
```

> ⚠️ **V10.2.1 change:** do **NOT** archive or delete `external_data/raw/*` before
> the new download is confirmed. The earlier flow archived old data first and a
> failed fetch left us restoring manually. The chunked fetcher (step 4) downloads
> into an isolated **staging** dir and only touches `raw/` on a fully successful
> publish, so old data is never at risk from an API failure.

## 2. Verify the key is present (never printed)

```bash
python -c "import os;print('COINALYZE_API_KEY_set:', bool(os.environ.get('COINALYZE_API_KEY')))"
# Expect: COINALYZE_API_KEY_set: True   (the value is never shown)
```

## 3. Stop the bot, update code, run tests, restart

```bash
# stop bot (use the project's standard stop; do NOT kill mid-write)
git pull                      # gets commit with V10.2 tools
python -m compileall app tests scripts
python -m pytest -q           # must be fully green before proceeding
# start bot again (standard start)
curl -s localhost:<port>/health   # expect ok
```

## 4. Download BTC/ETH — chunked, STAGING-ONLY first (safe; never touches raw)

Use the V10.2.1 chunked fetcher. It downloads in 30-day chunks into an
isolated staging dir and, in `staging-only` mode, does NOT publish to `raw/`
and does NOT archive/delete old data. A mid-download API failure leaves
`raw/` and old data fully intact.

```bash
# 180 days, 30-day chunks, staging-only (default). Symbol override avoids discovery issues.
python scripts/fetch_coinalyze_chunked_v102.py \
  --coinalyze-symbols "BTCUSDT=BTCUSDT_PERP.A,ETHUSDT=ETHUSDT_PERP.A" \
  --days 180 --interval 1hour --chunk-days 30 \
  --publish-mode staging-only
```

Inspect the printed report + `external_data/reports/coinalyze_chunked_fetch_*.json`:
- `report_status` should be `PARTIAL_STAGING_ONLY`.
- `chunks_ok == chunks_total`, `chunks_failed == 0`.
- `old_data_touched: false`.
- review `rows_market_state`, `rows_liquidations`, `min/max_timestamp`, `duplicates_removed`.

If a chunk failed (e.g. `report_status: FAILED`), the staging dir is intact and
`raw/` is untouched. Re-run with `--resume` (completed chunks are skipped):

```bash
python scripts/fetch_coinalyze_chunked_v102.py \
  --coinalyze-symbols "BTCUSDT=BTCUSDT_PERP.A,ETHUSDT=ETHUSDT_PERP.A" \
  --days 180 --interval 1hour --chunk-days 30 --resume \
  --staging-dir external_data/staging/coinalyze_long_history_<the_same_timestamp> \
  --publish-mode staging-only
```

## 4b. Publish ONLY after staging looks correct

```bash
# Replace mode: archives the current raw files (only now, after success) and publishes the new ones.
python scripts/fetch_coinalyze_chunked_v102.py \
  --coinalyze-symbols "BTCUSDT=BTCUSDT_PERP.A,ETHUSDT=ETHUSDT_PERP.A" \
  --days 180 --interval 1hour --chunk-days 30 \
  --publish-mode replace
```

Optional, only if 180d is clean — extend to 365 days (staging-only first, then replace):

```bash
python scripts/fetch_coinalyze_chunked_v102.py \
  --coinalyze-symbols "BTCUSDT=BTCUSDT_PERP.A,ETHUSDT=ETHUSDT_PERP.A" \
  --days 365 --interval 1hour --chunk-days 30 --publish-mode staging-only
# inspect, then:
python scripts/fetch_coinalyze_chunked_v102.py \
  --coinalyze-symbols "BTCUSDT=BTCUSDT_PERP.A,ETHUSDT=ETHUSDT_PERP.A" \
  --days 365 --interval 1hour --chunk-days 30 --publish-mode replace
```

## 4c. UNDERCOVERAGE (V10.2.2) — do NOT publish incomplete history

If the report says `report_status: UNDERCOVERAGE` (coverage by days OR by rows
< 80% of the requested window), the fetcher **blocks all publishing** even in
`--publish-mode replace`: `publish_allowed=false`, `do_not_replace_raw=true`,
`old_data_touched=false`. This is the safe outcome — **do NOT override it.**

What to do on UNDERCOVERAGE:
- **Do NOT publish.** Never replace good raw with incomplete staging.
- Try a shorter window the API can actually serve: `--days 90`.
- Try finer chunks: `--chunk-days 15`.
- If `possible_api_range_cap_or_ignored_from_to: true` or chunk markers show
  empty/overlapping old chunks, the Coinalyze plan likely **caps historical
  range** — investigate the API/plan limits before retrying bigger windows.
- Inspect `external_data/staging/.../chunk_*.done.json` markers: `endpoint_rows`,
  `min/max_timestamp`, `empty_endpoints`, `chunk_status` reveal which chunks
  came back empty.
- **Do NOT advance to `external-long-history-validation-v102` as a 180d
  validation** — it would be a false "long history".
- ~84d of extra recent data may be used as `intermediate_extra_history` for
  exploratory diagnostics, but **NOT** as a 180d validation and **NOT** to
  replace the good restored raw.

```bash
# Smaller window the API can serve, staging-only (never touches raw):
python scripts/fetch_coinalyze_chunked_v102.py \
  --coinalyze-symbols "BTCUSDT=BTCUSDT_PERP.A,ETHUSDT=ETHUSDT_PERP.A" \
  --days 90 --interval 1hour --chunk-days 15 --publish-mode staging-only
```

The future backtester stub refuses to run on incomplete data:
`python -m app.research_lab strategy-replay-backtest-v103` returns
`NEED_LONG_HISTORY` (<180d) or `UNDERCOVERAGE_BLOCK` (latest fetch undercovered).

## 5. Re-ingest (validation + clean output; no DB writes)

```bash
python -m app.research_lab external-edge-ingest-v101 --dataset perp_market_state  --input-dir external_data/raw/perp_market_state
python -m app.research_lab external-edge-ingest-v101 --dataset perp_liquidations  --input-dir external_data/raw/perp_liquidations
```

## 6. Health + missing-OI audit

```bash
python -m app.research_lab external-data-health-v101
python -m app.research_lab external-missing-oi-audit-v102 --hours 8760
```

Read the audit: if `status` is `MISSING_OI_HIGH` or `MISSING_OI_CLUSTERED`, the
OI-based buckets stay BLOCKED until provider cross-check / refetch.

## 7. Diagnostics + stability + consolidated validation

```bash
python -m app.research_lab external-funding-oi-diagnostics-v101 --hours 8760
python -m app.research_lab external-funding-oi-stability-v101  --hours 8760
python -m app.research_lab external-long-history-validation-v102 --hours 8760
```

## 8. Final health + decision

```bash
curl -s localhost:<port>/health    # expect ok
# Read external-long-history-validation-v102:
#   history_status, stability_green, missing_oi_audit.status, next_research_decision
```

Interpret `next_research_decision.suggested_next_code_prompt_type`:
- `EXTEND_HISTORY_BTC_ETH` → history still too short; download more.
- `STRATEGY_BACKTEST_DESIGN` → a non-OI bucket is STABILITY_GREEN with acceptable
  missing-OI; design a READ-ONLY backtest (no promotion).
- `FIX_MISSING_OI_OR_PROVIDER_CROSSCHECK` → strong candidate depends on OI but
  missing-OI is material; fix/cross-check before judging.
- `REJECT_OR_PIVOT` → candidates fail OOS; reject funding/OI for BTC/ETH or pivot.
- `LIMITED_ALT_EXPANSION` → BTC/ETH validated on stronger history; consider a
  gated, research-only alt expansion (max 3-5 alts).

## 9. Reminders (repeat — they matter)

- Do **NOT** upload CSV/JSON to Git.
- Do **NOT** touch `.env`.
- Do **NOT** enable paper filter.
- Do **NOT** enable live. `can_send_real_orders` stays `false`.
- Ceiling for any candidate remains `SHADOW_RESEARCH_ONLY_FUTURE`.

**FINAL_RECOMMENDATION: NO LIVE.**
