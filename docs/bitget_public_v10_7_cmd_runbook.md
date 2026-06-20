# ResearchOps V10.7 — Bitget Public Free Data Collector (CMD Runbook)

> **RESEARCH ONLY. NO LIVE. NO PAPER FILTER.**
> Uses ONLY public HTTPS **GET** endpoints on `api.bitget.com` — no API key, no
> `.env`, no auth headers, no private/trading endpoints, no VPS. The fetcher is
> **dry-run by default**; `--apply` writes ONLY under a local staging dir (never
> raw, never DB). Nothing here can set `paper_ready`/`live_ready`.

Windows **CMD** (`cmd.exe`). Run from the repo root:

```cmd
cd C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot
```

The allowlisted public endpoints (the ONLY URLs reachable):
- `GET /api/v2/mix/market/candles`
- `GET /api/v2/mix/market/history-fund-rate`
- `GET /api/v2/mix/market/open-interest`

---

## 1. Plan (coverage matrix, honest limits)

```cmd
python -m app.research_lab bitget-public-plan-v107
```
Shows the official queryable lookback note per timeframe (`official_queryable_limit_note`),
the free starter recommendation (BTCUSDT/ETHUSDT, 1H+4H, 30 days), and the hard
limitations (no long OI history, no public historical liquidations, low TFs ~1
month, **no live readiness**).

## 2. Dry-run (default — NO network, NO files)

```cmd
python -m app.research_lab bitget-public-fetch-v107 --symbols BTCUSDT,ETHUSDT --timeframes 1H,4H --days 30 --data-types candles,funding,oi_snapshot
```
`dry_run: true`, `staging_dir:` empty, `planned_fetches` listed. Nothing is
written and no request is made.

## 3. Apply (REAL public GET → local staging only)

```cmd
python -m app.research_lab bitget-public-fetch-v107 --symbols BTCUSDT,ETHUSDT --timeframes 1H,4H --days 30 --data-types candles,funding,oi_snapshot --apply
```
Writes under:
```
external_data\staging\bitget_public_v10_7\<run_id>\
  candles\BTCUSDT\1H.csv
  candles\ETHUSDT\4H.csv
  funding\BTCUSDT\funding.csv
  oi_snapshot\BTCUSDT\oi_snapshot.csv
  run_report.json
```
Conservative rate-limit (~3 req/s), short timeout, bounded retries; errors are
accumulated in `run_report.json`, never fatal. `paper_ready`/`live_ready` stay
false.

## 4. Audit the staging

```cmd
python -m app.research_lab bitget-public-staging-audit-v107 --staging-dir external_data\staging\bitget_public_v10_7\<run_id>
```
Validates files exist, CSV parses, rows>0, OHLCV sanity (high≥low, high≥open/close,
low≤open/close, volume≥0), funding finite, OI non-negative, duplicates, gaps,
coverage, sha256, no unsafe/percent-encoded paths. `audit_status` ∈
{`STAGING_OK`, `STAGING_HAS_WARNINGS`, `STAGING_BLOCKED`}.

## 5. Convert to a V10.6-validatable sample, then validate

```cmd
python -m app.research_lab bitget-public-to-sample-v107 --staging-dir external_data\staging\bitget_public_v10_7\<run_id>
```
Writes validator-friendly files (`BTCUSDT_1h_ohlcv.csv`, `BTCUSDT_funding.csv`)
under `...\<run_id>\_sample_v106\` (OI snapshot skipped — single-point, not a
series). Then run the **real** V10.6 validator on that sample dir:

```cmd
python -m app.research_lab provider-sample-validate-v106 --sample-dir external_data\staging\bitget_public_v10_7\<run_id>\_sample_v106 --expected-days 30 --provider bitget_official
python -m app.research_lab provider-sample-manifest-v106 --sample-dir external_data\staging\bitget_public_v10_7\<run_id>\_sample_v106 --expected-days 30 --provider bitget_official
```
The manifest stays **non-promotable** (`gate_promote_allowed: false`,
`explicit_human_authorization: false`) — there is NO readiness bypass. Backtester
readiness still re-validates through `evaluate_manifest_v105` (V10.6.1):

```cmd
:: only meaningful once a manifest JSON is written with --apply
python -m app.research_lab backtester-readiness-v106 --manifest <manifest.json>
```

## 6. Collector status

```cmd
python -m app.research_lab bitget-public-collector-status-v107
```
Implemented endpoints, what is free now, what is still missing (long OI history,
historical liquidations, 180/365d on low timeframes), and the latest staging dir.

## 7. What to paste to ChatGPT
Paste the full `report_json:` line from steps **2/3** (the run report) and the
audit output from step **4**, plus the `provider-sample-validate-v106` output
from step **5**.

## 8. What NOT to do
- No `.env`, no API key, no private endpoints, no auth headers.
- No `--apply` on raw dirs; staging only.
- No live, no paper filter, no VPS, no DB writes, no raw writes.

---

## Honest readiness
- **Free now:** candles (capped per timeframe), historical funding (paged), OI
  snapshot (point-in-time, accumulates only going forward).
- **Still missing for 180/365d:** long OI history and complete historical
  liquidations are NOT publicly available; low timeframes are capped ~1 month.
  4H reaches ~240d and 1H ~83d as a queryable note (verify per symbol).
- **Backtester-ready?** Not automatically. A manifest from this data still needs
  explicit human authorization and must pass `evaluate_manifest_v105`.

**FINAL_RECOMMENDATION: NO LIVE.**
