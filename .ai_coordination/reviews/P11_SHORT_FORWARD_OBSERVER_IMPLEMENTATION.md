# P11_SHORT forward observer — diagnóstico e implementación

Fecha de diagnóstico: 2026-07-15

Ámbito: `BTCUSDT · Bitget · 15m · P11_SHORT · forward shadow`
Decisión científica conservada: `NO_CONFIRMED_EDGE`; P11_SHORT continúa siendo únicamente el lead de investigación forward.

## Diagnóstico previo a la implementación

### Causa raíz exacta

El flujo que produjo los 27.013 candidate snapshots termina deliberadamente en una decisión no accionable. `app/labs/multi_symbol_opportunity_scanner_v10_28.py` crea snapshots compactos y fija `executed=false`, `not_actionable=true` y `no_orders=true`; después solo los añade a `reports/research/v10_28/scanner_scans.jsonl`. No publica esos snapshots a un subscriber de P11_SHORT ni crea una entidad durable de lifecycle.

Ese escáner tampoco es el pipeline canónico solicitado: su CLI usa el proveedor público cross-exchange configurado por defecto para Binance Futures, mientras que la autoridad de P11_SHORT exige `BTCUSDT`, Bitget y agregación causal 15m desde velas públicas 1m. Por ello los 27.013 registros históricos no pueden convertirse retrospectivamente en observaciones forward de P11_SHORT.

La implementación científica V10.46 sí contiene las dos piezas necesarias, pero están desconectadas del proceso continuo:

- `app/labs/v10_46/families.py` contiene la señal canónica P11 y sus parámetros preregistrados (`SHORT`, stop 0,8 %, take profit 1,2 % y salida temporal a 15 barras).
- `app/labs/v10_46/causal_ledger.py` y `app/labs/v10_46/sim_oms.py` pueden recorrer un trade en una simulación offline, incluyendo prioridad conservadora stop-first, costes, MFE y MAE.

El ledger causal es de memoria y espera una ventana ya disponible de barras futuras. No tiene scheduler continuo, checkpoint, máquina de estados durable, listener de velas cerradas, control de instancia, recuperación tras reinicio ni reconciliación. Además, un resultado `END` de la simulación representa una ventana todavía incompleta y no debe finalizar prematuramente una posición forward. En el stack actual no existe ningún componente que vuelva a evaluar la posición al cerrar cada nueva vela, por lo que nunca se alcanzan de forma continua las salidas SL/TP/TIME, outcomes ni labels.

En consecuencia, el corte concreto es:

`candidate snapshot / decisión` **→ falta observer durable →** `lifecycle / entrada / seguimiento / salida / outcome / label`.

No es un problema de frecuencia de la señal, parámetros de P11_SHORT ni ejecución de órdenes. Faltan la conexión operativa, el estado durable y la reconciliación.

### Fallos derivados confirmados

- No hay exactamente-un-lifecycle por oportunidad porque no existe lifecycle forward persistido.
- Los IDs científicos no se propagan más allá del snapshot.
- No se registran entrada causal, actualización de posición ni eventos de salida.
- La salida temporal no puede ejecutarse en el loop actual.
- Un reinicio pierde todo posible estado intermedio porque el recorrido offline vive en memoria.
- No existe deduplicación transaccional frente a una vela o evento repetidos.
- No existe detección de vela ausente, orden temporal inválido, lifecycle huérfano o doble proceso.
- Los snapshots del dashboard/log no son una fuente de verdad y no pueden producir métricas forward reconciliadas.
- No existen outcomes cerrados; por tanto `forward_n_raw`, `forward_n_eff` y estadísticas de rentabilidad no tienen muestra y deben mostrarse como `N/A`.

## Contrato de reparación

Se añadirá un observer aislado que no importa ni llama módulos de órdenes, endpoints privados, wallet, sizing productivo, leverage, margin ni `.env`. Consumirá exclusivamente velas públicas cerradas de Bitget, agregadas causalmente a 15m, y verificará la definición canónica de P11_SHORT contra la autoridad registrada sin modificarla.

La frontera forward se congelará de forma durable antes de descargar o evaluar nuevos resultados. Guardará timestamp, HEAD, tree, fingerprint de política, hash de configuración, fuente y versión de esquema. Las barras anteriores solo podrán calentar features; nunca contarán como forward.

La fuente de verdad será SQLite transaccional con WAL, restricciones únicas y eventos encadenados por hash. Mantendrá:

- boundary/configuración inmutable;
- checkpoint de barras cerradas;
- ledger append-only de eventos;
- snapshot durable del lifecycle;
- outcomes y labels finalizados append-only;
- lease exclusivo con heartbeat para impedir dos observers simultáneos;
- diagnósticos estructurados y reconciliaciones periódicas;
- exports reproducibles derivados de la base, no del dashboard.

La máquina de estados será:

`OBSERVED → REJECTED_FINAL`

o bien:

`OBSERVED → ELIGIBLE → ENTRY_PLANNED → OPEN_SHADOW → EXITED → OUTCOME_FINALIZED → LABEL_FINALIZED`.

Cada transición se validará y persistirá en una única transacción. Una señal elegible planificará la entrada al open de la siguiente vela cerrada disponible, que es el primer precio causal permitido. Una posición abierta se reevaluará al cierre de cada vela. Solo SL, TP o TIME finalizan normalmente; una ventana aún incompleta permanece abierta. Si SL y TP aparecen en la misma vela se aplica stop-first. Los costes se registran una sola vez con la política canónica observada.

