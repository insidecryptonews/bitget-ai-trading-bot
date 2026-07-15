# BITGET AI TRADING BOT — OPERATIONAL ACTIVATION REVIEW

**Fecha de revisión:** 2026-07-15  
**Corte de la evidencia dinámica:** 2026-07-15 15:37:28Z, salvo que se indique otro instante  
**Rama:** `local-v10-47-8-scientific-repair`  
**HEAD:** `81d8b0b07c93b13a28cca75c220b4def79ac68b1`  
**Tree:** `6c0775620c45e28939c23692593a558dbe9f0e16`  
**Alcance:** revisión operativa y cuantitativa; sin cambios de código, `.env`, VPS, Railway, órdenes, capital, leverage ni holdout  
**Seguridad local auditada/default:** `PAPER_TRADING=true`, `LIVE_TRADING=false`, `DRY_RUN=true`, `can_send_real_orders=false`; estado remoto `MISSING_DATA`

Esta revisión no reabre la certificación V10.47.25. Parte de su resultado
`PASS WITH LIMITATIONS`, mantiene el holdout sellado y responde otra pregunta:
cuál es el camino más corto que convierte el trabajo existente en observaciones
forward con señal, posición virtual, cierre, costes y label comparables.

Los conteos de colectores y escáner son acumulativos. Para evitar que un proceso
activo cambie los denominadores durante la lectura, todas las cifras dinámicas se
cierran en el instante indicado arriba. “Trade V10.47” significa simulación causal
en discovery; “paper trade” significa una posición persistida por el runtime paper;
“shadow trade” significa una posición contrafactual completa que nunca envía una
orden. No son categorías intercambiables.

---

## 1. Estado real actual

El bot tiene una infraestructura científica certificada, datos de mercado en
acumulación y un escáner continuo, pero **no tiene una política V10.47 conectada a
un lifecycle forward**. En el host auditado están activos colectores públicos, el
escáner V10.28 y el watcher del dashboard; no está activo `python -m app.main`.
El último log local del runtime paper termina el 2 de mayo. Si existe una instancia
remota adicional, no hay DB/log/export local reciente que permita acreditarla.

La campaña científica autoritativa V10.47.25 contiene:

| Estado | Resultados | Porcentaje de 564 |
|---|---:|---:|
| `NO_GROSS_EDGE` | 399 | 70,74% |
| `GROSS_EDGE_COST_KILLED` | 146 | 25,89% |
| `NET_EDGE_POSITIVE` en TRAIN | 19 | 3,37% |
| Admitidos por validation | 0 | 0% |
| Walk-forward completado | 0 | 0% |
| Shadow candidates promovidos | 0 | 0% |

La cifra histórica de **21** net-positive pertenece a builds anteriores. La
reconciliación final, con categorías disjuntas, es **19**. Además, identificadores
como `P11` y `P11_SHORT` pueden ser duplicados conductuales; no representan 19
edges independientes.

La base local `bot_state.db` tampoco es un ledger operativo actual: tiene 43
observaciones `TEST`, cuatro trades `TEST`, cero labels, cero outcomes, cero
shadow candidates y cero filas de latencia. El escáner/colectores declaran
`makes_no_trades=true`/`no_orders=true`; el dashboard serializa
`edge_validated=false` y `paper_filter_enabled=false`. Lo que no muestra con
suficiente claridad es que el escáner y el runtime paper son dos circuitos
distintos.

**Conclusión del estado:** existe una candidata defendible para recopilar shadow,
pero no existe todavía el cableado que la observe de forma continua y no existe
base para abrir paper positions con ella.

---

## 2. Cronología verificable de los dos o tres meses

No se acredita una actividad paper continua de dos o tres meses. Sí se acredita
un proyecto de aproximadamente 75 días con un episodio paper corto, desarrollo
intenso, replays y, desde julio, adquisición forward de mercado y emisiones de
un escáner estable.

| Periodo | Hechos comprobados | Política/código congelado | Clasificación | Uso válido |
|---|---|---|---|---|
| 1 mayo 05:10–2 mayo 04:12 local | 180 ciclos, 1.800 evaluaciones, 17 selecciones, 11 bloqueos de riesgo, 6 `PAPER_OPEN`, 0 cierres | No hay prueba de freeze previo; seis sesiones/restarts agrupadas en dos bloques, con hueco máximo de 21h16m53s | `PARTIALLY_USABLE_EVIDENCE` para funcionamiento; `INVALID_FOR_EDGE` para retorno | Frecuencia de señal, allocator, risk y capacidad de apertura |
| Export 3 mayo | Snapshot de 160 observaciones en 7m31s, 159 `NO_TRADE`, una LONG operada, 6 posiciones aún abiertas, 0 labels | Snapshot de la política original LONG-heavy | `PARTIALLY_USABLE_EVIDENCE` | Auditoría de campos y costes estimados de apertura |
| 2 mayo–30 junio | Decenas de módulos y gates añadidos; cambios continuos de implementación; replays V8–V10 | No. El historial Git completo tiene 217 commits hasta el corte (65 en mayo, 79 en junio y 73 en julio) | `DEVELOPMENT_CONTAMINATED` | Diseño, depuración y prior informativo, no confirmación forward |
| 6 mayo–4 junio, DB/vault; export de vault 8 junio | 43 señales y 4 trades `TEST`; vaults con `SMOKE*`, precios 50/100 y labels sintéticos | Fixtures y smoke tests | `INVALID_FOR_EDGE` | Pruebas técnicas exclusivamente; el 8 de junio es fecha de export, no de actividad |
| 20 junio–1 julio | Backfills, replays, diarios de régimen y laboratorios de shadow | Políticas cambiantes y ejecución retrospectiva | `DEVELOPMENT_CONTAMINATED` | Cobertura de datos y diagnóstico |
| Desde 2 julio 00:43Z | Colector Binance BTC forward: 1.611 ciclos, 1.594.517 trades, 4.568 orderbooks, 4.422 OI, 535 funding, 0 liquidations al corte cercano | Colector estable, no política de trading | `VALID_FORWARD_EVIDENCE` de adquisición; `INVALID_FOR_EDGE` de estrategia | Calidad, continuidad y futuros costes/microestructura |
| Desde 3 julio 03:02Z | Escáner V10.28: 9.337 scans hasta 15:37Z; core sin cambios desde antes del primer scan | Sí para la heurística del escáner | `VALID_FORWARD_EVIDENCE` de emisiones; `INVALID_FOR_EDGE` de performance | Uptime, frecuencia y razones de abstención |
| Desde 5 julio | Colector Bybit BTC: 1.464 ciclos, 1.459.249 trades, 4.392 orderbooks, 3.139 OI, 230 funding, 5.640 liquidations al corte cercano | Colector estable, no política | `VALID_FORWARD_EVIDENCE` de mercado | Control cross-venue y ejecución futura |
| 8–15 julio | WS persistente BTC, dashboard y laboratorios; existen reinicios/gaps | Fuente en evolución | `PARTIALLY_USABLE_EVIDENCE` | Diagnóstico de continuidad, no edge |
| 13–15 julio, V10.46/V10.47 | Backtests causales, torneos, reparación y certificación | Sí dentro de cada ejecución cerrada | `DEVELOPMENT_CONTAMINATED` para selección; evidencia científica válida de rechazo | Ranking de hipótesis y diseño del siguiente forward |

Dos aclaraciones evitan “reiniciar el reloj” de forma incorrecta:

1. Los 12,52 días transcurridos del escáner y las coberturas V10.29 separadas
   —12,66 días de trades, 13,62 de orderbook y 15,35 de OI— sí cuentan para
   continuidad operativa y preparación de datos, no como cobertura densa única.
2. No cuentan como días de rendimiento de P11, porque el escáner no ejecuta P11,
   no deduplica oportunidades como posiciones y no persiste cierres/outcomes.

---

## 3. Evidencia forward realmente utilizable

### 3.1 Episodio paper de mayo

`logs/bot.log` demuestra que el sistema podía consultar mercado, generar señales,
seleccionar candidatos y abrir posiciones paper. Fueron 180 ciclos sobre diez
símbolos: BTC, ETH, SOL, XRP, DOGE, BNB, LINK, AVAX, ADA y DOT, exactamente 180
evaluaciones por símbolo.

- 257 señales direccionales, todas LONG; 1.543 `NO_TRADE`; cero SHORT.
- Regímenes: 1.700 `TREND_UP` y 100 `RISK_ON`.
- Estrategias: 82 PULLBACK, 67 TREND_FOLLOWING, 47 BREAKOUT, 33
  SUPPORT_RESISTANCE_REJECTION y 28 MOMENTUM_FAST; 1.543 NO_TRADE.
