# Runbook — Bugatti Reliability (V10.42)

> Cómo operar la capa de datos + research del bot. **RESEARCH ONLY · NO LIVE · sin órdenes · sin claves.**
> El objetivo de V10.42 es que los datos sean fiables antes de creer en cualquier resultado de estrategia.

---

## 1. Arrancar el collector de trades por websocket (la solución al DATA_GAP)

El collector REST actual saca ~1000 trades por ciclo (~5 min) → datos en clusters con huecos.
El collector **websocket continuo** captura tick a tick mientras el PC está encendido.

Arráncalo en **tu propia consola** (para que sobreviva al cierre de sesiones de Claude):
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\collect_bybit_trades_ws_forever.ps1"
```
- Público, sin claves, sin órdenes. Escribe a un dataset **separado**:
  `external_data/staging/bybit_trades_ws_v10_42/trades.csv` (no toca el dataset V10.32).
- Mutex `Local\BitgetBotBybitTradesWsV1042` evita instancias duplicadas.
- Requiere `websocket-client` en el `.venv` (ya declarado en requirements). Si falta, el CLI
  devuelve `DEPENDENCY_MISSING` de forma fail-closed.
- Ctrl+C para parar (limpio).

> Nota: el collector solo captura con el PC encendido. Un hueco nocturno con el PC apagado
> **no es un fallo**; es datos que no existen porque no había captura.

---

## 2. Mirar la salud (health / watchdog)

```powershell
python -m app.research_lab collector-health-v1042 --symbols BTCUSDT
```
Estados y qué significan:

| status | significado | qué hacer |
|---|---|---|
| `HEALTHY` | datos frescos, sin mezcla, cobertura razonable | seguir |
| `DEGRADED` | fresco pero gappy o mezclado | dejar acumular; revisar sub_states |
| `STALE` | último dato > 15 min | ¿collector parado? reabrir la consola |
| `COLLECTOR_DOWN` | último dato > 45 min | collector caído o **PC estuvo apagado** |
| `DATASET_MIXED` | hay backfill 2020 + forward 2026 | usar métricas forward-only, no globales |
| `TOO_GAPPY` | cobertura forward < 60% | el ws collector lo mejora con el tiempo |
| `RATE_LIMIT_RISK` | disco bajo u otra presión | liberar disco |

---

## 3. Ver cobertura real (forward-only, ignorando el backfill 2020)

```powershell
python -m app.research_lab forward-dataset-view-v1042 --symbols BTCUSDT
```
- Usa **`forward_*`** para readiness, **no** `global_*` (el día de backfill 2020 hunde la cobertura global a ~0%).
- `data_quality_gate` te dice si el resultado del tournament es `USABLE` / `EXPLORATORY` / `NOT_RELIABLE_GAPS` / `NOT_RELIABLE_SAMPLE`.

## 4. Auditoría de gaps y plan de reparación

```powershell
python -m app.research_lab data-gap-audit-v1041 --symbols BTCUSDT
python -m app.research_lab gap-repair-plan-v1042 --symbols BTCUSDT
```
- Los gaps de microestructura pasados **no se pueden rellenar** con REST público (solo devuelve trades recientes).
  Verdict honesto: `UNREPAIRABLE_MICROSTRUCTURE_GAP`. **No se inventan ticks.**
- Solo días pasados COMPLETOS se pueden backfillear vía los dumps diarios oficiales (V10.36).
- La solución real es **hacia adelante**: el collector ws continuo.

## 5. Mapa de cuellos de botella

```powershell
python -m app.research_lab bottleneck-map-v1042 --symbols BTCUSDT
```
Agrega health + forward view + gap repair + universo de estrategias + prioridades.

## 6. Correr el research (tournament) — y cuándo fiarse

```powershell
python -m app.research_lab shadow-simulation-tournament-v1040 --symbols BTCUSDT
```
- **Fíate** del resultado solo si `data_quality_gate` = `USABLE` (hoy NO lo es).
- Hoy: `TOO_GAPPY` / `NOT_RELIABLE_GAPS` → el resultado es **exploratorio**, no concluyente.
- El tournament ya usa `entry_mode=next_open` (realista) y compara con `close`.

---

## 7. Qué hacer según el caso

- **DATA_GAP alto** → dejar el collector ws corriendo; acumular días; no forzar conclusiones.
- **PC estuvo apagado** → normal; no es fallo; el hueco es irreparable a nivel tick.
- **API_BACKOFF / rate-limit** → el ws no machaca REST; si aparece, espaciar ciclos REST.
- **DATASET_MIXED** → usar forward-only view; (futuro) podar el día 2020 del dataset forward.

## 8. Qué pegar a Code por la noche

Pega el contenido de `reports/research/reliability/`:
`bottleneck_map_v1042.md`, `collector_health_v1042.md`, `forward_dataset_view_v1042.md`
y de `reports/research/shadow_simulation/` (`shadow_scoreboard_v1040.csv`, `shadow_research_memo_v1040.md`).

- **Buena señal:** `forward_coverage` subiendo, `max_contiguous_run` creciendo, `collector: HEALTHY`,
  y `data_quality_gate` pasando a `USABLE`; alguna política con `net_EV_lower_bound > 0` que bate baselines.
- **Mala señal (lo esperable hoy):** `TOO_GAPPY`, todo REJECTED, `any_strategy_beats_baseline_and_costs=False`.

---

## 9. Estado honesto y prohibiciones

- No hay edge validada. El collector ws mejora la **base de datos**, no crea ventaja.
- Cost-crusher y estrategias avanzadas (RSI/EMA/Bollinger/fibo/candles/multitimeframe) quedan **pendientes de datos continuos** — implementarlas sobre datos gappy sería sobreajustar ruido.
- **NO LIVE. NO paper filter. NO órdenes. NO `.env`. NO keys. FINAL_RECOMMENDATION=NO LIVE.**
