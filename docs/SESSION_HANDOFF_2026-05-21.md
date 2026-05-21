# Session Handoff — Phase 7.2 — 2026-05-21

Documento exhaustivo para que cualquier sesión futura (ChatGPT, Claude, Codex, otra IA) pueda continuar el proyecto con contexto completo y exacto.

---

## 0. Identidad de la sesión

| Campo | Valor |
|---|---|
| Fecha | 2026-05-21 |
| Working directory | `C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot` |
| Plataforma | Windows 11 Pro, PowerShell 5.1 + Bash (via Git Bash) |
| Branch | `main` |
| Starting commit | `748fcb7` ("Fix dashboard reports and add OHLCV replay loader") |
| Final commit | `748fcb7` (NO se commitearon los cambios — usuario no lo pidió) |
| Cambios pendientes | Modificados: `app/bitget_client.py`, `app/database.py`. Nuevos: `app/multi_tf_backfill.py`, `app/ohlcv_backfill.py`, `docs/OHLCV_BACKFILL_PLAN.md`, `docs/PHASE_7_2_FINDINGS.md`, `docs/SESSION_HANDOFF_2026-05-21.md`, `tests/test_ohlcv_backfill.py`, `tests/test_ohlcv_candles_table.py` |
| Test count antes | 408 passing |
| Test count después | **428 passing** (+20, suite completa verde) |
| Líneas modificadas | ~100 en código existente + ~700 en código nuevo + ~1200 en docs |

---

## 1. Resumen ejecutivo (3 párrafos)

Esta sesión rompió con la planificación incremental de Codex (esperar a que el bot acumule datos viviendo en la VPS durante meses) y aplicó una alternativa **histórica masiva**: backfillear años de OHLCV directamente desde el endpoint público de Bitget en horas, no meses. Se creó la tabla `ohlcv_candles` (que el `OhlcvReplayLoader` ya esperaba pero nunca existió), se implementó `app/ohlcv_backfill.py` con resume bidireccional idempotente, y se descargó **1 año de velas 1h+4h y 90 días de 5m** para BTC/ETH/SOL.

Se corrió `RealStrategyBacktester` sobre todos esos datos. Hallazgo brutal: **5m no tiene edge net después de costes** (-0.14% a -0.19% por trade en BTC/ETH/SOL). 1h marginal. 4h marginal-real (BTC +0.19% honest, ETH +1.08%, SOL +1.28%) pero **regime-dependent** — los últimos 2 meses (abril-mayo 2026) son negativos en los 3 símbolos. Se confirmó **sin lookahead** en `add_indicators` y se midió la contaminación multi-timeframe del backtester (BTC infló ~50% del edge aparente; ETH/SOL casi nada).

Se importó el **training vault del VPS (137,163 labels reales en 7 días)** y se descubrió algo clave: **a muestra grande con filtros, hay setups con net positivo**. `score >= 85 AND regime in (TREND_DOWN, RISK_ON)` produce +0.12% net sobre 46,664 muestras. Setups específicos como LINKUSDT/AVAXUSDT/DOGEUSDT SHORT en TREND_DOWN llegan a +0.27% a +0.44% net con 1,500-7,000 muestras cada uno. La conclusión "no hay edge" del análisis 24h del dashboard era prematura a muestra pequeña. Sin embargo, **la muestra es 7 días en un periodo TREND_DOWN-heavy** — el edge podría ser regime tailwind, no edge estructural. La validación pendiente es correr esos setups específicos contra el histórico de 1 año.

---

## 2. Estado inicial del proyecto (lo que encontré)

### 2.1 Arquitectura y tamaño

- **154 módulos** en `app/` (`*.py`)
- **39,278 líneas totales** en `app/`
- **83 archivos de test**, 408 tests pasando
- **25 tablas en DB** (`bot_state.db` local, ~1MB; VPS Postgres con 4.9GB según dashboard)
- **app/database.py: 2,897 líneas**
- **app/main.py: 1,405 líneas**
- **app/research_lab.py: 2,238 líneas**
- 13 módulos `*_lab.py`
- 16 módulos `*_smoke_test.py` dentro de `app/` (deberían estar en `tests/` según el propio `duplicate_module_audit.py` del bot)

### 2.2 Auditoría de duplicados del propio bot (`python -m` ejecutado)

```
walk_forward: 2 archivos (walk_forward_validation.py + walk_forward_validator.py) — MERGE_CANDIDATE
walkforward:  2 archivos (walkforward.py + walkforward_validator.py)              — MERGE_CANDIDATE
exit_policy:  5 archivos (adaptive_exit_policy_lab + dynamic_exit_policy +
              exit_policy_backtest + exit_policy_v3 + exit_policy_v3_backtest)    — MERGE_CANDIDATE
score_calibration: 3 archivos                                                      — MERGE_CANDIDATE
backtester:   2 archivos (backtester.py=LEGACY, real_strategy_backtester.py=KEEP)
smoke_test:   16 archivos dentro de app/                                           — MIGRATION_CANDIDATE
```

Total: ~32 archivos marcados por el propio bot como candidatos a cleanup. **No los toqué** esta sesión (alta blast radius, no era el objetivo).

### 2.3 Datos locales antes de la sesión

```
signal_observations: 271 filas (timestamp range: 2026-05-02 → 2026-05-14)
signal_labels:       110 filas (53 +1, 57 -1 → 48% raw win rate, sin descontar costes)
trades:              6 (todos paper, mayo 2026)
events:              6
ohlcv_candles:       0 (la tabla NO EXISTÍA — el loader fallaba con NEED_DATA)
```

### 2.4 Estado del bot en VPS (de los reports compartidos por el usuario)

- `git_version: 748fcb7`
- `db_size_mb: 4908.46` (4.9 GB Postgres)
- `LIVE_TRADING=False`, `DRY_RUN=True`, `PAPER_TRADING=True`, `ENABLE_PAPER_POLICY_FILTER=False`
- Open positions: 0
- Short report status: `PARTIAL_REPORT` con 9 secciones en TIMEOUT (Operational Intelligence, Strategy Research Library, Data Pipeline Diagnosis, Label Quality V2, Bitget Cost Model Audit, Worker Health Audit, Edge Guard, Paper Policy Orchestrator, Time Death Autopsy)
- Training summary 6h: 7,479 observations, 766 labels, TP%=6.8 / SL%=5.6 / **TIME%=87.6**
- Candidate Ranking 24h: **NO_VALID_CANDIDATES**
- Score Calibration 24h: **biggest_problem=negative_net_EV**, `gross_edge_net_negative=true`
- Vault uploads: 20 backups en R2 Cloudflare (bucket `bitget-ai-training-vault`)

