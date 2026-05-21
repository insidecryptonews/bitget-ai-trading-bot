# OHLCV Backfill Plan — Phase 7.2 (Historical Path)

Status: proposal, awaiting user approval before implementation.
Mode: research-only. No live, no paper filter, no exchange writes.

## 0. Goal

Unblock the `RealStrategyBacktester` and Issue #1 requirements (TP/SL optimizer with bar_path, multi-block walk-forward) by **persisting historical OHLCV from Bitget into a local `ohlcv_candles` table**, then running the existing backtester over real history instead of waiting for live collection.

Why this and not Codex's "wait and collect live":
- Live collection rate: ~271 signal_observations in 3 weeks → reaching 1000+ labels per setup category takes many months.
- Bitget's public historical-candles endpoint can deliver years of OHLCV in minutes.
- The `ohlcv_replay_loader.py` and `real_strategy_backtester.py` are already wired and only blocked on the missing table.

## 1. Success criteria

The backfill phase is successful when:
1. `ohlcv-replay-loader-audit --hours 72` returns `status: OK` (not `NEED_DATA`).
2. `real-strategy-backtest --hours 8760` (1 year) returns `status: OK` with `trades > 1000` aggregate.
3. No `DUPLICATE_CANDLES` or `TOO_MANY_GAPS` warnings on BTCUSDT/ETHUSDT/SOLUSDT for the canonical 5m series.
4. All 408 existing tests still pass.
5. No exchange writes (zero private endpoints touched, zero orders, zero margin changes).

The **decision phase** that follows is successful when we have an honest answer to: **does this strategy show edge on out-of-sample historical data?** Yes/No, not "more research needed."

## 2. Schema — `ohlcv_candles`

```sql
CREATE TABLE IF NOT EXISTS ohlcv_candles (
    symbol       TEXT NOT NULL,
    timeframe    TEXT NOT NULL,                 -- '5m', '15m', '1h', etc.
    timestamp    TEXT NOT NULL,                 -- ISO 8601 UTC, candle open time
    open         REAL NOT NULL,
    high         REAL NOT NULL,
    low          REAL NOT NULL,
    close        REAL NOT NULL,
    volume       REAL NOT NULL,
    quote_volume REAL DEFAULT 0,
    source       TEXT NOT NULL DEFAULT 'bitget_rest_v2',
    ingested_at  TEXT NOT NULL,
    PRIMARY KEY (symbol, timeframe, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_ohlcv_candles_symbol_tf_ts
    ON ohlcv_candles(symbol, timeframe, timestamp);
CREATE INDEX IF NOT EXISTS idx_ohlcv_candles_ingested
    ON ohlcv_candles(ingested_at);
```

Column names match exactly what `app/ohlcv_replay_loader.py` already expects in `CANONICAL_ALIASES` — no changes needed to the loader.

Composite PRIMARY KEY → `INSERT OR IGNORE` for idempotent upsert. Re-running the backfill is safe and only writes missing rows.

## 3. Scope

- **Symbols (10)**: BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT, DOGEUSDT, BNBUSDT, LINKUSDT, AVAXUSDT, ADAUSDT, DOTUSDT (matches current `config.symbols`).
- **Timeframes (3)**: 5m, 15m, 1h.
- **Horizon**: 365 days back from today as the canonical run. 730 days as a stretch goal if Bitget serves it.
- **Endpoint**: `/api/v2/mix/market/history-candles` (Bitget v2 public, no auth needed). Existing `get_candles` uses the *non-historical* endpoint capped to recent data — we will add a `get_history_candles` method.

## 4. API call math

| Timeframe | Candles/day | Candles/year | Calls/year per symbol (1000/call) |
|-----------|-------------|--------------|------------------------------------|
| 5m  | 288 | 105 120 | 106 |
| 15m | 96  | 35 040  | 36  |
| 1h  | 24  | 8 760   | 9   |

Per symbol, 1 year, all 3 timeframes: ~151 calls.
10 symbols × 1 year × 3 timeframes: ~1 510 calls.
At rate limit 8 calls/sec (existing `SimpleRateLimiter` in `bitget_client.py:71`): ~190 seconds nominal, ~5–10 minutes realistic with backoff.

Storage: ~1.05M 5m rows × 10 symbols = 10.5M rows for 5m alone. SQLite handles this comfortably (~500MB–1GB). PostgreSQL on Railway also fine.

## 5. Idempotency strategy

```python
INSERT OR IGNORE INTO ohlcv_candles (...) VALUES (...)
```

For each (symbol, timeframe), the backfill:
1. Reads the *latest* `timestamp` already in the table.
2. Resumes from `max(timestamp) + 1 candle` going forward, or from `since` going backward if behind.
3. Continues until either reaching the target horizon or hitting a 7-day stretch with zero rows returned (treated as end of historical availability for that symbol).

Re-running the script is safe at any time, even mid-failure.

## 6. Validation strategy

