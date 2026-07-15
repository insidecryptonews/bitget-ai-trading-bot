# V10.47.25 — Auditoría final independiente y de cierre

Fecha: 2026-07-15  
Rol: Work, auditor científico, estadístico y técnico independiente  
Estado auditado: preescritura de este informe  
Veredicto global: **PASS WITH LIMITATIONS**

## 1. Conclusión ejecutiva

No reproduje ningún defecto P0, P1 ni P2 capaz de cambiar la conclusión científica, reducir la multiplicidad, inflar el emparejamiento o `n_eff`, abrir walk-forward indebidamente, acceder al holdout, alterar la causalidad, invalidar provenance/manifest/sello o hacer alcanzable la ejecución real con la configuración observada.

La campaña canónica es `V10_47_OFFICIAL_4X3X47`, versión `10.47.25`: 4 símbolos × 3 timeframes × 47 participantes = `m_campaign=564`, `alpha=0.05`, corrección Bonferroni. Las 12 entradas se autorizaron contra sus manifests reales; BTC, ETH y DOGE usan el venue principal declarado por sus manifests y XRP usa Bybit. Las dos recomputaciones finales son científicamente idénticas, no solo iguales en tamaño.

El cierre conserva necesariamente:

- `NO_CONFIRMED_EDGE`
- `SHADOW_CANDIDATES=0`
- `HOLDOUT=SEALED`
- `FINAL_RECOMMENDATION=NO LIVE`

El matiz `WITH LIMITATIONS` se debe principalmente a que el bundle final es local, está ignorado por Git y aún no tiene archivo externo de solo lectura; además no se hizo captura visual real y algunos detalles de trazabilidad están preservados como hashes/resúmenes o son inferibles, en vez de estar serializados de forma autosuficiente. Nada de ello cambia el resultado negativo ni habilita promoción.

## 2. Límites y método

- No abrí ni leí barras del holdout real.
- No implementé correcciones ni modifiqué código, datasets, outputs, manifest, sello, dashboard, configuración o Git.
- No borré, moví ni regeneré artefactos bajo `reports/`.
- No relancé la suite completa; la evidencia certificada era internamente consistente. Solo ejecuté tests focalizados, fixtures sintéticos y mutaciones en memoria/directorios temporales externos al repositorio.
- No hice red, despliegue, VPS, push, stage ni commit.
- Antes de la escritura solo existían como cambios los tres untracked históricos declarados.
- Las únicas escrituras posteriores al snapshot preauditoría son este informe y la entrada correspondiente en `WORK_RESEARCH.md`. Por diseño, esa escritura autorizada posterior modifica el hub cubierto por el sello; no es un fallo retroactivo del estado preauditoría certificado.

## 3. Snapshot preescritura y valores previos

| Control | Valor observado antes de escribir |
|---|---|
| Rama | `local-v10-47-8-scientific-repair` |
| HEAD | `81d8b0b07c93b13a28cca75c220b4def79ac68b1` |
| Tree | `6c0775620c45e28939c23692593a558dbe9f0e16` |
| `origin/main` | `adc7b9c47ed2390eddaf80436287172455bb32d8` |
| Tracked worktree | limpio |
| Índice | vacío, 0 paths |
| `git diff --check` | sin errores |
| `.env` | ausente; no se modificó |
| Informe V10.47.25 | no existía |
| Bundle `reports/...closure` | presente, 225 archivos físicos, ignorado por Git |

`git status --short` preescritura contenía exactamente:

```text
?? CODEX_RESULT.md
?? CODE_RESULT.md
?? docs/research/LOCAL_AI_RESEARCH_ASSISTANT_FEASIBILITY_V10_40.md
```

La rama no tenía upstream y no aparecía una rama remota equivalente entre las referencias remotas locales. Esto confirma ausencia de un push actual visible desde este clon; no pretende demostrar que jamás existiera un push histórico fuera de las referencias disponibles.

Hashes SHA-256 previos de coordinación:

| Archivo | SHA-256 preescritura |
|---|---|
| `WORK_RESEARCH.md` | `b8ba72f1cf04d8f2243a0fb82c0cbaa110eaf1cda2662bb4f2aeec3fcb1f3533` |
| `V10_47_14_WORK_FINAL_AUDIT.md` | `a8869b6fbdd7ad022f7bd2ba3848c51d7bc33001ebd2c758011154e3b54d7c15` |
| `V10_47_18_WORK_REAUDIT.md` | `e0b6188048e95608704da76ba2c835d003874b621c29ae6533d425db34a8e36b` |
| `V10_47_22_WORK_FINAL_REAUDIT.md` | `f9c64adcea78886b21be188f7a67180dc193bde23ddbf421a6de74da9ba22f8a` |
| `V10_47_23_WORK_FINAL_REAUDIT.md` | `be8a4745a947317353e88bdc2df32b211f4feb83cc762fe173001d0501a7e5f2` |

Los commits finales declarados aparecen en el orden esperado: `b48ed31`, `6acb7f8`, `a135b03`, `81d8b0b`. Las auditorías históricas V10.47.14/18/22/23 permanecían intactas; sus fallos documentan estados anteriores y no se reescribieron.

## 4. Veredictos individuales

| Área | Veredicto | Motivo principal |
|---|---|---|
| Autoridad | PASS | Autoridad canónica fija; 12/12 manifests autorizados; mutaciones y campañas autoconsistentes no canónicas fallan cerrado. |
| Multiplicidad | PASS | `m_campaign=564`, `alpha=0.05`, Bonferroni; sin override ni reducción post hoc. |
| Pairing | PASS | IDs deterministas y únicos, preflight completo y reconciliación directa de records. |
| Dependencias / `n_eff` | PASS WITH LIMITATIONS | No hay inflación; dos campos no se serializan explícitamente en el record pareado, aunque siguen siendo atribuibles e inferibles. |
| Validation / WF | PASS WITH LIMITATIONS | Todos los gates preceden WF y los fallos abortan cerrado; excepción/salida parcial no producen un record estructurado de rechazo. |
| Holdout | PASS | Separación física/lógica, commitment sin barras, 24/24 outputs con holdout no cargado. |
| Datasets / venues | PASS | 12/12 manifests, commitments y venues concordantes; XRP/Bybit verificado. |
| SimOMS | PASS | Causalidad y casos de borde cubiertos por fuente y tests focalizados. |
| Ledger | PASS WITH LIMITATIONS | Integridad, secuencias e hashes reproducibles; el output conserva resumen/hash, no el ledger evento-a-evento completo. |
| Recomputaciones | PASS | 12 primaria + 12 replay iguales byte a byte en los JSON científicos actuales. |
| Tests | PASS | Certificación 3107/3107 única y consistente; focalizados 234/234. |
| Manifest / sello | PASS | Payload, sello, Git real, categorías disjuntas y hashes verificados independientemente. |
| Safety | PASS | `SAFE_PAPER_ONLY`; la ruta live existe pero es inalcanzable con el estado observado. |
| Hub | PASS WITH LIMITATIONS | `COHERENT`, una NEXT_ACTION y auditorías intactas; el hub sellado conserva el pendiente histórico V10.47.23 hasta la decisión de Work. |
| Dashboard | PASS WITH LIMITATIONS | HTML estático correcto; no hubo captura visual real. |
| Portabilidad de evidencias | PASS WITH LIMITATIONS | Auditable localmente hoy, pero ignorada por Git y sin copia externa inmutable. |

## 5. Autoridad canónica, datasets y multiplicidad

La autoridad observada define:

- campaign ID `V10_47_OFFICIAL_4X3X47`, versión `10.47.25`;
- símbolos `BTCUSDT`, `ETHUSDT`, `XRPUSDT`, `DOGEUSDT`;
- timeframes `1m`, `5m`, `15m`;
- 47 participantes y 12 entradas;
- `m_campaign=564`, `alpha=0.05`, Bonferroni;
- root anchor `1b71acb7509ea3b1e980b0e41910a6cb83c36d8cbab9d9d652273258cce7e6ee`;
- participant root `67beb846b276df62f50454969bc4059cd83d4b07c1ee7b63580899fcf7fe8ea6`.