- El allocator eligió un candidato en 17 ciclos. Risk Manager bloqueó 11:
  cuatro por desviación de notional tras redondeo, tres por margen insuficiente
  y cuatro por posición ya abierta.
- Se abrieron seis posiciones, todas LONG: BTC 2, ETH 2, LINK 1 y SOL 1;
  BREAKOUT 3, MOMENTUM_FAST 2 y PULLBACK 1.
- No hay `PAPER_CLOSE`, label maduro ni PnL final.
- El export contiene `funding_rate`, `spread_pct` y `open_interest` completos en
  sus 160 observaciones, pero no contiene latencia de ejecución ni fills de cierre.

Por tanto, es evidencia de funcionamiento parcial, no una muestra de edge.

### 3.2 Escáner de julio

Hasta el corte contiene 9.337 scans válidos, 15 sesiones y 13 fechas UTC. El
tiempo sumado de sesión es 190,342 horas frente a 300,578 horas transcurridas:
63,33% de continuidad, con 14 gaps superiores a diez minutos y un máximo de
68.413 segundos.

En 177.019 evaluaciones símbolo se registran:

- 27.013 decisiones-candidato por snapshot (15,26%);
- 149.722 filas `stayed_out` (84,58%);
- 284 descartes de calidad (0,16%);
- 15.449 decisiones LONG y 11.564 SHORT;
- 97 scans sin candidato, 164 con uno, 379 con dos y 8.697 con tres.

Todas usan timeframe **15m** y lookback **7d**. Las 27.013 decisiones se reparten
por símbolo así: ADA 2.393; SOL 2.326; BTC 2.074; AVAX 1.922; BCH 1.852; XRP
1.734; DOT 1.670; NEAR 1.662; LTC 1.642; ETH 1.435; OP 1.243; APT 1.172; BNB
1.109; ATOM 1.109; INJ 1.076; DOGE 909; ARB 899; SUI 494; LINK 292.

El campo de régimen no está serializado en `decisions`; sólo puede contabilizarse
en la lista `top`, una unidad distinta que no debe confundirse con operaciones:
24.637 tags `RISK_ON_RECOVERY`, 9.464 `LONG_BLOCKED`, 9.095
`RISK_OFF_EARLY_WARNING`, 2.829 `NO_EDGE` y 560 `RANGE_NO_TRADE`.

Estas 27.013 filas no son 27.013 oportunidades independientes: el mismo setup
puede reaparecer cada ~70 segundos, no existe apertura virtual persistente y no
hay cierre, coste, MFE, MAE, funding de la posición ni label. Son válidas para
medir emisión y abstención, pero su `n_eff` de rendimiento es cero.

### 3.3 Datos públicos forward

Los colectores Binance/Bybit contienen observaciones prospectivas de trades,
book, OI, funding y liquidaciones. Reducen el trabajo pendiente de data readiness,
pero el dataset Binance presenta 15 huecos superiores a una hora y el estado
V10.29 lo declara demasiado gappy para fine backtest/shadow-forward. Además,
parte de funding es backfill: sólo 40 de 535 filas Binance eran posteriores al
inicio del colector en el corte revisado. No se deben convertir 178 días de
funding descargado en 178 días forward.

El readiness V10.29 separa la cobertura efectiva: trades 12,66/30 días
(déficit 17,34), orderbook 13,62/30 (déficit 16,38), OI 15,35/30 (déficit
14,65) y liquidaciones 0/20 filas y 0/30 días. Mientras no existan frames de
liquidación no hay ETA defendible para ese requisito. El valor aproximado 13,6
describe span/eventos, no 13,6 días densos intercambiables entre feeds, y ninguno
de estos relojes cuenta como rendimiento P11.

### 3.4 Total acreditable para una política de rendimiento

| Unidad | Acumulado válido |
|---|---:|
| Operaciones paper/shadow cerradas con política congelada y outcome | **0** |
| Labels forward válidos de estrategia | **0** |
| `n_eff` forward de estrategia | **0** |
| Días forward del escáner como emisión estable | **12,52** |
| Días forward de P11 con lifecycle completo | **0** |

---

## 4. Evidencia contaminada, inválida o ausente

1. **DB local.** Las 43 observaciones y cuatro trades coinciden con fixtures de
   `tests/test_researchops_v8_2_1.py`: estrategia `TEST`, entradas 100, PnL,
   fees y slippage cero. Son `INVALID_FOR_EDGE`.
2. **Vaults de entrenamiento.** Los 110 labels del paquete de mayo llevan IDs
   `SMOKE*` y precios sintéticos; el paquete de junio contiene 36 observaciones
   y cuatro trades `TEST`. No son labels de mercado.
3. **V10.8–V10.12.** Son ejecuciones retrospectivas/offline o Temp/pytest. En
   los seis resúmenes V10.12 asociados al repo hubo 8.216 setups raw y cero
   shadow trades; no fue un forward lifecycle.
4. **Diario de régimen V10.21.** De 102 filas, 100 proceden de Temp/pytest y las
   dos reales duplican el mismo snapshot. No constituye una serie temporal.
5. **Shadow simulation V10.40.** Replay retrospectivo: 820 señales, 389 outcomes
   simulados y 431 filas `DATA_GAP`; no es evidencia prospectiva.
6. **V10.46/V10.47.** Son backtests de selección y validación científica, útiles
   para escoger una hipótesis y rechazar sobreclaims. No son paper-forward.
7. **Antiguas P08 DOGE/XRP.** Los positivos provisionales quedaron invalidados
   por `LAST_SIGNAL_CLUSTER_OVERWRITE`; al usar el primer evento causal, las
   cifras cambiaron de positivas a negativas. No se rescatan.
8. **Narrativa de ~134.960 observaciones.** Aparece en un audit de mayo, pero la
   base/vault que permitiría verificarla ya no está en el workspace. Se clasifica
   `MISSING_DATA`, no cero y tampoco evidencia acreditada.
9. **Pre-Move, Time Death, Exit Calibration, Exit Simulation y Latency.** Las
   tablas operativas necesarias están vacías en la DB actual. Sus diseños existen,
   pero no hay muestra forward local que soporte una promoción.

---

## 5. Número real de señales, decisiones y operaciones

| Fuente | Señales/evaluaciones | Decisiones elegibles | Aperturas | Cierres | Labels válidos | Interpretación |
|---|---:|---:|---:|---:|---:|---|
| Log paper 1–2 mayo | 1.800; 257 direccionales | 17 selecciones | 6 | 0 | 0 | Operación parcial real del runtime |
| Export 3 mayo | 160 en 7m31s; 159 NO_TRADE | 1 operada | Las mismas 6 seguían abiertas | 0 | 0 | Snapshot, no periodo independiente |
| `bot_state.db` actual | 43 `TEST` | 0 | 4 filas trade insertadas ya cerradas | Lifecycle no acreditable (`CLOSED_TP` 2, `CLOSED_SL` 2) | 0 | Fixture; excluir |
| Escáner 3–15 julio | 177.019 evaluaciones | 27.013 snapshots candidato | 0 | 0 | 0 | Emisión forward, no lifecycle |
| P11 BTC 15m V10.47 TRAIN | 101 raw | 35 simuladas | 35 SimOMS | 35 SimOMS | No forward | Discovery |
| P11 BTC 15m V10.47 validation diagnóstica | 18 raw | 9 simuladas | 9 SimOMS | 9 SimOMS | No forward | Post-selección, no admitida |

Los costes disponibles en mayo sólo corresponden a apertura: fees estimadas
€0,472003 y slippage estimado €0,118001 para las seis posiciones; no existe fee
de salida ni retorno neto final. En V10.47 los costes son **modelados**, aunque
el escenario se denomine `observed`: no son fills paper observados.

---

## 6. Por qué el bot no opera y dónde se pierde exactamente

La causa de cero actividad útil actual no es una única frase “falta edge”. Hay
dos niveles diferentes:

- **Nivel operativo:** ningún proceso continuo ejecuta P11 como observador con
  posición virtual, cierre y label. El escáner está codificado con
  `executed=false`, `not_actionable=true`, `no_orders=true`; el runtime genérico
  no está activo localmente.
- **Nivel de promoción:** aun si P11 se cableara, la evidencia existente no
  autoriza paper positions: baseline incompleto, `n_eff` insuficiente,
  concentración y ausencia de walk-forward.

Los estados se interpretan así: `NO_VALID_CANDIDATES` y
`no_actionable_candidates=true` describen el routing no accionable del escáner,
no ausencia de emisiones; `PAPER_ONLY` es una barrera de seguridad local, no un
motor de señales; cero candidatos promovidos procede de los gates científicos.

