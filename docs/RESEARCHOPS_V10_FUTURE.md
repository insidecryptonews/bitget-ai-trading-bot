# ResearchOps V10 — Micro-Live Readiness (FUTURE / NOT IMPLEMENTED)

**Estado:** sólo nota de diseño. **V10 no se implementa en esta tanda.**

V10 sólo existirá si en su día se cumplen TODAS estas condiciones simultáneas:

- Data quality OK durante un periodo sostenido (no sólo en un snapshot puntual).
- OHLCV fresco para todos los símbolos en todos los timeframes activos.
- net_EV positivo con muestra suficiente y estable.
- net_PF positivo y > umbral conservador (mínimo 1.25 sugerido).
- Walk-forward V2 positivo con bootstrap CI 95% low > 0.
- Cost / funding / slippage stress sin colapsar la edge.
- Drawdown aceptable y risk-of-ruin proxy bajo.
- Paper/shadow estable durante semanas.

## Lo que V10 NO añade automáticamente

V10 nunca añadirá por inercia:

- Activación live.
- Activación de paper filter.
- Cambios de leverage / margin / sizing / slots.
- Apertura de órdenes reales.
- Endpoints privados nuevos.
- Botones live en el dashboard.

V10 deberá ser una **decisión humana explícita**, con tiempo, capital de prueba mínimo, y rollback plan.

## Composición sugerida de V10 cuando llegue

1. Confirmación de gates V9 sostenidos durante una ventana mínima (sugerido: 30 días).
2. Activación de `PAPER_TRADING=True` con `ENABLE_PAPER_POLICY_FILTER=False` (estado actual).
3. Definir un universo mínimo (símbolo único, leverage 1x, capital 5–10 USDT).
4. Definir tope diario de pérdida y de número de trades.
5. Definir mecanismo de auto-pause irreversible (kill-switch).
6. Validar funding y liquidation models con datos reales backfilleados.
7. Sólo entonces, considerar live micro-pilot.

## Estado actual

V10 = pendiente, sin código activado. La presente foundation (V7.5 + V8/V9) deja
preparada la arquitectura para investigar la edge, no para abrir órdenes.

`FINAL_RECOMMENDATION: NO LIVE.`
