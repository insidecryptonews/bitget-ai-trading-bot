# HANDOFF CORTO — Bitget AI Trading Bot / ResearchOps (V10.39.1)

Para pegar rápido en un chat nuevo. Español, directo. **NO LIVE. NO hay edge validada.**

- **Repo:** `insidecryptonews/bitget-ai-trading-bot`
- **Local:** `C:\Users\Adrian\Documents\New project\bitget-ai-trading-bot`
- **Branch:** `local-v10-8-1-research`
- **HEAD == origin/main:** `56ea54ec8defc988d86bb2edb7be360e3c446824`
- **Fase actual:** V10.39 + V10.39.1 (Alpha Improvement Sprint + CLI search + tests multitimeframe), Codex APTO, pusheado.

**Commits clave:**
```
56ea54e V10.39.1 (search CLI + multitimeframe tests)
32f97d1 V10.39 (alpha improvement sprint)
53d31a3 V10.38.1 (fix leakage + bar availability)
cacc343 V10.38 (continuous edge factory)
3ab891b V10.36 hotfix (fail-close source stamp)
```

**Seguridad:** `SAFE_PAPER_ONLY` · `can_send_real_orders=false` · `paper_filter_enabled=false` · `actual_live_ready=false` · `FINAL_RECOMMENDATION=NO LIVE`.

**Collectors/dataset:** Bybit forward público en `external_data/staging/bybit_microstructure_v10_32/dataset/` (gitignored). ~239k trades, dup 0.00%, `source_exchange=bybit_linear`, `errors: []`, creciendo (~1053 barras 1m). Solo captura con el PC encendido; hueco con PC apagado = normal, NO es fallo. El collector Bybit no tiene autostart y muere si lo lanza una sesión de Claude → Adrián debe lanzarlo desde su consola.

**Estado edge (honesto):** `0 promising` · `NO_EDGE_ALL_REJECTED_RESEARCH_ONLY`. Mejor familia `micro_momentum` = `REJECTED_COSTS_TOO_HIGH`. Coste ≈18bps > señal bruta ≈10–12bps → `net_EV` y `net_EV_lower_bound` negativos. Ningún timeframe rescata. Esto es correcto, no un fallo.

**CLIs principales:**
```
python -m app.research_lab continuous-edge-cycle-v1038 --symbols BTCUSDT
python -m app.research_lab alpha-improvement-search-v1039 --symbols BTCUSDT
python -m app.research_lab alpha-improvement-diagnose-v1039 --symbols BTCUSDT
python -m app.research_lab security-audit
```

**Reglas:** Code implementa → Codex audita → si bug, hotfix antes de push. Full suite (2443 verde) valida metodología, NO rentabilidad. No fiarse solo de "tests passed". Untracked `CODEX_RESULT.md`/`CODE_RESULT.md` NO se commitean.

**Siguiente paso:** acumular datos hacia ~30 días; correr ciclos periódicos; NO forzar edge; cualquier promising futuro → auditoría antes de shadow/paper.

**Errores ya corregidos que no repetir:** (1) PC apagado ≠ collector caído; (2) proceso de diagnóstico PowerShell que se auto-detecta ≠ collector duplicado; (3) 0 promising ≠ fallo; (4) edge sintético de tests ≠ edge real; (5) no proponer persistencia extra si el autostart funciona; (6) research ≠ listo para operar.

**NO LIVE. NO paper filter. NO órdenes. NO `.env`. NO keys. FINAL_RECOMMENDATION=NO LIVE.**
