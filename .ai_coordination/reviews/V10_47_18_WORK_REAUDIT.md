# V10.47.18 — REAUDITORÍA FOCALIZADA INDEPENDIENTE

**Fecha:** 2026-07-14

**Alcance:** exclusivamente las reparaciones V10.47.15–V10.47.18 posteriores al
FAIL de V10.47.14.

**Veredicto global:** **FAIL**

**Conclusión conservadora:** se mantiene `SHADOW_CANDIDATES=0 ·
NO_CONFIRMED_EDGE · HOLD · FINAL_RECOMMENDATION=NO LIVE`. El FAIL afecta a la
certificación de las reparaciones, no demuestra edge ni autoriza live.

No se abrió el holdout real. Todas las pruebas de acceso, autorización, traversal
y consumo usaron barras sintéticas. No se modificaron código productivo,
datasets, configuración, informes de Fable ni Git.

## 1. Veredictos individuales

| Bloque | Veredicto | Resultado focal |
|---|---|---|
| VALIDATION | **FAIL** | Tiene métricas y gate propias, pero WALK_FORWARD se ejecuta incluso cuando VALIDATION falla. |
| HOLDOUT | **FAIL** | Wrapper en memoria, sin ruta/loader físico separado; acceso directo y self-authorization posibles; traversal de compromiso aceptado. |
| BASELINE | **FAIL** | No conserva todos los campos exigidos, no expone IDs/pares explícitos y no aplica corrección múltiple. |
| MTF 4h→1h | **FAIL** | Causal en series regulares, pero publica velas 4h incompletas y no es participante de los doce torneos. |
| ATR STOP | **FAIL** | 2 ATR correcto en LONG/SHORT y SimOMS, pero ATR/initial_stop no quedan en el ledger append-only. |
| MANIFEST/SEAL | **FAIL** | Manifest actual stale; verifier no revalida Git/dataset/spec externos ni cubre auditorías/hub/collection; rebuild no estable. |
| UNIQUE TESTS | **FAIL** | El conteo único pasa (2912/2912/0), pero el log no contiene HEAD/tree y el manifest que debía ligarlo falla. |
| DOCE TORNEOS | **FAIL** | Totales y cero candidatos confirmados; el protocolo regenerado hereda los FAIL de validation/baseline/holdout. |
| HUB | **PASS** | COHERENT, una NEXT_ACTION, FAIL histórico intacto, enlaces válidos y opiniones conservadas. |

## 2. Git y alcance

Verificado:

- rama `local-v10-47-8-scientific-repair`;
- HEAD `142630c6fedb4a8d20bf17511e4fc4d482afb475`;
- tree `11683088c3a91729309874df15440606dde186ae`;
- `origin/main=adc7b9c47ed2390eddaf80436287172455bb32d8`;
- 28 commits ahead y 0 behind;
- existen `9af42d1`, `8160c7f`, `87bfa40` y `142630c`;
- worktree tracked limpio antes de esta reauditoría;
- tres untracked históricos intactos: `CODEX_RESULT.md`, `CODE_RESULT.md` y
  `docs/research/LOCAL_AI_RESEARCH_ASSISTANT_FEASIBILITY_V10_40.md`;
- ninguna rama remota contiene HEAD y la rama no tiene upstream: no está
  publicada actualmente. Esto no prueba que nunca se intentara un push;
- `.env` está ausente; no hay diff entre `e33b1ef` y HEAD en `app/config.py`,
  `.env`, `.env.example` ni `.env.railway.paper.example`;
- la auditoría V10.47.14 coincide byte-a-byte con el blob de `9af42d1` y no fue
  modificada en commits posteriores.

## 3. VALIDATION real — FAIL

### Confirmado

- `evaluate_candidate()` recibe TRAIN, VALIDATION y WALK_FORWARD separados.
- Ejecuta `drive_causal()` sobre VALIDATION y produce
  `validation_net_eur`, `validation_trades` y `validation_positive`.
