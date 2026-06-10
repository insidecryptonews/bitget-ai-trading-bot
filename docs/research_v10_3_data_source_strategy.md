# ResearchOps V10.3 — Historical Data Source Strategy + Missing-OI Provider Audit

**Status: RESEARCH-ONLY. NO LIVE. NO paper. No paid download without explicit
authorization.** This document is the objective analysis behind the V10.3
provider registry (`app/labs/external_data_provider_registry_v10_3.py`) and the
`external-data-source-audit-v103` / `external-provider-readiness-v103` CLIs.

Fields we cannot verify here (live pricing, exact rate limits, exact Bitget-perp
history depth) are marked **`NEEDS_MANUAL_VERIFICATION`** — never invented.

---

## 1. Root cause: why 180d only returned ~84d

**Coinalyze caps intraday history.** Public docs/community: Coinalyze keeps only
~1500–2000 datapoints for intraday granularities (1m–12h) and **deletes old
intraday data daily**; daily granularity is retained long-term. At **1h**,
~1500–2000 points ≈ **60–80 days**, which matches the observed ~84d.

Answers to the audit questions:
1. **Is Coinalyze limited to ~84d?** Yes, at 1h (intraday retention), by plan/
   policy — not per symbol/exchange. Daily granularity goes back further.
2. **Does it respect `from/to`?** Effectively the data beyond retention simply
   does not exist for intraday, so deep `from` returns nothing — not a bug, a cap.
3. **Hidden API cap?** Yes: the intraday datapoint-retention cap (~1500–2000).
4. **Bitget-specific problem?** No — Bitget perps (`BTCUSDT_PERP.A`,
   `ETHUSDT_PERP.A`) are supported; the cap is the intraday retention policy.
5. **Alternative for 180–365d clean?** Yes — see matrix (Tardis.dev / CoinGlass).
6. **Which provider for OHLCV/OI/funding/liquidations?** Tardis.dev or CoinGlass
   (verify pricing/limits first); Coinalyze stays for intermediate only.
7. **Combine sources?** Yes — long history from Tardis/CoinGlass; cross-check
   funding from Bitget official API; Binance/OKX only as research proxy.
8. **Backtester minimum data?** 180d+ clean 1h with OHLCV + funding (+ OI/liq
   when missing-OI < 10%). Below that → `NEED_LONG_HISTORY`.
9. **Stay blocked while data insufficient?** Yes — `NEED_LONG_HISTORY`,
   `oi_bucket_policy=BLOCK_OI_BUCKETS` on unavailable/clustered/high missing OI,
   `paper_ready=false`, `live_ready=false`. Always `NO LIVE`.

## 2. Provider matrix (objective)

| Provider | Datasets | Bitget perp | Est. history (1h) | OI | Funding | Liq | 180d | 365d | Paid | Status |
|---|---|---|---|---|---|---|---|---|---|---|
| **Coinalyze** | OHLCV/OI/funding/liq/LSR | Yes | ~60–80d intraday (daily=long) | ✅ | ✅ | ✅ | ❌ | ❌ | freemium | **CURRENT** |
| **Tardis.dev** | tick OB/trades/OI/funding/liq/tickers | Yes (since 2024-11-08) | `NEEDS_MANUAL_VERIFICATION` (grows; >365d by 2026) | ✅ | ✅ | ✅ | ✅ | ✅ | paid_subscription | **CANDIDATE** |
| **CoinGlass** | OI/funding/liq OHLC, OHLCV | Yes | docs: since 2019 | ✅ | ✅ | ✅ | ✅ | ✅ | `NEEDS_MANUAL_VERIFICATION` | **CANDIDATE** |
| **CoinAPI** | OHLCV/funding/OI/trades/OB | `NEEDS_MANUAL_VERIFICATION` | `NEEDS_MANUAL_VERIFICATION` | ✅ | ✅ | `?` | `?` | `?` | `NEEDS_MANUAL_VERIFICATION` | NEEDS_MANUAL_VERIFICATION |
| **Kaiko** | OHLCV/funding/OI/derivs analytics/OB | `NEEDS_MANUAL_VERIFICATION` | `NEEDS_MANUAL_VERIFICATION` | ✅ | ✅ | `?` | ✅ | ✅ | enterprise | **ENTERPRISE_ONLY** |
| **CCData (CryptoCompare)** | OHLCV/funding/OI/trades | `NEEDS_MANUAL_VERIFICATION` | `NEEDS_MANUAL_VERIFICATION` | `?` | ✅ | `?` | `?` | `?` | `NEEDS_MANUAL_VERIFICATION` | NEEDS_MANUAL_VERIFICATION |
| **Bitget official API** | OHLCV/funding history/OI current/interest | Yes (source) | `NEEDS_MANUAL_VERIFICATION` (funding retention undocumented) | `?` | ✅ | ❌ | `?` | `?` | free | **CANDIDATE (funding cross-check)** |
| **Binance/OKX** | OHLCV/funding/OI/liq | **No (not Bitget)** | deep | ✅ | ✅ | `?` | ✅ | ✅ | free | **PROXY_ONLY** |

