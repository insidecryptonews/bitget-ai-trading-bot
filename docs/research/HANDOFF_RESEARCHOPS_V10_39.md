# HANDOFF MAESTRO — Bitget AI Trading Bot / ResearchOps (V10.39.1)

> Documento de traspaso para abrir un hilo nuevo (Code / Codex / ChatGPT) sin perder contexto.
> Español, directo, sin marketing. **El bot NO está listo para operar en real. NO hay edge validada.**
> Generado el 2026-07-07. Documento local, **no commiteado** salvo que Adrián lo pida.

---

## 1. Executive summary

Proyecto de investigación cuantitativa (research-only / shadow-only) sobre un bot de trading. El objetivo de largo plazo es **descubrir un edge real** y, solo si algún día se valida con evidencia dura y aprobación humana, avanzar hacia paper y micro-live. **Hoy no hay edge.** Toda la maquinaria es de investigación: descubre, valida, rechaza y reporta candidatos, pero **nunca emite una señal operable ni envía órdenes**.

Estado actual (2026-07-07):
- Último paquete en `origin/main`: **V10.39 + V10.39.1** (Alpha Improvement Sprint + CLI search/tests multitimeframe), auditado por Codex como **APTO PARA PUSH** y pusheado.
- Resultado honesto del research: **0 promising / `NO_EDGE_ALL_REJECTED_RESEARCH_ONLY`**. La mejor familia (`micro_momentum`) se rechaza por **coste > señal bruta** (`REJECTED_COSTS_TOO_HIGH`).
- Seguridad: `SAFE_PAPER_ONLY`, `FINAL_RECOMMENDATION=NO LIVE`.
- Collector Bybit público **funcionando** y dataset creciendo (~1053 barras 1m ≈ pocas horas de historia efectiva; falta muchísimo para ~30 días).

**Esto no es un fallo. Es el comportamiento correcto: el bot no se inventa edge.**

---

## 2. Estado actual exacto

| Campo | Valor |
|---|---|
| Repo | `insidecryptonews/bitget-ai-trading-bot` |
| Repo local | `C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot` |
| Branch local | `local-v10-8-1-research` |
| HEAD local | `56ea54ec8defc988d86bb2edb7be360e3c446824` |
| origin/main | `56ea54ec8defc988d86bb2edb7be360e3c446824` (HEAD == origin/main) |
| Working tree | limpio salvo `CODEX_RESULT.md` y `CODE_RESULT.md` (untracked, **NO commitear**) |
| Security | `SAFE_PAPER_ONLY` · `can_send_real_orders=false` · `paper_filter_enabled=false` · `actual_live_ready=false` |
| Edge | 0 promising · `NO_EDGE_ALL_REJECTED_RESEARCH_ONLY` |
| Recomendación | `FINAL_RECOMMENDATION=NO LIVE` |

---

## 3. Reglas de seguridad (líneas rojas absolutas)

Se mantienen SIEMPRE:
```
research_only=true
shadow_only=true
paper_ready=false
live_ready=false
can_send_real_orders=false
paper_filter_enabled=false
edge_validated=false
final_recommendation=NO LIVE
```
Prohibido: live, paper filter, órdenes reales, abrir posiciones, tocar Bitget/Binance/Hyperliquid real, broker/exchange real, `.env`, keys/secrets, imprimir secretos, endpoints privados, DB producción, leverage/margin/sizing, bajar costes para fabricar positivo, bajar thresholds para fabricar promising, usar OOS para elegir reglas, declarar edge sin pruebas, commitear `CODEX_RESULT.md` / `CODE_RESULT.md`.

---

## 4. Estado git y commits