| Diagnóstico A–G | Resultado |
|---|---|
| A. No existe política seleccionada | **Sí**, P11 no está seleccionada/ruteada en el ciclo continuo |
| B. Política deliberadamente desactivada | Parcial: el escáner sí es deliberadamente no accionable; P11 ni siquiera está conectada |
| C. Gates impiden todas las entradas | **Sí para promoción paper**, no deben impedir recopilar shadow |
| D. Criterios demasiado estrictos | No demostrado; relajarlos fabricaría comparabilidad. El problema inmediato es lifecycle ausente |
| E. No hay señales reales | Falso: existen señales, pero no outcomes comparables |
| F. Problema de configuración | `app.main` ausente localmente; no hay evidencia de que un `.env` concreto sea la causa ni estado remoto verificable |
| G. Dashboard incorrecto | No causal; mezcla estados de circuitos y necesita separar scanner/runtime/observer |

| Causa solicitada | Frecuencia/impacto comprobado | ¿Sigue vigente? | Evidencia | Corrección mínima |
|---|---|---|---|---|
| No se generan señales | Falso | No | 257 direccionales en mayo; 27.013 snapshots en julio; P11 119 raw TRAIN+validation | No tocar umbrales para “crear” señales |
| Señales demasiado restrictivas | P11 produce 1,1–2,2 raw/día según ventana; el escáner produjo candidatos en 9.240/9.337 scans | Parcial para P11, no para escáner | Contadores causales y scanner log | Medir P11, no relajar antes del forward |
| Validation rechaza | De 19 net-positive: 10 por net negativo, 5 por `n_eff`, 4 sin trades; 0 admitidos | Sí | Gates V10.47.25 | Shadow congelado; no saltarse validation |
| Baseline incompleto | 0/19 candidatos con cobertura completa; 3/172 pares nominales aceptados, incluyendo duplicados conductuales P11/P11_SHORT | Sí, severo | `matched_random_paired` | Preregistrar no-trade, gemelo de integridad y placebo retrasado; certificar su nuevo contrato antes de promoción |
| Costes destruyen edge | 146/165 resultados con gross edge, 88,48%, mueren por coste | Sí | Clasificación disjunta final | Mantener SimOMS y escenarios observado/conservador; no bajar costes ficticiamente |
| `n_eff` insuficiente | 19/19 fallan en selección y 19/19 en validation | Sí | Gate actual requiere 30 | Acumular outcomes deduplicados, no scans repetidos |
| Filtros incompatibles | El baseline independiente raramente coincide exactamente; scanner apila razones correlacionadas | Sí | 1/35 matches para P11; razones del scanner | Rediseñar el baseline futuro y simplificar diagnóstico, sin relajar promoción |
| Entrada demasiado tardía | No hay métrica de delay/alpha decay serializada para P11 | `MISSING_DATA` | Latency DB = 0; ledger final no agrega delay | Registrar close de señal, next-open y p50/p95 de latencia |
| Objetivos poco realistas | No cuantificable con ledger final porque no agrega TP/SL/TIME por P11 | `MISSING_DATA` | MFE/MAE y exit counts no serializados | Persistir lifecycle; no cambiar TP antes de 30 cierres |
| TIME exits | Diagnósticos antiguos eran altos, pero la cifra P11 actual no está publicada | Parcial/pasado | Labs Time Death y V10.43/44 | Monitor, no gate de emisión; cohortes separadas si se cambia exit |
| Mala dirección | La política original fue 100% LONG; Trend Rider amplio perdió. P11 SHORT es el mejor lead, pero no está ligado a RISK_OFF | Sí para política original; no demostrado para todo SHORT | Logs mayo y V10.46/V10.47 | Shadow SHORT BTC únicamente |
| Malos símbolos | DOGE/XRP leads antiguos se invalidaron; ETH P11 queda net negativo por coste | Sí para esos leads | Reparación causal y torneo final | BTC único; ETH sólo control de costes |
| Regímenes incorrectos | P11 no implementa `RISK_OFF`/`TREND_DOWN`; “agotamiento tras subida rápida” sólo describe el setup, ledger `UNSPECIFIED` | Sí como limitación de interpretación | Regla P11 | Registrar régimen como metadata/estrato, no añadir un filtro post hoc |
| Datos insuficientes/gappy | 12,52 días de scanner con 63,33% de sesión; V10.29 marca gaps | Sí | Scanner y status V10.29 | Fuente OHLCV cerrada, gap gate y heartbeat para el observer |
| Bugs ya corregidos | P08/cluster overwrite y fallos de pairing/campaign se corrigieron | Pasado | V10.47.8–25 | No reutilizar artefactos invalidados |
| Política paper desactivada | Los flags Edge/Paper están false, pero false es permisivo para Edge y Orchestrator ni se llama desde main | No es la causa causal | `app/main.py`, config y orchestrator | No encender flags; no generan P11 |
| Ausencia de estrategia promovible | P11 existe sólo como hipótesis de laboratorio; cero shadow candidate | Sí, causa científica | `shadow_candidates=0` | Observarla como hipótesis, no declararla promovida |
| Ausencia de routing/lifecycle | 100% de las 27.013 decisiones del scanner no se ejecutan; P11 no se importa en main | Sí, causa operativa principal | Procesos, scanner y `app/main.py` | Un observer append-only P11/SimOMS sin órdenes |

La arquitectura paper existente tampoco es un sustituto: `PaperTrader` no tiene
time exit, aplica TP1→break-even y TP2, y sólo descuenta fee de entrada. P11 fue
evaluada con un TP único, sin break-even/trailing, fee de entrada y salida,
spread, slippage y funding. Enviar P11 a `PaperTrader` cambiaría la política.

---

## 7. Mejor configuración existente

La mejor configuración disponible para **aprender forward**, no para declarar
rentabilidad, es:

`BTCUSDT · Bitget · 15m · P11_SHORT`  
Setup descriptivo: `HIGH_VOL_UP_EXHAUSTION`; régimen oficial: `UNSPECIFIED`.

P11 no detecta crash ni tendencia bajista. Opera SHORT después de una subida
rápida: percentil de true range alto, retorno positivo de 15 barras y mecha
superior. La condición de mecha usa unidades absolutas (`> 0.001`), no porcentaje;
en BTC es muy permisiva y no debe extrapolarse a DOGE/XRP.

### Reglas existentes

- `atr_pct > 0.85`, donde `atr_pct` es el rango percentil del true range actual
  frente a los últimos `min(240, N)` true ranges.
- `close[-1] / close[-16] - 1 > 0.01`.
- `high[-1] - max(open[-1], close[-1]) > 0.001`.
- Entrada SHORT en el open de la siguiente vela 15m.
- Stop +0,8%, target −1,2%, time exit 15 velas (3h45m).
- Una posición, primer evento causal por cluster horario, stop antes que TP si
  ambos aparecen en una vela.

### Métricas

| Métrica | TRAIN, 46 días | Validation diagnóstica, 16 días |
|---|---:|---:|
| Señales raw | 101 | 18 |
| Trades SimOMS | 35 | 9 |
| Bloqueadas por posición/cooldown | 66 (65,35%) | 9 (50,00%) |
| `n_eff` | 20,0000 | 6,1723 |
| Gross PnL | €0,469131 | €0,127400 |
| Gross EV/trade | €0,013404 | €0,014156 |
| Coste total | €0,305000 | €0,078000 |
| Coste/trade | €0,008714 | €0,008667 |
| Net PnL | €0,164131 | €0,049400 |
| Net EV/trade | €0,004689 | €0,005489 |
| Net sin top-3 | €0,009631 | **−€0,104600** |
| Neto conservador | €0,072881 | No serializado |
| Baseline exacto | 1/35, 2,86% | No admitido |

El resultado positivo sobrevive al escenario conservador en TRAIN por €0,072881,
pero en validation depende de los mejores eventos. El coste consume el 65,01%
del gross PnL de TRAIN. Es el mejor lead existente, no edge confirmado.

---

## 8. Ranking de tres candidatas y descartes

El ranking siguiente es de **prioridad operativa de investigación**. Sólo la
primera merece un observer forward primario; la segunda es un control de costes
y la tercera un challenger diagnóstico. No hay tres candidatas paper.

| Rango | Configuración | Frecuencia TRAIN | Economía TRAIN | Evidencia posterior | Decisión |
|---:|---|---|---|---|---|
| 1 | BTCUSDT 15m P11_SHORT | 2,196 raw/día; 0,761 trades/día; 5,33/semana; 65,35% bloqueadas | gross EV €0,013404; coste €0,008714; net EV €0,004689; `n_eff=20`; net sin top-3 +€0,009631 | Validation net +€0,0494, pero `n_eff=6,1723`, sin top-3 −€0,1046 y baseline 2,86% | **Observer shadow principal** |
| 2 | ETHUSDT 15m P11_SHORT | 2,311 raw/día; 1,022 trades/día; 7,16/semana; 55,77% bloqueadas | gross EV €0,008002; coste €0,008630; net EV −€0,000628; `n_eff=25`; net sin top-3 −€0,183396 | `GROSS_EDGE_COST_KILLED`; no validation | **Comparador del modelo de costes, sin posición principal** |
| 3 | BTCUSDT 15m TR_B_pullback | 0,891 raw/día; 0,435 trades/día; 3,04/semana; 51,22% bloqueadas | gross EV €0,014386; coste €0,008800; net EV €0,005586; `n_eff=15`; net sin top-3 −€0,042789 | Validation: 14 trades, net EV −€0,004114 y sin top-3 −€0,211601 | **Challenger rechazado; conservar como comparador** |