After every backfill batch, per (symbol, timeframe):
- **Continuity**: gap detection via `_count_time_gaps` already in `ohlcv_replay_loader.py` — log per-symbol gap count.
- **Duplicates**: composite PK prevents writes; we also verify post-hoc that `(symbol, timeframe, timestamp)` distinct count equals row count.
- **Range sanity**: `high >= max(open, close) AND low <= min(open, close)`. Reject any row failing this.
- **Volume sanity**: volume >= 0. Reject negatives.

Outputs a JSON summary per run with rows ingested, rows skipped (already present), rows rejected (sanity fail), gaps detected, time elapsed.

## 7. Backtest plan (after backfill complete)

Once `ohlcv_candles` is populated:

1. Run `RealStrategyBacktester.run()` per symbol over the full 365-day window.
2. **Aggregate metrics** (existing summary in `real_strategy_backtester.py:55`): net_PF, net_EV, win_rate, max_drawdown, TP%/SL%/TIME%.
3. **Walk-forward partition**: split each symbol's history into 12 monthly windows. Report per-window net_PF and net_EV stability (variance across windows is the actual signal of overfitting risk, not just walk_forward_decision flags).
4. **Setup-level slicing**: stratify trades by `(symbol, side, regime, score_bucket)` and identify which buckets, if any, have sample ≥200 and net_PF ≥1.2 across at least 3 of 4 quarters. These would be Issue #1's candidate setups.
5. **Cost sensitivity**: re-run with slippage 2x and fees 1.5x — if edge collapses with realistic adverse costs, it's not edge.

## 8. Decision criteria — honest

After step 7 we have one of three outcomes:

**A. Edge confirmed** (multi-window stability + net_PF ≥1.2 + sample sufficient + cost-robust):
→ Proceed to Phase 7.3 (research hypotheses) and 7.4 (candidate validation) with confidence the base motor works.

**B. Edge marginal/inconsistent** (positive aggregate, but inconsistent across windows or collapses under cost stress):
→ Don't move to paper filter. Examine where edge comes from. Possibly tighten filters (score threshold, regime gating) but accept smaller sample.

**C. No edge or negative** (net_PF ≤1.0 aggregate, or stable losses across windows):
→ **Stop adding fases.** This is the most important outcome to face honestly. Options to discuss with user:
  - Change timeframe (5m is fee-toxic for retail; 1h/4h have better fee/move ratio).
  - Change strategy class (current is classical-indicator; may need microstructure, funding, or event-driven signals).
  - Change market (FTMO/forex futures are a different beast from crypto perpetuals).
  - Drop the project. This is on the table and we shouldn't pretend it's not.

I will not soften outcome C. The user has explicitly said they want fast progress and to not waste time, which means brutal honesty if the data says no.

## 9. Risks and limits

- **Survivorship bias**: Bitget may have delisted symbols not represented. Mitigation: only use symbols still active today (per `_load_instruments`).
- **Look-ahead via `add_indicators`**: `RealStrategyBacktester` already iterates `data.iloc[:index+1]` and enters on `i+1.open` ([real_strategy_backtester.py:126](app/real_strategy_backtester.py#L126)). The lookahead protection is real for indicators computed within the slice, but verify on first dry-run that no indicator in `add_indicators` peeks beyond `index`.
- **Funding rate**: Bitget historical candles don't include funding. The current cost model uses a config-default funding when missing ([real_strategy_backtester.py:202](app/real_strategy_backtester.py#L202)). Acceptable for v1; flag as known approximation.
- **5m vs reality**: Bitget's candle close timestamps and the bot's live runtime may have small differences (millisecond drift, candle delay). Backfilled candles are post-close finalized; live is current candle that may revise. This means **backtest is slightly optimistic vs live**. Document this gap explicitly.
- **Survivorship of indicators**: any indicator computed using more than 60 bars of history will discard early candles. Per-symbol, we lose the first ~60 candles regardless.

## 10. What this plan does NOT change

- No new strategy logic.
- No new exit rules.
- No paper filter activation.
- No leverage/margin/sizing config changes.
- No live orders, no private endpoints touched.
- No tests deleted; only adds tests.
- No lab modules removed (separate cleanup decision).
- No data already in DB modified or migrated.

## 11. Order of work

1. Add `ohlcv_candles` schema to `database.py` + tests.
2. Add `get_history_candles` to `bitget_client.py` + test against public endpoint.
3. Add `app/ohlcv_backfill.py` CLI + tests.
4. Dry-run: BTCUSDT 5m, 72h. Verify loader audit returns OK.
5. Stage 1 backfill: BTC/ETH/SOL, 5m only, 90 days. Verify backtester runs end-to-end.
6. Stage 2 backfill: full scope (10 symbols × 3 timeframes × 365 days).
7. Run full backtest and walk-forward analysis.
8. Deliver honest decision report.

Steps 1–4 are safe and fast (couple hours of work). Steps 5–7 take a few hours of mostly waiting. Step 8 is a writeup.

## 12. Estimated calendar

- Day 1: steps 1–5.
- Day 2: steps 6–7.
- Day 3: step 8 + decision conversation with user.

If we're disciplined, **3 days to have a real answer**. Compare with weeks-to-months of incremental collection on the VPS.

---

Awaiting user approval to proceed with steps 1–4.
