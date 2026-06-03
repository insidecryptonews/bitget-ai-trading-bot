# ResearchOps V7.5 — Data Reliability + Funding/WalkForward/Liquidation Safety

**Base commit:** `a6a53ea` (ResearchOps V7). **Investigación únicamente.**
`final_recommendation: NO LIVE`.

## Qué cambió

### Bloque 1 — Duplicate Guard Hook (modo audit por defecto)
- `app/duplicate_guard_hook.py` — singleton por proceso. Mantiene memoria de
  huellas vistas y reporta `seen_count`, `new_count`, `would_block_count`,
  `actual_block_count`, `reasons_top`. Cero writes a la DB desde el hook.
- `app/feature_logger.py` — `record_observation` consulta el hook antes de
  insertar. En modo `audit` solo cuenta; en modo `enforce` devuelve `0` y no
  inserta. Si el hook falla, la inserción procede (fail-open por seguridad).
- `app/main.py` — `configure_global_hook(...)` se invoca al arrancar.
- Flags en `app/config.py`:
  - `enable_duplicate_guard_hook: bool = False`
  - `duplicate_guard_hook_mode: str = "audit"`

### Bloque 2 — AST No-Lookahead Guard
- `tests/test_no_lookahead_guard.py` — escanea módulos de señal estrictos y
  busca `iloc[expr + N]`, `iloc[i:i+N]`, `shift(-N)`. Respeta:
  - Comentario inline `# allow_future_access: razón`.
  - Decorador `@allow_future_access` (identidad, definible por el caller).
  - Whitelist por path (labelers, autopsy, backtesters acotados).
- Meta-tests sintéticos verifican que detecta los 3 patrones y respeta
  whitelist + decorador.

### Bloque 3 — Funding Cost Model
- `app/funding_cost_model.py` — aplica funding por trade solo si cruza
  timestamp UTC (`00:00`, `08:00`, `16:00`). Signos correctos: LONG paga si
  rate > 0; SHORT paga si rate < 0. Si la tabla `funding_rates` no existe,
  devuelve `funding_data_status=NEED_DATA` y `net_adjustment_pct=0`. Nunca
  inventa magnitudes.
- Flag en `app/config.py`: `enable_funding_cost_model: bool = False`.

### Bloque 4 — Walk-Forward V2 + Bootstrap CI
- `app/walk_forward_runner_v2.py` — rolling windows con `train_days`,
  `test_days`, `step_days` configurables. Bootstrap con seed fija (1729)
  sobre net_EV y net_PF por fold para IC 95%. Detecta `single_fold_dominance`
  y `regime_instability`. Decisión máxima `WF2_PROMISING_LABEL_ONLY`.

### Bloque 5 — Liquidation Model Bitget
- `app/bitget_liquidation_tiers.py` — tabla local con tiers reales para top-10
  USDT-M. `LAST_VERIFIED_DATE` documentado. Fallback conservador para símbolos
  no listados.
- `app/liquidation_model_bitget.py` — `evaluate_liquidation` aplica
  `1/L - mmr + mmr_amount/notional`. Clasifica riesgo
  (LOW/MEDIUM/HIGH/CRITICAL) y emite `blocks_scale_up=True` en HIGH/CRITICAL
  o si la tabla está stale (> 60 días).
- Flag en `app/config.py`: `enable_liquidation_model_bitget: bool = False`.

### Bloque 6 — Dashboard / CLI / Pack V7.5
- Panel **V7.5 Data Reliability** en el Overview con 4 cards
  (Duplicate Guard Hook, Funding, Liquidation, WF V2).
- Endpoints (read-only, autenticados):
  - `GET /api/research/duplicate-guard-hook-status`
  - `GET /api/research/funding-cost-model`
  - `GET /api/research/liquidation-model-bitget`
  - `GET /api/research/walk-forward-v2` (default `allow_heavy=false`)
  - `GET /api/research-pack-v7-5`
- CLI: `duplicate-guard-hook-status`, `funding-cost-model`,
  `liquidation-model-bitget`, `walk-forward-v2`, `research-pack-v7-5`.
- `app/research_pack_v7_5.py` extiende V7 con las nuevas secciones.

## Qué NO se activó

- Live, paper filter, candidate shadow monitor: intactos.
- Leverage / margin / sizing / slots: intactos.
- `.env` / VPS / DB destructiva: intactos.
- Todos los flags V7.5 arrancan `False`. El hook está deshabilitado por defecto.
- OHLCV auto-refresh sigue `False`. El dashboard solo sugiere CLI.

## Cómo activar el guard sin susto

1. Pegar `ENABLE_DUPLICATE_GUARD_HOOK=true` y `DUPLICATE_GUARD_HOOK_MODE=audit`
   en el `.env` del VPS y reiniciar el worker.
2. Esperar 24h. Pulsar Refresh V7.5 en el dashboard y leer `would_block_count`.
3. Si la magnitud cuadra con el `duplicate_rate` del root cause y
   `actual_block_count=0`, pasar a `DUPLICATE_GUARD_HOOK_MODE=enforce`.
4. Vigilar 24h adicionales. Si los `signal_observations` legítimos no bajan
   más de lo esperado, mantener enforce.

## Honest read sobre el edge

V7.5 es **infraestructura**, no edge. Hasta que VPS pase a `enforce` y se
poble la tabla `funding_rates` (decisión humana fuera de scope V7.5), los
gates V6/V7 seguirán bloqueando promoción. `DOT` continúa REJECT.

## Final recommendation

**NO LIVE.**
