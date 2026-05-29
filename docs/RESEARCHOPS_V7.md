# ResearchOps V7 — Data Pipeline Fix + Clean Strategy Library + Operator Loop

**Base commit:** `69f31db` (ResearchOps V6). **Investigación únicamente**:
sin live, sin paper filter, sin órdenes reales, sin cambios de
leverage / margin / sizing / slots, sin tocar `.env`.
`final_recommendation: NO LIVE`.

## Qué cambió

### Parte 1 — Data Pipeline Root Cause (`app/data_pipeline_root_cause.py`)

Auditoría read-only que clasifica los duplicados y explica por qué la calidad
de datos es BAD. Devuelve:

- `raw_sample_count`, `clean_sample_count`, `source_adjusted_clean_count`.
- `exact_duplicate_count`, `semantic_duplicate_count`,
  `dangerous_duplicate_count`, `benign_scan_repeat_count`.
- `market_probe_count` / `trade_signal_count` y sus equivalentes "clean".
- `orphan_labels`, `orphan_path_metrics`, `label_duplicates`.
- `duplicate_key_is_too_aggressive`, `biggest_problem`, `recommended_fix`,
  `can_use_for_strategy_eval`.
- `final_recommendation: NO LIVE`.

CLI: `python -m app.research_lab data-pipeline-root-cause --hours 720 \
  --symbols BTCUSDT,ETHUSDT,DOTUSDT --timeframes 5m,15m,1h`.

### Parte 2 — Duplicate Guard (`app/duplicate_guard.py`)

Fingerprint determinista para futuras observaciones. Incluye `source`,
`strategy_type`, `symbol`, `timeframe`, `side`, `market_regime`,
`score_bucket`, `reject_reason`, bucket de minuto y hash de features. Reglas:

- Mismo contexto completo → `EXACT_DUPLICATE`.
- Mismo setup, distinto minuto → `SEMANTIC_DUPLICATE`.
- Mismo setup y minuto → `BENIGN_SCAN_REPEAT`.
- `market_probe` nunca actionable; siempre marca `actionable=False` en el verdict.
- `deduplicate(observations)` opera en memoria sin tocar la DB.

### Parte 3 — OHLCV historical vs freshness

El módulo de freshness ya distingue `historical` y `freshness`. El nuevo
Operator Loop expone ambas verticales por separado y mantiene el comando
público (`ohlcv-freshness-refresh --apply --allow-real-writes`) como única
ruta que escribe — y solo si el operador lo invoca explícitamente.

### Parte 4 — Clean metrics consistency

El helper central `clean_research_metrics.get_clean_research_metrics()` sigue
siendo la fuente de verdad. V7 lo consume desde:

- `data_pipeline_root_cause` (para reconfirmar magnitudes).
- `clean_strategy_lab` (gating).
- `capital_scaling_simulator` (base EV/PF).
- `research_pack_v7` (sección dedicada).
- Dashboard Overview / cockpit (panels existentes).

### Parte 5 — Clean Strategy Lab (`app/clean_strategy_lab.py`)

Framework research-only con 11 familias:

A) SHORT trend continuation (RISK_OFF / TREND_DOWN, SHORT-only).
B) EMA200 structure breakout / breakdown.
C) EMA50 / EMA200 regime filter.
D) FVG imbalance pullback (bearish para SHORT).
E) Pullback continuation.
F) Breakout / breakdown confirmation.
G) Volatility compression → expansion.
H) Mean reversion RANGE-only.
I) BTC lead / alt lag.
J) Candle pattern confluence.
K) RSI / momentum filter.

Cada familia entrega: samples raw/clean/trade_signal/market_probe, TP%, SL%,
TIME%, gross/net EV, gross/net PF, MFE/MAE medio y mediano, bars to TP/SL,
fee impact, slippage stress, fold count, confidence y decisión. La decisión
máxima posible es `PAPER_CANDIDATE_LABEL_ONLY` (etiqueta de research; nunca
activa paper filter).

### Parte 6 — Exit / TP improvement