Commits relevantes (recientes primero):
```
56ea54e ResearchOps V10.39.1: add alpha improvement search CLI and multitimeframe tests  (origin/main)
32f97d1 ResearchOps V10.39: add alpha improvement sprint
53d31a3 ResearchOps V10.38.1: fix edge discovery leakage and bar availability
cacc343 ResearchOps V10.38: add continuous edge factory
3ab891b ResearchOps V10.36: fail-close Bybit source stamp mismatches
a4427f4 ResearchOps: document optional token and trading tooling stack
98ce926 ResearchOps V10.36: harden Bybit collection and add backfill coverage probes
75b0bec ResearchOps V10.35: add alpha discovery and backfill acceleration plan
ed7a959 ResearchOps V10.32: add Bybit linear readiness and future live safeguards
b5cb604 ResearchOps V10.31: harden local collectors and diagnostics
```
Flujo de trabajo: **Code implementa → Codex audita → si Codex marca bug, hotfix ANTES de push → push solo con Codex APTO.** Nunca se hace push sin auditoría.

---

## 5. Arquitectura actual (módulos clave)

- `app/labs/continuous_edge_factory_v10_38.py` — **Continuous Edge Factory** (V10.38 + hotfix V10.38.1). Pipeline fail-closed:
  `bars → features point-in-time → labels future-only (triple barrier side-aware, cost-adjusted) → discovery (thresholds TRAIN-ONLY) → net-EV-after-costs → walk-forward vs baselines → incubator → policy registry (audit) → shadow decisions → paper gate (SIEMPRE bloqueado) → drift detector → reports`.
  Piezas: `build_bars_from_trades` (con `bar_start_ts`/`bar_close_ts`/`last_trade_ts`/`available_at`), `build_features`, `build_labels(side=...)`, `assert_no_lookahead`, `evaluate_net_ev`, `discover_candidates`, `walk_forward`, `CandidateIncubator`, `PolicyRegistry`, `shadow_decide`, `paper_promotion_gate`, `drift_check`, `future_micro_live_scaffold` (bloqueado), `run_cycle`.
- `app/labs/alpha_improvement_sprint_v10_39.py` — **Alpha Improvement Sprint** (V10.39 + V10.39.1). Construido SOBRE los primitivos de V10.38 (no relaja costes/thresholds/guards). Piezas: `resample_bars` (1m→3m/5m/15m con availability preservada), `eval_rule` (train-only + baseline aleatorio), `_verdict_for` (gate con penalización de complejidad multiple-comparison), `cost_aware_horizon_scan`, `strategy_family_benchmark` (12 familias), `regime_edge_report` (10 regímenes), `feature_quality_audit`, `diagnose`, `run_sprint`.
- `app/research_lab.py` — dispatcher de CLIs (`ResearchLab`), con **early dispatch fail-closed** para comandos public-research (`PUBLIC_RESEARCH_ONLY_COMMANDS`) que corre ANTES de load_config/DB (sin `.env`).
- `app/labs/future_live_readiness_v10_33.py` — scaffold de live futuro, TODO bloqueado (`actual_live_ready=false` siempre).
- `app/labs/bybit_public_microstructure_collector_v10_32.py` — collector Bybit full microstructure.
- `app/labs/bybit_backfill_importer_v10_36.py` — importador de dumps oficiales Bybit.

---

## 6. Collectors y dataset

**Dataset forward Bybit:** `external_data/staging/bybit_microstructure_v10_32/dataset/` (gitignored). Archivos: `trades.csv`, `orderbook_l2.csv`, `open_interest.csv`, `funding.csv`, `liquidations.csv`.

Estado medido (2026-07-07 ~11:53 UTC):
- `trades ≈ 239.525` · `trade_id` duplicados **0.00%** · `source_exchange = {bybit_linear}` · sin `SOURCE_MISMATCH` · `errors: []` · file_age ≈ 2 min (**creciendo/live**).
- `n_bars ≈ 1053` barras de 1m (≈ pocas horas efectivas, con un hueco). **Falta muchísimo para ~30 días.**

