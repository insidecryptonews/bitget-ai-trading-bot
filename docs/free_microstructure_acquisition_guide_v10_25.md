# Free Microstructure Acquisition Guide (V10.25) — for Adrián

Goal: get usable microstructure data for FREE (no Tardis/CoinGlass/Kaiko, no API
keys, no account) and validate it with the V10.24.3 adapter. RESEARCH ONLY. NO LIVE.

## What V10.25 is (and is NOT) — read this first
- It is a **plan + a partial FORWARD collector**, NOT a full historical pipeline.
- There is **no end-to-end CLI** that downloads and unzips Binance ZIP dumps yet —
  the module exposes only in-process row converters + a bounded REST forward fetch.
  You download the historical dumps manually; conversion of those dump rows is via
  the converters (no one-command "give me 365 days" exists in V10.25).
- REST `aggTrades` returns only **recent** trades; it does **not** replace 180/365d dumps.
- `orderbook_l2.csv` produced from `bookTicker` is **L1** (it carries
  `depth_level=L1_BOOKTICKER`), **not** real historical L2 depth.
- Free historical **liquidations do not exist** (forward websocket only).
- This does **not** promise an instant `MICROSTRUCTURE_RESEARCH_READY` sample.

## Honest verdict first
- **trades + open interest + funding**: FREE and historical right now (Binance).
- **orderbook**: FREE but only **L1** (best bid/ask) and effectively **forward**
  (poll from now); full historical L2 depth is NOT free.
- **liquidations**: NO free historical dump — only a forward websocket stream.
- => A fully `MICROSTRUCTURE_RESEARCH_READY` free sample needs ~30 days of
  forward orderbook-L1 + liquidations collection on top of the historical
  trades/OI/funding. Free path = **PARTIAL_FREE**, not instant-READY.

## Step by step

### 1. See the plan (no network, no risk)
```
python -m app.research_lab free-microstructure-sources-plan-v1025
```

### 2. First free source = Binance USD-M (no account, no key)
- **Trades (historical)**: download daily aggTrades dumps from
  `https://data.binance.vision/data/futures/um/daily/aggTrades/BTCUSDT/`
  (one ZIP per day; unzip to CSV). Verified reachable (~4.7 MB/day).
- **Open interest (historical)**: `.../daily/metrics/BTCUSDT/` dumps.
- **Funding (historical)**: no download needed — the collector pulls it via REST.

### 3. Convert / collect into the canonical format
- Forward (live, free, REST GET, bounded) — dry-run first:
```
python -m app.research_lab free-microstructure-collector-dry-run-v1025 --symbols BTCUSDT
```
  Then actually collect into staging:
```
python -m app.research_lab free-microstructure-forward-collect-v1025 --symbols BTCUSDT --apply
```
  This writes canonical CSVs to
  `external_data/staging/free_microstructure_v10_25/<run_id>/`:
  `trades.csv`, `orderbook_l2.csv` (L1), `open_interest.csv`, `funding.csv`.

### 4. Where to put files
- Everything stays under `external_data/staging/...` (gitignored). Never commit data.
- To validate, point the V10.24.3 validator at a folder that has the canonical CSVs
  (you can copy the collector's run dir, or stage Binance-dump conversions there).

### 5. Validate with V10.24.3
```
python -m app.research_lab microstructure-sample-validate-v1024 --sample-dir <your_folder>
```

### 6. What result to expect (and what to do)
- `NO_SAMPLE`: folder empty / no recognized CSVs -> add the canonical CSVs.
- `INVALID_SAMPLE`: a file is corrupt / crossed book / bad timestamps / secret-like
  file present -> read `critical_errors` + `critical_errors_by_file`, fix that file.
- `NEEDS_AGGRESSOR_SIDE`: trades lack a buy/sell aggressor column -> use aggTrades
  (the collector maps `is_buyer_maker` correctly).
- `NEEDS_ORDERBOOK`: no L1 sizes -> collect bookTicker over time (orderbook_l2.csv
  must have bid_size_1/ask_size_1).
- `NEEDS_OI` / `NEEDS_LIQUIDATIONS`: add open_interest / liquidations.
- `NEEDS_MORE_HISTORY`: too few rows / too sparse -> collect ≥30 days, dense
  (trades ≥1000, orderbook ≥100, oi ≥24, liquidations ≥20).
- `MICROSTRUCTURE_RESEARCH_READY`: only then do we design microstructure labs.

### 7. The liquidations gap (the honest blocker)
Free historical liquidations do not exist. Options:
- forward-collect Binance `!forceOrder` websocket for ≥30 days (future small module), or
- accept PARTIAL and research only the components you have (trades/OI/funding/L1),
  which is still useful but will NOT reach full READY.

## What NOT to do
- No API keys, no account login, no `.env`, no Bitget private, no orders, no live,
  no paper, no paid providers. The collector is GET-only and dry-run by default.

research_only: true
shadow_only: true
paper_ready: false
live_ready: false
can_send_real_orders: false
final_recommendation: NO LIVE
