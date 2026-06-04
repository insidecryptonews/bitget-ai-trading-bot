# ResearchOps V8.1 — Event Foundation (research-only)

Capa **event-driven** que se monta sobre V7.5/V8/V9 sin tocar nada operativo.
Reemplaza progresivamente el score genérico por **candidatos por eventos
únicos e idempotentes**.

## Familias

- `crowding_oi_funding` — funding/OI/liquidation crowding + ruptura de estructura.
- `post_listing_high_fdv` — dump post-listing con alta FDV / float bajo.
- `token_unlock` — short en token unlock, con control de conflicto de fuentes.
- `macro_scheduled_context` — solo contexto, **nunca accionable**.

## Módulos

- `app/events/event_store.py` — almacenamiento namespaced JSONL idempotente.
  Colecciones: `event_raw`, `event_canonical`, `event_candidates`,
  `event_sources`, `event_registry_runs`. Path por defecto:
  `training_exports/events_v8_1/`.
- `app/events/catalyst_layer.py` — generadores de `event_id` deterministas
  + canonicalización + detección de conflicto de fuentes.
- `app/events/listing_tracker.py` — fetch de listings recientes vía wrapper
  DB. `NEED_DATA` si la fuente no está cableada.
- `app/events/unlock_watchlist.py` — cross-check entre fuentes (Tokenomist
  vs TokenUnlocks por convención). Conflicto si `unlock_date` discrepa o
  `size_pct_circ` divergen > 10%.
- `app/events/perp_availability_checker.py` — confirma perp Bitget. Sin
  llamadas privadas. Si no hay perp → candidato no actionable.
- `app/events/shortability_score.py` — score 0–1 con spread + depth + volumen
  + signo de funding. `NEED_DATA` si faltan inputs críticos.
- `app/events/event_candidate_registry.py` — orquestador. Promoción máxima
  `ACTIONABLE_LABEL_ONLY` (solo etiqueta de research).
- `app/events/research_pack_event_v1.py` — pack ChatGPT minimalista.

## `event_id` estable

Patrones documentados (deterministas, idempotentes):

```
crowding:BTCUSDT:2026-06-04T01:00:00Z:funding_oi_break
listing:XYZUSDT:bitget:launch_day_3
unlock:LAB:2026-08-15:tokenomist
macro:cpi_us:2026-06-12T12:00:00Z:context
```

Cada `(family, symbol, timestamp_floored, trigger/venue/source)` produce
exactamente un `event_id`. Re-ingerir no duplica.

## Schema EventCandidate

22 campos + 4 invariantes de seguridad (`research_only=True`, `paper_filter_enabled=False`,
`can_send_real_orders=False`, `final_recommendation="NO LIVE"`).

## Estados

- `DETECTED` — registrado, sin enriquecer.
- `NEED_DATA` — falta campo crítico.
- `NEEDS_REVIEW` — fuentes en conflicto.
- `NOT_ACTIONABLE_NO_PERP` — sin perp en Bitget.
- `LOW_SHORTABILITY` — score < 0.30.
- `ACTIONABLE_LABEL_ONLY` — promoción máxima (etiqueta, no trade).
- `REJECTED` — descartado.
- `CONTEXT_ONLY` — macro, nunca accionable.

## CLI

```
event-catalyst-status
listing-tracker-audit
unlock-watchlist-audit
perp-availability-audit
shortability-score-audit
event-candidate-registry-status
research-pack-event-v1
```

## Qué NO añade

- No live, no paper filter, no órdenes reales.
- No endpoints privados nuevos.
- No `set_leverage` / `set_margin_mode`.
- No CCXT / LangGraph / dependencias externas.
- No tocar tablas existentes. Sólo crea archivos JSONL bajo
  `training_exports/events_v8_1/`.

## V8.2 / V9 (siguiente, no incluido)

- Bitget public API real para tickers/depth/funding (wrapper).
- Binance/Bybit public para cross-venue listings.
- Tokenomist/TokenUnlocks/CoinGecko enrichment.
- Integración con `validation_gates_v9` (gates duros sobre candidatos event-driven).
- Backtest de families con OHLCV histórico.

`FINAL_RECOMMENDATION: NO LIVE.`