**Scripts:**
- `scripts/collect_bybit_microstructure_forever.ps1` — loop Bybit (trades/OB/OI/funding + liq ws). **SIN autostart** (lanzamiento manual por diseño).
- `scripts/collect_bybit_liquidations_forever.ps1` — wrapper legacy.
- `scripts/collect_forever.ps1` — loop Binance (tiene Startup `.bat`).
- `scripts/run_scanner.bat` — scanner shadow (tiene Startup `.bat`).

**Cómo saber si el collector Bybit está vivo:** ventana PowerShell titulada *"BitgetBot Bybit Microstructure (RESEARCH ONLY - NO LIVE)"*; o `trades.csv max_ts` avanza; o el log `external_data/staging/bybit_liquidations_v10_30/collector.log` crece.

**IMPORTANTE (durabilidad):** el collector Bybit **solo captura mientras el PC está encendido** y solo si su ventana está corriendo. Un hueco nocturno con el PC apagado **NO es un fallo**. Además, un collector lanzado desde una sesión de Claude **muere cuando la sesión termina** (le pasó al PID 17200 → ~9,6h sin datos). Para persistencia real, Adrián debe lanzarlo desde su propia consola o añadirlo a Inicio de Windows.

---

## 7. CLIs disponibles (verificados en `--help`)

Research V10.38/39 (public-research, sin `.env`):
```
continuous-edge-cycle-v1038 --symbols BTCUSDT
alpha-discovery-run-v1038 --symbols BTCUSDT
candidate-incubator-report-v1038
alpha-improvement-cycle-v1039 --symbols BTCUSDT
alpha-improvement-diagnose-v1039 --symbols BTCUSDT
alpha-improvement-search-v1039 --symbols BTCUSDT
alpha-improvement-report-v1039
security-audit
```
Nota: `strategy-family-benchmark`, `cost-aware-horizon-scan` y `regime-feature-report` **NO son CLIs sueltos**; son etapas internas del sprint que corre `alpha-improvement-cycle-v1039` / `-search-v1039` (documentado así en el propio output).

---

## 8. Tests y validaciones

Full suite tras V10.39.1: **2443 passed, 0 failed** (~7 min; ejecutar SIEMPRE en background, no en foreground). Grupos:

V10.38:
- `test_researchops_v10_38_alpha_discovery.py` — features point-in-time, triple barrier, costes reducen EV, no-lookahead guard, discovery encuentra edge plantado / rechaza ruido, thresholds train-only, availability de barras.
- `test_researchops_v10_38_short_labels.py` — short side-aware real (barreras propias), no es inversión del long, approx-short bloqueado.
- `test_researchops_v10_38_candidate_incubator.py` — máquina de estados fail-closed, rechaza estados LIVE.
- `test_researchops_v10_38_net_ev_trainer.py` — abstención por defecto, REJECT negativo, TRADE_IN_SIMULATION solo si lower bound supera min-edge.
- `test_researchops_v10_38_walk_forward.py` — OOS vs baselines aleatorio/no-trade, verdicts honestos.
- `test_researchops_v10_38_policy_registry.py` — audit trail, blocked_live_flags.
- `test_researchops_v10_38_shadow_runner.py` — `SHADOW_DECISION_ONLY_NOT_ACTIONABLE`, paper gate siempre bloqueado.
- `test_researchops_v10_38_drift_detector.py` — acciones solo de research.
- `test_researchops_v10_38_continuous_edge_cycle.py` — ciclo completo research-only, future micro-live bloqueado.

V10.39:
- `test_researchops_v10_39_no_leakage.py` — resample preserva availability, thresholds train-only, sin primitivos peligrosos, no baja costes.
- `test_researchops_v10_39_cost_aware_search.py` — coste = 18bps, `REJECTED_COSTS_TOO_HIGH`, ruido sin promising.
- `test_researchops_v10_39_strategy_families.py` — 12 familias, penalización de complejidad, edge plantado detectado / ruido rechazado.
- `test_researchops_v10_39_regime_filters.py` — regímenes, muestra pequeña nunca promising.
- `test_researchops_v10_39_multitimeframe.py` — contrato available_at en resample, features TF superior sin lookahead, small-sample nunca promising, factor-1 identidad.
- `test_researchops_v10_39_alpha_improvement.py` — sprint encuentra edge sintético / rechaza real, audit de features, parser/CLI de `alpha-improvement-search-v1039`.

