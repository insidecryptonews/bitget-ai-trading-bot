# Veredictos Codex / Code — V10.38 → V10.39.1

> Registro cronológico de los ciclos **Code implementa → Codex audita → hotfix si hace falta → push solo tras APTO**
> para V10.38, V10.38.1, V10.39 y V10.39.1. Este documento elimina el `MISSING_CHAT_CONTEXT_FILE`
> señalado en `docs/research/HANDOFF_RESEARCHOPS_V10_39.md` (los veredictos vivían solo en el chat/memoria).
>
> Documento local, **no commiteado** salvo que Adrián lo pida. Sin secretos. **FINAL_RECOMMENDATION=NO LIVE.**
>
> Nota de fidelidad: los veredictos Codex se reconstruyen desde el historial de trabajo (chat/memoria), no desde
> un log exportado por Codex. Lo verificable contra el repo (commits, hashes, test counts, push ranges, security)
> está confirmado. `CODEX_RESULT.md` / `CODE_RESULT.md` locales son HISTÓRICOS (Fases 5–9, mayo 2026) y NO cubren V10.38+.

---

## 1. V10.38 inicial — Continuous Edge Factory

**Commit:** `cacc343 ResearchOps V10.38: add continuous edge factory`

**Estado Code inicial:**
- V10.38 implementado: Continuous Edge Factory (Alpha Discovery, labels, discovery, candidate incubator, net-EV trainer, walk-forward, policy registry, shadow runner, paper gate bloqueado, drift detector, ciclo continuo, reports).
- Full suite verde.
- Datos reales: **0 promising**.
- Security: `SAFE_PAPER_ONLY`.

**Codex audit → `NO APTO PARA PUSH`.** Safety OK; research/anti-overfit NO apto hasta hotfix.

**Blockers detectados (bugs reales):**
1. **Data snooping en discovery** — los thresholds se calculaban usando **todo el dataset** (train+OOS) antes del split train/OOS. Eso permite que la distribución OOS influya en el umbral elegido → OOS contaminado.
2. **Lookahead temporal** — las barras agregadas desde trades se marcaban como disponibles al **inicio del bucket** (`ts = bucket_start`), aunque el OHLC usaba **toda la vela** del minuto. Una vela completa no es conocible en su apertura.
3. **SHORT aproximado** — el short **invertía el outcome long ya costeado** (`-outcome_long - carga_costes`), no era un replay side-aware real con barreras propias.

---

## 2. V10.38.1 — hotfix de leakage y disponibilidad de barras

**Commit:** `53d31a3 ResearchOps V10.38.1: fix edge discovery leakage and bar availability`

**Fixes (cierre de los 3 blockers):**
- **Thresholds train-only**: split cronológico **primero**; el umbral es un cuantil de las features de **train únicamente**; `threshold_source=train_only`; train insuficiente (`split < MIN_SAMPLE`) → `REJECTED_DATA_QUALITY`, nunca PROMISING.
- **Disponibilidad de barras**: `build_bars_from_trades` emite `bar_start_ts`, `bar_close_ts`, `first_trade_ts`, `last_trade_ts` y `available_at`; `available_at = bar_close_ts (≥ last_trade_ts)`; `ts` ancla al **cierre**, no a la apertura.
- **build_features hereda la disponibilidad real** de la barra fuente (`available_at`); `assert_no_lookahead(features, labels, bars=None)` gana comprobación opcional contra la barra fuente.
- **SHORT real side-aware**: `build_labels(side="long"|"short")` con barreras propias por side y costes por side (`side_label_method=real_side_aware`); si no hay barras, el short cae a `approx_inverse_long`, marcado con blocker `SHORT_APPROXIMATE_LABELS` y **jamás PROMISING** (fallback bloqueado).
- **Tests nuevos** (+12): anti-snooping (thresholds train-only, mutar OOS no cambia el umbral, train insuficiente nunca promising), contratos de bar availability, y short labels side-aware (barreras propias, no inversión del long).

**Estado:** full suite **2408 passed**; security `SAFE_PAPER_ONLY`.

**Codex re-audit → `APTO PARA PUSH`.**

**Push realizado:** `origin/main` pasó de `3ab891b` → `53d31a3` (`git push origin HEAD:main` = `3ab891b..53d31a3`).

---

## 3. V10.39 inicial — Alpha Improvement Sprint

**Commit:** `32f97d1 ResearchOps V10.39: add alpha improvement sprint`

**Qué añadió** (construido SOBRE los primitivos de V10.38, sin relajar costes/thresholds/guards):
- Alpha Improvement Sprint.
- Strategy family benchmark (12 familias con protocolo único + penalización de complejidad multiple-comparison).
- Cost-aware horizon scan (nuevo verdicto `REJECTED_COSTS_TOO_HIGH` = bruto>0 pero neto≤0).
- Regime filters (10 regímenes; muestra pequeña nunca promocionada).
- Feature quality audit (coverage/estabilidad/redundancia/relación con label).
- Abstención / confidence (baseline_delta, complexity_penalty, cost_floor).
- Synthetic planted edge (prueba que la maquinaria detecta edge cuando existe).
- Real data reporting honesto; reports V10.39 gitignored.