TR_B fue bidireccional en TRAIN, 10 LONG y 10 SHORT. Su cambio de signo en
validation impide usarlo para actividad paper aunque su frecuencia sea útil.

### Heterogeneidad por timeframe; no es un test de sensibilidad controlado

No existe un sweep paramétrico limpio de P11, por lo que la sensibilidad a
0,85/1%/0,8%/1,2% es `MISSING`. La comparación siguiente muestra
heterogeneidad/no-portabilidad entre timeframes; no aísla causalmente el efecto
del timeframe ni sustituye una sensibilidad preregistrada:

| Configuración | Net EV 1m | Net EV 5m | Net EV 15m TRAIN |
|---|---:|---:|---:|
| BTC P11_SHORT | −€0,008750 | −€0,010684 | **+€0,004689** |
| ETH P11_SHORT | −€0,009223 | −€0,011772 | −€0,000628 |
| BTC TR_B_pullback | −€0,009306 | −€0,011532 | **+€0,005586** |

Esto prohíbe generalizar “P11 funciona” fuera de BTC 15m.

### Configuraciones descartadas

- **ETH 15m P09_SHORT:** tres trades TRAIN y uno de validation; `n_eff` 3/1.
  Demasiado escaso.
- **BTC 15m P09_SHORT:** cinco trades TRAIN con net sin top-3 negativo y dos
  trades validation con net −€0,077.
- **XRP 1m P11_SHORT:** TRAIN net EV +€0,001510, pero validation net
  −€0,410036 total; además no sobrevive costes conservadores/top-3.
- **DOGE P10/P10_SHORT:** uno o ningún trade; no evidencia.
- **DOGE/XRP P08 antiguos:** invalidados por el bug causal de cluster.
- **LONG original:** seis posiciones sin cierres en mayo y Trend Rider amplio
  negativo; no hay base forward para reactivarlo.
- **Trend Rider V10.46:** 5m A perdió €8,516733 en 556 trades; 15m A perdió
  €2,521 en 225. El learner evitó operar, no aprendió a ganar.
- **Pre-Move y exit recalibration:** tablas forward vacías y variantes de salida
  sin rescate neto validado. Un exit no fabrica edge de entrada.

Win rate, payoff, profit factor, max drawdown, distribución de motivos de salida
y sensibilidad paramétrica de los tres resultados V10.47 actuales no están
serializados. Se marcan `MISSING`; no se sustituyen por ceros ni por cifras de
artefactos invalidados.

---

## 9. Política mínima operativa

**Nombre:** `MINIMUM_ACTIONABLE_PAPER_POLICY_V1`  
**Estado inicial obligatorio:** `FORWARD_SHADOW_ONLY`  
**Implementación económica:** reutilizar literalmente P11 + causal ledger +
SimOMS; no usar `PaperTrader` ni `ExecutionEngine`.

Aunque el nombre contenga “PAPER”, esta versión no abre paper positions. El
nombre identifica el candidato cuyo objetivo final sería paper; su primera
cohorte es shadow.

### Especificación determinista

| Campo | Regla exacta |
|---|---|
| Símbolo | `BTCUSDT` únicamente |
| Venue/contrato | `bitget`, producto USDT futures. La referencia P11 oficial usa Bitget; Binance/Bybit sólo pueden ser diagnóstico. Sustituir venue crea otra identidad y otro dataset |
| Fuente pública | `GET api.bitget.com/api/v2/mix/market/history-candles`, `productType=usdt-futures`, `granularity=1m`, sin autenticación; manifest/generación fijados al freeze |
| Construcción 15m | 15 barras 1m UTC consecutivas y alineadas a `floor(epoch_ms/900000)`; open de la primera, high máximo, low mínimo, close de la última, volume/turnover sumados; timestamp del inicio del bucket; sólo publicar si `bucket_end<=as_of`; bucket incompleto, duplicado, desordenado o gappy se rechaza |
| Side | `SHORT` únicamente |
| Timeframe de régimen | `N/A`: ledger oficial `UNSPECIFIED`; `HIGH_VOL_UP_EXHAUSTION` es nombre descriptivo del setup, no un gate validado. Metadata 4h/1h no bloqueante |
| Timeframe de preparación | 15m cerrada |
| Timeframe de decisión/entrada | decisión al cierre 15m; entrada contrafactual en el siguiente open 15m |
| Ventana | El decider conserva como máximo 260 velas; exige `decision_index>=60`: 60 velas 15m cerradas, consecutivas y anteriores más la vela cerrada de decisión; el siguiente open debe existir. El percentil TR usa hasta 240 valores disponibles |
| Indicador 1 | `TR_t=max(high_t-low_t, abs(high_t-close_{t-1}), abs(low_t-close_{t-1}))` |
| Indicador 2 | `atr_pct=count(TR_i<=TR_t)/N`, `i` en los últimos `N=min(240, disponibles)` |
| Indicador 3 | `ret_15=close_t/close_{t-15}-1` |
| Indicador 4 | `upper_wick=high_t-max(open_t,close_t)` en unidades de precio BTC |
| Setup | `atr_pct>0.85 AND ret_15>0.01 AND upper_wick>0.001` |
| Confirmación | El propio cierre de la vela que satisface las tres condiciones; no se añade score, RSI, régimen ni volumen |
| Entrada | Market/taker al open raw de la siguiente vela; precio económico ajustado por spread/slippage del escenario |
| Exposición | €5 notional, 1x, una posición máxima |
| Stop | `entry_raw*1.008` |
| TP1 | `entry_raw*0.988`; cierra el 100% |
| TP2 | Desactivado |
| Parcial | 0%; desactivado |
| Breakeven | Desactivado |
| Trailing | Desactivado |
| Time exit | Cierre de la vela 15 de exposición, contando la vela de entrada: 225 minutos |
| Prioridad intrabar | Stop antes que TP; si hay gap a través del stop/TP, fill al open de la vela |
| Cooldown | Primer evento causal por cluster horario; se siguen evaluando y registrando `RAW_TRIGGER` durante posición abierta, pero no hay nueva entrada ni reingreso en el mismo cluster |
| Coste base | Taker 6 bps por lado; spread total 1 bp roundtrip; slippage 2 bps por lado; total habitual 17 bps roundtrip |
| Funding base | 1 bp por settlement de 8h realmente cruzado; máximo temporal P11 < 8h, aunque puede cruzar un settlement |
| Escenario conservador paralelo | Taker 6 bps/lado; spread 2 bps roundtrip; slippage 4 bps/lado; funding 1,5 bps/settlement |
| No operar | Setup falso; vela no cerrada; `decision_index<60`; timestamps desordenados/duplicados; gap de 15m; OHLC inválido; falta siguiente open; posición abierta; cluster consumido; hash/spec distinto; coste no calculable |

El régimen 4h/1h se registra como metadata para estratificación, pero no bloquea.
Agregar ahora `RISK_OFF` o `TREND_DOWN` cambiaría la hipótesis después de verla y
haría incomparables los 44 trades de referencia.

---

## 10. WARREN_MTF_CONFLUENCE_V1 formalizada

Warren se trata como hipótesis nueva e independiente. No se usan estadísticas
de WarrenAI ni resultados manuales como si fueran una muestra. La especificación
es SHORT-only porque los smokes MTF recientes emitieron sólo SHORT y el lead
P11 es SHORT; esto no demuestra que SHORT tenga edge.

**Nombre:** `WARREN_MTF_CONFLUENCE_V1_SHORT_RESEARCH`  
**Símbolos:** BTCUSDT y ETHUSDT  
**Venue/datos:** Bitget USDT futures público; 1h/4h agregadas causalmente desde
1m con la misma convención UTC estricta de la sección 9  
**Máximo:** una posición total  
**Modo:** research/shadow, €5, 1x, sin órdenes

