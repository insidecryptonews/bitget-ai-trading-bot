# V10.35 — Alpha Discovery & Prediction Lab (DISEÑO) + V10.36 Backfill (VEREDICTOS)

Estado: DISEÑO aprobable. Sin implementar todavía (ed7a959 pendiente de auditoría
Codex; no se apila código encima). RESEARCH_ONLY · NOT_ACTIONABLE · NO LIVE.

---

## 1) Backfill histórico — veredictos con sondas en vivo (2026-07-05)

| Fuente | Sonda | Veredicto |
|---|---|---|
| `public.bybit.com/trading/<SYM>/` dumps diarios de trades | 2.293 ficheros BTCUSDT, 2020-03-25 → 2026-07-04 (ayer) | **APTO PARA READINESS** (fuente oficial del mismo exchange, gratis, granularidad tick) |
| `public.bybit.com/spot/` dumps spot | existe | APTO (ruta spot / research secundario) |
| Bybit REST `funding/history` con ventanas | filas desde 2020-03 | **APTO PARA READINESS** (paginado, oficial) |
| Bybit REST `open-interest` con cursor | ventanas 2023 responden, cursor sí | **APTO PARA READINESS** (profundidad por paginación) |
| Dumps de liquidaciones | `public.bybit.com/liquidation/` → 404; root solo tiene kline_mt4/premium_index/spot_index/trading/spot | **NO EXISTE** → forward-only (V10.30) |
| Orderbook histórico gratuito | no hay fuente pública | **NO EXISTE** → forward-only (V10.32) |
| Kaggle/GitHub datasets | sin licencia/procedencia auditable por fila | NO APTO PARA READINESS (solo research secundario con licencia clara) |
| Tardis/CoinGlass/Kaiko | de pago | REQUIERE APROBACIÓN (no evaluado) |
| OHLCV kline (años) | disponible | APTO SOLO RESEARCH SECUNDARIO (regímenes/contexto; jamás microstructure READY) |

**Conclusión honesta del calendario**: el backfill NO elimina los ~30 días de
readiness (orderbook y liquidaciones son forward-only y el gate de cobertura
≥30d aplica a ambos). Lo que SÍ elimina es la espera para EMPEZAR el research:
con dumps de trades + funding + OI tenemos AÑOS de historia auditable para
construir features/labels/validación YA, en paralelo al reloj forward.

**V10.36 propuesto (tras audit)**: importer de dumps oficiales →
`external_data/staging/bybit_backfill_v10_36/` separado del forward; manifest
con source URL + fecha + checksum; `backfill=true` por fila-set; merge con
forward SOLO explícito y testado; tests: monotonía, dedupe, gaps visibles,
no-mezcla sin flag, licencia/atribución en manifest.

## 2) V10.35 Feature Bank (todas point-in-time, sin lookahead)
- **Trades**: intensidad (n/min), imbalance buy-sell (rolling), aceleración de
  volumen, detección de bursts (z-score de intensidad), proxy de trade grande
  (percentil 99 de size), clustering temporal, impacto (Δmid/volumen).
- **Orderbook L1**: spread, mid, imbalance top-of-book (bid1/ask1 sizes),
  reposición tras consumo, huecos de liquidez (spread spikes).
- **OI**: ΔOI 5m/1h, aceleración, divergencia precio-OI (precio↑+OI↓ = short
  squeeze fuel), régimen de OI.
- **Funding**: nivel, Δ, extremos (percentiles históricos con AÑOS de backfill),
  crowdedness proxy (funding alto + OI alto).
- **Liquidaciones**: conteo/notional por ventana, imbalance de lado, cascada
  (≥N en M segundos), aftershock (retorno post-cascada), interacción liq+OI+precio.
- **Régimen**: vol realizada, tendencia/chop, sesión horaria, modo BTC.
- Regla dura: cada feature declara `available_at_ts`; tests de no-futuro.

## 3) Labels
Triple-barrier (TP/SL/tiempo) con costes+slippage incluidos; retornos futuros
1m/5m/15m/1h; MAE/MFE; outcome cost-adjusted; label explícito de STAY_OUT.
Purga de solapamiento entre muestras (embargo temporal). Los labels usan futuro;
los features JAMÁS — test estructural que lo verifica.

## 4) Modelos (en este orden, sin saltarse etapas)
1. Scorers por reglas (transparentes, pocos parámetros).
2. Regresión logística + capa de calibración (Brier/reliability).
3. Ensemble conservador por régimen + clasificador de abstención.
Prohibido: deep learning con estos volúmenes, grids masivos, métricas sin OOS.

## 5) Validación dura (gates de rechazo)
Split cronológico + walk-forward rolling + purged CV; baselines no-trade y
random (×1.3 mínimo para considerar señal); costes/slippage/latencia SIEMPRE;
bootstrap CI; rechazo si: solo in-sample, solo 1 símbolo, solo 1 régimen,
muere con costes, <100 trades OOS, turnover mata EV, calibración mala.

## 6) Auto-abstención (el default es NO operar)
No-trade si: prob < umbral calibrado, EV≤0 tras costes, spread alto, liquidez
baja, vol anómala, régimen no visto, incertidumbre alta, datos stale, dashboard
stale, gate de riesgo. Salida SIEMPRE: `RESEARCH_ONLY_NOT_ACTIONABLE` con
{direction_probability, expected_move, EV_after_costs, confidence, regime,
risk_flags}.

## 7) Spot vs Futuros — análisis honesto
- **Research**: futuros ganan (liq/OI/funding solo existen ahí; son las señales).
- **Primera operativa real** (si algún día hay edge): **SPOT_FIRST** gana —
  sin liquidación forzosa, sin funding en contra, sizing trivial, el error de
  novato no se amplifica. Bybit publica dumps spot (verificado) → los labels de
  ejecución spot son construibles.
- Propuesta: señal descubierta en microstructure de futuros → ejecutada (si se
  valida) primero en spot sin leverage, en micro-tamaño, tras la escalera V10.33.
- Decisión final por evidencia del lab, no por preferencia.

## 8) Ruta más corta realista (sin humo)
- P0: auditar (Codex) + push de ed7a959 (V10.32/V10.33).
- P1: V10.36 backfill importer (trades/funding/OI, años) → empezar V10.35 lab
  con 3/5 señales YA, mientras orderbook+liq acumulan forward (~30d).
- P1: implementar V10.35 (features+labels+validación+tests no-lookahead).
- P2: copiloto visual Pine (exporter+linter, sin navegador/broker) — solo si
  aporta a la revisión humana; jamás fuente de verdad.
- P3: paper SOLO con edge validada; micro-live spot SOLO tras escalera V10.33
  completa con aprobación humana.
- **Lo que ahorra tiempo**: backfill oficial (research empieza ya).
- **Lo que NO lo ahorra**: el reloj forward de orderbook+liquidaciones.
- **Atajo peligroso**: bajar MIN_HISTORY_DAYS o mezclar fuentes → READY falso.
- **Evidencia mínima para operar**: EV>0 tras costes en OOS + walk-forward +
  ≥30d forward-shadow consistente + las 10 puertas V10.33.

FINAL_RECOMMENDATION: NO LIVE.