- Un resultado negativo de VALIDATION hace `all_pass=False`.
- Los parámetros/decider/exit config no se reajustan después de VALIDATION.
- VALIDATION está nombrada como región intermedia, no como holdout/OOS final.

### Falsificación ejecutada

Fixture sintético: TRAIN `+1`, VALIDATION `−1`, WALK_FORWARD `+99`. Resultado:

```text
validation_gate=False
walk_forward_called=True
call_sequence=TRAIN(observed), TRAIN(conservative), VALIDATION, WALK_FORWARD
all_pass=False
```

El candidato se rechaza al final, pero no **antes** de WALK_FORWARD. El código
calcula WF incondicionalmente y no implementa el flujo requerido
`TRAIN → admitido por VALIDATION → WALK_FORWARD`. Los 14 candidatos negativos en
VALIDATION de los outputs conservan métricas WF, confirmando el mismo recorrido.

## 4. Holdout físicamente sellado — FAIL

### Lo que sí funciona en fixtures sintéticos

- estado inicial `SEALED` y compromiso SHA-256 de 64 caracteres;
- `load()` sin token se deniega;
- token de un uso; segundo consumo rechazado;
- intentos registrados con secuencias append-only en la API pública;
- frames cuyo módulo contiene `causal_tournament`, `validation` o
  `walk_forward` son rechazados;
- los doce JSON declaran estado SEALED, `holdout_touched=false` y access log con
  un único evento `seal`.

### Falsificaciones

1. El runner carga primero la serie completa y construye
   `SealedHoldout(holdout_bars=bars[hstart:])`. No hay dataset/ruta/loader físico
   separado; las barras del holdout ya están presentes en `bars`.
2. `h._bars` es directamente legible. El guion bajo es convención, no guardia.
3. Un caller neutral ejecutó `authorize_once(audit_ref="ANY-NONEMPTY-STRING")` y
   consumió las barras. No existe autoridad externa, firma, allowlist ni
   capability emitida fuera del objeto.
4. `write_commitment(child/../escaped.commitment.json)` escribió fuera del
   directorio hijo. La prueba denominada “path traversal” del proyecto solo
   comprueba un token falso; no prueba paths.
5. `_selection_caller()` depende de substrings en nombres de módulos y puede
   eludirse mediante un wrapper neutral.

No se abrió ni se intentó abrir el holdout real durante esta reauditoría. Los
commitments reales solo se inspeccionaron como metadata/hash.

## 5. Baseline pareado — FAIL

### Confirmado

- usa el mismo `symbol`, `timeframe` y `side` al simular;
- busca el mismo cluster y mantiene un `busy_until` para el path baseline;
- conserva parámetros máximos de salida, escenario de exposición y costes;
- calcula deltas candidato menos baseline esperado, media, mediana, bootstrap y
  lower bound;
- un cluster imposible produce `BASELINE_MATCH_INCOMPLETE`, coverage 0 y gate
  fail.

### Falsificación de match completo

Se presentó un candidato con cluster válido pero con:

- `bars_held=99` frente a baseline con holding aproximado de 2;
- sesión y día imposibles;
- censura incompatible;
- notional 999;
- funding 999;
- régimen incompatible.

El resultado fue:

```text
match_status=OK
coverage=1.0
beats_matched_random=True
```

También se aceptó `OK` en un fin de dataset con holding/censura incompatibles.
La función no devuelve `candidate_trade_id`, `baseline_trade_id` ni una lista de
pares. Para cada candidato promedia todas las simulaciones disponibles y empareja
el PnL candidato con ese promedio; `pairs_found` solo significa “al menos una
simulación disponible”, no match exacto de campos.

Campos no comprobados: fecha/día, sesión, oportunidad exacta, holding realizado,
censura, notional real, funding settlement y régimen. `m_unique` se pasa a
`evaluate_candidate()` pero no se usa; no existe p-value ni corrección de
multiple testing en la gate baseline. No se acepta la etiqueta “exactly paired”.

