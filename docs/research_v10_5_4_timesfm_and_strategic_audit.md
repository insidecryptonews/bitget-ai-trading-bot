# ResearchOps V10.5.4 — TimesFM Analysis + Strategic Audit (research-only)

**Status:** analysis & design ONLY · nothing implemented · NO LIVE
This document is the Part B (TimesFM) and Part C (strategic audit) deliverable.
No code, dependency, or runtime change is proposed here — only a future-phase
proposal and an honest assessment.

---

## PART B — TimesFM / predictive AI

### 1. What TimesFM actually is
TimesFM (Google Research) is a **decoder-only foundation model for time-series
forecasting** — a single pre-trained model that produces zero-shot point (and
quantile) forecasts for an arbitrary univariate series without per-series
training. It is the time-series analogue of an LLM: patch the history into
tokens, predict future patches. v2.x (~200M params) improved long-context and
quantile heads over v1.

### 2. License
**Apache-2.0** for the code; weights on Hugging Face under a permissive
research/commercial license. No legal blocker for internal research use.
(Verify the exact weights card before any download — treat as
`NEEDS_MANUAL_VERIFICATION` until a human confirms the checkpoint terms.)

### 3. Dependencies (the real cost)
Heavy: PyTorch or JAX, the `timesfm` package, Hugging Face download of the
checkpoint (hundreds of MB). This is a **hard dependency-lock-in concern** —
exactly what our rules forbid in runtime. It must never enter `app/` runtime
or the VPS image.

### 4. Compute
CPU inference works for short batches but is slow; comfortable on a single
GPU. For an **offline lab** over 10 symbols this is fine on a workstation.
Not viable inside the live worker loop on the current VPS.

### 5. Local CPU/GPU
Yes, runs locally on CPU (slow) or GPU (fast). Offline only.

### 6. Inputs
A univariate numeric series + context length + horizon + frequency hint.
Optional covariates in newer versions. No exchange keys, no network at
inference once weights are cached.

### 7. Horizons
Arbitrary horizon (trained up to long horizons; quality degrades further out).
For us: short intraday horizons (next N bars of 5m/15m/1h).

### 8. Output
Point forecast **and quantiles** (e.g. p10/p50/p90) → usable as an
**uncertainty/interval** estimate, which is the genuinely interesting part for
risk gating (not the point forecast).

### 9. Crypto 24/7
Works — no calendar/seasonality assumption that breaks on 24/7 markets. But it
was trained largely on non-crypto data; **crypto microstructure is adversarial
and reflexive**, so zero-shot skill on price is doubtful. Volatility/volume are
more plausible.

### 10. Where it might help / not help
- **Plausible:** volatility / expected-range forecast, volume/liquidity
  forecast, funding drift, OI drift — slower, more autocorrelated series.
- **Doubtful:** directional price/return forecast (this is where false
  confidence kills accounts).
- **Not its job:** spread/slippage proxy, regime detection (better with
  purpose-built features).

### 11. Risks (the honest list)
Leakage (must forecast strictly from past patches, score on future bars only);
overfit/selection across many series and horizons; lookahead in feature
construction; **a good forecast that is not tradable** (RMSE ↓ but net EV ≤ 0
after fees/slippage/funding); benchmark illusion (beating naive on MAE ≠ money).

### 12. Baselines it MUST beat before it earns a place
last-value (naive), moving average, EMA, ATR (for range), naive seasonal,
a simple GARCH/EWMA volatility baseline, and a no-skill/random control. If
TimesFM does not beat **ATR for range** and **EWMA for vol** out-of-sample, it
adds nothing.

### Conclusion
**Worth a future, isolated research phase — NOT now, NOT in runtime.** Its only
defensible first use is as an **uncertainty/range gate** (a NO_TRADE filter:
"if expected range < round-trip costs, do not trade"), never as a directional
signal. It must clear strict gates before it influences anything, and even then
research-only.

### Proposed future phase (do NOT implement in V10.5.x)
**`TimesFM Shadow Forecast Lab` (V10.6+ candidate)** — offline, read-only:
- inputs: the same validated 180/365d dataset (post manifest gates);
- mandatory provenance: `dataset_hash`, `model_version`, `horizon`,
  `context_len`, `params` on every record;
- experiments: vol/range, volume/liquidity, funding, OI, quantile p10/p50/p90;
- evaluation: walk-forward, OOS, **pinball loss for quantiles**, MAE/RMSE vs
  every baseline above, and — decisively — a **net-EV simulation** of any gate
  derived from it;
- gates to "earn a place": beat baselines OOS AND improve net EV in a
  cost-aware sim AND survive walk-forward stability;
- never a direct signal; at most a NO_TRADE/risk-context gate, research-only,
  no paper filter, no live, no dashboard polling, no dependency in `app/`.

---

## PART C — Strategic audit (brutal, evidence-based)