(✅ supported, ❌ not, `?`/`NEEDS_MANUAL_VERIFICATION` = verify before relying.)

### Pros / Cons (summary)
- **Coinalyze** — Pro: free, easy, Bitget perps, all derived metrics. Con:
  intraday history cap ~84d at 1h → cannot do 180/365d at 1h. Lock-in: low.
- **Tardis.dev** — Pro: tick-level Bitget futures since 2024-11-08, all datasets,
  resample to any TF, free monthly samples. Con: paid; tick volume is large;
  pricing = verify. Lock-in: medium. Best fit for 180/365d.
- **CoinGlass** — Pro: long OHLC history (2019), Bitget, OI/funding/liq. Con:
  pricing/limits = verify; OHLC (not tick). Lock-in: medium. Good fit 180/365d.
- **CoinAPI / CCData** — Pro: broad coverage. Con: Bitget perp derivatives depth
  = verify. 
- **Kaiko** — Pro: institutional quality. Con: enterprise cost; overkill now.
- **Bitget official** — Pro: source of truth for funding. Con: no liquidations
  history, OI history depth uncertain. Use to cross-check funding.
- **Binance/OKX** — Pro: deep free history. Con: NOT Bitget — proxy only.

## 3. Recommendation (objective)

For **180d+ clean 1h with OHLCV + OI + funding + liquidations on Bitget perps**,
the registry shortlist is **`tardis_dev`** (primary) and **`coinglass`**
(alternate). Before any paid download:
- **MANUAL VERIFICATION required**: current pricing, exact rate limits, exact
  Bitget-perp history depth, and OI/liquidation completeness for Bitget.
- Keep **Coinalyze** as CURRENT for intermediate research only.
- Cross-check funding against **Bitget official API** (source of truth).
- Use **Binance/OKX** only as a research proxy, never as a direct Bitget signal.

**Do NOT download paid data without explicit authorization.**

## 4. Data-readiness gating (enforced in code)
- `current_clean_days < 180` → `backtester_readiness=NEED_LONG_HISTORY`,
  `paper_ready=false`, `live_ready=false`.
- missing OI unavailable / `NEED_MORE_DATA` / clustered / > 10% →
  `oi_bucket_policy=BLOCK_OI_BUCKETS`.
- ~84d (or any `<180d`) → `data_classification=INTERMEDIATE_RESEARCH_ONLY`.
- 180–365d → `INITIAL_VALIDATION_READY`; ≥365d → `STRONGER_VALIDATION_READY`.
- `live_ready` is **always false** in this phase.

## 5. What to verify manually next (checklist)
- [ ] Tardis.dev: Bitget perp history depth for BTCUSDT/ETHUSDT, 1h resample
      feasibility, **price** of the needed window, rate limits.
- [ ] CoinGlass: Bitget OI/funding/liq history depth at 1h, **price/tier**,
      per-endpoint rate limits.
- [ ] CoinAPI / CCData: Bitget perp derivatives (OI/liq) availability + price.
- [ ] Bitget official API: funding history retention (how far back), OI history.

**FINAL_RECOMMENDATION: NO LIVE.**

Sources (verify; specs change):
- Coinalyze API docs — https://api.coinalyze.net/v1/doc/
- Tardis.dev Bitget Futures — https://docs.tardis.dev/historical-data-details/bitget-futures
- CoinGlass API — https://docs.coinglass.com/
- CoinAPI funding/OI — https://www.coinapi.io/blog/historical-crypto-funding-rates-api-coinapi
- Bitget historical funding — https://www.bitget.com/api-doc/contract/market/Get-History-Funding-Rate
- Kaiko — https://www.kaiko.com/