Reproducciones cubiertas: match ordinario, holding distinto, censura distinta,
sesión distinta, side conservado, fin de dataset, funding distinto y match
imposible.

## 6. Estrategia 4h→1h — FAIL

### Confirmado

- agrega 1h en buckets 4h y calcula EMA50/EMA200, ATR, ADX/+DI/−DI en 4h;
- publica el régimen a `bucket_start + 4h`;
- en serie regular, las fronteras 00:00/04:00/08:00 y cambio de día usan el
  régimen solo desde la primera barra 1h cuyo open coincide o sucede al close 4h;
- ninguna barra 1h previa lo recibe;
- las features de entrada permanecen en 1h y el ledger entra en el siguiente
  open 1h;
- mutar barras futuras no alteró una señal anterior;
- Donchian ejecutó LONG y SHORT en el fixture.

### Fallos

- Al eliminar las barras 01:00, 02:00 y 03:00 de un bucket que solo conservaba
  00:00, el régimen se publicó igualmente a 04:00 con `regime_ready=True`. No se
  comprueba completitud, conteo ni continuidad de la vela 4h.
- `causal_tournament.preregister()` no contiene ningún participante `DET_*`; los
  doce torneos reparados siguen siendo 1m/5m/15m P01–P12/Trend Rider. Existe un
  runner de smoke separado, pero no satisface “participante real del torneo”.
- El fixture EMA/ADX utilizado no produjo trades; las pruebas incluidas solo
  verifican presencia de campos/orden temporal, no LONG y SHORT end-to-end para
  ese participante.

## 7. Stop real de 2 ATR — FAIL

Reproducción numérica ejecutada con `entry=100`, `atr_entry=2`:

| Side | Fórmula | Stop obtenido | SimOMS |
|---|---|---:|---|
| LONG | `100 − 2×2` | 96 | salida SL a 96 |
| SHORT | `100 + 2×2` | 104 | salida SL a 104 |

Confirmado: ATR proviene de la señal cerrada anterior a la entrada; el stop se
convierte a fracción y llega a SimOMS; no usa el porcentaje legacy; un ATR futuro
no recalcula el stop inicial; trailing se activa tras 1R, se aplica desde la vela
siguiente y el stop solo se estrecha. El fixture de trailing salió a 100.8 en la
vela siguiente.

Fallo obligatorio: `atr_entry`, `initial_stop`, multiplicador y distancia se
guardan en el diccionario de trade devuelto, pero ninguno aparece en los records
del `ImmutableLedger`. Los records `entry`, `position` y `trade` omiten esos
campos. Por tanto no existe prueba append-only del riesgo inicial.

## 8. Manifest y seal — FAIL

### Valores recalculados

- schema: `v10_47_18_manifest_seal`;
- `manifest_payload_sha256`:
  `1fadbbd967b991c011bc44ae9d666444e7e380cc211e848b74e6c0df398f846e`;
- `dataset_root_hash` desde el mapping declarado:
  `9645ed3ceaf160b65960355b8834dbb89a9448e3bade57c795eaae145591514d`;
- `spec_root_hash`:
  `192453adc117c7082cb44df908f612f22433f69b054440f68372e3f4ecc9a2e8`;
- registry root: el mismo `192453ad…`;
- `holdout_commitment_hash` desde los doce JSON de commitment:
  `73aa96337b0d59d2ee32897302a4e6c41ef4ac493eae5859db9ba0243b9aec76`;
- seal declarado/recalculado desde el payload registrado:
  `53e68acd80f73faa909c63d09645793aeabf98b92a5c9a32d16264f0f1c41295`.

### Manifest actual

`python scripts/v10_47_18_manifest.py verify` devolvió exit 1:

```text
VERIFY ok=False payload_ok=True seal_ok=True
stale: .../progress_checkpoint.md
```

