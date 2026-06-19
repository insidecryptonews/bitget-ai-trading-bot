# ResearchOps V10.6 — Local-First Live-Readiness Foundation (CMD Runbook)

> **RESEARCH ONLY. NO LIVE. NO PAPER FILTER.**
> Every command below is offline, read-only, and dependency-light (Python
> stdlib). Nothing here touches `.env`, the VPS, the database, real money,
> or real orders. Nothing can flip `paper_ready` or `live_ready` to `true`.

This runbook is written for **Windows CMD** (`cmd.exe`). PowerShell and Git
Bash work too, but the examples avoid bash-only syntax. Run everything from the
repo root:

```cmd
cd C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot
```

All commands are invoked through the research-lab module:

```cmd
python -m app.research_lab <command> [flags]
```

---

## The evidence chain (run in order)

The V10.6 modules form a chain that turns "we have no verified data" into
"here is exactly what is missing and what the next human action is":

```
provider-matrix  ->  provider-sample-validate  ->  provider-sample-manifest
        -> backtester-readiness -> replay-backtester-contract
        -> edge / walk-forward / meta / forecast readiness
        -> paper-readiness -> live-readiness
```

---

## A. Provider strategy & data-source matrix

Compare candidate providers (Tardis.dev, CoinGlass, Coinalyze, Bitget public,
Binance/OKX proxy). Makes **no** network calls — vendor claims are recorded as
`NEEDS_MANUAL_VERIFICATION`, never as confirmed facts.

```cmd
python -m app.research_lab provider-matrix-v106
```

Expect: `preferred_sample_candidate: tardis_dev`, `any_verified: false`,
`no_network_calls: true`.

---

## B. Provider sample validator (offline, read-only)

Point it at a **local** directory of provider sample files (`.csv`, `.jsonl`,
`.ndjson`). It computes real SHA-256, coverage/gaps/duplicates, and per-type
content sanity (OHLCV / open interest / funding / liquidations). It never
ingests, never writes to raw, never touches the DB.

Filename convention (tokens separated by `_` or `-`):
`<SYMBOL>USDT_<TIMEFRAME>_<DATATYPE>.csv`, e.g. `BTCUSDT_1d_ohlcv.csv`,
`BTCUSDT_5m_oi.csv`, `BTCUSDT_1h_funding.csv`.

```cmd
python -m app.research_lab provider-sample-validate-v106 --sample-dir "C:\path\to\sample" --expected-days 180 --provider tardis_dev
```

Key output fields:

- `dataset_hash` — real SHA-256 over the file digests.
- `data_classification` — `SAMPLE_ONLY` / `INTERMEDIATE_RESEARCH_ONLY` /
  `LONG_HISTORY_RESEARCH_READY`.
- `sample_ready` — `true` only if no blockers **and** all required data types
  (ohlcv, open_interest, funding, liquidations) are present.
- `blockers` — e.g. `oi_missing_clustered`, `duplicate_timestamps:N`,
  `ohlcv_invalid_rows:N`, `invalid_file_path:...` (percent-encoded / unsafe).
- `paper_ready: false`, `live_ready: false` — always.

---

## C. Content-aware manifest builder

Build a research-only manifest from a validated sample. Without `--apply` it is
a dry run (`written_path: NONE`). With `--apply` it writes JSON to
`external_data/reports/v10_6_manifests/` — **never** to raw, **never** to the
DB.

```cmd
:: dry run
python -m app.research_lab provider-sample-manifest-v106 --sample-dir "C:\path\to\sample" --expected-days 180 --provider tardis_dev

:: write the manifest file to the reports dir
python -m app.research_lab provider-sample-manifest-v106 --sample-dir "C:\path\to\sample" --expected-days 180 --provider tardis_dev --apply
```

The manifest is intentionally **not promotable** by a machine:
`import_status: STAGED`, `explicit_human_authorization: false`,
`gate_promote_allowed: false`. Promotion requires an explicit human step and
the V10.5.6 manifest gates to pass.

---

## D. Backtester readiness