### 2.5 Plan de Codex previo a esta sesión

Según `CODEX_RESULT.md` (197 KB de notas locales del usuario) y los reports, Codex había completado:

- Fase 5: Candidate Incubator / Net EV / Cost models / integridad — research/shadow
- Fase 6: Pre-live hardening (net R:R real, fees ida/vuelta, funding, fresh balance, idempotencia, emergency failsafe, circuit breaker, isolated margin, config hardening)
- Fase 7: Operational Intelligence (con bug F7-001 que usaba MFE/MAE como retorno)
- Fase 7 FIX: corregidos F7-001..F7-007, walk-forward rolling, anti-overfit endurecido
- **Fase 7.1**: short report timeout corregido, dashboard flaky test arreglado, `app/ohlcv_replay_loader.py` añadido (pero esperando una tabla que no existía), `RealStrategyBacktester` conectado al loader (pero devolviendo `NEED_DATA` siempre por falta de tabla)
- **Fase 7.2 propuesta por Codex**: persistir candles incrementalmente conforme corre el bot, esperar meses, seguir acumulando

---

## 3. Mi crítica del plan de Codex y por qué cambié de dirección

ChatGPT/Codex te tenía en un treadmill: cada "fase" añadía más labs (`*_lab.py`) sin acercarse a la pregunta única importante: **¿tiene edge esta estrategia?**. El roadmap incremental "persistir candles conforme corre el bot" significaba esperar meses para acumular las ~1000 labels por setup que requiere validación estadística (a tu ritmo: 271 observations en 3 semanas).

Vi tres problemas estructurales que el roadmap de Codex no atacaba:

1. **Cuello de botella mal identificado.** No era "persistencia incremental"; era **falta de OHLCV histórico**. Bitget tiene endpoint público `/api/v2/mix/market/history-candles` que sirve años de velas sin auth. En horas se puede tener lo que el bot vivo acumularía en meses.

2. **La complejidad del código es en sí un riesgo.** 39k LOC en 154 módulos para un bot que aún no había hecho un trade real es signo de research theater. Cada fase añadía labs sin borrar nada.

3. **Validación circular.** Los labs operaban sobre `signal_observations` (señales que el motor ya había filtrado). Eso te dice si el bot está calibrado, **no si el universo de oportunidades reales tiene edge**.

**Mi propuesta**: detener la cascada de fases, hacer **backfill histórico masivo** desde Bitget, correr `RealStrategyBacktester` sobre años de datos reales, y **medir si la estrategia base tiene edge antes de añadir más capas**.

El usuario aprobó este pivote vía `AskUserQuestion`. Opciones que eligió: 1 (backfill), 2 (plan detallado), 4 (limpieza).

---

## 4. Trabajo realizado, paso a paso

### Paso 1: Limpieza (Task #1)

**Acciones**:
- Borrado de `ts` (archivo de 3,397 bytes en root del repo). Contenido: output capturado de `git diff --stat` (warnings CRLF + diff summary). Basura pura.
- `CODEX_RESULT.md` (197 KB / 5,254 líneas) **dejado en su sitio** — decisión del usuario si commitearlo. Es el canal de notas con ChatGPT.
- DBs en root (`bot_state.db`, `simple.db`, `simple_abs.db`) **dejadas** — `*.db` ya está en `.gitignore`, no se trackean.
- Labs muertos: **NO borrados**, solo identificados vía `python -m app.duplicate_module_audit`. Cleanup deferred.

### Paso 2: Plan técnico (Task #2)

**Entregable**: [docs/OHLCV_BACKFILL_PLAN.md](docs/OHLCV_BACKFILL_PLAN.md)

12 secciones cubriendo:
1. Goal
2. Success criteria (5 chequeos concretos)
3. Schema de `ohlcv_candles`
4. Scope (10 símbolos, 3 timeframes, 365 días)
5. API call math (150 calls/símbolo/año, total ~1500 calls)
6. Idempotency strategy (composite PK + INSERT OR IGNORE)
7. Validation strategy (range sanity, volume sanity, gap detection)
8. Backtest plan
9. Decision criteria (3 outcomes: edge / marginal / no edge)
10. Risks y limits (survivorship, lookahead, funding approximation, candle finalization)
11. What this plan does NOT change (cero código de trading)
12. Calendar estimate (3 días)

Aprobado por usuario. Procedí.

### Paso 3: Schema `ohlcv_candles` (Task #3)

**Archivo modificado**: [app/database.py](app/database.py)

