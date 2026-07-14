# V10.47.14 — AUDITORÍA FINAL INDEPENDIENTE

**Fecha:** 2026-07-14

**Rol:** auditor científico/técnico independiente (CODEX)

**Veredicto:** **FAIL**

**Alcance del FAIL:** falla la certificación `SCIENTIFIC REPAIR COMPLETE`; no
invalida la conclusión conservadora `SHADOW_CANDIDATES=0 / NO_CONFIRMED_EDGE`.

**Invariantes mantenidos:** `PAPER_TRADING=True · LIVE_TRADING=False ·
DRY_RUN=True · can_send_real_orders=false · FINAL_RECOMMENDATION=NO LIVE`.
No se abrió el holdout para esta auditoría, no se ejecutaron órdenes, no se
modificaron código, estrategias, datasets, configuración ni informes de Fable.

## 1. Veredicto

La reparación causal del defecto `LAST_SIGNAL_CLUSTER_OVERWRITE` es real y la
reproducción DOGE/XRP es exacta dentro del redondeo declarado. El ledger causal,
los contadores y los totales de los doce torneos también son consistentes con
los artefactos. La conclusión prudente —cero candidatos, ningún edge confirmado
y ninguna transición a live— está respaldada.

No obstante, V10.47.14 no puede certificarse como reparación científica final:

- `VALIDATION` no participa en ninguna gate;
- el supuesto holdout sellado se carga y se preprocesa junto con toda la serie,
  mientras `holdout_touched=False` es un literal no instrumentado;
- el baseline no cumple íntegramente holding, censura ni comparación pareada;
- las estrategias deterministas no implementan parámetros prerregistrados
  centrales (4h→1h, stop 2 ATR y trailing desde 1R);
- el manifest/seal actual no verifica el conjunto real de archivos ni liga la
  provenance declarada;
- los 2896 resultados de pytest son invocaciones, pero solo 2895 nodeids únicos.

Por estas contradicciones materiales, el veredicto estricto es **FAIL**, no
`PASS WITH LIMITATIONS`.

## 2. Hallazgos por severidad

### P0

Ninguno. No se encontró envío de órdenes reales, activación live, apertura del
holdout por esta auditoría ni promoción de un candidato.

### P1

1. **Split/holdout no cumple lo certificado.** `split_indices()` define TRAIN,
   VALIDATION, WALK-FORWARD y HOLDOUT, pero `run_causal_tournament()` solo corta
   TRAIN y WALK-FORWARD. VALIDATION nunca se pasa a `evaluate_candidate()` y no
   existe `validation_positive`. Además, `precompute_sigs(bars)` procesa la
   serie completa antes de cortar regiones, incluido el rango nominal de
   holdout. `holdout_touched=False` se devuelve de forma constante, sin guardia
   de acceso ni prueba de sellado. No hay evidencia de uso del holdout en
   métricas o selección, pero sí queda refutada la afirmación fuerte “nunca
   abierto/sellado”.

2. **Baseline incompleto respecto del protocolo declarado.** El null conserva
   número de entradas, mezcla LONG/SHORT agregada, cluster, nominal y parámetros
   de costes/salida. No conserva necesariamente holding realizado ni censura,
   puede crear solapamientos sin reejecutar el ledger single-position y no
   calcula una serie pareada candidato−random. `paired_delta_vs_zero()` produce
   el lower bound contra no-trade, no contra el baseline aleatorio. Por tanto,
   “same holding/censorship/paired comparison” queda solo parcial.

3. **Estrategias deterministas no corresponden a la prerregistración.** La
   propuesta EMA/ADX exige régimen 4h y entrada pullback 1h, stop 2 ATR y trailing
   desde 1R. El código ejecuta una única serie por timeframe, sin enlace causal
   4h→1h, y `DET_EXIT` fija `stop_frac=0.02`, `tp_frac=0.06` y
   `trailing_frac=0.02`. Donchian también declara stop 2 ATR, pero usa los mismos
   porcentajes fijos. El estado `INSUFFICIENT_DATA` y la etiqueta smoke son
   correctos; `IMPLEMENTATION_STATUS=COMPLETE` no lo es.

4. **Manifest/seal inválido para el estado entregado.** De 41 entradas SHA-256,
   `progress_checkpoint.md` no coincide: declarado
   `5bd2cc09…`, actual `b4ac21c2…`. El manifest/seal se generó a las 13:31:11 y
   el checkpoint se modificó a las 13:31:30. El seal declarado
   `a9743f85…` se reproduce únicamente con los hashes registrados; usando los
   archivos actuales resulta `bfcff3cc…`. Además, el seal solo cubre líneas
   `ruta:hash` de outputs: no liga branch/HEAD/tree/origin, y el manifest no
   expone hashes de dataset ni spec. La provenance final no está sellada.