Existentes relevantes: `test_researchops_v10_32_bybit_microstructure.py`, `test_researchops_v10_36_bybit_backfill.py`, `test_researchops_future_live_readiness.py`.

**Limitación clave de todos los tests:** validan **corrección metodológica** (no-lookahead, costes, no-snooping, fail-closed), **no rentabilidad**. Que la suite esté verde NO significa que haya edge.

---

## 9. Reports disponibles (gitignored, no commitear)

- `reports/research/v10_38/`: `continuous_edge_summary_v1038.json`, `candidate_rankings_v1038.csv`, `shadow_policy_metrics_v1038.csv`, `walk_forward_report_v1038.json`, `drift_report_v1038.json`, `promotion_gate_report_v1038.json`.
- `reports/research/v10_39/`: `alpha_improvement_summary_v1039.json`, `cost_aware_horizon_scan_v1039.json`, `strategy_family_benchmark_v1039.csv`, `regime_edge_report_v1039.csv`, `feature_quality_audit_v1039.json`, `diagnose_v1039.json`.

Todos llevan `methodology` con: `threshold_source=train_only`, `bar_time_semantics=bar_close_available`, `feature_available_at_contract=after_source_available`, `short_label_method=real_side_aware`, guards activos.

---

## 10. Fases V10.32 → V10.39.1

1. **Base previa**: bot en paper/research, no live, no órdenes, dashboards/reports, recomendación persistente NO LIVE (Fases 5–9, ver `CODE_RESULT.md`/`CODEX_RESULT.md` históricos).
2. **V10.32** (`ed7a959`): Bybit linear readiness + microstructure collector (trades/OB/OI/funding/liquidations), `source_exchange=bybit_linear`, readiness separado de Binance. Binance derivatives WS **entrega 0 frames** desde la red de Adrián → Binance-native liquidaciones inalcanzables; Bybit funciona como fuente pública alternativa. + future live safeguards.
3. **V10.33**: future live readiness scaffold (kill switch, order simulator, reconciliation, stale-data halt, duplicate-order protection, human approval) — TODO bloqueado, `actual_live_ready=false`.
4. **V10.35** (`75b0bec`): diseño Alpha Discovery + plan de backfill.
5. **V10.36** (`98ce926`): hardening Bybit, `collect_bybit_microstructure_forever.ps1`, source stamping, backoff 429, backfill probes/coverage, checks fail-closed.
6. **V10.36 hotfix** (`3ab891b`): Codex detectó que un dataset denso con source mismatch podía quedar INVALID pero `can_research_microstructure=true`. Fix fail-close (`C_INVALID` + `SOURCE_MISMATCH_OR_MISSING_STAMP`). Push realizado.
7. **V10.38** (`cacc343`): Continuous Edge Factory (Alpha Discovery, labels, discovery, incubator, net-EV trainer, walk-forward, policy registry, shadow runner, paper gate bloqueado, drift detector, ciclo continuo, reports).
8. **Codex audit V10.38**: NO APTO inicialmente. Blockers: (a) data snooping en thresholds, (b) lookahead temporal de barras, (c) SHORT aproximado invirtiendo long costeado.
9. **V10.38.1** (`53d31a3`): thresholds TRAIN-ONLY (split antes), `bar_start_ts`/`bar_close_ts`/`last_trade_ts`/`available_at` (ancla al cierre), SHORT real side-aware, +12 tests. Codex re-audit **APTO** → push.
10. **V10.39** (`32f97d1`): Alpha Improvement Sprint (familias, cost-aware horizon scan, strategy family benchmark, regime filters, feature quality audit, abstención/confidence, multitimeframe, edge sintético test, reporting real honesto).
11. **Codex audit V10.39**: HOTFIX NECESARIO por contrato incompleto (faltaba CLI `alpha-improvement-search-v1039`, test multitimeframe, test parser/CLI). Safety y research core estaban bien.
12. **V10.39.1** (`56ea54e`): añadido `alpha-improvement-search-v1039`, `test_researchops_v10_39_multitimeframe.py`, test parser/CLI. Codex **APTO** → push.