El manifest se generó a las 14:48:23 y el checkpoint incluido se modificó a las
14:48:39. `output_root_hash` declarado `bed75e61…`; root actual `569ef6f8…`.
Se repite exactamente la clase de fallo de V10.47.14.

### Mutaciones sobre copias temporales

| Mutación | ¿rompe verify? |
|---|---|
| report | sí |
| dashboard | sí |
| test `.log` | sí |
| tournament/registry output | sí |
| HEAD real del repo temporal, sin cambiar payload | **no** |
| tree real del repo temporal | **no** |
| dataset externo | **no** |
| spec externa | **no** |
| registry externo | **no** |

`verify_manifest()` re-hashea únicamente `files_sha256`; no compara Git actual y
no tiene rutas para rehashear datasets/specs/registry externos. Mutar campos
dentro del JSON rompería el payload, pero eso no demuestra que la provenance real
siga coincidiendo.

Además:

- `spec_hashes[key]` se rellena con el mismo registry hash; no es un hash de spec;
- `generated_utc` pertenece al payload: dos builds consecutivos con los mismos
  contenidos producen payload y seal diferentes;
- los `.txt` de collection están excluidos por extensión;
- no se incluyen la auditoría V10.47.14, el hub, políticas/código ni ledgers
  append-only completos;
- no existe `SEAL.txt`; el seal solo está embebido en el manifest.

## 9. Tests únicos — FAIL de certificación, conteo PASS

Ejecuciones realizadas:

- `pytest --collect-only -q`: **2912 invocaciones, 2912 nodeids únicos,
  0 duplicados**;
- `tests/test_researchops_v10_47_15_certification.py`: **16 passed**;
- `tests/test_researchops_v10_47_8_det.py`: **6 passed**.

Log verificado: `2912 passed in 575.55s`; SHA-256
`4c745e052cfda8a1dfb4d5060789669f7fd1981cfcca03f6a905276afeb47fa6`
coincide con la entrada registrada. No se sumaron suites parciales.

Limitación material: el log real no contiene HEAD, tree ni una línea explícita
de exit code. `collection_summary.txt` y `collection_nodeids.txt` tampoco están
incluidos en el manifest. Como el manifest final falla y no revalida Git actual,
no certifica que ese log se ejecutara exactamente en HEAD/tree declarados.

Las 16 pruebas focales son demasiado débiles para los claims: solo comprueban que
VALIDATION es parámetro, no que WF quede sin llamar; no comparan campos de pares;
la prueba “path traversal” prueba un bad token; la estabilidad del manifest solo
compara `output_root_hash`; MTF no prueba vela 4h incompleta ni participante del
torneo; ATR no inspecciona el ledger.

## 10. Doce torneos — FAIL de protocolo, totales confirmados

Confirmado por agregación independiente de los doce JSON:

- 12/12 outputs distintos byte-a-byte de los anteriores;
- 564 participant-runs nominales;
- 389 `NO_GROSS_EDGE`;
- 154 `GROSS_EDGE_COST_KILLED`;
- 21 `NET_EDGE_POSITIVE`;
- 0 `SHADOW_CANDIDATES`;
- cada positivo tiene al menos una gate explícita en false;
- ledger causal, single-position y n_eff conservador permanecen en el código;
- los doce outputs declaran holdout SEALED y access log solo con `seal`;
- hashes nuevos constan en el manifest.

No pasan la certificación porque fueron producidos con el flujo que ejecuta WF
antes de aplicar el rechazo de VALIDATION, el baseline no es exact-match y el
holdout no está físicamente aislado. Los totales conservadores siguen siendo
evidencia útil de **cero candidato**, no de protocolo certificado.

## 11. Hub — PASS

`python scripts/ai_coordination_status.py` devolvió:

- `COHERENT`;
- una única NEXT_ACTION;
- cinco decisiones D001–D005;
- cero broken links;
- el FAIL V10.47.14 está intacto y enlazado;
- la reparación V10.47.15–18 está registrada;
- `WORK_RESEARCH.md` conserva los hallazgos previos y añade, no sobrescribe.

