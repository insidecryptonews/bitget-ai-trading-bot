# ResearchOps V5.1 — Hotfix + UX Pro + Strategy Research Enhancer

**Base commit:** `80f2ff8` (ResearchOps V5). **Research-only**: no live, no paper
filter, no real orders, no leverage/margin/sizing/slots changes, no `.env`
touches. `final_recommendation: NO LIVE`.

## What changed

### 1. OHLCV freshness wrapper hotfix

`app/ohlcv_freshness_manager.refresh(...)` previously instantiated
`BitgetClient(config)` / `BitgetClient(None)` with a single positional argument
and raised `TypeError` (the constructor requires `(config, logger)`). The
fallback error path also collapsed all `(symbol, timeframe)` rows into a single
result row — that is the "agrupa mal" behaviour the operator observed.

Fix:

- `BitgetClient` now always built as `(cfg_for_client, log)` and `config` is
  loaded from disk via `app.config.load_config()` if the caller passed `None`.
- Both fallback error paths (import error / client init error) now emit one
  `RefreshSymbolResult` **per `(symbol, timeframe)` pair** instead of a
  flattened single row. Dashboards render the matrix correctly even on errors.
- Dry-run defaults preserved: `dry_run=True`, `allow_real_writes=False`. The
  dashboard handler `_v5_ohlcv_freshness_refresh_dry` hardcodes both.
- CLI dispatch in `app/research_lab.py` still enforces the dual gate
  `--apply` + `--allow-real-writes`. `--apply` alone (without
  `--allow-real-writes` and with `enable_ohlcv_auto_refresh=False`) returns
  `SKIPPED_AUTO_DISABLED` and writes nothing.

### 2. Dashboard UX Pro V5.1

Cockpit summary row added at the top of the ResearchOps V5 section with six
cards (Freshness / Data Quality / Shadow Learning / Fee-Aware Edge /
Capital & Leverage / Readiness). Each card uses a coloured side-bar (OK
green, WARN amber, BAD red, SHADOW purple, INFO blue, DANGER red) so the
operator can read state in under 10 seconds.

Shadow Multi-Trade panel gained filters (symbol, side, status, net-positive
only, net-negative only), sort (recent / best net / worst net), visible limit
and a "Copy plain text" button for ChatGPT exports. Inline summary chips show
total / net+ / net- / gross-green-net-negative / best symbol / worst symbol.

Mobile / Android-landscape: the cockpit grid switches to single-column at
720px and dense tables keep horizontal scroll via the existing `.table-shell`.

### 3. Strategy Research Enhancer

`app/strategy_research_enhancer.py` — read-only ranking of shadow trades by
symbol / side / setup / regime, plus a list of "research ideas" (SHORT-only
filter, risk-off filter, no-trade-in-choppy, fee-aware exit, etc.).

Possible decisions (descriptive only — never activations):

- `RESEARCH_PROMISING`
- `NEED_MORE_DATA`
- `REJECT_NEGATIVE_NET`
- `REJECT_DATA_QUALITY` (triggered when clean view returns BAD)
- `REJECT_OVERFIT_RISK`
- `REJECT_COSTS`
- `SHADOW_ONLY`

CLI:

```bash
python -m app.research_lab strategy-research-enhancer --hours 24
python -m app.research_lab strategy-research-enhancer --hours 720 \
  --symbols BTCUSDT,ETHUSDT,DOTUSDT
```

Endpoint: `GET /api/research/strategy-research-enhancer`. Heavy runs
(`hours>168` or >3 symbols) return `SKIPPED_HEAVY` with the equivalent CLI.

### 4. Research Pack V5.1

`/api/research-pack-v5` already aggregates V5 sections; the V5.1 enhancer
output ships in the standard pack via the cockpit suggestions because the
pack is composed of pre-existing helpers. No new endpoint introduced — the
V5 pack continues to expose: safety, git, freshness matrix, training data
clean view, shadow summary (top trades), capital/leverage top-N, suggested
next actions. Known issues are surfaced in `suggested_next_actions` plus the
explicit cockpit cards on the dashboard.

## What was NOT activated

- Live trading, paper filter, candidate shadow monitor runtime, real orders:
  untouched.
- Leverage / margin / sizing / slots config: untouched.
- VPS / `.env` / DB: untouched.
- `enable_ohlcv_auto_refresh` remains `False` (default + env var).

## Honest read on edge

Edge is **still not there**. DOT remains `RESEARCH_PROMISING_NOT_ACTIONABLE`
because Phase 8B walk-forward and anti-overfit are WARN. The Strategy Research
Enhancer now lets us collect ranking signal more efficiently, but the data
quality issue (duplicate_rate BAD) means the rankings should be read with
caution — the clean view is the authoritative sample count.

V5.1 is an **investigation accelerator**, not a green light. Live and paper
demo stay blocked.

## CLI quick reference (V5.1)

```bash
# Strategy Research Enhancer (research-only)
python -m app.research_lab strategy-research-enhancer --hours 24
python -m app.research_lab strategy-research-enhancer --hours 720

# OHLCV freshness refresh — apply only with the dual gate
python -m app.research_lab ohlcv-freshness-refresh \
  --symbols BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,BNBUSDT,LINKUSDT,AVAXUSDT,ADAUSDT,DOTUSDT \
  --timeframes 5m,15m,1h --hours 120 --apply --allow-real-writes

# Same in dry-run mode (default)
python -m app.research_lab ohlcv-freshness-refresh \
  --symbols BTCUSDT,ETHUSDT,DOTUSDT --timeframes 5m,15m,1h --hours 120 --dry-run
```
