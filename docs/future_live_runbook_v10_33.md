# Future Live Runbook (V10.33) — NO LIVE HOY

Este documento existe para que, SI algún día se aprueba operar en real, no se
improvise nada. Hoy: `live_ready=false`, `can_send_real_orders=false`,
`FINAL_RECOMMENDATION: NO LIVE`. Nada de este runbook autoriza a operar.

## Condiciones que BLOQUEAN live (todas deben resolverse antes de discutirlo)
Ejecutar `python -m app.research_lab future-live-readiness-audit`. Las 10
puertas duras (edge validado OOS+forward, walk-forward, anti-overfit, net EV
positivo tras costes, paper/shadow consistente ≥30d, kill switch probado,
circuit breakers probados, reconciliación diseñada, runbook, aprobación humana
explícita) deben estar TODAS en `[x]`. Hoy están todas en `[ ]`.

## Escalera de promoción (nunca automática con dinero real)
`research → shadow → paper → micro_live → limited_live` — cada salto requiere
todas sus puertas + firma humana. `micro_live` = importe mínimo absoluto.

## Antes de encender (el día que toque)
1. `future-live-readiness-audit` → `checklist_complete: true` y `ACTUAL_LIVE_READY: false` hasta aprobación humana final.
2. `future-live-preflight-dry-run` → todos los checks OK.
3. Dashboard fresco y en verde; datos NO stale; sin errores de colector.
4. Kill switch probado ESE día (manual y automático).
5. Límite diario de pérdida configurado y verificado.
6. Persona responsable delante de la pantalla durante la primera sesión.

## Cómo PARAR (siempre disponible)
- Kill switch → detiene todo envío de órdenes inmediatamente (fail-closed).
- Ctrl+C en las consolas → apagado limpio con estado guardado.
- Si hay dudas: parar primero, investigar después. Parar nunca es un error.

## Si algo diverge en live (posición/orden/balance inesperado)
1. Kill switch. 2. NO enviar órdenes "correctoras" a mano sin reconciliar.
3. Reconciliación: posición esperada vs real, órdenes esperadas vs reales,
   huérfanas/stale/fills fantasma, balance. 4. Documentar antes de reanudar.

## Si el exchange da errores o no llegan datos
- Errores de exchange → los circuit breakers deben haber parado ya; verificar.
- Datos stale > umbral → halt automático (DATA_STALE); no operar a ciegas.
- No reintentar órdenes sin idempotency key (client_order_id): at-most-once.

## Qué NO hacer jamás
- Saltarse una puerta "solo esta vez". - Subir tamaño tras pérdidas.
- Desactivar un breaker en caliente. - Operar sin dashboard/heartbeat.
- Confundir score del scanner con señal: `NOT_ACTIONABLE` significa eso.