Convención determinista de indicadores: cada cálculo usa sólo velas cerradas.
`EMA_n` se inicializa con la SMA de los primeros `n` closes y después aplica
`EMA_t=EMA_{t-1}+2/(n+1)*(close_t-EMA_{t-1})`. TR usa la fórmula de la sección 9;
ATR14 es Wilder/RMA, semilla SMA de los primeros 14 TR y recurrencia
`RMA_t=((n-1)*RMA_{t-1}+x_t)/n`. RSI14 usa la misma RMA sobre ganancias/pérdidas
(ambas cero → 50; pérdida cero → 100). `+DM/-DM`, `+DI/-DI`, DX y ADX14 usan la
definición de Wilder y la misma semilla/recurrencia. Para MACD, EMA12 y EMA26
usan esa convención; `MACD=EMA12-EMA26`; signal9 se inicializa con la SMA de los
primeros nueve MACD disponibles y luego usa EMA con `alpha=2/10`; histograma es
`MACD-signal`. Indicadores no inicializados producen abstención, nunca un cero.

### Régimen 4h

Usar únicamente la última vela 4h completamente cerrada cuyo `close_ts` sea
menor o igual al `open_ts` de la vela 1h evaluada. Requiere 201 velas 4h
cerradas y consecutivas para reproducir literalmente `regime_ready` existente.

1. `close_4h < EMA50_4h < EMA200_4h`.
2. `ADX14_4h >= 20`.
3. `minus_DI14_4h > plus_DI14_4h`.
4. Estructura bajista exacta:
   `max(high[-5:]) < max(high[-10:-5])` y
   `min(low[-5:]) < min(low[-10:-5])`.

### Preparación 1h

Usar sólo velas 1h cerradas bajo el régimen 4h vigente:

1. `close_1h < EMA50_1h`.
2. `abs(close_1h-EMA50_1h)/ATR14_1h <= 1.0`.
3. `RSI14_prev > 55` y `RSI14_actual <= 55`.
4. MACD estándar 12/26/9: `hist_actual <= 0` y
   `hist_actual < hist_prev`.
5. Volatilidad: el percentil de `ATR14/close` dentro de las últimas 200 velas
   1h está entre 0,20 y 0,90, ambos incluidos.
6. Soporte/distancia al objetivo: sea
   `support20=min(low[-21:-1])`; exigir
   `close_1h-support20 >= 2*ATR14_1h`.

La preparación expira después de cuatro velas 15m cerradas. También se cancela
si el régimen 4h deja de cumplirse o aparece un cierre 1h `>= EMA50_1h`.

### Confirmación y entrada 15m

En una de las cuatro velas posteriores a la preparación:

1. `donchian_low20=min(low[-21:-1])`.
2. `close_15m <= donchian_low20`.
3. `close_15m < open_15m`.
4. `donchian_low20-close_15m <= ATR14_15m` para no perseguir una ruptura ya
   extendida más de un ATR.
5. `mean(volume[-5:])/mean(volume[-30:]) > 1.30`.
6. Entrada SHORT en el open de la siguiente vela 15m.
7. En ese open, exigir que la distancia hasta `support20_1h` sea al menos
   `6*ATR14_15m + coste_round_trip_en_precio`; si el gap elimina esa distancia,
   cancelar la entrada. Así el TP2 de 3R no se coloca atravesando soporte ya
   conocido ni se confunde un breakout sin recorrido con oportunidad operable.
8. Calcular antes de entrar
   `net_rr_conservative=PnL_conservador(TP1_y_TP2)/abs(PnL_conservador(stop_inicial))`
   sobre el notional completo, incluyendo entry, fills parciales y el funding
   programado que pueda cruzarse; exigir `net_rr_conservative>=1.40`. TP2 está a
   3R, pero 50% a 1R + 50% a 3R produce **2R bruto ponderado** si ambos ejecutan.

Si BTC y ETH confirman en el mismo timestamp y no hay posición, se ordenan por
la lista congelada `[BTCUSDT, ETHUSDT]` y sólo entra el primero; el otro queda
registrado como `PORTFOLIO_PRIORITY_SKIP`.

### Gestión exacta

- Riesgo inicial `R=2*ATR14_15m` fijado con datos cerrados de la confirmación.
- Stop inicial `entry+R`.
- TP1 `entry-R`: cerrar 50%.
- Tras TP1, el stop del 50% restante pasa a `entry` desde el open de la vela
  siguiente; nunca dentro de la misma vela.
- Trailing del remanente:
  `lowest_low_since_entry + 2*ATR14_15m_frozen_at_confirmation`, calculado al
  cierre y efectivo desde la siguiente vela; nunca puede ampliar el stop.
- TP2 `entry-3R`: cerrar todo el remanente.
- Time exit: cierre de la vela 24 de exposición, 6 horas.
- Prioridad si una vela toca ambos lados: stop activo primero, luego TP1 y TP2.
  Si abre más allá de un nivel, fill al open.
- Costes y funding: los mismos escenarios base/conservador de la política mínima,
  aplicados a cada fill parcial; no duplicar el coste del notional ya cerrado.
- No operar si falta una barra, hay mapping MTF no causal, gap, duplicado,
  indicador no finito, distancia al soporte insuficiente, coste no calculable,
  posición abierta o preparación expirada.

### ¿Puede probarse ya?

Los datos existentes permiten un **replay técnico de desarrollo**: hay 1.814
velas 1h y 453 velas 4h por BTC en el artefacto actual, 75,60 días. Tras el
warmup EMA200 quedan aproximadamente 253 velas 4h. Eso no es confirmación y
cualquier resultado retrospectivo queda contaminado porque esta especificación
se escribió después de mirar P11 y los smokes MTF.

Sólo pueden reutilizarse piezas de bajo nivel de mapping, indicadores y entrada.
No existe un compositor contractual Warren 4h→1h→15m que implemente juntos
estructura, MACD, percentil ATR, soporte, prioridad multi-símbolo y R:R neto.
Tampoco existe en SimOMS la gestión 50%/breakeven/trailing/TP2. Un replay exacto
requiere un adaptador de señal y otro de exits; sin ambos sólo cabe probar inputs
aislados, no declarar frecuencia ni PnL de Warren.

| Política | Estado comparable actual | Evidencia |
|---|---|---|
| Runtime “actual” | SignalEngine genérico, no congelado ni conectado a P11 | Seis aperturas LONG sin cierre |
| Trend Rider | Evaluado | 5m y 15m net negativos; abstener domina |
| BTC P11_SHORT 15m | Lead de discovery | Net positivo pequeño, validation concentrada, sin baseline completo |
| Warren MTF | Especificación nueva | Métricas/frecuencia/PF/DD `MISSING`; sólo replay técnico posible |
| No-trade | Baseline económico | €0, drawdown 0; domina las políticas amplias perdedoras |
| Matched random | No comparable todavía | Coverage exacta incompleta; debe generarse prospectivamente |

---

## 11. Gates que bloquean actividad

Hay que separar gates de **emisión**, gates de **portfolio** y gates de
**promoción**. Un gate de promoción debe impedir el salto a paper, pero no debe
impedir recopilar un ledger shadow. Hoy esos papeles están mezclados.

### 11.1 Gates del escáner forward

Los motivos se apilan; sus porcentajes usan como denominador las 177.019
evaluaciones y no suman 100%.

| Gate/estado | Veces | Porcentaje | Razón científica | Beneficio | Coste en frecuencia | Acción |
|---|---:|---:|---|---|---|---|
| `not_actionable/no_orders` | 9.337/9.337 scans; 27.013/27.013 candidatos | **100%** | Seguridad explícita | Garantiza cero órdenes | Impide todo lifecycle, incluso virtual | Sustituir sólo para P11 por un observer SimOMS; mantener cero órdenes |
| `max_open_positions_reached` | 133.789 | 75,58% | Limitar concentración | Útil cuando hay posiciones reales/virtuales | En el scanner sólo limita top-3 por snapshot; pierde labels | Aplicarlo al estado de posiciones, no al tablero de observación |
| `no_directional_setup` | 74.234 | 41,94% | No operar sin side | Correcto | Se triplica con no-stop/no-RR | Conservar una razón raíz y monitorizar derivados |
| `no_stop_loss` | 74.234 | 41,94% | No riesgo desnudo | Correcto | Redundante cuando side es null | Derivar del mismo root cause |
| `rr_below_min` | 74.234 | 41,94% | RR mínimo 1,5 | Correcto para posición | Redundante cuando side/stop son null | No contar como fallo independiente |
| `edge_below_min(35<62)` | 57.577 | 32,53% | Evitar scores débiles | Reduce ruido heurístico | El score no es edge validado | Mantener en ese scanner, no aplicarlo a P11 |
| `edge_below_min(20<62)` | 15.640 | 8,84% | Igual | Igual | Igual | Igual |
| Correlación >0,8 | 5.160 | 2,91% | Limitar exposición común | Útil para portfolio | Bloquea observaciones contrafactuales | Monitor en shadow; hard gate sólo al abrir portfolio |

### 11.2 Gates de V10.47.25

