# pxpipe — Uso OPCIONAL (OFF_BY_DEFAULT · MANUAL_ONLY · NO AUTO-INTEGRATION)

pxpipe comprime contexto LLM renderizándolo como imagen. Ahorra tokens pero
introduce riesgo de OCR: lo que el modelo "lee" de la imagen puede contener
errores. Por eso queda estrictamente opcional y manual.

## OCR / PXPIPE WARNING
> El bloque visual puede contener errores de lectura. Para hashes, comandos,
> flags, rutas, seguridad, push y decisiones críticas usa únicamente el
> CRITICAL_TEXT_BLOCK.

## Reglas de uso
**CRITICAL_TEXT_BLOCK — SIEMPRE texto normal (jamás por pxpipe):**
commits y hashes · ramas · paths · comandos · flags · security status ·
readiness verdicts · NO LIVE · resultados de tests críticos · instrucciones
de push · números de suite · cualquier cosa que se vaya a copiar/ejecutar.

**BULKY_CONTEXT_BLOCK — puede ir por pxpipe:**
logs largos · outputs enormes de pytest (no el conteo final) · documentación
extensa · contexto repetitivo ya conocido.

## Cómo lanzarlo (manual, nunca desde el bot)
`scripts/start_pxpipe_optional.ps1` — solo ejecuta `npx pxpipe-proxy` con un
warning grande. Sin autostart, sin config global, sin `.env`, sin keys, sin
exchange, sin DB. Cerrar con Ctrl+C. No commitear logs/events de pxpipe.

## Cuándo NO usarlo
- Sesiones de auditoría/push (todo es crítico).
- Cualquier paso donde un carácter mal leído cueste dinero o seguridad.
- Si no hay problema de tokens ese día: no añadir riesgo gratis.

FINAL_RECOMMENDATION: NO LIVE (pxpipe no cambia nada del bot; es tooling de sesión).