Recalculé ambos roots y reconstruí las 12 preregistrations desde las fuentes reales. Todas fueron autorizadas y sus tournament registry hashes, matching/baseline/tolerance hashes, dataset manifest hashes y commitments coincidieron con la autoridad. Los 47 participant hashes coincidieron en las 12 entradas.

Venues observados:

- BTC y ETH: dataset principal Bitget, con referencia Bybit ligada al manifest;
- DOGE: dataset principal Bitget, sin referencia externa declarada;
- XRP: dataset principal Bybit, sin sustitución por Bitget ni alias manual.

La API de autorización no acepta del caller overrides de `m`, `alpha`, método, venue, registry ni hashes. Fallaron cerrado nueve ataques: campaña reducida 1×47 con SHA propio, `m=47`, símbolo ausente, timeframe ausente, participante ausente, entrada duplicada, venue XRP distinta, registry alternativo y campaña autoconsistente con root propio pero no autorizada.

## 6. Pairing, dependencias y reconciliación desde records

La reconciliación directa de las 12 salidas primarias produjo 564 clasificaciones:

- 399 `NO_GROSS_EDGE`;
- 146 eliminadas por costes;
- 19 net-positive en TRAIN que entraron al análisis exacto de baseline;
- 0 gates de baseline completos;
- 0 admitidas a validation;
- 0 llamadas WF;
- 0 shadow candidates.

En esos 19 bloques se solicitaron 172 pares: 3 aceptados, 163 imposibles y 6 incompatibles. Los conteos se reconciliaron desde los records y no solo desde `scientific_summary.json`. Los `pair_id` aceptados fueron recalculados y coincidieron; candidate ID y baseline ID fueron únicos dentro de cada evaluación, los valores obligatorios fueron finitos y los deltas fueron exactamente candidate menos baseline. El lower bound, los p-values y `p_adjusted = min(1, p × 564)` respetan la autoridad fija.

Los records y el preflight vinculan `global_event_id`, `underlying_trade_id`, `dependency_cluster_id` e `hypothesis_id`. Todo resultado con trades no nulos tenía IDs de dependencia/underlying completos y se verificó `0 <= n_eff <= trades`. La reducción de `n_eff` contempla eventos, overlap, clusters, sesiones, días, proximidad temporal, ACF y underlying trades; IDs ausentes fuerzan `n_eff=0`.

Existe un evento económico reutilizado entre P11 y P11_SHORT en BTC 15m. Conserva el mismo `global_event_id` y dependencia: no se convirtió en dos observaciones independientes dentro de un test, no elevó `n_eff`, no se trató como corroboración y no redujo `m_campaign`. Ninguna de las dos hipótesis superó el gate completo.

## 7. Validation, walk-forward y holdout

La ejecución y los tests confirman el orden TRAIN → VALIDATION → WF. Validation solo se abre después de todos los gates TRAIN/baseline; WF se instancia de forma lazy únicamente después de aprobar validation. Para rechazados no se calcularon señales, trades ni métricas WF y no existe caché WF precalculada.

Se verificaron rechazos por validation sin trades, valores no finitos, `n_eff` insuficiente, parámetros mutados y gates previos fallidos. Dos faults adicionales dieron comportamiento fail-closed:

- excepción sintética en validation: propagó `RuntimeError`, 0 llamadas WF;
- salida parcial sin contadores obligatorios: propagó `KeyError`, 0 llamadas WF.

El abortar sin promoción satisface la seguridad científica; la ausencia de un record de rechazo estructurado queda como P3 de operabilidad/trazabilidad.

No se abrió el holdout real. Los manifests muestran particiones contiguas discovery/train → validation → WF → holdout y commitments de metadatos, sin barras. El loader normal y el runner no recibieron contenido/path de holdout. Los tests sintéticos rechazaron alias, sustituciones y objetos no ligados al hash del archivo; las referencias cross-venue también están ligadas a su manifest.

En las 12 salidas primarias y las 12 replay se observó `holdout_data_loaded=false`, además de flags equivalentes de importación, touch y carga física en falso. No aparecen features, señales, trades ni métricas de holdout.

## 8. SimOMS, causalidad y ledger

La revisión de fuente y los tests focalizados cubrieron:

- entrada causal al open y exposición correcta de la vela de entrada;
- stop y TP en la misma vela con prioridad conservadora del stop;
- gaps LONG/SHORT y timestamp exacto de fill;
- time exit y `realised_holding <= max_holding`;
- reentrada sin barra fantasma y máximo una posición;
- trailing activo desde la vela siguiente;
- funding, fees, spread y slippage;
- censura y cierre al final del dataset.

El ledger es append-only, realiza deep copy, conserva secuencia, IDs, dependencia, ATR, initial stop, trailing, cierre y PnL. Los resúmenes de ambas recomputaciones tienen iguales conteos, secuencias y `ledger_sha256`. La limitación de portabilidad es que los JSON finales preservan `ledger_integrity` y hash, no el conjunto completo de eventos para recalcular ese hash sin rerun; el comportamiento sí queda cubierto por tests y por dos recomputaciones deterministas.

## 9. Dos recomputaciones finales

Se revisaron directamente:

- `tournaments/final_primary_81d8b0b/`: 12/12;
- `tournaments/final_replay_81d8b0b/`: 12/12.

Cada JSON primario fue igual byte a byte a su replay correspondiente. Por tanto, la igualdad es más fuerte que una comparación de tamaños o que el hash canónico que excluye provenance operacional. Coinciden datasets, HEAD/tree, autoridad, specs, 47 participantes, `m_campaign=564`, safety, holdout no cargado y conclusión científica.

Los cuatro summaries están cubiertos como categoría `tournament`; los 24 JSON científicos están cubiertos como `ledger`. Las duraciones de los summaries operativos pueden variar y no forman parte de la igualdad científica. Los 12 logs primarios están certificados; los 12 logs replay son evidencia operativa auxiliar y están expresamente fuera del sello final.

## 10. Suite certificada y pruebas ejecutadas

Suite certificada final: `certified_tests/final_code_81d8b0b_certified/`.

Verificación independiente de sus records/logs:

- collected 3107;
- unique 3107;
- duplicates 0;
- passed 3107;
- failed/skipped/xfailed/xpassed/deselected 0;
- exit code 0;
- línea final: `3107 passed in 684.23s (0:11:24)`;
- duración del wrapper: `687.339686 s`;
- HEAD/tree exactos;
- comando de colección: suite completa con `pytest --collect-only -q`;
- comando de ejecución: suite completa con `python -m pytest -q`, sin sumar suites parciales.

Hashes verificados:

- collection log: `3e208554...`;
- nodeids: `58bba4f6...`;
- execution log: `bef419e3...`;
- collection record: `7a899c3c...`.

Se comprobó además que la lista de nodeids contenía 3107 entradas y 3107 únicas.

Tests focalizados ejecutados:

```text
tests/test_researchops_v10_47_15_certification.py
tests/test_researchops_v10_47_20_validation_holdout.py
tests/test_researchops_v10_47_21_exact_baseline_mtf_atr.py
tests/test_researchops_v10_47_22_real_state_manifest.py
tests/test_researchops_v10_47_23_bijective_pairing_campaign.py
tests/test_researchops_v10_47_24_comprehensive_invariants.py
tests/test_researchops_v10_47_8_causal.py
tests/test_researchops_v10_47_8_det.py
```

Resultado: **234 passed in 46.41s**, exit 0. Se usaron `-B`, `-p no:cacheprovider` y `--basetemp` fuera del repositorio.

Además se ejecutaron las nueve mutaciones de autoridad descritas y dos faults de validation. Los tests de manifest/sello ejercieron en fixtures/copias temporales mutaciones de las 15 categorías, autoridad, XRP venue, dataset manifest, registry, pairing, output, test log, report, dashboard, HEAD, tree y archivo repetido entre categorías; también dirty tracked state, sello alterado, paths malformados/aliases, hardlinks, symlinks y traversal. Todas rompieron autorización o verificación según correspondía.

## 11. Manifest, sello y cobertura

La verificación independiente dio:

- `verify_manifest`: `ok=true`, 0 problemas;
- `verify_seal_text`: `ok=true`, 0 problemas;
- payload SHA-256 recalculado: `a86e4663d48fbbf4da3a9887b9c4642b6369e559b284af19c19fa6b47e1430aa`;
- seal SHA-256 canónico recalculado: `de93a0c1d733ed2d2a0e153c9c177a7eedae5164ae951338f765ea13ea92341d`;
- raw SHA-256 de `output_manifest.json`: `3516a46012aa3dc7615949e7f572f14fe45907333f3dfbe419203ca4c4ce2bae`;
- raw SHA-256 de `SEAL.txt`: `16a404b910a98190b9bdde7f86151ed6e778135ab6bb9e3aec59a92df0e4c578`.

El raw hash de `SEAL.txt` no debe confundirse con el seal SHA canónico: el contrato calcula el segundo sobre los campos canónicos antes de verificar el texto que incluye su propio valor. Ambos fueron comprobados conforme al algoritmo del manifest.

El manifest contiene 15 categorías, 161 entries y 161 paths únicos: audit 16, collection log 14, dashboard 1, dataset 42, dataset manifest 13, execution log 2, holdout 12, hub 8, ledger 24, policy 20, registry 1, report 2, spec 1, test nodeids 1 y tournament 4. No existe duplicación dentro ni reutilización entre categorías.

De los 225 archivos físicos bajo el cierre, 55 forman parte directamente de la cobertura final; el resto son generaciones históricas o auxiliares. En la generación actual quedan expresamente fuera de cobertura los 12 logs replay, `coverage_seed.json`, y el propio manifest/sello/verification, que se verifican por su contrato externo/autorreferencial. Los logs primarios, logs/records de colección y ejecución, security audit, dashboard, reports, evidencia científica y JSON de ambas recomputaciones sí están cubiertos. El código transitivo principal está cubierto por la categoría policy; los inicializadores tracked y cualquier otro archivo tracked quedan además anclados por HEAD/tree y el control de dirty tracked state.

## 12. Safety, hub y dashboard

Configuración observada sin cargar `.env`:

- `PAPER_TRADING=True`;
- `LIVE_TRADING=False`;
- `DRY_RUN=True`;
- `WORKER_LIGHTWEIGHT_MODE=True`;
- `ENABLE_PAPER_POLICY_FILTER=False`;
- `can_send_real_orders=False`;
- credenciales Bitget no presentes.

La repetición del security audit, junto con lectura/imports y scan del routing, encontró las funciones privadas reales (`place_order`, TP/SL, close, leverage e isolated margin), pero la ruta no es alcanzable salvo activación simultánea de los tres flags peligrosos, salida de lightweight mode cuando corresponda y credenciales. Margin aislado, leverage, sizing productivo y order routing permanecen detrás de esos gates y preflight/risk controls. Resultado: `SAFE_PAPER_ONLY`. No se hizo conexión de red ni VPS durante la auditoría.

`scripts/ai_coordination_status.py` devolvió `COHERENT`, una única NEXT_ACTION, 0 broken links y estado pendiente de Work. El nombre sellado de la acción aún referencia la reauditoría V10.47.23; se interpreta como provenance histórica congelada, no como estado científico V10.47.25. Codex no se autocertificó y los cuatro informes Work históricos conservaron sus hashes preauditoría.

El dashboard `status.html` es estático, de 1900 bytes, sin scripts, fetch, URLs externas ni secretos. Muestra HEAD/tree correctos, `m_campaign=564`, 12/12 + 12/12, 3107 tests, cero candidatos, holdout sellado, pendiente de certificación y `NO LIVE`. No se realizó captura visual mediante navegador real.

## 13. Hallazgos por severidad

### P0

Ninguno.

### P1

Ninguno.

### P2

Ninguno. No se halló defecto material con capacidad de falsa promoción, inflación de evidencia, acceso a holdout, WF indebido, causalidad incorrecta, sello inválido, evidencia no atribuible o riesgo operativo actual.

### P3