### P2

1. **Recuento de tests no único.** El log real termina con `2896 passed`, y la
   colección actual vuelve a dar 2896 invocaciones. Solo hay 2895 nodeids
   únicos: dos parámetros (`%2E%2E` y `%2e%2e`) generan el mismo nodeid
   `tests/test_researchops_v10_45_2_truth_hotfix.py::test_symbol_whitelist_rejects_traversal[%2E%2E]`.

2. **EventClock parcialmente timeframe-aware.** `interval_ms_for()` y el ledger
   soportan 1m/5m/15m/1h/4h, pero el adaptador canónico `bars_to_events()` deja
   `interval_ms=60_000` por defecto y usa `cluster_id()` de 30 minutos. Si se
   invoca con `timeframe="4h"` sin intervalo explícito, publica la vela tras un
   minuto, no tras cuatro horas.

3. **Deduplicación del registry débil.** El hash de spec incorpora el nombre del
   participante; dos hipótesis operacionalmente iguales con nombres distintos
   no pueden aparecer como duplicadas. `m_nominal=47`, `m_unique=47` y Bonferroni
   están en los artefactos, pero `duplicated_runs=0` no demuestra ausencia de
   duplicados semánticos.

4. **n_eff conservador, pero block bootstrap no justificado.** El mínimo de
   evento/overlap/cluster/session/temporal/ACF es verificable y suele ser menor
   que trades. El bootstrap usa bloque fijo 5 para todos los timeframes sin
   selección/justificación basada en dependencia; aporta un LB conservador
   contra cero, no completa la comparación pareada contra random.

5. **“No push” solo demostrable como estado actual.** HEAD no está contenido en
   ninguna rama remota y la rama no tiene upstream; esto demuestra “no publicado
   actualmente”, no permite probar históricamente que nunca se intentó un push.

### P3

1. `ImmutableLedger.records()` hace copias superficiales. Los registros usados
   aquí contienen escalares y no se observó overwrite, pero la clase genérica no
   protege mutables anidados.
2. El dashboard muestra la identidad final correcta, pero su afirmación
   `manifest+seal` hereda el P1 de provenance y la NEXT_ACTION queda truncada a
   la primera línea visible.

## 3. Matriz de comprobaciones

| # | Área | Estado | Conclusión |
|---|---|---|---|
| 1 | Git/config | DEMOSTRADO | Rama, HEAD, tree, origin/main, 24 ahead/0 behind, tracked limpio antes de la auditoría; 3 untracked históricos; `.env` ausente y ejemplos/config sin diff. Ausencia histórica de push: parcial. |
| 2 | Invalidation | DEMOSTRADO | DOGE/XRP P08_LONG invalidated con razón correcta; ocho artefactos conservados y hashes `after` coincidentes; candidatos activos=0. |
| 3 | Ledger causal | DEMOSTRADO | Primera señal, single-position, skips PAI/cooldown, sin overwrite en resultados, secuencia append-only. Inmutabilidad genérica profunda: parcial. |
| 4 | Reproducción | DEMOSTRADO | Flip exacto reproducido sobre generaciones verificadas. |
| 5 | EventClock/SimOMS | PARCIAL | Intervalos del ledger, timestamps, bars held, settlements, trailing next-bar y SL-first están implementados; `bars_to_events()` mantiene default 1m incorrecto para TF superiores. |
| 6 | n_eff | PARCIAL | No igualado a trades; incluye overlap, clusters, sesiones, temporal y ACF; elección min conservadora. Bloque bootstrap fijo no justificado. |
| 7 | Baseline | PARCIAL | Count/side/cluster/exposure/cost params sí; holding realizado, censura, single-position null y paired LB vs random no. |
| 8 | Registry/multiple testing | PARCIAL | Cerrado/hasheado antes de outputs, Bonferroni y gates versionadas; deduplicación semántica no demostrada. |
| 9 | Splits/holdout | INVALIDADO | TRAIN y WF usados; VALIDATION no usada; holdout no entra en métricas, pero la serie completa se precomputa y el flag es hard-coded. No es un sellado verificable. |
| 10 | 12 torneos | DEMOSTRADO | 12/12, 564, 47/combo, clases 389/154/21, shadow=0. La gate de promoción está incompleta por #7/#9. |
| 11 | P08/data truth | DEMOSTRADO | Proxy correctamente nombrado; real OI/funding=false; canonical P08 no validado; costes MODELLED y funding PROXY. |
| 12 | Deterministas | INVALIDADO | Causalidad básica, next-open, Donchian excluye barra actual, LONG/SHORT y smoke/INSUFFICIENT_DATA sí; 4h→1h, 2 ATR y trailing desde 1R no implementados. |
| 13 | Hub multi-IA | DEMOSTRADO | 20 ficheros raíz + 3 directorios, una NEXT_ACTION, IDs y links coherentes; status=COHERENT. |
| 14 | Provenance/seal | INVALIDADO | Un hash no coincide; seal real difiere; no liga Git/dataset/spec. Dashboard sí contiene HEAD/tree finales. |
| 15 | Tests | PARCIAL | Log real, suite verde y 29 nuevos verdes; 2896 invocaciones, 2895 nodeids únicos. |