| Etapa | Fallo | Porcentaje | Interpretación |
|---|---:|---:|---|
| Sin gross edge | 399/564 | 70,74% | No hay señal económica antes de costes |
| Gross edge destruido por costes | 146/564 | 25,89% | 88,48% de los 165 con gross edge no cubren costes |
| Sobrevive neto en TRAIN | 19/564 | 3,37% | Sólo candidatos a análisis, no promociones |
| `n_eff` TRAIN insuficiente | 19/19 | 100% | Ninguno llega a 30 |
| Baseline match completo | 0/19 | 100% falla | No existe comparación uno-a-uno completa |
| Beats matched baseline | 0/19 | 100% falla | No se demuestra superioridad |
| Net sin top-3 negativo | 11/19 | 57,89% | Concentración en eventos extremos |
| Conservador no positivo | 4/19 | 21,05% | Sensibilidad a coste |
| Validation no positiva | 14/19 | 73,68% | Diez negativas y cuatro sin trades |
| `n_eff` validation insuficiente | 19/19 | 100% | Incluso cinco positivas son demasiado pequeñas |
| Walk-forward | 0 ejecutados | No aplicable | `false` significa no evaluado, no pérdida observada |

No se recomienda eliminar ninguno de estos gates de promoción. La corrección es
permitir que una hipótesis congelada genere evidencia shadow **antes** del gate,
no declararla aprobada.

### 11.3 Gates del runtime genérico

- `ENABLE_EDGE_GUARD_PAPER_FILTER=false`: porcentaje operativo `N/A` porque no
  hay runtime activo; estructuralmente, con false, `evaluate_signal` permite paper.
- `ENABLE_PAPER_POLICY_FILTER=false`: no es causal porque el Orchestrator no se
  importa ni se llama desde `app/main.py`.
- Candidate Ranking, Anti Overfit, Stability, Time Death, Net Edge y Exit labs
  tampoco forman parte del camino de entrada de `main.py`.
- El proceso `app.main` no estaba activo en el host. Sin ciclo no hay porcentaje
  de bloqueo runtime actual que calcular.

Encender Edge Guard/Paper Policy no genera P11; sólo añade decisiones sobre
señales del SignalEngine genérico.

---

## 12. Gates redundantes o dobles

1. **Triple razón del scanner.** `no_directional_setup`, `no_stop_loss` y
   `rr_below_min` son el mismo fallo cuando `side=None`. Debe persistirse un root
   cause y dos campos derivados, no tres votos.
2. **`max_open_positions` sin posiciones.** V10.28 no mantiene lifecycle; el
   límite cuenta filas seleccionadas del snapshot. Es un cap de tablero, no un
   control de exposición. Para recopilar labels de oportunidades debe moverse a
   la apertura virtual.
3. **Correlación en discovery.** Impedir registrar ETH porque BTC aparece arriba
   elimina el contrafactual necesario para saber si la correlación dañaba el
   portfolio. En shadow se registra y se marca; en paper sí se bloquea.
4. **Edge/Net/EV-Slippage.** Net Edge y EV-Slippage recalculan muestra, EV, PF,
   TIME y costes. Debe existir una única métrica canónica de net outcome y vistas
   consumidoras, no votos independientes sobre números casi idénticos.
5. **Anti Overfit/Stability/Time Death.** Las tres capas usan muestra pequeña,
   deterioro reciente y distribución TP/SL/TIME. Durante acumulación deben ser
   monitores; para promoción se consolida un gate de robustez con subchecks
   visibles.
6. **Edge Guard por cuatro vistas correlacionadas.** Símbolo, side, régimen y
   score pueden describir las mismas operaciones y multiplicar bloqueos. Se debe
   reportar la intersección y el primer motivo causal.
7. **Paper Policy Orchestrator.** Agrega Edge Guard, Net Edge, Anti Overfit,
   Stability y Ranking, y vuelve a imponer muestras mínimas. No debe conectarse
   al observer P11.
8. **Semántica shadow del Orchestrator.** En modo `shadow` devuelve razón
   `shadow_mode_no_block`, pero conserva `decision=BLOCK_PAPER`; la propiedad
   `blocks_paper` sigue siendo true. Antes de integrarlo hay que corregir esa
   contradicción y su test.
9. **Baseline exacto.** No es redundante, pero su generación actual es
   operacionalmente incompatible con cobertura 100%: una política random
   independiente rara vez elige exactamente la oportunidad/side/exposición del
   candidato. Copiar la misma operación para forzar el match produciría un gemelo
   con delta cero por definición y tampoco demostraría edge. La cohorte futura
   debe adjuntar, desde cada `candidate_event_id`: (a) no-trade, (b) un gemelo de
   ejecución idéntico sólo como control de integridad, y (c) un placebo económico
   de entrada retrasada entre 1 y 4 velas, elegido antes del outcome. Definir
   `digest=SHA256(UTF8(policy_hash+"|"+candidate_event_id+"|BASELINE_DELAYED_V1"))`
   y `offset=1+int.from_bytes(digest[0:8],"big")%4`, con side, exposición y reglas
   de salida congelados. El placebo requiere un contrato de matching nuevo,
   preregistrado y revisado, enlazado por `source_candidate_event_id`; hasta que
   ese contrato esté certificado, el gate de baseline de promoción sigue
   incumplido. Esto repara el experimento; no relaja el estándar.

---

## 13. Déficit exacto de evidencia

### 13.1 Déficit de la P11 histórica

| Gate | Actual | Requerido | Déficit exacto |
|---|---:|---:|---:|
| `n_eff` TRAIN | 20,0000 | 30 | 10,0000 |
| `n_eff` validation | 6,1723 | 30 | 23,8277 |
| Baseline exacto TRAIN | 1/35, 2,86% | 35/35, 100% | 34 pares compatibles |
| Superioridad paired TRAIN | `beats=false`; lower bound €0; p campaña corregida 1 | lower bound >0 y p corregida <0,05 bajo familia preregistrada | No demostrada; no se arregla sólo aumentando coverage |
| Net validation | +€0,0494 | >0 | Cumple signo |
| Diagnóstico validation sin top-3 | −€0,1046 | >=0 como requisito prospectivo adicional de esta revisión | Concentración; no fue gate oficial V10.47.25 |
| Walk-forward | No alcanzado; 0 llamadas | Etapa separada y lazy después de admisión | Sólo una cohorte prospectiva nueva puede cubrirla |
| Shadow candidate | false | true tras todos los gates | Todos los gates previos vinculantes |

La validation positiva no puede sumarse a TRAIN para “llegar a 26,17” y declarar
un gate de 30: son particiones con funciones diferentes y ambas deben cumplir.
Tampoco se pueden fabricar 34 pares retrospectivos después de ver outcomes.
El rechazo oficial de BTC P11 fue `VALIDATION_N_EFF_INSUFFICIENT`; el diagnóstico
sin top-3 no se reetiqueta aquí como causa histórica del rechazo.

### 13.2 Déficit de una cohorte P11 forward limpia

La política se escoge ahora usando TRAIN/validation, por lo que la confirmación
prospectiva empieza en el freeze. Esto no reinicia los relojes de infraestructura;
separa selección de confirmación.

| Unidad | Actual | Primer checkpoint | Déficit |
|---|---:|---:|---:|
| Velas P11 evaluadas bajo spec congelada | 0 | 96 en 24 h válidas | 96 |
| Operaciones cerradas | 0 | 30 | 30 |
| Labels completos | 0 | 30 | 30 |
| Controles no-trade/gemelo/placebo | 0 | Tres registros creados por cada `ENTRY_ELIGIBLE`, antes del outcome | Tres por oportunidad; el placebo exige contrato certificado |
| `n_eff` | 0 | 30 para promoción científica | 30 |
| Regímenes etiquetados | 0 | 100% de operaciones | 100% |

El primer checkpoint de 30 trades no implica `n_eff=30`. En los 44 trades
TRAIN+validation, `n_eff/trade` estuvo entre 0,571 y 0,686; 30 trades producirían
aproximadamente 17–21 unidades efectivas. Con la razón combinada, harían falta
aproximadamente 51 cierres nominales para alcanzar `n_eff=30`, sujeto a la
dependencia que realmente se observe.

---

## 14. Plan del día 0 y primeras 24 horas

### Día 0

1. Congelar el JSON de `MINIMUM_ACTIONABLE_PAPER_POLICY_V1`, su hash, commit,
   símbolo, venue Bitget, contrato de agregación 1m→15m, generación, costes y
   timestamp de inicio.
2. Cargar sólo warmup anterior al freeze: 60 barras 15m consecutivas previas más
   la barra cerrada de decisión (`decision_index>=60`). Ningún evento anterior
   se contabiliza como forward outcome.