### Estado honesto del proyecto
The **safety and research scaffolding is genuinely strong**: NO LIVE is
enforced at multiple layers, the dashboard is read-only and DB-free in
polling, the manifest validator is now fail-closed and semantically strict,
and the edge pipeline refuses to declare PASS without a real, revalidated,
cost-positive candidate. **What is missing is the thing that actually makes
money: real long-history data and a demonstrated, cost-survived edge.** None
of the hardening we have shipped is edge — it is the honest machinery that
will stop us fooling ourselves once data arrives.

There is currently **no actionable candidate, no demonstrated net EV, and no
verified 180/365d dataset.** That is not a failure; it is the truth the gates
are designed to report.

### Readiness % estimado (defendido con evidencia)
- **Research infrastructure: ~85%** — labs, gates, contracts, tests (~1760
  passing), audits are mature. Remaining 15%: real backtester wiring + the
  TimesFM/edge-hunter labs.
- **Data foundation: ~20%** — Coinalyze gives ~63 clean days, OI clustered;
  no verified 180/365d provider. This is the binding constraint.
- **Backtesting real: ~25%** — contract + replay scaffolding exist; cannot run
  meaningfully without the data above.
- **Edge discovery: ~10%** — observation/learning infra alive, but zero
  net-EV-positive buckets found; every promising-looking bucket dies on costs.
- **Paper readiness: ~5%** — blocked by everything above.
- **Live readiness: 0%** — and correctly so.

### Bloqueadores (en orden)
1. **No verified long-history data** (Tardis.dev/CoinGlass unverified; no sample
   validated). Everything downstream waits on this.
2. **No real bar-by-bar backtester run** on validated data.
3. **No net-EV-positive candidate** under realistic costs.
4. **No walk-forward / OOS / anti-overfit evidence.**

### Camino a rentabilidad (gated, sin atajos)
1. Human-verify Tardis.dev → obtain BTCUSDT+ETHUSDT 7–30d sample → validate
   **offline** against the now-strict V10.5.x manifest.
2. Acquire 180/365d (manifest + structured inventory + checksums + human auth).
3. Run the real replay backtester (no lookahead, worst-case same-bar, real
   cost model x1/x2/x3).
4. Edge Hunter over validated buckets → require net EV>0, net PF≥1.30, ≥150
   samples, TIME<80%, cost x2 survival.
5. Walk-forward (monthly + rolling) + OOS + anti-overfit + stability matrix.
6. Shadow → (human-gated) paper → micro-live only after audit.

### Métricas mínimas antes de paper filter
net EV>0 after x2 costs, net PF≥1.30, ≥150 samples per bucket, TIME<80%,
positive OOS, walk-forward stability, no single-trade/week dominance, OI
audited if used. **None are met today.**

### Métricas mínimas antes de micro-live
Everything above **plus** a clean shadow/paper period with consistent net EV,
controlled drawdown, reconciled costs, and explicit human authorization.
Micro-live = smallest size, isolated margin, hard daily-loss circuit breaker.

### 5 cosas que más acercan a rentabilidad
1. Verify + acquire the 180/365d dataset (the unlock).
2. Real replay backtester on that data.
3. Cost model honesty (fees + slippage + funding) baked into every metric.
4. Edge Hunter with the frozen anti-overfit gates.
5. TIME-death / exit-policy research — the current bottleneck on every bucket.

### 5 pérdidas de tiempo ahora
1. Implementing TimesFM now.
2. New strategies/indicators before there is data to validate them.
3. Tuning thresholds on 63 days of data (overfitting noise).
4. Any dashboard ornamentation.
5. Anything touching live/paper/leverage/copy-trading.

### Señales prometedoras vs basura
- **Worth research (not actionable):** SHORT in RISK_OFF/TREND_DOWN on
  ETH/DOGE/XRP showed raw asymmetry — but only as hypotheses to test on real
  data, never promoted.
- **Basura / false hope:** any bucket with gross_PF high + net_EV≤0; RANGE with
  TIME≈100%; LONG (persistently weak — keep blocked); anything from
  market_probe; gross_PF=999 no-SL artifacts.

### Qué NO hacer
No live, no paper filter, no leverage, no copy trading, no promoting on 63d, no
TimesFM in runtime, no paid download before sample validation.

### Dónde encaja TimesFM
Future offline `Shadow Forecast Lab` as a **range/uncertainty NO_TRADE gate**,
only after it beats ATR/EWMA OOS and improves net EV in a cost-aware sim. Never
a directional signal.

### Próximos 7 días
Human: contact Tardis.dev (contact pack), request sample. Code: nothing new in
runtime — keep the gates green; optionally draft the Edge Hunter V10.6 contract
on paper. Validate any sample offline against the manifest validator.

### Próximos 30 días
If a verified sample arrives: acquire 180/365d → run replay backtester →
Edge Hunter → walk-forward. If not: do not fabricate progress; the honest state
remains NEED_VERIFIED_PROVIDER.

### Próximo paso recomendado
Codex re-audit of `V10.5.4`, then the human Tardis.dev verification. The bot is
**not close to making money** (data foundation ~20%, edge ~10%); the realistic
gap is large (call it ~70%+ of the way to a defensible paper-trading decision),
and the gates now make that honest instead of hideable.

FINAL_RECOMMENDATION: **NO LIVE**
