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

## 1. Backup first (R2 / Data Vault)

```bash
# Back up current state BEFORE any change (use the project's existing vault tooling).
python -m app.research_lab data-vault-status
python -m app.research_lab post-migration-backup    # or your standard vault backup
```

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

## 4. Download BTC/ETH — 180 days first (explicit symbol override)

```bash
python scripts/fetch_coinalyze_v101.py \
  --coinalyze-symbols "BTCUSDT=BTCUSDT_PERP.A,ETHUSDT=ETHUSDT_PERP.A" \
  --days 180 --interval 1hour
```

Optional, only if 180d looks clean:

```bash
python scripts/fetch_coinalyze_v101.py \
  --coinalyze-symbols "BTCUSDT=BTCUSDT_PERP.A,ETHUSDT=ETHUSDT_PERP.A" \
  --days 365 --interval 1hour
```

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