## 4. Reproducciones ejecutadas

1. **Flip real, sin escribir outputs:**

| Símbolo | generation_id | flawed last-signal | causal first-signal | resultado |
|---|---|---:|---:|---|
| DOGEUSDT | `333a7d47345167df` | +0.672646 € | −0.730039 € | flip confirmado |
| XRPUSDT | `f95ae658d18da47b` | +0.310297 € | −0.557404 € | flip confirmado |

DOGE: raw 688, executed 87, `POSITION_ALREADY_OPEN` 345,
`CLUSTER_COOLDOWN` 256. XRP: raw 459, executed 73, PAI 239, cooldown
147. Coinciden con los artefactos.

2. **Tests focales:** 29/29 pasaron en 1.72 s (17 causal/registry, 6
   deterministic, 6 hub).
3. **Colección completa:** 2896 invocaciones, 2895 nodeids únicos, un duplicado.
4. **Hub:** `ai_coordination_status.py` devolvió `COHERENT`, una NEXT_ACTION,
   broken_links=0.
5. **Manifest:** 41 hashes recomputados; un mismatch. Seal declarado reproducible
   desde el inventario registrado, no desde los archivos actuales.
6. **Torneos:** agregación independiente de los doce JSON; totales exactos.

## 5. Resultados de los doce torneos

| Combo | NO_GROSS | COST_KILLED | NET_POSITIVE | Shadow | m_nominal/m_unique |
|---|---:|---:|---:|---:|---:|
| BTCUSDT 1m | 40 | 7 | 0 | 0 | 47/47 |
| BTCUSDT 5m | 33 | 13 | 1 | 0 | 47/47 |
| BTCUSDT 15m | 21 | 18 | 8 | 0 | 47/47 |
| ETHUSDT 1m | 24 | 23 | 0 | 0 | 47/47 |
| ETHUSDT 5m | 31 | 15 | 1 | 0 | 47/47 |
| ETHUSDT 15m | 31 | 12 | 4 | 0 | 47/47 |
| XRPUSDT 1m | 32 | 12 | 3 | 0 | 47/47 |
| XRPUSDT 5m | 31 | 15 | 1 | 0 | 47/47 |
| XRPUSDT 15m | 35 | 12 | 0 | 0 | 47/47 |
| DOGEUSDT 1m | 36 | 11 | 0 | 0 | 47/47 |
| DOGEUSDT 5m | 40 | 7 | 0 | 0 | 47/47 |
| DOGEUSDT 15m | 35 | 9 | 3 | 0 | 47/47 |
| **Total** | **389** | **154** | **21** | **0** | **564 nominales** |

Los 21 positivos son TRAIN-only y ninguno figura promovido. La ausencia de
promoción es correcta; no debe interpretarse como validación completa de la gate.

## 6. Evaluación del ledger

`drive_causal()` evalúa señales en orden temporal, bloquea mientras la posición
está abierta, bloquea reentrada en cluster consumido y registra decision/order/
entry/position/exit/trade sin reemplazar registros previos. Los contadores
agregados satisfacen raw = executed + position-open + cooldown en las ejecuciones
sin rechazos. La reproducción sintética y la real muestran que el ganador tardío
ya no reemplaza al perdedor inicial. Evaluación: **demostrado**, con la limitación
menor de copias superficiales en la API genérica.

## 7. Evaluación de n_eff y baseline

`n_eff_final` es el mínimo de estimadores de evento, no-overlap, cluster, sesión,
tiempo y autocorrelación; no es un alias de trades. En XRP reproducido fue
41.0516 frente a 73 trades. Esto es materialmente mejor que V10.47.

El baseline es solo parcialmente matched. El muestreo por el mismo cluster y la
permutación de sides preservan exposición gruesa, pero cada trade aleatorio se
simula aislado: no se preserva la trayectoria single-position, el holding
realizado ni la censura. El bootstrap de bloque se aplica al PnL del candidato
contra cero. Se requiere un delta pareado candidato−baseline y una política
explícita de censura/holding.

## 8. Evaluación del holdout

No hay resultados de holdout ni evidencia de que se usara para seleccionar o
promover. Eso es positivo. Sin embargo, el runner carga todas las barras y llama
`precompute_sigs(bars)` antes de segmentar; por tanto procesa el rango nominal
del holdout. El booleano `holdout_touched=False` no está conectado a un guard.
VALIDATION se define, pero nunca se evalúa. Evaluación: **no se demuestra un
holdout sellado**, y no debe abrirse ahora para corregir el informe.