**Estado real:**
- No edge real; **0 promising**; `NO_EDGE_ALL_REJECTED_RESEARCH_ONLY`.
- Best family **cost-dominated** (`REJECTED_COSTS_TOO_HIGH`).
- Security `SAFE_PAPER_ONLY`; full suite **2435 passed**.

**Codex audit → `HOTFIX NECESARIO`.** El núcleo research y la safety estaban **bien**, pero el **contrato de entrega estaba incompleto**.

**Blockers (contrato CLI/tests, no metodológicos):**
1. Faltaba el CLI `alpha-improvement-search-v1039` (Codex lo ejecutó y falló con *invalid choice*).
2. Faltaba `tests/test_researchops_v10_39_multitimeframe.py` (Codex lo ejecutó y falló porque no existía).
3. Faltaba un test de parser/CLI que confirmara que el entrypoint existe.

---

## 4. V10.39.1 — hotfix de CLI search y tests multitimeframe

**Commit:** `56ea54e ResearchOps V10.39.1: add alpha improvement search CLI and multitimeframe tests`

**Fixes:**
- Añadido **`alpha-improvement-search-v1039`**: registrado en las `choices` del parser, en `PUBLIC_RESEARCH_ONLY_COMMANDS` y en el dispatch fail-closed real. Alias de la etapa de búsqueda del sprint (corre scan + family benchmark vía `run_sprint`, escribe los reports). Salida **research-only / not actionable** reportando `families_evaluated` / `cost_aware_rows` / `promising` / `rejected` / `best_family` / `best_net_EV(_lower_bound)` + `FINAL_RECOMMENDATION=NO LIVE`. `run_sprint` expone `cost_aware_rows` (informativo, sin cambio de lógica).
- Añadido **test parser/CLI**: comando presente en `--help`, presente en `PUBLIC_RESEARCH_ONLY_COMMANDS`, método existe, smoke monkeypatcheado se mantiene research-only sin tokens accionables.
- Añadido **`tests/test_researchops_v10_39_multitimeframe.py`** con tests multitimeframe reales:
  - contrato `available_at` en resample (== cierre del último sub-bar, nunca el inicio del bucket);
  - resample 1m→3m/5m;
  - features de TF superior sin lookahead (mutar futuro no cambia features previas);
  - small sample nunca `PROMISING_RESEARCH_ONLY`;
  - `factor=1` es copia identidad (no alias);
  - cost-aware scan multitimeframe research-only.

**Estado:** full suite **2443 passed**; security `SAFE_PAPER_ONLY`.

**Codex re-audit → `APTO PARA PUSH`.**

**Push realizado:** `origin/main` pasó de `53d31a3` → `56ea54e` (`git push origin HEAD:main` = `53d31a3..56ea54e`).

---

## 5. Estado final tras V10.39.1

**HEAD == origin/main:** `56ea54ec8defc988d86bb2edb7be360e3c446824`

**Estado operativo:**
```
SAFE_PAPER_ONLY
NO LIVE
NO paper filter
NO órdenes
NO .env
NO keys
NO DB producción
NO leverage/margin/sizing
FINAL_RECOMMENDATION=NO LIVE
```

**Resultado research:**
- 0 promising; `NO_EDGE_ALL_REJECTED_RESEARCH_ONLY`.
- Costes ≈ **18 bps**; mejor señal bruta ≈ **10–12 bps**.
- `net_EV` negativo; `net_EV_lower_bound` negativo.
- `paper_gate: BLOCKED`; `NOT_ACTIONABLE`.

---

## 6. Lecciones importantes

- **Codex sí encontró bugs reales en V10.38** (no cosméticos): data snooping, lookahead temporal y short aproximado.
- **No se pusheó hasta corregir** — el push de V10.38 solo ocurrió empaquetado con el hotfix V10.38.1.
- **V10.38.1 mejoró la validez metodológica** (train-only, availability al cierre, short side-aware real). Efecto colateral honesto: cerrar el leakage empeoró el resultado real (más rechazos), que es lo correcto.
- **V10.39 no inventa edge; amplía la búsqueda** (familias, timeframes, regímenes) con guards intactos.
- **V10.39.1 cerró el contrato CLI/tests**, no la metodología (que ya era correcta).
- **El synthetic planted edge demuestra que el sistema PUEDE detectar edge sintético**, pero **eso no es edge real de mercado**.
- **El resultado real sigue siendo NO EDGE.**
- **La disciplina correcta es:** Code implementa → Codex audita → hotfix si hace falta → **push solo tras APTO**. No fiarse solo de "tests passed" (validan metodología, no rentabilidad).

---

## 7. Tabla resumen

| Paquete | Commit | Codex | Acción | Push (origin/main) | Full suite |
|---|---|---|---|---|---|
| V10.38 | `cacc343` | NO APTO (3 blockers metodológicos) | hotfix | — | verde |
| V10.38.1 | `53d31a3` | APTO | push | `3ab891b → 53d31a3` | 2408 |
| V10.39 | `32f97d1` | HOTFIX NECESARIO (contrato CLI/tests) | hotfix | — | 2435 |
| V10.39.1 | `56ea54e` | APTO | push | `53d31a3 → 56ea54e` | 2443 |

---

**Confirmación final: NO LIVE. NO paper filter. NO órdenes. NO `.env`. NO keys. NO DB producción. NO leverage/margin/sizing. FINAL_RECOMMENDATION=NO LIVE.**