---

## 11. Fallos encontrados y hotfixes

1. **Source mismatch Bybit** — un dataset denso con stamp inconsistente podía quedar INVALID pero marcar `can_research_microstructure=true`. Fix fail-closed (`3ab891b`).
2. **LIVE_READY ambiguo (future live scaffold)** — se separó `checklist_complete`/`simulated_live_readiness` (sintético) de `actual_live_ready=false` (hardcoded, nunca cambia).
3. **V10.38 data snooping** — thresholds calculados con TODO el dataset (train+OOS) contaminaban OOS. Fix: split cronológico primero, threshold = cuantil de **train only**, `threshold_source=train_only`, train insuficiente → REJECTED_DATA_QUALITY.
4. **V10.38 temporal availability** — barras marcadas al inicio del bucket con OHLC de todo el minuto = lookahead. Fix: `available_at = bar_close_ts (≥ last_trade_ts)`, `ts` ancla al cierre.
5. **SHORT aproximado** — short = inverso del long costeado (no replay real). Fix: `build_labels(side="short")` con barreras propias (`real_side_aware`); si no hay barras, short queda `approx_inverse_long` + blocker `SHORT_APPROXIMATE_LABELS`, **nunca promising**.
6. **V10.39 contract incompleto** — faltaba CLI search + test multitimeframe + test parser. Fix V10.39.1.
7. **Falso positivo de "collectors duplicados"** — el comando PowerShell de diagnóstico contenía el string buscado (`-match 'collect_bybit...'`), así que `Get-CimInstance` se auto-detectaba: aparecían "loops duplicados" con parent `claude.exe` y PIDs cambiantes. **Eran artefactos del diagnóstico, no collectors reales.** Método correcto: exportar todos los procesos a JSON y analizar en Python excluyendo cmdlines con `Get-CimInstance`/`Select-String`/`-match`.
8. **Hueco nocturno** — no era caída del collector; el **PC estaba apagado**. Con PC local, solo se captura mientras Windows está encendido. Si hay autostart y funciona, no proponer persistencia extra.

---

## 12. Estado actual de edge (medido 2026-07-07)

`continuous-edge-cycle-v1038 --symbols BTCUSDT`:
```
bars: 1053 · candidates_total: 32 · promising: 0 · rejected: 26 · shadow_eligible: 0
drift: ALERT_ONLY · paper_gate: BLOCKED · FINAL_RECOMMENDATION=NO LIVE
```
`alpha-improvement-search-v1039 --symbols BTCUSDT`:
```
verdict: NO_EDGE_ALL_REJECTED_RESEARCH_ONLY · families_evaluated: 12 · promising: 0
best_family: micro_momentum [REJECTED_COSTS_TOO_HIGH]
best_net_EV: -0.00105 · best_net_EV_lower_bound: -0.00165 · any_timeframe_promising: False
```
Diagnóstico: coste round-trip ≈ **18 bps**; señales brutas mejores ≈ **10–12 bps** (`burst_score`, `oi_change`). El bruto positivo existe pero **no paga costes** → `net_EV` y `net_EV_lower_bound` negativos. Ningún timeframe (1/3/5/15m) rescata el edge. Varias features son **cost-dominated**.

---

## 13. Por qué NO LIVE

- No hay `net_EV_lower_bound > 0` tras costes en ninguna familia/timeframe/régimen.
- Muestra escasa (pocas horas / ~1053 barras) → estadísticamente insuficiente.
- El coste (18bps) supera la señal bruta (~12bps). Operar ahora = pérdida garantizada por fees.
- El paper gate exige aprobación humana **no codificable** → siempre bloqueado.
- Operar sin edge validada solo transfiere dinero a fees/spread.