## 9. Evaluación de P08 y data truth

Demostrado: implementación `P08_FUNDING_HOUR_RETURN_REVERSAL_PROXY`,
`uses_real_oi=false`, `uses_real_funding=false`, timestamp de funding únicamente
y `does_not_validate_canonical_p08=true`. Costes fijos: `MODELLED`; funding:
`PROXY`; OI real y L2: `UNAVAILABLE`. El P08 canónico no está validado.

## 10. Evaluación 1h/4h

La resample/smoke usa ~90 días y queda correctamente marcado
`INSUFFICIENT_DATA`; ningún smoke positivo se trata como edge. Las entradas son
next-open y Donchian excluye la barra actual. Pero la estrategia EMA/ADX no une
un régimen 4h con una entrada 1h, y ambas estrategias usan stops/trailing fijos
por porcentaje en vez de los parámetros ATR prerregistrados. Estado correcto:
`NEEDS_DATA`, pero también **NEEDS_IMPLEMENTATION_REPAIR** antes de adquirir datos.

## 11. Evaluación del hub

El hub contiene 20 ficheros raíz y directorios `experiments/`, `proposals/` y
`reviews/`. Hay una única NEXT_ACTION, tres decisiones con IDs D001–D003, dos
propuestas revisadas, tres experimentos y cero enlaces rotos. El validador
devuelve `COHERENT`. La coherencia estructural no detecta las contradicciones
científicas descritas en esta auditoría.

## 12. Evaluación del manifest/seal

HEAD/tree/branch/origin y el dashboard coinciden con el HEAD auditado. Los hashes
de 40/41 outputs coinciden. El checkpoint posterior rompe el inventario y el seal
actual. Aun sin ese cambio, el algoritmo sella solo outputs y deja fuera los
campos Git del manifest; no hay hashes explícitos de datasets/specs. Resultado:
**provenance no certificada**.

## 13. Tests verificados

- Log existente: `logs/full_suite_v10_47_8.log`, resumen `2896 passed in
  506.29s (0:08:26)`.
- Hash del log: coincide con el manifest.
- Colección independiente: 2896 invocaciones, 2895 nodeids únicos.
- Duplicado exacto: test de whitelist parametrizado con dos variantes de case
  que colisionan en el ID renderizado por pytest.
- 29 tests nuevos: 29 passed; cobertura declarada de causalidad/registry,
  estrategias y hub confirmada, pero faltan tests que fallen por VALIDATION no
  usada, acceso físico al holdout, ATR real, vínculo 4h→1h, manifest ligado a
  provenance y baseline pareado/censurado.

## 14. Cambios exactos pendientes

1. Separar físicamente TRAIN/VALIDATION/WF/HOLDOUT antes de cualquier carga o
   feature computation; añadir un guard auditable que impida leer holdout.
2. Evaluar VALIDATION con gate explícita y timestamp posterior a
   `selection_end_ms`; conservar WALK-FORWARD como región distinta.
3. Rehacer el null matched para preservar single-position, holding/censura,
   sesiones, side, número de entradas, exposición y costes; calcular delta
   pareado y lower bound de bloque contra random.
4. Implementar el régimen 4h→pullback 1h causal, stop dinámico 2 ATR y trailing
   desde 1R; hacer lo equivalente para Donchian según su spec.
5. Hacer que `bars_to_events()` derive siempre el intervalo y cluster del
   timeframe, sin default silencioso de 1m.
6. Definir deduplicación por spec canónica sin usar el nombre como sustituto de
   semántica; registrar `m_nominal`, `m_unique` y duplicados reales.
7. Sellar al final un payload canónico que incluya hashes reales de outputs,
   commit/tree/origin, generaciones/hashes de dataset, registry y specs. Verificar
   el manifest contra disco después de generar el último artefacto.
8. Asignar IDs pytest explícitos únicos a los dos casos colisionados y volver a
   ejecutar colección + suite completa con exit code registrado.
9. Regenerar los doce torneos y todos los informes sin abrir el holdout; repetir
   esta auditoría antes de declarar “complete”.

## 15. NEXT_ACTION recomendada

- [ ] **NEXT:** Corregir primero los bloqueadores de certificación P1
  (VALIDATION/holdout físico, baseline pareado, implementación determinista
  conforme a spec y provenance/seal), añadir los tests de falsificación y
  regenerar los doce torneos. Mantener el holdout cerrado. Solo después, y si la
  auditoría pasa, retomar la adquisición de ≥2 años de OHLCV 1h/4h.

No se propone live, no se promete rentabilidad y no se añaden estrategias.