**Esquema añadido** ([line 745](app/database.py#L745)):

```sql
CREATE TABLE IF NOT EXISTS ohlcv_candles (
    symbol       TEXT NOT NULL,
    timeframe    TEXT NOT NULL,
    timestamp    TEXT NOT NULL,
    open         REAL NOT NULL,
    high         REAL NOT NULL,
    low          REAL NOT NULL,
    close        REAL NOT NULL,
    volume       REAL NOT NULL,
    quote_volume REAL DEFAULT 0,
    source       TEXT NOT NULL DEFAULT 'bitget_rest_v2',
    ingested_at  TEXT NOT NULL,
    PRIMARY KEY (symbol, timeframe, timestamp)
)
```

**Índices añadidos** (en `_create_indexes`):
```sql
CREATE INDEX IF NOT EXISTS idx_ohlcv_candles_symbol_tf_ts ON ohlcv_candles(symbol, timeframe, timestamp);
CREATE INDEX IF NOT EXISTS idx_ohlcv_candles_ingested ON ohlcv_candles(ingested_at);
```

**Helpers añadidos en `app/database.py`** (~120 líneas nuevas):

| Método | Propósito |
|---|---|
| `insert_ohlcv_batch(rows)` | Bulk insert idempotente. Valida: timestamp/symbol/timeframe no vacíos, `high >= max(open, close)`, `low <= min(open, close)`, `volume >= 0`, todos los OHLC > 0. Devuelve `{inserted, skipped, rejected}`. Usa `INSERT OR IGNORE` en SQLite, `ON CONFLICT DO NOTHING` en Postgres. |
| `get_latest_ohlcv_timestamp(symbol, timeframe)` | Devuelve el MAX timestamp guardado, o `None`. |
| `count_ohlcv_rows(symbol=None, timeframe=None)` | Conteo opcional filtrado. |
| `fetch_ohlcv_range(symbol, timeframe, since_iso, until_iso, limit)` | Range query ordenado ascending. |

**Tests añadidos**: [tests/test_ohlcv_candles_table.py](tests/test_ohlcv_candles_table.py) — 9 tests, todos pasan.

**Compatibilidad**: 100% compatible con `OhlcvReplayLoader` existente. El loader usa nombres canónicos (`symbol`, `timeframe`, `timestamp`, `open`, `high`, `low`, `close`, `volume`) que coinciden exactamente.

### Paso 4: Backfill script y cliente Bitget extendido (Task #4)

**Archivo modificado**: [app/bitget_client.py:194](app/bitget_client.py#L194)

Añadido método `get_history_candles(symbol, granularity, start_ms, end_ms, limit)`:

```python
def get_history_candles(self, symbol, granularity, *, start_ms=None, end_ms=None, limit=200):
    params = {
        "symbol": symbol,
        "granularity": granularity,
        "limit": min(max(1, int(limit or 200)), 200),
        "productType": self.config.product_type,
    }
    if start_ms is not None:
        params["startTime"] = int(start_ms)
    if end_ms is not None:
        params["endTime"] = int(end_ms)
    return self.public_get("/api/v2/mix/market/history-candles", params=params) or []
```

Usa `public_get` → cero auth required, cero acceso a private endpoints.

**Archivo nuevo**: [app/ohlcv_backfill.py](app/ohlcv_backfill.py) — ~310 líneas.

**Estructura**:
- `BackfillStats` dataclass: tracking per (symbol, timeframe)
- `BackfillReport` dataclass: aggregate session report
- `_candle_to_row(symbol, timeframe, raw_bitget_array)`: parser de las arrays de 7 elementos que devuelve Bitget
- `_parse_iso_to_ms(iso_text)`: helper bidireccional
- `_resume_start_ms(...)`: forward resume
- `_get_oldest_ohlcv_ms(...)`: backward resume helper (añadido tras detectar bug)
- `backfill_pair(client, db, symbol, timeframe, days, dry_run, ...)`: **bidirectional** — calcula rangos a buscar:
  - Backward: `[target_start_ms, oldest_in_db_ms)` si oldest_in_db > target_start
  - Forward: `(latest_in_db_ms, target_end_ms]` si latest_in_db existe
  - Si DB vacía: rango único `[target_start, target_end]`
- `run_backfill(symbols, timeframes, days, dry_run)`: orquesta múltiples (símbolo, timeframe)
- CLI con argparse: `--symbols`, `--timeframes`, `--days` / `--hours` (exclusivos), `--dry-run`

**Configuración**:
- `DEFAULT_BATCH_LIMIT = 200` (límite del endpoint Bitget v2 para history-candles)
- `MAX_EMPTY_BATCHES = 5` (corta el rango si Bitget devuelve vacíos consecutivos)
- Rate limit heredado del `SimpleRateLimiter` del cliente (8 calls/s)

**Tests añadidos**: [tests/test_ohlcv_backfill.py](tests/test_ohlcv_backfill.py) — 9 tests inicial + 2 reescritos tras el fix bidireccional + 1 nuevo. Total 12 cubriendo:
- Parser válido / malformado
- Insert/dry-run paths
- Resume forward
- Resume backward
- Skip si DB ya cubre target window
- Backfill backward cuando target > oldest_in_db
- Empty-batches stop
- Invalid timeframe
- Render report contains NO_LIVE
- Granularity tables consistent

### Paso 5: Dry-run y bugs encontrados (Task #5)

**Comando ejecutado**:
```bash
python -m app.ohlcv_backfill --symbols BTCUSDT --timeframes 5m --hours 72 --dry-run
```

**Resultado**:
```
inserted=866 skipped=0 rejected=0 batches=6 duration=2.12s status=OK
first=2026-05-18T01:35:00+00:00 last=2026-05-21T01:30:00+00:00
```

72h × 12 candles/h = 864 esperadas. Got 866 (overlap en boundaries, OK). API funciona, formato de Bitget parseado correcto. **0 rejected**.

**Re-ejecutado SIN dry-run**: 864 inserted + 2 skipped (duplicados por re-fetch boundary), 0 rejected.

**Verificación end-to-end** con loader y backtester:
```
ohlcv_replay_loader_audit_text(hours=72):
  status: OK (era NEED_DATA antes), table: ohlcv_candles, rows: 863, gaps: 0, duplicates: 0

real_strategy_backtest_text(hours=72):
  status: OK (era NEED_DATA), uses_signal_engine: true, no_lookahead_status: OK_PREFIX_ONLY
  trades: 31, net_ev: -0.368%, net_pf: 0.071, same_bar_stop_tp_count: 0
```

Primer backtest real de la historia del proyecto. Resultado malo, pero muestra mínima (72h).

### Paso 6: Bug del resume — synthetic rows + unidirectional

**Bug 1 identificado**: el smoke test inicial que corrí dejó **2 filas sintéticas** en `BTCUSDT 5m` (timestamp 2026-01-01, open=50000). Esas filas envenenaron el `_get_oldest_ohlcv_ms()` haciéndolo creer que el oldest en DB era enero, así que cuando pedí backfill de 90 días pensó "ya está cubierto" y no descargó nada.

**Fix**:
```sql
DELETE FROM ohlcv_candles WHERE symbol='BTCUSDT' AND timestamp < '2026-05-18'
```
2 filas borradas. Sin afectar las 865 reales de las últimas 72h.

**Bug 2 identificado**: La lógica inicial de `_resume_start_ms` solo era **forward**. Si el DB tenía datos recientes pero el usuario pedía un horizonte mayor, no llenaba el hueco hacia atrás.

**Fix**: reescrita `backfill_pair` para calcular `ranges_to_fetch: list[tuple[int, int]]` con range backward + range forward por separado, ambos idempotentes. Test `test_backfill_pair_fills_backward_when_target_window_extends_before_db` añadido.

### Paso 7: Backtest 5m × 90 días × 3 símbolos

**Backfill ejecutado** tras los bugfixes:
```
BTCUSDT 5m: 25,920 candles, batches=131, duration=37s
ETHUSDT 5m: 25,920 candles, batches=131, duration=37s
SOLUSDT 5m: 25,920 candles, batches=131, duration=38s
Total: 77,760 candles, 263 API calls, 76s
```

**Backtest ejecutado** (en background, ~5 min):

| Símbolo | trades | gross_EV | **net_EV** | win_rate | TP% | SL% | TIME% | max_dd | fees_bps | slip_bps |
|---|---|---|---|---|---|---|---|---|---|---|
| BTCUSDT | 943 | +0.005% | **-0.175%** | 33.8% | 10.4% | 18.6% | 71.0% | 182.65 | 11,316 | 5,658 |
| ETHUSDT | 1154 | -0.005% | **-0.185%** | 34.4% | 10.0% | 20.4% | 69.7% | 236.38 | 13,848 | 6,924 |
| SOLUSDT | 1319 | +0.043% | **-0.137%** | 37.8% | 10.0% | 20.8% | 69.2% | 252.82 | 15,828 | 7,914 |

**Total**: 3,416 trades. Los 3 símbolos con net_EV negativo. fees_bps coincide con cost model (~12 bps por trade round-trip). Consistente exactamente con los 8,698 samples del VPS que mostraban -0.18% en todos los buckets de score.

### Paso 8: Verificación de lookahead

**Test ejecutado**:
```python
# Build 200 random candles, compute indicators on full df, compare to prefix df[:100]
# at index 99. If indicators don't peek, both should be identical.
full = add_indicators(df).reset_index(drop=True)
prefix = add_indicators(df.iloc[:100].copy()).reset_index(drop=True)
# Compare 44 indicator columns at index 99
```

**Resultado**: **NO LOOKAHEAD detected**. Los 44 indicadores coinciden exactamente entre full y prefix. Eso descarta que el resultado malo del backtest 5m sea un bug — es real.

### Paso 9: Backfill 1h y 4h × 1 año

**Comando**:
```bash
python -m app.ohlcv_backfill --symbols BTCUSDT,ETHUSDT,SOLUSDT --timeframes 1h,4h --days 365
```

**Resultado**:
```
BTCUSDT 1h: 8,760 candles, batches=45, duration=14s
BTCUSDT 4h: 2,190 candles, batches=12, duration=4s
ETHUSDT 1h: 8,760 candles, batches=45, duration=14s
ETHUSDT 4h: 2,190 candles, batches=12, duration=4s
SOLUSDT 1h: 8,760 candles, batches=45, duration=14s
SOLUSDT 4h: 2,190 candles, batches=12, duration=4s
Total: 32,850 candles, 171 API calls, 53s
```

Cobertura: 1 año completo, primera vela = 2025-05-21, última = 2026-05-21.

### Paso 10: Backtest 1h y 4h × 1 año

**1h × 1 año**:

| Símbolo | trades | net_EV | net_PF | win_rate | TIME% |
|---|---|---|---|---|---|
| BTCUSDT | 1191 | -0.16% | 0.84 | 42.4% | 58.3% |
| ETHUSDT | 1066 | +0.033% | 1.03 | 43.9% | 40.6% |
| SOLUSDT | 1064 | +0.050% | 1.04 | 45.0% | 38.8% |

Marginal positive en ETH/SOL. Margen insuficiente para considerar edge.

**4h × 1 año (versión inflada por contaminación multi-tf)**:

| Símbolo | trades | net_EV | net_PF | win_rate |
|---|---|---|---|---|
| BTCUSDT | 240 | **+0.382%** | 1.30 | 49.2% |
| ETHUSDT | 90 | **+1.062%** | 1.76 | 55.6% |
| SOLUSDT | 42 | **+1.148%** | 1.91 | 59.5% |

**¡Positivo en los 3!** Pero el `RealStrategyBacktester.run()` alimenta las MISMAS velas 4h en los slots "5m", "15m" y "1h" del `MarketSnapshot` ([real_strategy_backtester.py:127-137](app/real_strategy_backtester.py#L127-L137)). El `SignalEngine` da bonificaciones:
- `+20` por "tendencia 5m/15m alineada"
- `+10` por "1h no contradice"

Con las 3 series idénticas, esos +30 puntos se ganan trivialmente cada vez. Inflación.

### Paso 11: Multi-timeframe honest backtest

**Archivo nuevo**: [app/multi_tf_backtest.py](app/multi_tf_backtest.py) — ~180 líneas.

Función `run_multi_tf_backtest(config, symbol, primary_frame, higher_frame, ...)` que:
1. Toma `primary_frame` (4h) y `higher_frame` (1h)
2. En cada índice del primary, construye `MarketSnapshot` donde:
   - `5m` / `15m` / `main_timeframe` / `confirmation_timeframe` slots: usa `primary_slice` (4h aliased)
   - `1h` / `higher_timeframe` slot: usa **prefix REAL de 1h** alineado al timestamp del primary
3. Llama `SignalEngine.generate_signal(...)` con ese snapshot
4. Simula la salida igual que `RealStrategyBacktester._simulate_trade`

El cambio neto: el +10 de "1h no contradice" ya NO es trivial. Solo se gana si las velas 1h reales en el momento del signal coinciden con el bias del primary.

**Resultado 4h honest**:

| Símbolo | trades | net_EV | net_PF | win_rate | TP% | SL% | TIME% | Max DD |
|---|---|---|---|---|---|---|---|---|
| BTCUSDT | 204 | **+0.191%** | 1.14 | 46.6% | 30.4% | 40.2% | 29.4% | 57.2 |
| ETHUSDT | 84 | **+1.076%** | 1.79 | 56.0% | 48.8% | 41.7% | 9.5% | 54.3 |
| SOLUSDT | 40 | **+1.278%** | 2.04 | 60.0% | 45.0% | 37.5% | 17.5% | 37.4 |

Delta vs inflado:
- BTCUSDT: -36 trades, net_EV pasa de +0.38% a +0.19% → **50% de la edge aparente era contaminación multi-tf**
- ETHUSDT: -6 trades, net_EV apenas cambia (+1.06% → +1.08%) → contaminación residual
- SOLUSDT: -2 trades, net_EV apenas cambia (+1.15% → +1.28%) → contaminación residual

**Conclusión**: en BTC la inflación era ~50%. En ETH/SOL la edge era genuina. Los tres siguen positivos después del honest test.

### Paso 12: Walk-forward mensual (12 meses, 4h honest)

**BTCUSDT 4h honest walk-forward**:

```
month     trades  net_EV%   sum%  win_rate  notes
2025-06     17    +2.006   +34.1   62%     solid
2025-07     34    -1.120   -38.1   18%     ← worst-month, big sample
2025-08     25    +1.696   +42.4   65%     best in size
2025-09     22    -0.373    -8.2   45%
2025-10      1    -3.354    -3.4    0%     single-trade noise
2025-11      3    +5.299   +15.9  100%     tiny sample
2025-12     34    -0.949   -32.3   29%     ← bad, big sample
2026-01     13    +3.713   +48.3  100%     suspect 100% win-rate
2026-02      7    +1.839   +12.9  100%     suspect 100% win-rate
2026-03      5    +3.305   +16.5   80%
2026-04     24    -0.957   -23.0   38%     ← recent bad
2026-05     19    -1.379   -26.2   26%     ← in progress, bad
TOTAL:     204    +0.191   +38.99  46.6%  6 positive / 6 negative
```

**ETHUSDT 4h honest walk-forward** (operó solo 8 de 12 meses): 4 positivos / 4 negativos. Sum +90.40%. Carrier: Enero 2026 (+62.61%, 18 trades, 89% wr). Abril/Mayo 2026 negativos.

**SOLUSDT 4h honest walk-forward**: 5 positivos / 3 negativos. Sum +51.13%. Carrier: Enero 2026 (+34.09%, 6 trades, 100% wr). Abril negativo.

**Patrón común**:
- **Mes ganador en los 3**: Enero 2026 (rally regime)
- **Meses recientes (Abril–Mayo 2026) negativos en los 3** → posible regime drift
- **Drawdowns 37-57%** sobre 1 año → con 5x leverage = wipe-out
- **3 meses con 100% win-rate (Nov 25, Ene 26, Feb 26)** → 1 robusto (Ene con 18 trades), 2 ruidosos (Nov con 3, Feb con 7)

### Paso 13: Análisis del VPS training vault

**Archivos descargados**: 8 zips en `C:/Users/Adrian/Downloads/bitget-ai-trading-bot_training_training_vault_*.zip` (May 15 a May 20 2026, total ~880 MB).

**Vault canónico analizado**: `training_vault_20260520_223640.zip` (95.62 MB compressed, 101.11 MB uncompressed)

**Estructura interna del vault**:
```
manifest.json (1.1 KB)
export_summary.json (0.3 KB)
schema_summary.json (0.5 KB)
tables/signal_observations.jsonl.gz   (93.85 MB) — 316,380 rows
tables/signal_labels.jsonl.gz         (3.02 MB)  — 138,209 rows
tables/signal_path_metrics.jsonl.gz   (2.43 MB)  — 39,043 rows
tables/latency_metrics.jsonl.gz       (1.47 MB)
tables/events.jsonl.gz                (0.32 MB)  — 20,520 rows
tables/trades.jsonl.gz                — 7 rows
tables/market_catalysts.jsonl.gz      — 0 rows
... 13 más vacíos o triviales
```

**Manifest metadata**:
- `git_commit: 0975144fea273d911ca9ae99fab6b5ea15743d51` (Fase 7 FIX)
- `schema_version: training_vault_v1`
- `hours: 168` (1 semana de datos)
- `export_started_at: 2026-05-20T22:36:40 → finished 22:40:23 (3.7 min)`

**Comando de import usado**:
```bash
# data-restore-latest IGNORA --file (limitación del CLI), correcto:
python -m app.research_lab data-import --file "<path>" --dry-run   # validar
python -m app.research_lab data-import --file "<path>" --apply     # aplicar
```

Background import iniciado pero **lento** (~20k rows/min debido a dedup row-by-row del bot). Análisis se hizo en paralelo leyendo el zip directo en streaming.

### Paso 14: Análisis del vault — score buckets, regime, setups (137k labels)

**Coste asumido**: 0.18% round-trip (12 bps fees + 6 bps slippage estimado).

**Por score bucket (137,163 labels joineados con observations)**:

```
bucket    samples     gross%       net%    win   TP%   SL%  TIME%
70-74     30 044    -0.0315    -0.2115  44.5%   2.9   7.0   90.0  ← loses
75-79     18 164    +0.0688    -0.1112  46.5%   9.2  14.6   76.2  ← loses
80-84     20 352    +0.0719    -0.1081  42.8%   7.0  14.1   78.8  ← loses
85-89     31 611    +0.2037    +0.0237  48.2%  18.8  10.8   70.3  ← WINS
90-94     16 685    +0.0764    -0.1036  46.8%   7.7   9.7   82.6  ← loses (anomaly)
95-100    20 307    +0.2290    +0.0490  52.2%  16.3   8.9   74.7  ← WINS
```

**Por side (todas las labels)**:
- LONG: 38,524 samples, gross +0.011%, net **-0.169%** ← loses
- SHORT: 98,639 samples, gross +0.139%, net **-0.041%** ← marginal lose

**Por regime**:
```
RANGE         4 253  gross=-0.335%  net=-0.515%  win=30.0%  ← worst
RISK_OFF     41 498  gross=-0.041%  net=-0.221%  win=40.2%  ← loses
RISK_ON       5 811  gross=+0.392%  net=+0.212%  win=53.5%  ← WINS strong
TREND_DOWN   59 666  gross=+0.271%  net=+0.091%  win=55.5%  ← WINS
TREND_UP     25 550  gross=-0.041%  net=-0.221%  win=39.2%  ← loses (counterintuitive)
```

**Filtro combinado** `score>=85 AND regime in (TREND_DOWN, RISK_ON)`:
- Samples: 46,664
- gross_mean: +0.300% / **net_mean: +0.120%** / win_rate: 55.7%
- LONG subset: 6,183 → net -0.183% (LONG sigue perdiendo)
- SHORT subset: 40,481 → **net +0.166%** ← este es el filtro útil

**Top setups (score≥85, samples≥100, sorted by net%)**:

```
GANADORES:
LINKUSDT  SHORT TREND_DOWN  3,373  net +0.446%   ← más fuerte
AVAXUSDT  SHORT TREND_DOWN  1,609  net +0.440%
DOGEUSDT  LONG  RISK_ON     1,557  net +0.402%
DOGEUSDT  SHORT TREND_DOWN  4,006  net +0.272%
ADAUSDT   SHORT TREND_DOWN  3,893  net +0.252%
DOTUSDT   SHORT TREND_DOWN  1,277  net +0.237%
SOLUSDT   SHORT TREND_DOWN  7,169  net +0.148%
XRPUSDT   SHORT TREND_DOWN  6,343  net +0.124%
ETHUSDT   SHORT TREND_DOWN  6,011  net +0.115%
BNBUSDT   SHORT TREND_DOWN    715  net +0.072%

PERDEDORES:
BTCUSDT   SHORT TREND_DOWN  5,864  net -0.066%   ← BTC excepción
ADAUSDT   LONG  RISK_ON       140  net -0.146%
DOTUSDT   LONG  RISK_ON       164  net -0.261%
BTCUSDT   LONG  TREND_DOWN    452  net -0.287%
ETHUSDT   LONG  TREND_DOWN    478  net -0.363%
BTCUSDT   LONG  RISK_ON       538  net -0.380%
SOLUSDT   LONG  TREND_DOWN    571  net -0.398%
ADAUSDT   LONG  TREND_DOWN    311  net -0.448%
XRPUSDT   LONG  TREND_DOWN    510  net -0.449%
DOGEUSDT  LONG  TREND_DOWN    321  net -0.476%
ETHUSDT   LONG  RISK_ON       147  net -0.549%
DOTUSDT   LONG  TREND_DOWN    104  net -0.558%
DOGEUSDT  SHORT RISK_ON       116  net -0.592%
AVAXUSDT  LONG  TREND_DOWN    132  net -0.622%
LINKUSDT  LONG  TREND_DOWN    272  net -0.628%
```

**Patrones**:
- **SHORT en TREND_DOWN funciona en 10 de 11 altcoins** (toda menos BTCUSDT que es la excepción)
- **BTCUSDT consistentemente menor edge extraíble** — el más líquido, spreads más tight, menos alpha disponible para retail
- **LONG en TREND_DOWN pierde sistemáticamente** (counter-trend, no funciona)
- **DOGE/XRP LONG en RISK_ON son las únicas LONG positivas** (alta beta en risk-on)
- **CHOPPY_MARKET y RANGE son lo peor** (esperado)

---

## 5. Estado final del repo

### 5.1 Archivos modificados (`git status`)

```
M  app/bitget_client.py            (+30 líneas: get_history_candles)
M  app/database.py                 (+140 líneas: schema + helpers ohlcv_candles)
?? CODEX_RESULT.md                 (197 KB, decisión del usuario commit o no)
?? app/multi_tf_backtest.py        (NUEVO, 180 líneas)
?? app/ohlcv_backfill.py           (NUEVO, 310 líneas)
?? docs/OHLCV_BACKFILL_PLAN.md     (NUEVO, plan técnico)
?? docs/PHASE_7_2_FINDINGS.md      (NUEVO, findings consolidados)
?? docs/SESSION_HANDOFF_2026-05-21.md  (NUEVO, este documento)
?? tests/test_ohlcv_backfill.py    (NUEVO, 12 tests)
?? tests/test_ohlcv_candles_table.py  (NUEVO, 9 tests)
```

### 5.2 Datos persistidos en `bot_state.db`

```
ohlcv_candles:        110,612 filas
  BTCUSDT 5m:  25,919 (90 días, 2026-02-20 → 2026-05-21)
  BTCUSDT 1h:   8,759 (1 año, 2025-05-21 → 2026-05-21)
  BTCUSDT 4h:   2,189 (1 año)
  ETHUSDT 5m:  25,920
  ETHUSDT 1h:   8,759
  ETHUSDT 4h:   2,189
  SOLUSDT 5m:  25,920
  SOLUSDT 1h:   8,759
  SOLUSDT 4h:   2,189
signal_observations:  ~316,380 (import en progreso desde vault VPS)
signal_labels:        110 originales + 138,209 del vault (cuando termine import)
```

Tamaño DB: 34 MB (eran 1 MB al empezar).

### 5.3 Tests

| Antes | Después | Delta |
|---|---|---|
| 408 passed | **428 passed** | +20 |

Nuevos tests por archivo:
- `tests/test_ohlcv_candles_table.py`: 9 tests
- `tests/test_ohlcv_backfill.py`: 11 tests

Tiempo total suite: 328 segundos (5m 28s).

### 5.4 Seguridad — sin cambios

```
LIVE_TRADING=false
DRY_RUN=true
PAPER_TRADING=true
ENABLE_PAPER_POLICY_FILTER=false
can_send_real_orders=false
órdenes reales esta sesión: 0
private endpoints tocados: 0
VPS modificada: no (todo local)
final_recommendation: NO LIVE
```

---

## 6. Caveats honestos y limitaciones

### 6.1 Sobre el "edge filtrado" del vault (137k labels)

1. **Es 1 SEMANA de datos**. Pudo ser una semana TREND_DOWN-heavy donde el SHORT bias era estructural por el mercado, no por la estrategia. El SHORT-en-TREND_DOWN podría ser básicamente "BTC bajó esa semana, los SHORTs de altcoins ganaron por correlación, no por edge de señal".

2. **Source mixing**: 137k incluye `trade_signal`, `shadow_signal` y `market_probe`. Solo `trade_signal` es lo que el bot habría operado. Shadow/probe son variantes que no reflejan ejecución real.

3. **Coste 0.18% es estimado**. Bitget real depende de taker/maker, símbolo, slippage. Altcoins suelen tener slippage mayor que BTC. Si el coste real es 0.22%, varios "ganadores" se vuelven neutros.

4. **No hay walk-forward a nivel de setup**. Cada (symbol, side, regime) con 1.5k-7k samples podría tener todos sus wins concentrados en 2-3 días.

5. **No hay drawdown analysis** a nivel de setup. Average per-trade positivo puede ocultar equity curve con drawdowns que liquiden cuenta apalancada.

### 6.2 Sobre el 4h honest backtest (1 año real)

1. **Sample size limitante en ETH (84) y SOL (40)**. BTC 204 es decente, los otros dos están en zona de alta variabilidad.

2. **Max drawdown 37-57%**. Con 5x leverage y 40 USDT account, primer drawdown wipe.

3. **Recencia adversa**: los 2 meses más recientes (Abril y Mayo 2026) son negativos en los 3 símbolos. Es posible que el "edge" se haya deteriorado y que el bot deployado HOY estaría perdiendo dinero.

4. **El 4h backtest todavía aliasa primary frame en slots 5m/15m**. El honest test solo desconcaminó el slot `1h` (real). Un backtest verdaderamente multi-tf requeriría también backfillear 15m y 5m alineados.

### 6.3 Limitaciones del backtester sintético

1. **No hay funding rate histórico**. El cost model usa una aproximación del config cuando falta. Funding extremos puntuales pueden cambiar el resultado real.

2. **Slippage es constante por bps**. En realidad varía por símbolo, hora, volumen.

3. **Entry en `i+1.open`** asume que la orden market se llena exactamente al open de la siguiente vela. En realidad puede haber gap o demora.

4. **Same-bar STOP_BEFORE_TP** asume el peor caso si TP/SL tocan en la misma vela. Más conservador que la realidad cuando el path real fue favorable, pero correcto para safety.

5. **5m candles de Bitget pueden tener microestructura no capturada** — wicks profundos no visibles en OHLCV pueden activar stops que en backtest no se ven.

### 6.4 Decisiones que se quedaron fuera de scope

1. **Cleanup de labs muertos**: 32 archivos marcados por `duplicate_module_audit.py` no se tocaron. Pendiente de sesión dedicada.
2. **Postgres sync**: todo el OHLCV está en SQLite local. Si se quiere usar en VPS, hay que exportar o re-backfill allí.
3. **Más símbolos**: solo BTC/ETH/SOL en histórico. Los 7 restantes (XRP, DOGE, BNB, LINK, AVAX, ADA, DOT) están como prioridad T1.1.
4. **Funding/OI/liquidation signal sources** (Tier 2 en el plan): no investigados — son días de trabajo cuando se decida.
5. **Walk-forward por setup específico**: el 4h walk-forward es por símbolo entero, no por filtro (symbol, side, regime, score>=85). Pendiente para validar los winners del vault.

---

## 7. Próximos pasos exactos (orden recomendado)

### T1.1 — Validar setups del vault contra histórico (1-2h de trabajo)

```bash
# Backfillear los 7 símbolos faltantes (1h × 1 año cada uno)
python -m app.ohlcv_backfill --symbols XRPUSDT,DOGEUSDT,BNBUSDT,LINKUSDT,AVAXUSDT,ADAUSDT,DOTUSDT --timeframes 1h,4h --days 365

# Correr backtester (honest multi-tf) filtrado por los setups ganadores del vault:
# LINK/AVAX/DOGE/ADA/DOT SHORT en TREND_DOWN sobre 1 año real
# Si net_EV > 0 con muestra >= 100 trades en 1 año → edge confirmado estructural
# Si net_EV ≈ 0 o negativo → era regime tailwind de la semana del vault, no edge real
```

Esto **es la validación crítica**. Decide si el descubrimiento del vault (137k filtrado positivo) era real o un artefacto de regime.

### T1.2 — Walk-forward por setup

Para cada setup ganador validado en T1.1, partir 1 año en 12 meses y reportar net_EV por mes. Estabilidad = no overfit. 1-2 meses carrying = fragile.

### T1.3 — Cost stress test

Re-correr el análisis del vault y los backtests con cost 0.22% y 0.25%. Setups que sobreviven el stress son robustos.

### T2 — Paper filter design (Fase 7.3, días)

Si T1.1+T1.2 sobreviven:
- Implementar filtro config-driven: allowlist de (symbol, side, regime, score_min) tuples
- Activar en `paper_policy_filter` SHADOW mode (observa sin bloquear)
- Comparar resultado del filtro vs señales sin filtrar durante 7 días
- Si concuerda con backtest → activar filtro en modo PAPER (bloquea trades que no pasan filtro, solo paper)
- 30 días en paper con filtro activo

### T3 — Multi-strategy architecture (Fase 7.4, semanas)

Construir el "mega algoritmo" que el usuario describió:
- RegimeDetector ya existe
- Routing layer: regime → eligible strategies
- Cada strategy con su propio paper filter gate
- Auto-disable de strategies con últimas 4 semanas negativas
- Risk budget split por strategy (no por position)

### T4 — Live readiness (meses fuera)

Solo después de 30+ días paper rentable sostenido. Micro-live con $1-2/trade max. Single setup más-validado primero.

### Comandos clave que dejé funcionando

```bash
# Estado actual del proyecto
python -m pytest -q --basetemp .manual_test_tmp/check

# OHLCV loader audit (verifica que la tabla tiene datos)
python -c "from app.config import load_config; from app.database import Database; from app.ohlcv_replay_loader import ohlcv_replay_loader_audit_text; import logging; c=load_config(); db=Database(c, logging.getLogger()); db.initialize(); print(ohlcv_replay_loader_audit_text(c, db, hours=72))"

# Real backtester sobre histórico (1 año, 4h)
python -c "from app.config import load_config; from app.database import Database; from app.ohlcv_replay_loader import OhlcvReplayLoader; from app.real_strategy_backtester import RealStrategyBacktester; from datetime import datetime, timedelta, timezone; import logging; c=load_config(); db=Database(c, logging.getLogger()); db.initialize(); since=datetime.now(timezone.utc)-timedelta(days=365); f=OhlcvReplayLoader(db).load_ohlcv(symbols=['BTCUSDT'], timeframe='4h', since=since).frames_by_symbol['BTCUSDT']; print(RealStrategyBacktester(c).run('BTCUSDT', f).summary())"

# Honest multi-tf backtest (4h primary + 1h real confluence)
python -c "from app.config import load_config; from app.database import Database; from app.ohlcv_replay_loader import OhlcvReplayLoader; from app.multi_tf_backtest import run_multi_tf_backtest; from datetime import datetime, timedelta, timezone; import logging; c=load_config(); db=Database(c, logging.getLogger()); db.initialize(); since=datetime.now(timezone.utc)-timedelta(days=365); f4=OhlcvReplayLoader(db).load_ohlcv(symbols=['BTCUSDT'], timeframe='4h', since=since).frames_by_symbol['BTCUSDT']; f1=OhlcvReplayLoader(db).load_ohlcv(symbols=['BTCUSDT'], timeframe='1h', since=since).frames_by_symbol['BTCUSDT']; print(run_multi_tf_backtest(c, 'BTCUSDT', f4, f1).summary())"
```

---

## 8. Errores / decisiones discutibles que cometí en sesión

Por transparencia con la próxima IA o el usuario:

1. **Dejé 2 filas sintéticas en `BTCUSDT 5m` tras un smoke test temprano**. Esas filas (timestamp 2026-01-01, open=50000) envenenaron `_get_oldest_ohlcv_ms()` y rompieron el primer intento de backfill 90 días. Detectado al iterar, borrado con SQL directo, no rompe nada en producción porque era una DB local. **Lección**: los smoke tests deberían usar un DB temp via `monkeypatch.setattr("app.database.PROJECT_ROOT", tmp_path)`. Los nuevos tests sí lo hacen.

2. **La primera versión de `_resume_start_ms` era solo forward.** Cuando el DB ya tenía datos recientes (las 72h del primer dry-run) y se pidió backfill de 90 días, no llenó el hueco hacia atrás. Detectado al ver "BTCUSDT 5m: inserted=0 skipped=1" en el log. Refactorizado a lógica bidireccional con `ranges_to_fetch`. Tests `test_backfill_pair_fills_backward_when_target_window_extends_before_db` añadido para no recaer.

3. **El primer comando de import del vault usé `data-restore-latest --file ...`** sin saber que ese comando IGNORA `--file` (busca local-latest). Correcto: `data-import --file ...`. Detectado por el log "latest_backup: training_vault_20260515_141702.zip" — un archivo distinto al que pasé.

4. **El bot's `import_backup` hace dedup row-by-row** y por eso el import de 316k observations es lento (~20k rows/min). Para análisis switchié a streaming JSONL directo (segundos). Para next session: si quieres importar al DB local para queries SQL, dejar que termine el background o reimplementar con bulk insert.

5. **El backtest 4h "inflado" tenía contaminación multi-tf** identificable solo leyendo `signal_engine.py:117` donde se da +20 por "5m/15m alineados". No lo flagueé en el primer reporte, solo después de hacer el honest test. Lección: el `RealStrategyBacktester` actual NO es trustworthy para backtest de timeframes que no sean el `main_timeframe` por defecto (5m) sin la modificación de `multi_tf_backtest.py`.

6. **No commitee nada** porque el usuario no lo pidió. Todo el trabajo está en `working tree`. Si la próxima sesión decide commitear, recomiendo:
   - Commit 1: schema + helpers de `ohlcv_candles` + tests
   - Commit 2: backfill CLI + tests
   - Commit 3: multi_tf_backtest variant
   - Commit 4: docs (plan + findings + handoff)

7. **Token del dashboard del usuario** está en el historial de chat. El usuario aceptó tras advertencia que lo rotará después.

---

## 9. Lo que aprendí del codebase que el bot/Codex no había documentado claro

1. **`OhlcvReplayLoader` (existente, Fase 7.1) esperaba una tabla `ohlcv_candles`** que **NUNCA EXISTIÓ**. Estaba 100% diseñado correctamente pero sin la otra mitad. Esa es la pieza que esta sesión completó.

2. **`RealStrategyBacktester` (existente, Fase 7.1) tiene contaminación multi-timeframe estructural** en `run()` línea 127-137 cuando solo se pasa una serie de candles. El `MarketSnapshot` necesita series de timeframes diferentes para evitar trivializar la bonificación de "5m/15m alineados". No documentado en código ni en CODEX_RESULT.

3. **Bitget endpoint `/api/v2/mix/market/history-candles` tiene `limit` máximo de 200** (no 1000 como el endpoint `candles` regular). Lo confirmé empíricamente.

4. **El esquema de `signal_observations` tiene 64 columnas** — incluye features técnicos (ema, rsi, macd, atr, etc.), contexto (BTC dominance, volume), y metadatos (operated, selected_by_allocator, risk_manager_approved).

5. **El vault del bot exporta tablas a JSONL.gz gzipeado** dentro de un zip, con manifest.json + checksum. Es streaming-friendly para análisis sin import.

6. **`data-restore-latest` ignora `--file` flag**. Para importar archivo concreto: `data-import --file ...`. Bug menor del CLI.

7. **El bot's `duplicate_module_audit.py` reconoce que hay 32 archivos cleanup-worthy** dentro del propio bot. No actúa, solo reporta.

---

## 10. Referencia rápida — qué pasar a ChatGPT

Para continuar el proyecto, pásale a ChatGPT estos 3 archivos como contexto principal:

1. **`docs/PHASE_7_2_FINDINGS.md`** — los hallazgos del backtest histórico + vault (137k)
2. **`docs/SESSION_HANDOFF_2026-05-21.md`** — este documento (handoff exhaustivo)
3. **`docs/OHLCV_BACKFILL_PLAN.md`** — el plan técnico aprobado

Y opcionalmente como contexto secundario:

4. **`docs/WEBSOCKET_ROADMAP.md`** — sigue vigente, WebSocket aún no procede
5. **`CODEX_RESULT.md`** — notas históricas de Codex hasta Fase 7.1

Prompt sugerido para ChatGPT al continuar:

> "Continuamos el bitget-ai-trading-bot. La sesión anterior con Claude completó Phase 7.2 (OHLCV backfill histórico + backtest real). Lee `docs/SESSION_HANDOFF_2026-05-21.md` para contexto completo. El hallazgo principal: a 137k labels reales del VPS, hay setups específicos con net positivo (LINK/AVAX/DOGE SHORT en TREND_DOWN). Pero la muestra es 1 semana TREND_DOWN-heavy, por lo que **debe validarse contra histórico de 1 año** antes de cualquier paper filter. Próximo paso: T1.1 — backfill de los 7 símbolos faltantes (XRP/DOGE/BNB/LINK/AVAX/ADA/DOT) en 1h+4h × 365 días, y correr `run_multi_tf_backtest` filtrado por los setups ganadores del vault. Si net_EV positivo en 1 año real → edge estructural. Si no → era regime tailwind."

---

## 11. Apéndice — comandos exactos que dejé ejecutados (audit trail)

```bash
# Verificación inicial
git log --oneline -30
git remote -v
python -c "import sqlite3; ..."  # count tables, rows

# Cleanup
rm -f ts
python -c "from app.duplicate_module_audit import duplicate_module_audit_text; print(duplicate_module_audit_text())"

# Schema test
python -c "...test ohlcv_candles table..."
python -m pytest tests/test_ohlcv_candles_table.py -q

# Backfill dry-run 72h
python -m app.ohlcv_backfill --symbols BTCUSDT --timeframes 5m --hours 72 --dry-run
python -m app.ohlcv_backfill --symbols BTCUSDT --timeframes 5m --hours 72

# Backfill 90d × 3 symbols × 5m
python -m app.ohlcv_backfill --symbols BTCUSDT,ETHUSDT,SOLUSDT --timeframes 5m --days 90

# Backfill 365d × 3 symbols × 1h+4h
python -m app.ohlcv_backfill --symbols BTCUSDT,ETHUSDT,SOLUSDT --timeframes 1h,4h --days 365

# Backtests (ad-hoc Python)
python -c "...RealStrategyBacktester loop sobre 3 symbols 5m..."
python -c "...RealStrategyBacktester loop sobre 1h y 4h..."
python -c "...walk_forward monthly bucketing..."
python -c "...run_multi_tf_backtest honest..."

# Lookahead audit
python -c "...compare add_indicators(full) vs add_indicators(prefix)..."

# Vault dry-run + import
python -m app.research_lab data-import --file "<vault>" --dry-run
python -m app.research_lab data-import --file "<vault>" --apply   # en background

# Vault streaming analysis
python -c "...zipfile + gzip + json streaming + pandas analysis..."

# Full regression
python -m pytest -q --basetemp .manual_test_tmp/full_regression
# Result: 428 passed in 328.26s
```

---

Fin del handoff. Si tienes la próxima sesión con cualquier IA, este documento + los 3 docs en `docs/` es todo el contexto necesario para retomar exactamente donde paramos.
