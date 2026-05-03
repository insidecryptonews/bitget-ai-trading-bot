# Research Lab Report

## Resumen ejecutivo

- Recomendacion live: **NO ACTIVAR LIVE**. Esta fase es research-only.
- Dataset: 160 observaciones, 0 labels, 0 reverse/shadow labels.
- Profit factor global: 0.00. Expectancy: 0.00000.
- Mejor candidato: sin candidato con evidencia suficiente.
- Peor estrategia: sin evidencia suficiente.

## Estado del dataset

- Observaciones totales: 160
- Labels totales: 0
- Labels TIME: 0
- Labels SL: 0
- Labels TP1: 0
- Labels TP2: 0

## Normal vs reverse

- Normal: labels=0, PF=0.00
- Reverse: labels=0, PF=0.00

## Estrategias candidatas

- Sin estrategias candidatas. Evidencia insuficiente o edge negativo.

## Rechazos principales

- all_labeled: REJECTED_TOO_FEW_SAMPLES, labels=0, PF=0.00, expectancy=0.00000
- normal_only: REJECTED_TOO_FEW_SAMPLES, labels=0, PF=0.00, expectancy=0.00000
- reverse_only: REJECTED_TOO_FEW_SAMPLES, labels=0, PF=0.00, expectancy=0.00000

## Simbolos y regimenes

### Simbolos
- Sin labels suficientes.

### Regimenes
- Sin labels suficientes.

## TP/SL recomendado

- No implementado en fase 1. El optimizador TP/SL avanzado queda bloqueado hasta que esta fase pase tests.

## Configuracion recomendada

- Ver `recommended_config.env`. Nunca activa `LIVE_TRADING=true` ni `DRY_RUN=false`.

## Riesgos y limitaciones

- El dataset puede estar sesgado por periodos de mercado concretos.
- Las labels TIME excesivas debilitan cualquier conclusion.
- Walk-forward basico solo valida estabilidad temporal inicial; no sustituye paper prolongado.

## Proxima accion sugerida

- Mantener Railway en PAPER + research hasta tener PF > 1.2 estable y al menos 100 labels por hipotesis.