3. Iniciar un observer BTCUSDT 15m que evalúe una vez cada vela Bitget cerrada y
   use el próximo open, condicionado a warmup, freshness y gap gate válidos. No
   llamar `PaperTrader`, `ExecutionEngine` ni APIs privadas.
4. Persistir eventos append-only: `BAR_EVALUATED`, `RAW_TRIGGER`, `ABSTAIN`,
   `ENTRY_ELIGIBLE`, `ENTRY_SHADOW`, `POSITION_BAR`, `EXIT_SHADOW`, `LABEL`,
   `BASELINE` y `ERROR`.
   En cada `ENTRY_ELIGIBLE`, antes de conocer el outcome, crear no-trade, gemelo
   de integridad y placebo determinista con seed/versión/IDs congelados.
5. Mostrar en dashboard, separados: velas esperadas/recibidas, triggers raw,
   skips por posición/cooldown, posiciones abiertas/cerradas, costes, MFE/MAE,
   cobertura de controles/placebo, hash y último heartbeat.
6. Mantener `LIVE=false`, `DRY_RUN=true`, `can_send_real_orders=false`. El flag
   paper puede seguir true por seguridad global, pero el observer no usa paper.

### Primeras 24 horas

- Esperado tras entrar con warmup válido y si no hay gaps: **96** decisiones de
  vela 15m.
- Rango histórico descriptivo, no garantía: **1,1–2,2 triggers raw** y
  **0,56–0,76 posiciones cerradas** por día.
- Cero triggers en 24 horas no es un error si existen 96 decisiones válidas y
  cada abstención contiene los tres valores y la condición falsa.
- Checks obligatorios:
  - el cierre lógico usado no es posterior al open de entrada; en la convención
    canónica ambos coinciden en el boundary y
    `entry_open_ts == signal_bar_open_ts + 15m`;
  - exactamente una decisión por timestamp;
  - ninguna posición solapada;
  - stop/TP/TIME reproducen SimOMS;
  - costes base y conservador presentes;
  - heartbeat y latencia de ingestión p50/p95;
  - cero llamadas a orden.
- Pausa inmediata ante: hash distinto, lookahead, timestamp duplicado, gap de
  una vela o más, OHLC inválido, coste ausente, divergencia con SimOMS, ledger no
  append-only o cualquier intento de orden. Ante gap, persistir `DATA_GAP` y no
  emitir entrada afectada: sólo continuar si un backfill público cerrado lo
  reconcilia con provenance íntegra; una decisión cuyo next-open ya pasó queda
  `LATE_RECOVERY` y fuera de la cohorte primaria. Si no se reconcilia antes de la
  siguiente decisión, resetear la ventana y exigir otras 60 barras anteriores
  consecutivas. Si había posición, censurarla y excluirla de métricas primarias.

---

## 15. Primeros siete días y primeras 30 operaciones

### Primeros siete días válidos

- 672 velas 15m esperadas si la continuidad es completa.
- Rango descriptivo P11: 8–15 triggers raw y aproximadamente 4–5 cierres. No es
  un mínimo garantizado y no se fuerza una entrada para cumplirlo.
- Criterio de continuidad: 100% de las velas esperadas reconciliadas; un gap
  explicitado sigue siendo gap y activa la recuperación/reset de la sección 14,
  no se convierte en decisión válida. Al menos 99% de decisiones válidas deben
  persistirse antes del siguiente cierre.
- Criterio de pausa técnica: cualquiera de los fallos de integridad de la
  sección 14.
- Criterio de baja frecuencia: si hay menos de cinco triggers raw, no rechazar
  automáticamente; comprobar primero régimen, fórmula y continuidad.
- No cambiar parámetros durante los siete días. Cualquier cambio crea
  `POLICY_V2` y una cohorte separada.

### Primeras 30 operaciones cerradas

Para cada operación y agregado se calcula:

- MFE/MAE y barras hasta MFE/MAE;
- motivo TP/SL/TIME y holding bars;
- hit rate, payoff, PF, gross EV, net EV base/conservador;
- fee, spread, slippage y funding separados;
- max drawdown y expected shortfall en euros;
- `n_eff`, clusters, días, sesiones y autocorrelación;
- net sin top-3 y concentración por día/sesión;
- régimen 4h/1h/15m observado, aunque no bloquee;
- errores/gaps/duplicados/latencia;
- cobertura de no-trade/gemelo/placebo; el delta económico sólo se calcula con
  el placebo bajo el contrato certificado.

Decisión al trade 30:

1. Si gross EV <=0 y net conservador <=0, congelar y clasificar
   `REJECT_OR_REDESIGN`; no ajustar la misma cohorte.
2. Si net base/conservador y net sin top-3 son positivos, pero `n_eff<30`,
   continuar shadow sin cambios hasta el gate efectivo.
3. Si el coste consume >=100% del gross edge, mantener únicamente como monitor
   de costes; no paper.
4. No pasar a paper por hit rate aislado. Deben cumplirse simultáneamente
   `n_eff>=30`, net base y conservador positivos, top-3 robusto, cobertura
   completa del placebo bajo contrato certificado, delta paired positivo,
   lower bound unilateral 95% >0, p corregida <0,05 para la familia
   preregistrada, identidad intacta y validation forward estable.
5. Incluso al cumplir lo anterior, sólo se habilita una etapa walk-forward lazy,
   posterior y separada. No paper hasta que esa etapa tenga métricas finitas,
   neto >0 e identidad intacta bajo el contrato vigente; no se reutiliza el
   holdout ni se cuenta la misma cohorte dos veces.

---

## 16. Métricas obligatorias

Las métricas ausentes se muestran como `MISSING`, nunca como cero.

### Provenance y causalidad

- `policy_spec_hash`, commit/tree, venue, dataset generation y timestamp de freeze.
- `signal_close_ts <= entry_open_ts` en 100% de entradas y, sin gap,
  `entry_open_ts == signal_bar_open_ts + 15m`; normalmente cierre lógico y open
  siguiente comparten el mismo timestamp boundary.
- Cobertura de velas, gaps, duplicados, corruptas y decisiones faltantes.
- Identidad de código y parámetros antes/después de cada etapa.

### Frecuencia y funnel

- Velas evaluadas; triggers raw; elegibles; bloqueados por posición/cooldown;
  entradas; fills; cierres; labels.
- Oportunidades/día, trades/semana y porcentaje bloqueado por motivo exclusivo.
- Símbolo, side, régimen, timeframe, día y sesión.

### Economía

- `gross_EV=sum(gross_pnl)/N`.
- `net_EV=sum(gross-fee-spread-slippage-funding)/N`.
- Coste medio y `cost_share=coste_total/abs(gross_pnl)` cuando gross !=0.
- Win rate; payoff `avg_win/abs(avg_loss)`; PF
  `sum(wins)/abs(sum(losses))`.
- Curva acumulada, max drawdown, expected shortfall y peor trade.
- Base, conservador y stress; net sin top-3.

### Dependencia y estabilidad

- `n_eff` con event/dependency cluster, día, sesión, solapamiento y ACF.
- Resultados por tercio temporal y por régimen; ninguna selección del mejor
  subgrupo para la cifra principal.
- Lower bound unilateral 95% por block bootstrap y placebo emparejado con
  cobertura completa bajo un contrato preregistrado/certificado; delta paired
  >0 y p corregida <0,05 para la familia fijada. El gemelo idéntico sólo valida
  el pipeline y nunca cuenta como evidencia de superioridad.
- Sensibilidad preregistrada sólo después de cerrar la cohorte principal; no
  elegir parámetros con la misma muestra.

### Ejecución y exits

- Open raw Bitget observado frente a fill económico **modelado**, gap-through y
  misma-vela stop-first; shadow no produce un fill real.
- MFE, MAE, bars-to-MFE/MAE, TP/SL/TIME ratios y holding.
- Latencia ingestión→decisión y decisión→persistencia p50/p95/p99.
- Fee, spread y slippage `MODELLED`; funding `PROXY` por timestamp de settlement,
  todos con status explícito. Ninguno se presenta como fill observado.

---

## 17. Cambios mínimos necesarios

Esta revisión no implementa cambios. El siguiente trabajo debe ser pequeño y
cohesivo:

1. **Un observer, una política:** `P11_SHORT_BTC_15M_FORWARD_SHADOW`.
2. Reutilizar `family_decider("P11", direction="SHORT")`, causal ledger y
   SimOMS existentes; no reescribir las reglas.
3. Fuente pública Bitget BTCUSDT USDT-futures 1m y agregación UTC estricta a 15m,
   idénticas al contrato de la sección 9, con gap/duplicate/freshness gate. Otro
   venue crea nueva identidad y no confirma P11 Bitget.
4. Ledger append-only persistente con una posición máxima y cierre TP/SL/TIME.
5. Tres controles preregistrados por oportunidad: no-trade, gemelo de integridad
   y placebo retrasado determinista; IDs reconciliables y contrato de matching
   del placebo revisado antes de usar su delta para promoción.