---

## 14. Qué hacer en la próxima sesión

1. Confirmar `git` (HEAD/origin), `security-audit` (SAFE_PAPER_ONLY), snapshot **limpio** de procesos (sin self-match).
2. Confirmar collector Bybit vivo y dataset creciendo (`max_ts` avanza, dup 0%, `bybit_linear`, `errors: []`). Si hay hueco, comprobar primero si el PC estuvo apagado antes de asumir caída.
3. Correr `continuous-edge-cycle-v1038` y `alpha-improvement-search-v1039`; registrar promising/net_EV/lower_bound/drift.
4. Si sigue 0 promising: **acumular más datos, no forzar edge.**
5. Si aparece un promising: **NO operar**; reportarlo como `PROMISING_RESEARCH_ONLY` y pedir auditoría metodológica (Codex) antes de cualquier fase shadow/paper.

---

## 15. Qué NO hacer

- No activar live ni paper filter. No enviar órdenes. No abrir posiciones.
- No tocar `.env`, keys, DB producción, leverage/margin/sizing.
- No bajar costes ni thresholds para fabricar promising. No usar OOS para elegir reglas.
- No declarar edge por hit-rate sin payoff, ni por muestra minúscula, ni por backtest bonito.
- No interpretar `0 promising` como fallo. No interpretar el edge sintético de los tests como edge real.
- No matar procesos por un diagnóstico que se auto-detecta (self-match PowerShell).
- No proponer persistencia extra si el autostart ya funciona. No confundir PC apagado con collector caído.
- No commitear `CODEX_RESULT.md` / `CODE_RESULT.md`. No push sin Codex APTO.

---

## 16. Cómo interpretar resultados

| Salida | Significado |
|---|---|
| `NO_EDGE_ALL_REJECTED_RESEARCH_ONLY` | Correcto y honesto: nada supera costes/lower-bound. |
| `REJECTED_COSTS_TOO_HIGH` | Hay señal bruta positiva pero el coste se la come (no es "no hay señal"). |
| `REJECTED_NEGATIVE_EV` | Ni siquiera hay señal bruta positiva. |
| `NEEDS_MORE_DATA` | Muestra insuficiente; seguir acumulando. |
| `PROMISING_RESEARCH_ONLY` | Candidato interesante **solo para research**; NO operable; requiere auditoría. |
| `paper_gate: BLOCKED` | Siempre; aprobación humana no codificable. |
| edge sintético promising en tests | Prueba que la maquinaria detecta edge cuando existe; **NO es edge de mercado real**. |

---

## 17. Checklist para Code (implementación)

- [ ] Trabajar sobre `local-v10-8-1-research`, encima de `56ea54e`.
- [ ] No tocar costes/thresholds/guards/short-side-aware sin justificación auditada.
- [ ] Reusar primitivos de V10.38 (no duplicar ni relajar).
- [ ] Mantngo `_safety()` / `NO LIVE` en toda salida nueva.
- [ ] Tests nuevos que midan algo real (no-lookahead, costes, no-snooping, fail-closed).
- [ ] `compileall` + suite en **background** + `security-audit` + `git diff --check`.
- [ ] Commit local claro; **NO push** hasta Codex APTO.

## 18. Checklist para Codex (auditoría)

- [ ] Verificar no data-snooping (threshold train-only, split primero).
- [ ] Verificar no lookahead (feature `available_at` ≥ cierre de su barra; label `label_available_at` > su barra).
- [ ] Verificar SHORT side-aware real (no inversión de long costeado).
- [ ] Verificar costes/slippage incluidos y no rebajados.
- [ ] Verificar penalización de complejidad y baselines en el sprint.
- [ ] Verificar que promising exige lower_bound > min-edge + complejidad y bate baseline.
- [ ] Verificar seguridad (SAFE_PAPER_ONLY, sin primitivos de orden/keys/.env).
- [ ] Verificar contrato CLI (comandos en parser + tests de parser).
- [ ] Si hay bug → HOTFIX antes de push.

