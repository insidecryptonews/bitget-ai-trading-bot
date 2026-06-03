# ResearchOps V8/V9 — Research Foundation (research-only)

Capa de research que se añade sobre V7.5 sin activar nada operativo.
Todos los módulos son `research_only=True`, sin endpoints privados, sin writes
ocultos, `paper_filter_enabled=False`, `can_send_real_orders=False`,
`final_recommendation=NO LIVE`.

## Nuevos módulos

### `app/auto_data_enrichment.py`
Devuelve un snapshot por símbolo con funding, spread, mark/index basis, OI,
volatilidad realizada, correlación BTC/ETH y sesión. Cada fuente vuelve
`NEED_DATA` si la tabla/método del DB no existe; nunca inventa magnitudes.

### `app/exit_intelligence_lab.py`
Simula 12 políticas de exit (ATR TP/SL, profit lock, BE post-fees, trailing
ATR, dynamic hold, time stop smart, regime flip, BTC reversal, anti-late-entry,
anti-chop, partial TP, baseline). Reporta delta vs baseline + tasa de
TIME deaths. No ejecuta nada.

### `app/strategy_experiment_registry.py`
Registro JSON-backed thread-safe para experimentos de estrategia. Estados:
`REJECT`, `WATCH_ONLY`, `NEED_MORE_DATA`, `SHADOW_CANDIDATE`,
`PAPER_CANDIDATE_LABEL_ONLY`. No promociona nada automáticamente.

### `app/shadow_candidate_lifecycle.py`
Máquina de estados con 15 gates (data quality, freshness, duplicates,
lookahead, EV, PF, sample, stress cost/slippage/funding, walk-forward,
estabilidad por régimen/símbolo/sesión, no single-fold dominance). Promoción
máxima: `PAPER_CANDIDATE_LABEL_ONLY` (sólo etiqueta).

### `app/validation_gates_v9.py`
Gates duros: Walk-Forward, Bootstrap CI, Monte Carlo shuffle, PBO,
Deflated Sharpe, cost/slippage/funding stress, estabilidad y risk-of-ruin
proxy. Cada uno emite `NEED_MORE_DATA` si la muestra es insuficiente.

## Pack V7.5 + V8/V9

`build_research_pack_v7_5` ahora incluye secciones:
- `auto_data_enrichment`
- `exit_intelligence`
- `strategy_experiment_registry`
- `shadow_candidate_lifecycle`
- `validation_gates_v9`

`pack_version = "v7_5_v8v9_foundation"`.

## Qué NO añade

- No paper filter.
- No órdenes reales.
- No live.
- No endpoints privados.
- No tocar leverage/margin/sizing/slots.
- No DB destructiva.

## Próximo paso (no incluido en esta tanda)

V10 — micro-live pilot — solo si los gates V9 son sostenidos durante una
ventana mínima y el operador toma la decisión explícita. Ver
`docs/RESEARCHOPS_V10_FUTURE.md`.

`FINAL_RECOMMENDATION: NO LIVE.`
