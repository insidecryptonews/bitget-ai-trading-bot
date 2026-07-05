# Tooling Stack Research (V10.37) — clasificación honesta

RESEARCH_ONLY · NOT_ACTIONABLE · NO LIVE. Nada de esta lista autoriza keys,
pagos ni ejecución real. "Adoptar" significa adoptar para RESEARCH.

| Herramienta | Qué es | Licencia/Coste | Red/Keys | ¿Puede tocar órdenes? | Utilidad real aquí | Prioridad | Recomendación |
|---|---|---|---|---|---|---|---|
| TradingView Lightweight Charts | Librería JS de gráficos financieros (open source, Apache-2.0) | Gratis | No (local, offline tras descarga) | No | Dashboard local avanzado: velas + marcadores de hipótesis del lab V10.35 sobre NUESTROS datos (no TV como fuente) | **P1** | Preparar cuando exista el lab; sustituiría al HTML estático solo si aporta |
| CCXT | Librería multi-exchange (MIT) | Gratis | Red pública; keys SOLO si se usan privados | SÍ (si le das keys) | Adapters de datos públicos normalizados para research multi-exchange | **P2** | Investigar más; nuestro stack stdlib ya cubre Bybit/Binance público. Si se adopta: wrapper research-only con allowlist, jamás keys |
| Binance Developer Center/API | Docs + endpoints oficiales | Gratis | Pública | SÍ (endpoints privados) | Diagnóstico (ya lo usamos: fapi REST + data dumps); ejecución bloqueada | **P2** | Ya en uso implícito para lo público; nada nuevo que adoptar |
| Hyperliquid Python SDK | SDK del DEX Hyperliquid | Gratis (MIT) | Red; wallet para operar | SÍ (con wallet) | Posible fuente futura de datos on-chain de perps; hoy no aporta al camino Bybit | **P3** | Solo futuro; sin wallet, sin keys |
| CrewAI / LangChain | Frameworks de orquestación de agentes LLM | Gratis (MIT); LLM detrás cuesta | Red + API keys de LLM | Indirectamente (si un agente tiene tools) | Orquestación research; hoy añade complejidad sin valor: nuestro pipeline es determinista y testeado | **P3 / NO USAR por ahora** | Descartar hasta que exista una necesidad concreta multi-agente |
| Ollama | Runner local de LLMs | Gratis | Local (descarga modelos) | No | Resúmenes locales de logs largos; NUNCA decisiones de trading | **P3** | Solo futuro; útil si los logs crecen mucho |
| Tavily | API de búsqueda web para LLMs | API key; freemium | Red + key | No | Búsqueda viva (noticias/eventos); riesgo prompt-injection + coste | **NO USAR sin aprobación** | Requiere aprobación explícita; no activar |
| Mem0 | Capa de memoria para agentes | API key/self-host | Depende | No | Memoria de agentes; riesgo de fuga de datos sensibles | **P3 / NO USAR por ahora** | Nuestro sistema de memoria actual basta |
| pxpipe | Proxy que comprime contexto LLM como imagen (OCR-risk) | Gratis (npx) | Local proxy | No | Ahorro opcional de tokens en bloques voluminosos NO críticos | **P2 opcional** | Ver docs/pxpipe_optional_usage.md; OFF por defecto |
| Azure AI Foundry (Claude) | Claude vía Azure con crédito/facturación Azure | Cuenta Azure + tarjeta; crédito temporal posible | Red + Azure key | No (es el LLM, no el bot) | Vía alternativa para límites/créditos de Code | **P2 opcional** | Ver docs/azure_foundry_claude_code_optional.md; manual, no en tareas críticas hasta validar |

## Síntesis
- **Nada de esta lista acelera el edge por sí mismo.** El cuello real es datos+validación (V10.32/V10.36/V10.35), no tooling.
- Adoptar ahora: nada nuevo (lo público de Binance/Bybit ya está integrado a mano con allowlists más estrictas que cualquier librería).
- Preparar: Lightweight Charts (cuando el lab produzca hipótesis que revisar visualmente).
- Con aprobación previa: pxpipe (tokens), Azure Foundry (créditos), Tavily (nunca sin aprobación).
- Descartar por ahora: CrewAI/LangChain/Mem0 (complejidad sin necesidad), Hyperliquid (otro camino).

FINAL_RECOMMENDATION: NO LIVE.