El hub es estructuralmente coherente aunque `CURRENT_STATE.md` sobreafirme que
los bloqueadores están cerrados.

## 12. Hallazgos por severidad

### P0

Ninguno. No hubo apertura del holdout real, live, órdenes ni promoción.

### P1

1. Holdout no físicamente aislado y el guard es evadible/directamente legible.
2. Baseline acepta pares materialmente incompatibles y carece de IDs/corrección
   múltiple.
3. Manifest final stale; verifier no valida provenance externa/actual.
4. VALIDATION no controla la admisión a WALK_FORWARD.

### P2

1. MTF publica buckets 4h incompletos y no participa en los doce torneos.
2. Stop 2 ATR no queda registrado en el ledger append-only.
3. Suite única correcta, pero sin provenance verificable de HEAD/tree.
4. Tests focales verdes no falsifican los claims fuertes que documentan.

### P3

1. Ausencia histórica de push no demostrable; solo estado remoto actual.
2. Semantic dedup usa un fixture conductual limitado; la corrección conservadora
   sigue usando m_nominal, por lo que no introduce promoción anti-conservadora.

## 13. Claims confirmados

- identidad Git, commits y configuración safety;
- conservación exacta del FAIL anterior;
- VALIDATION produce métricas y bloquea `all_pass`;
- conteo 2912/2912/0;
- EventClock deriva el intervalo del timeframe;
- ledger deep-copy;
- 2-ATR numérico y trailing next-bar desde 1R;
- mapping MTF causal en datos completos/regulares;
- 12 totales 389/154/21 y cero candidatos;
- hub COHERENT;
- invariantes `paper=true`, `live=false`, `dry_run=true`,
  `can_send_real_orders=false`, `NO LIVE`.

## 14. Claims no confirmados

- rechazo antes de WF;
- holdout físicamente separado/inalcanzable;
- baseline exact-match y multiple-testing corrected;
- vela 4h incompleta bloqueada y MTF como participante real del torneo;
- ATR/initial stop en ledger;
- manifest/seal reproducible y ligado a estado real;
- log full-suite ligado de forma válida al HEAD/tree declarado;
- certificación final de los doce torneos.

## 15. Archivos revisados

- diff completo `e33b1ef..142630c` y los cuatro commits;
- documentos requeridos del hub y auditoría V10.47.14;
- informes, doce tournament JSON, commitments, logs, dashboard y manifest de
  `v10_47_15_final_certification_repair`;
- `causal_tournament.py`, `causal_stats.py`, `sealed_holdout.py`,
  `det_strategies.py`, `causal_ledger.py`, `sim_oms.py`, `event_clock.py`,
  `manifest_seal.py`;
- scripts regen/finalize/manifest/deterministic runner;
- tests focales, deterministic y parametrización de nodeids.

## 16. Limitaciones restantes

- Por mandato, no se verificó el commitment real contra las barras del holdout.
- No se reejecutó la suite completa de nueve minutos; se verificó su log y se
  reejecutaron colección, 22 tests focales y fixtures adversariales.
- La evidencia Git solo demuestra que HEAD no está actualmente en remotos.

## 17. Próxima NEXT_ACTION recomendada

- [ ] **NEXT:** mantener el holdout real cerrado y corregir los cuatro P1 antes
  de adquirir datos: loader físico que no entregue barras a selección; gate que
  no invoque WF tras validation fail; pares uno-a-uno con IDs y validación de
  todos los campos/corrección múltiple; verifier que revalide Git/datasets/specs
  reales y genere el manifest como último artefacto estable. Añadir tests
  adversariales para bucket 4h incompleto y riesgo ATR en ledger, regenerar los
  doce outputs y repetir esta reauditoría.

No se proponen estrategias nuevas, no se recomienda live y no se promete
rentabilidad.