Los identificadores serán deterministas a partir de la frontera/política y del timestamp de la barra: `opportunity_id`, `signal_id`, `lifecycle_id`, `candidate_trade_id`, `hypothesis_id`, `global_event_id`, `dependency_cluster_id`, `underlying_trade_id`, `entry_bar_id` y `exit_bar_id`. Las claves únicas harán idempotentes los reintentos y evitarán reaperturas o dobles finalizaciones.

La reconciliación exigirá exactamente:

`oportunidades = rechazos finales + lifecycles activos + outcomes finalizados + errores estructurados pendientes`.

Además verificará por separado la cardinalidad de entradas, outcomes y labels, la cadena append-only, huérfanos, duplicados, transiciones inválidas, lag y última vela. Un dato ausente o inconsistente produce estado fail-closed y queda visible; nunca se pierde silenciosamente.

## Integración prevista

El observer arrancará automáticamente con el proceso continuo de research, tendrá también un comando aislado para diagnóstico/smoke, conservará checkpoint y recuperará una posición abierta sin reprocesar historia como forward. El dashboard leerá la misma fuente durable y mostrará `N/A` para métricas sin outcomes. Los exports incluirán ledger, outcomes, labels, reconciliación y resumen.

La reparación no activa paper ni live trading, no abre holdout y no afirma edge, rentabilidad, validación o promoción.

## Implementación completada

Se implementó `app/labs/p11_short_forward_observer.py` como un proceso aislado de investigación. La fuente consulta únicamente las velas públicas cerradas de Bitget y aplica una agregación estricta de 15 velas 1m consecutivas para formar cada vela 15m. La definición de P11_SHORT, la entrada causal y las reglas SL/TP/TIME permanecen congeladas; el observador no optimiza ni cambia la hipótesis.

La frontera forward se persiste antes de la primera descarga. Cada ejecución queda vinculada a HEAD, tree, configuración, política, fuente y versión de esquema. Las velas anteriores a la frontera solo calientan las features. SQLite opera con WAL, sincronización FULL, claves únicas, triggers de inmutabilidad, lease con fencing y una cadena hash append-only. La actualización de lifecycle, evento y checkpoint de cada vela cerrada ocurre dentro de una sola transacción.

La máquina de estados durable implementa rechazo final o el recorrido completo desde señal elegible hasta label finalizado. Conserva una posición planificada/abierta tras reinicio, aplica stop-first cuando SL y TP coinciden, mantiene abierta una ventana incompleta y falla de forma cerrada ante gaps, barras fuera de orden, conflictos de replay, valores no finitos, transiciones inválidas, huérfanos o pérdida temporal de datos públicos.

La reconciliación comprueba además una biyección 1:1 entre las barras forward procesadas hasta el checkpoint y sus lifecycles, la continuidad temporal, la partición de estados, las cardinalidades de entradas/salidas/outcomes/labels, la cadena de eventos y la ausencia de observaciones anteriores a la frontera. Solo una reconciliación `PASS` junto con un estado sano permite publicar `OBSERVER_CONNECTED` y `START_FORWARD_SHADOW_NOW`; cualquier fallo publica el bloqueo fail-closed.

## Integración continua y dashboard

El proceso continuo del escáner público conecta exactamente una instancia del observador mediante un hook sin argumentos: el observador obtiene sus propias velas de Bitget y no recibe barras ni decisiones del escáner. También existen los comandos públicos aislados `p11-forward-observer-once` y `p11-forward-observer-run`, despachados antes de cargar configuración privada, `.env` o base de datos productiva.

El dashboard V10.43c consume únicamente el snapshot atómico `observer_status.json`; no importa, inicia ni modifica el runtime. Expone identidad, frontera, heartbeat, checkpoint, lifecycle, reconciliación, errores, HEAD/tree y fingerprints. Hasta que exista al menos un outcome finalizado, las métricas económicas y `forward_n_eff` aparecen como `N/A`, no como cero. Los enlaces locales apuntan a ledger, outcomes, labels, reconciliación y resumen publicados por el observador.

## Evidencia local previa a la activación

- 21 pruebas adversariales del core cubren frontera, forming bars, rechazo, señal real P11, entrada, TP, SL, TIME=15, stop-first, gap-through, restart, idempotencia, conflictos, lease/fencing, huérfanos, unicidad, outage/recovery, orden temporal, costes no finitos, transiciones inválidas, reconciliación exacta, `n_eff`, seguridad, HTTP 503, propagación de IDs y finalización.
- La validación combinada relevante terminó con 142 pruebas superadas; las rutas generales afectadas añadieron otras 39 pruebas superadas.
- La compilación integral de `app`, `scripts` y `tests` terminó correctamente.
- La auditoría estática/dinámica no encontró imports ni llamadas a órdenes, endpoints privados, wallet, `.env` u holdout en la ruta del observador.
- Una comprobación pública real obtuvo 5.800 velas 1m cerradas y 385 velas 15m continuas, sin gaps; el agregador local fue byte-equivalente al de referencia, con SHA-256 `a2ffe24ccc240eb299612712245c3abaa912372f04e6eb5ea13acf1226dfc18c`.

## Criterio de activación

La activación operativa debe ejecutarse únicamente después del commit final de código, pruebas, dashboard y documentación, de modo que la frontera congele un HEAD/tree limpio y completo. El resultado seguirá siendo `NO_CONFIRMED_EDGE`: observar no equivale a demostrar rentabilidad, validar la hipótesis ni autorizar promoción, paper trading o live trading.
