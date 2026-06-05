# ResearchOps V8.2 — Bidirectional Forensics + Campaign + Exit Lab

**Estado:** investigación pura. **No toca producción.**
`final_recommendation: NO LIVE`.

## Objetivo

Medir con datos reales VPS por qué el bot no monetiza tendencias claras
(ni LONG ni SHORT) y simular cuantitativamente el impacto de:

- score / regime simétrico,
- suavizado de penalty ATR (con excepción si el side coincide con régimen),
- routing HIGH_VOLATILITY por momentum direccional,
- trend campaign manager bidireccional con reglas no-martingala,
- profit lock / trailing stop bidireccional con 14 políticas.

## Por qué no toca producción

V8.2 son labs read-only. **No modifica** `regime_detector.py`,
`signal_engine.py`, `paper_trader.py`, `strategy_engine.py`, `edge_guard.py`
ni `candidate_ranking.py`. Las simulaciones replay con funciones puras sobre
filas pasadas explícitamente o leídas via wrapper `safe_call`. Si falta una
fuente, devuelven `NEED_DATA` honesto.

## Estructura

```
app/labs/
├── __init__.py
├── bidirectional_forensic_lab.py
├── score_asymmetry_audit.py
├── regime_router_simulator.py
├── trend_campaign_simulator.py
├── profit_lock_simulator.py
└── research_pack_bidirectional_v1.py
```

## CLI

```bash
# Embudo bidireccional con filtro opcional
python -m app.research_lab bidirectional-funnel --hours 168
python -m app.research_lab bidirectional-funnel --hours 168 --side SHORT

# Forensic counterfactual
python -m app.research_lab missed-opportunities --hours 168 --side SHORT
python -m app.research_lab missed-opportunities --hours 168 --side LONG
python -m app.research_lab blocked-counterfactual --hours 168 --side SHORT
python -m app.research_lab failed-executed --hours 168 --side LONG
python -m app.research_lab good-not-monetized --hours 168 --side SHORT

# Score asymmetry + 3 simulaciones research-only
python -m app.research_lab score-asymmetry-audit --hours 168
python -m app.research_lab score-symmetric-simulation --hours 168
python -m app.research_lab score-atr-softened-simulation --hours 168
python -m app.research_lab score-high-vol-directional-simulation --hours 168

# Router research-only
python -m app.research_lab regime-router-simulation --hours 168

# Campaign + Exit
python -m app.research_lab trend-campaign-sim --hours 168 --side SHORT --max-adds 3
python -m app.research_lab profit-lock-sim --hours 168 --side LONG --policy all

# Pack consolidado
python -m app.research_lab research-pack-bidirectional-v1 --hours 168
```

## Endpoints (read-only, auth normal)

- `GET /api/research/bidirectional-funnel`
- `GET /api/research/score-asymmetry-audit`
- `GET /api/research/trend-campaign-sim` (`allow_heavy=false` por defecto)
- `GET /api/research/profit-lock-sim` (`allow_heavy=false` por defecto)
- `GET /api/research/research-pack-bidirectional-v1` (`allow_heavy=false` por defecto)

## Cómo interpretar resultados

### bidirectional-funnel
- `by_side.NO_TRADE` > 80% del total con bias bajista del mercado →
  evidencia fuerte de **asimetría score** contra SHORT.
- `by_reject_reason.score_asymmetry_high_atr` alto → confirma penalty ATR.
- `gross_ev_avg_by_side.SHORT` >> `net_ev_avg_by_side.SHORT` → costes están
  matando edge bruto.

### score-asymmetry-audit
- `gap_long_minus_short > 5` → asimetría material.
- `pct_short_pass_min_score < 0.10` con muestra > 100 → SHORT estructuralmente
  imposible de pasar.

### simulaciones
- `delta_short_pass > 0` en cualquier simulación → la fix correspondiente
  desbloquea SHORT.
- `delta_long_pass < 0` en alguna simulación → ese fix perjudica LONG
  (rojo: no aplicar sin más medición).

### regime-router-simulation
- `by_state.NO_TRADE / samples > 0.30` → router activa NO_TRADE demasiado.
- `by_state.LONG_ONLY + SHORT_ONLY / samples ≈ 0.5-0.7` → router discrimina
  bien.

### trend-campaign-sim
- `optimal_adds` 0 o 1 → pyramiding NO ayuda en ese side/régimen.
- `optimal_adds` 2-3 → pyramiding ayuda con cap moderado.
- `optimal_adds ≥ 5` marcado `HIGH_RISK_SIMULATION` → no implementar.
- `pct_adds_that_helped < 0.50` → la mayoría de adds no contribuyeron.

### profit-lock-sim
- `best_policy != baseline` y `best_delta_pct > 0.10` → política candidata
  para shadow paper.
- `delta_mfe_capture > 10%` con `delta_drawdown < 0` → trailing captura sin
  aumentar riesgo.

## Criterios PASS / FAIL para validar V8.2

| Criterio | PASS |
|---|---|
| compileall | exit 0 |
| pytest target | 100% |
| pytest completo | sin regresiones |
| AST safety scan | 0 hits prohibidos |
| Todos outputs | `research_only=true`, `final_recommendation=NO LIVE` |
| Modificación a regime/signal/paper trader productivos | 0 líneas |

## Checklist VPS post-pull futuro

1. `backup` DB.
2. `git pull origin main`.
3. `python -m compileall app tests`.
4. `python -m pytest -q`.
5. Confirmar safety flags sin cambios (LIVE=False, etc.).
6. `python -m app.research_lab bidirectional-funnel --hours 168`.
7. `python -m app.research_lab score-asymmetry-audit --hours 168`.
8. `python -m app.research_lab score-symmetric-simulation --hours 168`.
9. `python -m app.research_lab regime-router-simulation --hours 168`.
10. `python -m app.research_lab trend-campaign-sim --hours 168 --side SHORT`.
11. `python -m app.research_lab profit-lock-sim --hours 168 --side LONG`.
12. `python -m app.research_lab research-pack-bidirectional-v1 --hours 168`
    → revisar pack y decidir qué cambios a `regime_detector` /
    `signal_engine` / `paper_trader` proponer en tanda separada.

## Qué NO añade V8.2

- No live, no paper filter, no órdenes reales.
- No endpoints privados nuevos.
- No `set_leverage` / `set_margin_mode`.
- No CCXT / LangGraph / dependencias externas.
- No tocar tablas existentes. No migraciones destructivas.
- No tocar `regime_detector.py`, `signal_engine.py`, `paper_trader.py`.

## V8.3 (siguiente, no incluido)

Solo si V8.2 reporta `delta_short_pass > 0` material con datos VPS:

- Aplicar simetría regime adjustment.
- Aplicar softening ATR penalty.
- Conectar Regime Router al signal_engine en modo observer.
- Activar trailing post-TP2 en paper_trader (research-shadow primero).

`FINAL_RECOMMENDATION: NO LIVE.`
