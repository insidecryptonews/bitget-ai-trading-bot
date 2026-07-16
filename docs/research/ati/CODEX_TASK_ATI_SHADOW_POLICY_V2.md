# ATI Shadow Policy V2 Implementation Contract

## Implemented modules

- `app/labs/ati/contracts.py`: frozen policy/priors and safety validation.
- `features.py`: validated 1m ingestion, closed 15m/1h/4h aggregation, ATR,
  EMA, body strength, volatility, and causal higher-timeframe joins.
- `levels.py`: pivots visible only after right-side confirmation and repeated
  support/resistance clustering.
- `rules.py`: four deterministic setups and componentized ATI score.
- `replay.py`: next-open fills, structural risk, explicit costs, MFE/MAE,
  horizon outcomes, trailing grid, and `STOP_BEFORE_TP`.
- `metrics.py`: bootstrap uncertainty, concentration, drawdown, grouped metrics,
  and chronological 60/20/20 validation.
- `report.py`: manifest/SHA/raw validation and atomic CSV/JSON/JSONL exports.
- `shadow_engine.py`: restart-safe, boundary-frozen file ledger over externally
  refreshed snapshots.

## CLI

```powershell
python -m app.research_lab ati-shadow-replay-v2 --symbols BTCUSDT,ETHUSDT
python -m app.research_lab ati-shadow-forward-once-v2 --symbols BTCUSDT,ETHUSDT
python -m app.research_lab ati-shadow-status-v2
```

The commands are early-dispatched as public research commands. They do not load
config, `.env`, DB, exchange credentials, PaperTrader, or execution routing.

## Dashboard and health

Dashboard UI V3 reads only cached ATI reports from
`/api/research/ati-shadow`. `/health` exposes an `ati_shadow` component. Neither
endpoint runs replay or writes state. Heavy replay remains CLI-only.

## Not implemented or authorized

- No VPS changes or process restart without a verified R2/Data Vault backup.
- No live/paper execution.
- No paper filter or candidate auto-promotion.
- No runtime sizing, leverage, margin, or slot changes.
- No claim of edge from the 20 seed cases or the 90-day snapshot.

`FINAL_RECOMMENDATION: NO LIVE`