## 19. Checklist para Adrián (humano)

- [ ] Dejar el PC encendido con el collector Bybit corriendo (consola propia o autostart) para acumular hacia ~30 días.
- [ ] Revisar cada cierto tiempo que `max_ts` avanza y dup sigue 0%.
- [ ] No pedir live/paper hasta que haya edge validada + auditoría + tu firma explícita.
- [ ] Recordar: hueco con PC apagado = normal, no es fallo.
- [ ] Decidir push solo tras Codex APTO.

---

## 20. Próximo plan recomendado

1. **Acumular datos** (semanas) hasta tener historia útil (~30 días) con OB/liquidaciones.
2. Correr los ciclos periódicamente y vigilar si algún `net_EV_lower_bound` cruza 0 tras costes.
3. Si algo cruza: walk-forward reforzado + anti-overfit + auditoría Codex → shadow-only → paper gate (bloqueado hasta aprobación humana).
4. Solo mucho después, y con evidencia sostenida: live readiness audit → micro-live con riesgo mínimo. **Hoy: nada de esto.**

---

## Memoria del chat / decisiones humanas importantes

- Adrián quiere respuestas **directas, críticas y en español**; sin vender humo.
- Cuando pide prompts, **no anidar bloques negros dentro de bloques grises**.
- Flujo: **Code implementa, Codex audita**. Si Codex detecta bug → **hotfix antes de push**.
- **No fiarse solo de "tests passed"**: la suite valida metodología, no rentabilidad.
- **No** activar live ni paper filter. **No** tocar `.env`, keys, dinero real, leverage, margin, sizing.
- El **hueco nocturno del collector local NO es fallo** si el PC estaba apagado; el collector local solo captura con el PC encendido. Adrián indicó que el collector arranca al encender el PC.
- Los **falsos duplicados con parent `claude.exe`** fueron un **self-match de PowerShell** en el diagnóstico, no collectors reales.
- El resultado honesto actual es **0 promising / `NO_EDGE_ALL_REJECTED_RESEARCH_ONLY`**.
- El objetivo ahora es **acumular más datos y no forzar edge**.
- Cualquier promising futuro debe **pasar auditoría antes de shadow/paper**.
- `FINAL_RECOMMENDATION` sigue siendo **NO LIVE**.

## Errores de interpretación ya corregidos

1. **No** confundir PC apagado con collector caído.
2. **No** confundir un proceso temporal de diagnóstico con un collector duplicado (self-match).
3. **No** proponer persistencia extra si ya hay autostart funcionando.
4. **No** interpretar `0 promising` como fallo técnico (es research honesto).
5. **No** interpretar el edge sintético plantado en tests como edge real.
6. **No** decir que algo está listo para operar cuando solo es research.

---

## Nota de contexto (RESUELTO)

`MISSING_CHAT_CONTEXT_FILE` **resuelto**: los veredictos Codex recientes de V10.38/V10.38.1/V10.39/V10.39.1 (APTO/NO APTO + blockers + push ranges + test counts) están ahora documentados en **`docs/research/CODEX_VERDICTS_V10_38_39.md`**. Nota: esos veredictos se reconstruyen desde el historial de chat/memoria (no desde un log exportado por Codex); lo verificable contra el repo (commits, hashes, security) está confirmado. `CODEX_RESULT.md` y `CODE_RESULT.md` locales siguen siendo **históricos** (Fases 5–9, mayo 2026) y no cubren V10.38+.

---

**Confirmación final: NO LIVE. NO paper filter. NO órdenes. NO `.env`. NO keys. NO DB producción. NO leverage/margin/sizing. FINAL_RECOMMENDATION=NO LIVE.**