1. **Portabilidad local del bundle.** Los artefactos finales están presentes y sus hashes coinciden, pero viven en `reports/`, ignorados por Git. El manifest detecta una alteración cuando se verifica, pero sin una copia externa inmutable un actor con acceso local podría sustituir bundle, manifest y sello conjuntamente. Se requiere archivo externo de solo lectura después de aceptar este PASS.
2. **Granularidad del ledger preservado.** Los outputs conservan resumen, conteos y `ledger_sha256`, no todos los records del ledger evento-a-evento. Las dos ejecuciones y los tests validan reproducibilidad, pero un tercero necesita rerun para recalcular ese hash desde cero.
3. **Portabilidad del record pareado.** Los records serializados preservan `global_event_id` y `dependency_cluster_id`, pero no campos explícitos `underlying_trade_id` e `hypothesis_id`; son atribuibles por candidate ID, resultado exterior y preflight validado. No hubo inflación ni pérdida de atribución en esta auditoría.
4. **Faults de validation.** Excepción y salida parcial abortan antes de WF, pero lo hacen mediante excepción en vez de emitir un rechazo estructurado. Es una mejora de operabilidad, no un bypass.
5. **Presentación/coordination congelada.** Falta captura visual real y el hub/dashboard sellado conserva estado pendiente/histórico. No afecta la evidencia backend ni autoriza editar esos artefactos después del sello.

## 14. Claims confirmados

- Estado Git preauditoría coincide exactamente con rama, HEAD y tree declarados; tracked worktree limpio e índice vacío.
- Tres untracked históricos intactos y `.env` ausente.
- Autoridad 4×3×47, `m=564`, Bonferroni y venues/commitments reales 12/12.
- Caller sin autoridad para reducir campaña ni sustituir venue/registry/hash.
- Pairing bijectivo dentro de cada evaluación, IDs deterministas y campaña no reducida post hoc.
- Dependencias P11/P11_SHORT no se contabilizan como corroboración independiente ni inflan `n_eff`.
- TRAIN precede validation; todo rechazo/abort ocurre antes de WF; no hay cache WF de rechazados.
- Holdout físicamente no cargado en 12/12 primaria y 12/12 replay; commitment sin barras.
- SimOMS causal y ledger reproducible conforme a los casos focalizados.
- Dos recomputaciones científicas idénticas byte a byte.
- Suite certificada 3107 collected/unique/passed, 0 duplicados/fallos y exit 0.
- Manifest y sello válidos, categorías disjuntas y mutaciones fail-closed.
- Configuración `SAFE_PAPER_ONLY` y `can_send_real_orders=false`.
- Hub `COHERENT`, dashboard estático y bundle local auditable en el estado observado.

## 15. Claims falsificados o no sostenibles

- **“Existe edge confirmado”**: falsificado. Ninguna hipótesis completó baseline/validation/WF.
- **“Hay candidatos shadow”**: falsificado; son 0.
- **“El holdout puede abrirse”**: falsificado para el flujo certificado; permanece sellado y no cargado.
- **“Una campaña reducida autoconsistente puede autorizarse”**: falsificado por los fixtures adversariales.
- **“P11/P11_SHORT aportan dos corroboraciones independientes del mismo evento”**: falsificado; la dependencia se conserva y no genera gate aprobado.
- **“La evidencia está archivada de forma externa/inmutable”**: no sostenible; solo está presente y verificable localmente.
- **“El sistema está listo para live o demuestra rentabilidad”**: falsificado/no demostrado. La seguridad es PAPER_ONLY y los resultados no confirman edge.

## 16. Limitaciones externas y próxima acción

Limitaciones que no bloquean el cierre:

- bundle ignorado por Git, sin custodia externa inmutable;
- falta captura visual real del dashboard;
- no se verificó infraestructura VPS ni historial remoto fuera de las referencias locales;
- ventana de datos insuficiente para generalizar robustez MTF;
- ausencia de forward shadow porque no hubo candidatos;
- edge no confirmado y holdout deliberadamente sin abrir.

**Próxima acción única:** aceptar este cierre como `PASS WITH LIMITATIONS` y archivar externamente, en almacenamiento de solo lectura, el bundle preauditoría completo junto con `output_manifest.json`, `SEAL.txt`, rama/HEAD/tree y los hashes anteriores. No regenerar el bundle durante el archivado y verificar el manifest antes y después de copiarlo. Después, mantener el sistema en PAPER_ONLY y no abrir holdout ni iniciar forward shadow mientras `SHADOW_CANDIDATES=0`.

`FINAL_RECOMMENDATION=NO LIVE`
