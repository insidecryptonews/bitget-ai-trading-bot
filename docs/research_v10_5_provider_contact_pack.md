# ResearchOps V10.5 — Provider Contact Pack

**Status:** manual verification material · NO purchases without sample validation · NO LIVE
**Targets:** Tardis.dev (primary), CoinGlass (fallback), Bitget official (cross-check)
**Rule:** never paste API keys in any conversation; never pay before a sample
passes offline schema validation; record every answer in the provider
scorecard (`provider-verification-v105`).

## Required scope (same for every provider)

- Exchange: **Bitget**, USDT perpetuals only.
- Symbols (10): BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT, DOGEUSDT, BNBUSDT,
  LINKUSDT, AVAXUSDT, ADAUSDT, DOTUSDT.
- History: **180 days minimum, 365 days preferred**, per symbol.
- Data types: OHLCV, open interest, funding rates, liquidations,
  mark/index price (if available); trades/orderbook optional.
- Timeframes: 5m/15m/1h minimum (1m if available; 4h/1d optional).

## The 20 questions (ask ALL of them)

1. Do you have historical data for **Bitget USDT perpetuals** for these 10 symbols?
2. Exactly how much history exists **per symbol**? 180d? 365d? Since listing?
3. Which timeframes is OHLCV available in?
4. Is **historical open interest** available (granularity + depth)?
5. Are **historical funding rates** available (every interval, no gaps)?
6. Are **historical liquidations** available (per event or aggregated)?
7. Is mark price / index price available?
8. Bulk export (CSV/Parquet) or API-only?
9. What is the exact sample format (columns, file layout)?
10. Timezone and timestamp format (UTC? unix ms/s? ISO)?
11. What are the rate limits (requests/min, monthly caps) for backfill?
12. Price for the tier covering this scope (exact, not "from")?
13. License terms for the data?
14. Can we use it for **internal trading-bot research** (no redistribution)?
15. Are there redistribution/retention limitations after cancellation?
16. Can you provide a **sample of BTCUSDT and ETHUSDT covering 7-30 days**?
17. How do you normalise the symbol (e.g. BTCUSDT vs BTC-USDT-PERP)?
18. How do you distinguish perpetual vs spot vs delivery contracts?
19. Are there **known gaps** in Bitget coverage (dates, incidents)?
20. Do you provide checksums or a manifest for bulk files?

## Email template — English

> Subject: Bitget USDT-perp historical data — coverage and sample request
>
> Hi <provider> team,
>
> We run an internal research project on Bitget USDT perpetual futures and
> are evaluating data providers. Before any subscription we need to verify
> coverage. Could you confirm:
>
> 1) Historical coverage for Bitget USDT perps for: BTCUSDT, ETHUSDT,
> SOLUSDT, XRPUSDT, DOGEUSDT, BNBUSDT, LINKUSDT, AVAXUSDT, ADAUSDT, DOTUSDT —
> exact history depth per symbol (we need ≥180 days, ideally ≥365).
> 2) Availability and granularity of: OHLCV (5m/15m/1h, 1m if possible),
> open interest, funding rates, liquidations, mark/index price.
> 3) Delivery: bulk export vs API, file format, timezone/timestamp format,
> rate limits, checksums/manifest.
> 4) Pricing for this exact scope, license terms, and whether internal
> trading-research use is permitted (no redistribution).
> 5) A small sample of BTCUSDT and ETHUSDT (7–30 days) including OI, funding
> and liquidations, so we can validate the schema before purchasing.
>
> Known gaps in Bitget coverage, if any, would also be useful.
>
> Thanks!

## Plantilla de email — Español

> Asunto: Datos históricos de perpetuos USDT de Bitget — cobertura y muestra
>
> Hola equipo de <proveedor>,
>
> Tenemos un proyecto interno de research sobre futuros perpetuos USDT de
> Bitget y estamos evaluando proveedores de datos. Antes de contratar
> necesitamos verificar cobertura. ¿Podéis confirmar?:
>
> 1) Cobertura histórica de perpetuos USDT de Bitget para: BTCUSDT, ETHUSDT,
> SOLUSDT, XRPUSDT, DOGEUSDT, BNBUSDT, LINKUSDT, AVAXUSDT, ADAUSDT, DOTUSDT —
> profundidad exacta por símbolo (necesitamos ≥180 días, ideal ≥365).
> 2) Disponibilidad y granularidad de: OHLCV (5m/15m/1h, 1m si es posible),
> open interest, funding rates, liquidaciones, mark/index price.
> 3) Entrega: bulk export vs API, formato de archivo, timezone y formato de
> timestamp, rate limits, checksums/manifest.
> 4) Precio para este alcance exacto, términos de licencia y si se permite
> uso interno de research de trading (sin redistribución).
> 5) Una muestra pequeña de BTCUSDT y ETHUSDT (7–30 días) que incluya OI,
> funding y liquidaciones, para validar el esquema antes de comprar.
>
> Si hay gaps conocidos en la cobertura de Bitget, agradeceríamos saberlo.
>
> ¡Gracias!

## Bitget official (cross-check) — what to verify in their docs

Endpoints for historical candles/OI/funding and their **maximum lookback per
request and absolute depth**; whether liquidation history is exposed; rate
limits per IP/key (the public market-data endpoints need no key). Bitget is
the ground truth for cross-checking any vendor sample: same symbol, same
window, compare closes/funding values row by row.

## After the answers

1. Fill the scorecard (status → `SAMPLE_REQUIRED` → after a valid sample →
   `READY_FOR_HUMAN_AUTHORIZATION`).
2. Validate the sample OFFLINE against the V10.5 manifest contract
   (`docs/research_v10_5_data_manifest_contract.md`).
3. Only then a human decides authorization. `paid_download_authorized` is
   never set by code. FINAL: **NO LIVE**.