Given a manifest JSON, decide whether it can feed the research replay. States:
`NEED_DATA`, `NEED_LONG_HISTORY` (`< 180` clean days), `NEED_CONTENT_VALIDATION`
(manifest not promotable / OI not audited), `READY_FOR_REPLAY_RESEARCH`.

```cmd
python -m app.research_lab backtester-readiness-v106 --manifest "external_data\reports\v10_6_manifests\manifest_tardis_dev_<hash>.json"
```

`READY_FOR_REPLAY_RESEARCH` authorizes **research replay only** — never paper,
never live.

---

## E. Replay / backtester research contract

Print the frozen no-lookahead replay contract and confirm there is no validated
dataset to run yet (`live_run_status: NEED_VALIDATED_DATA`).

```cmd
python -m app.research_lab replay-backtester-contract-v106
```

No-lookahead guarantees (enforced in code, see `simulate_position`):

- decisions use only bars up to the signal bar;
- entry fills on the **next** bar's open (`latency_bars >= 1`);
- when TP and SL fall in the **same** bar, the **worst case (SL first)** is
  assumed — never the optimistic one;
- fees + slippage + spread + funding are always subtracted; results include a
  1x / 2x / 3x cost-stress.

---

## F-I. Research readiness gates (what is missing to find real edge)

```cmd
python -m app.research_lab edge-hunter-readiness-v106
python -m app.research_lab walk-forward-readiness-v106
python -m app.research_lab meta-model-readiness-v106
python -m app.research_lab forecast-lab-readiness-v106
```

- **edge-hunter** — gates on clean days, samples, net EV/PF, time-death, cost
  stress, OOS and walk-forward stability. Emits the candidate-incubator
  contract (no promotion on a single pretty PF; strong `candidate_id` required).
- **walk-forward** — rolling, chronological, no-leakage design + reject
  conditions (train-high/test-poor, too few samples, single-period edge, etc.).
- **meta-model** — readiness only; `ENABLE_META_MODEL: false`, no runtime
  filter, no `.env` change.
- **forecast-lab** — `FORECAST_LAB_FUTURE_OFFLINE_ONLY`, `implemented: false`.
  TimesFM and friends stay offline/shadow, must beat ATR/EWMA/naive baselines
  OOS and improve net EV before earning any place. No torch/jax/tensorflow/
  timesfm in runtime.

---

## J. Paper readiness gate

```cmd
python -m app.research_lab paper-readiness-v106
```

Always `PAPER_NOT_READY` in the current phase. Lists every prerequisite still
missing (clean history, content validation, backtester readiness, OOS,
walk-forward, edge candidates, positive net EV, paper policy still disabled).

---

## K. Live readiness audit (brutally conservative)

```cmd
python -m app.research_lab live-readiness-v106
```

Always `LIVE_NOT_READY`, `live_audit_ready: false`,
`can_send_real_orders: false`. `SAFE_PAPER_ONLY` is necessary but **not**
sufficient: sustained profitable paper, minimum days/trades, drawdown limits,
single-worker lock, kill switches, manual approval, micro-live risk rules,
exchange-key permission audit, rollback plan and monitoring are all required
before this could ever change — and only a human can make that call.

---

## L. Risk framework for future micro-live (contract only)

The future micro-live risk limits (daily/weekly loss, position risk, leverage
cap, open-position cap, loss-streak circuit breaker) and no-trade conditions are
documented as a **read-only contract** inside
`app/labs/readiness_gates_v10_6.py:risk_framework_contract`. No operative change
to `risk_manager.py` is made in this phase; activation requires explicit human
approval **and** a live-readiness pass.

---

## Verification (optional, local)

```cmd
python -m pytest tests\test_researchops_v10_6.py -q
python -m compileall app\labs\provider_registry_v10_6.py app\labs\provider_sample_validator_v10_6.py app\labs\real_replay_backtester_v10_6.py app\labs\readiness_gates_v10_6.py
```

---

## Hard guarantees (V10.6)

- No network calls, no downloads, no API keys, no `.env` access.
- No DB writes; manifests only ever land in a reports dir, never in raw.
- No order placement, no leverage/margin/sizing changes, no runtime mutation.
- `paper_ready` and `live_ready` are hard-wired `false` everywhere.
- **FINAL_RECOMMENDATION: NO LIVE.**