Se reutiliza el `fee_aware_exit_trainer` existente (V5.1) más el lab de
`net_profit_lock_lab` (V5). No se introducen nuevas variantes para evitar
overfit; los gates de V7 ya rechazan promociones con folds inestables.

### Parte 7 — Validation Gates

El Phase 9 validator (V6) sigue siendo el gate central. V7 añade los gates a
nivel de familia en el lab (`REJECT`, `NEED_MORE_DATA`, `WATCH_ONLY`,
`SHADOW_CANDIDATE_LABEL_ONLY`, `PAPER_CANDIDATE_LABEL_ONLY`). Las decisiones
positivas **nunca** activan paper filter ni órdenes.

### Parte 8 — Dashboard Operator Loop

Se añade una franja en el Overview con los 7 estados:
**SCAN → DETECT → VALIDATE → SIZE_SIM → MANAGE_SIM → SETTLE → LEARN**. Cada
estado muestra eyebrow, valor, sub y un CLI hint. Las cards ya existentes
del V6 cockpit siguen debajo.

### Parte 9 — Pack ChatGPT V7 (`app/research_pack_v7.py`)

Construye el pack V7 sobre el V5. Incluye `data_pipeline_root_cause`,
`clean_strategy_lab`, `capital_scaling_simulator`,
`suggested_next_cli_commands` y la sección V6 `clean_research_metrics`.
Sin secretos, sin DB dump, sin `.env`.

Endpoint: `GET /api/research-pack-v7`. CLI: `python -m app.research_lab
research-pack-v7 --hours 24`.

### Parte 10 — Capital Scaling Simulator (`app/capital_scaling_simulator.py`)

Matriz `capital × risk_pct × leverage × reinvestment_fraction`. Devuelve
`expected_net_pnl_usdt`, `max_drawdown_estimate_usdt`,
`risk_of_ruin_proxy`, `scale_up_eligible`, `next_capital_level` y
`do_not_scale_reason`. Bloquea automáticamente si `net_EV ≤ 0`, data quality
BAD o OHLCV stale. **Más capital amplifica beneficio y pérdida; nunca arregla
un EV negativo.**

## Qué NO se activó

- Live, paper filter, candidate shadow monitor, órdenes reales: intactos.
- Leverage / margin / sizing / slots: intactos.
- VPS / `.env` / DB: intactos.
- `enable_ohlcv_auto_refresh` sigue `False`.

## Cómo leer el informe

1. Empezar por el panel **Operator Loop**: si algún paso está en rojo, ese
   es el bloqueo prioritario.
2. Confirmar **Data Pipeline Root Cause**: si el `biggest_problem` es
   `duplicates_above_safe_threshold` o `market_probe_noise_inflates_raw_counts`,
   no promocionar ninguna familia.
3. Revisar **Clean Strategy Lab**: cualquier familia con
   `PAPER_CANDIDATE_LABEL_ONLY` requiere review humano antes de tocar nada.
4. **Capital Scaling**: nunca activar escalado si `scale_up_eligible=false`.

## Por qué `live` sigue bloqueado

- `LIVE_TRADING=False`, `DRY_RUN=True`, `PAPER_TRADING=True`,
  `ENABLE_PAPER_POLICY_FILTER=False`, `ENABLE_CANDIDATE_SHADOW_MONITOR=False`,
  `can_send_real_orders=False`, `enable_ohlcv_auto_refresh=False`.
- Las decisiones del lab son **etiquetas**; las dataclasses hardcodean
  `paper_filter_enabled=False` y `can_send_real_orders=False`.

## CLI quick reference V7

```bash
python -m app.research_lab data-pipeline-root-cause --hours 720 \
  --symbols BTCUSDT,ETHUSDT,DOTUSDT --timeframes 5m,15m,1h

python -m app.research_lab clean-strategy-lab --hours 24 \
  --symbols BTCUSDT,ETHUSDT,DOTUSDT --timeframe 5m

python -m app.research_lab capital-scaling-simulator --hours 720

python -m app.research_lab research-pack-v7 --hours 24
```