6. Dashboard mínimo del funnel y lifecycle; no mezclar scans con trades.
7. Export versionado JSONL/CSV del ledger forward con spec hash, generación,
   funnel, lifecycle, costes y controles; reconciliación byte/conteo con dashboard.
8. Pruebas de no-lookahead, next-open, stop-first, gap, costes una sola vez,
   funding, restart idempotente, hash mismatch y cero órdenes.

No cambiar:

- flags live ni `can_send_real_orders`;
- capital, leverage, margin o wallet/API;
- umbral 0,85, retorno 1%, stop 0,8%, TP 1,2% o time exit 15;
- Edge Guard/Paper Policy para “hacerlo pasar”;
- holdout;
- scanner multi-símbolo como sustituto del observer.

No usar `PaperTrader` hasta que pueda reproducir la misma semántica y costes. El
camino más corto es SimOMS shadow, no adaptar de golpe todo el runtime paper.

---

## 18. Tiempo restante calculado desde evidencia real

No se propone esperar un número arbitrario de semanas.

### Lo que ya cuenta

- 12,52 días transcurridos del escáner estable: útiles para uptime y frecuencia
  de su heurística.
- Cobertura V10.29 diferenciada: 12,66 días trades, 13,62 orderbook y 15,35 OI;
  útil para data readiness, con 0 días/frames de liquidaciones.
- 44 trades P11 SimOMS sobre 62 días TRAIN+validation: útiles para estimar
  frecuencia y diseñar el observer.
- 75,60 días de OHLCV MTF: útiles para smoke/replay Warren.

### Lo que no cuenta como confirmación P11

- seis paper opens LONG sin cierres;
- 27.013 decisiones snapshot sin lifecycle;
- fixtures/smokes/labels sintéticos;
- 44 trades usados para escoger P11;
- backfills previos al momento de adquisición.

### Estimación basada en la frecuencia observada

P11 tuvo:

- TRAIN: 35 cierres/46 días = 0,761/día; `n_eff` 20/46 = 0,435/día.
- Validation diagnóstica: 9/16 = 0,563/día; `n_eff` 6,1723/16 = 0,386/día.
- Heurística de planificación: 44/62 = 0,710 cierres/día y la suma aritmética
  `(20+6,1723)/62 = 0,422` unidades efectivas/día.

La segunda cifra **no es un `n_eff` combinado válido**: TRAIN y validation son
particiones distintas y la dependencia futura puede cambiar. Sólo sirve para
dimensionar el observer; el gate se recalcula desde la cohorte forward única.

Por tanto:

- Para **30 cierres** faltan 30 y el rango descriptivo es **39–53 días de datos
  válidos**, lo que ocurra cuando realmente se alcancen los 30; no una fecha fija.
- Para **`n_eff=30`**, esa heurística sugiere aproximadamente **69–78 días
  válidos** y alrededor de **51 trades nominales**; no es una fecha ni evidencia
  acumulada. El gate usa el `n_eff` observado de la cohorte, no el calendario.
- Si la frecuencia cambia de régimen, se recalcula con el funnel observado; no
  se fuerza una operación ni se reduce el gate.

El historial anterior evita empezar de cero en ingeniería, selección y cálculo
de frecuencia. No puede evitar que una hipótesis elegida post hoc necesite una
cohorte confirmatoria propia.

---

## 19. Decisión ejecutiva

### C. REPAIR_ONE_SPECIFIC_BLOCKER

**Bloqueo único para producir actividad útil:** no existe un observer
`BTCUSDT 15m P11_SHORT` conectado a un ciclo forward continuo que persista
señal→entrada virtual→posición→cierre→costes→label→baseline.

Al aceptar y completar la reparación, el estado operativo pasa inmediatamente a
**B. `START_FORWARD_SHADOW_NOW`** en la primera vela Bitget cerrada que cumpla
warmup/freshness/gap gate, sin esperar un calendario previo y sin relajar gates.
La opción C repara el lifecycle; no promociona P11 como edge y no incluye
implementar Warren. La evidencia actual no permite
`START_ACTIONABLE_PAPER_NOW`, porque:

- P11 no pasó baseline/`n_eff`/validation y, por ello, nunca llegó a WF;
- el `PaperTrader` cambiaría TP, breakeven, time exit y costes;
- no hay outcomes forward comparables.

Tampoco corresponde `NO_DEFENSIBLE_POLICY_FOUND`: BTC P11_SHORT es suficientemente
concreta y frecuente para una prueba shadow defendible. El problema es convertirla
en una cohorte forward, no inventar una estrategia nueva.

---

## 20. Próxima acción exacta

Abrir una tarea de implementación con un único objetivo:

> Implementar y arrancar `P11_SHORT_BTC_15M_FORWARD_SHADOW` desde una fecha de
> freeze nueva, reutilizando el decider P11, causal ledger y SimOMS exactos sobre
> el contrato público Bitget 1m→15m;
> persistir lifecycle, MFE/MAE, costes base/conservador y los controles
> no-trade/gemelo/placebo preregistrados;
> exponer el funnel en dashboard; demostrar por tests y ejecución que nunca
> llama PaperTrader/ExecutionEngine ni puede enviar órdenes.

Orden de ejecución:

1. Crear spec canónica y hash; registrar el freeze.
2. Implementar fuente de vela cerrada + observer idempotente.
3. Persistir ledger/lifecycle y los tres controles; certificar por separado el
   contrato de matching del placebo.
4. Añadir dashboard/counters y alertas de integridad.
5. Emitir export JSONL/CSV versionado y reconciliarlo con ledger/dashboard.
6. Ejecutar pruebas focales y un smoke sin red privada/órdenes.
7. Tras validar warmup/freshness/gap gate, arrancar en la siguiente vela Bitget
   15m cerrada y verificar 96 decisiones en las primeras 24 horas válidas.
8. Revisar al cierre 30 y después al alcanzar `n_eff=30`; paper sólo si se
   cumplen simultáneamente todos los gates definidos.

### Criterios de aceptación de esa tarea

- Política/hash idénticos tras restart.
- Una decisión por vela y una posición máxima.
- Next-open causal y stop-first reproducibles byte/valor contra SimOMS.
- Cada posición cierra por TP/SL/TIME y genera label, MFE/MAE y costes completos.
- Export versionado reconcilia hashes y conteos con ledger/dashboard.
- Presencia 100% de no-trade/gemelo/placebo por construcción prospectiva; el
  baseline de promoción permanece bloqueado hasta certificar el contrato del
  placebo y obtener cobertura completa bajo ese contrato.
- `LIVE=false`, `DRY_RUN=true`, `can_send_real_orders=false` y cero order calls.
- Ningún cambio de parámetros, wallet, capital, leverage, holdout o gates de
  promoción.

Esta es la ruta más corta defendible: reparar el lifecycle de una hipótesis ya
existente, empezar a medirla inmediatamente y posponer paper hasta que la misma
identidad demuestre resultados forward suficientes.

---

### Fuentes primarias revisadas

- `.ai_coordination/reviews/V10_47_25_WORK_FINAL_COMPREHENSIVE_REAUDIT.md`
- `.ai_coordination/{WORK_RESEARCH,CURRENT_STATE,NEXT_ACTION,DECISIONS,EVIDENCE_INDEX,SESSION_HANDOFF}.md`
- `logs/bot.log`
- `exports/research_export_20260503T020219Z/{signal_observations.csv,signal_labels.csv,trades.csv}`
- `bot_state.db`, abierto read-only/immutable
- `reports/research/v10_28/scanner_scans.jsonl`
- `reports/research/v10_28/scanner_state.json`
- `app/labs/multi_symbol_opportunity_scanner_v10_28.py`
- `external_data/staging/continuous_forward_v10_27/dataset/{manifest.json,checkpoint.json}`
- `external_data/staging/bybit_microstructure_v10_32/dataset/{manifest.json,checkpoint.json}`
- `reports/research/v10_29/status.html`
- `reports/research/v10_46_final_integrated/{final_report.md,tournament_scoreboard_eur.csv}`
- `reports/research/v10_47_25_comprehensive_closure/tournaments/final_primary_81d8b0b/*.json`
- `reports/research/v10_47_22_real_state_certification/mtf/work_reaudit_v10_47_22_final/*.json`
- `app/labs/v10_46/{families.py,causal_ledger.py,sim_oms.py,causal_tournament.py,det_strategies.py}`
- `app/{main.py,config.py,paper_trader.py,edge_guard.py,paper_policy_orchestrator.py,anti_overfit_gate.py,policy_stability_matrix.py,time_death_autopsy.py,net_edge_lab.py,exit_label_calibration_v2.py,exit_simulation_lab.py,latency_audit.py}`
