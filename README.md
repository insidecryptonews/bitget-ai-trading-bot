# Bitget AI Trading Bot

Bot modular de trading automático para Bitget USDT-Futures. Está diseñado para operar primero en `PAPER`/`DRY_RUN` y solo enviar órdenes reales si se activa explícitamente:

```env
PAPER_TRADING=false
LIVE_TRADING=true
DRY_RUN=false
```

No promete beneficios. El trading apalancado puede liquidar una cuenta pequeña muy rápido. El objetivo del sistema es buscar setups de mejor calidad y proteger capital, no operar por operar.

## Qué Hace

- Escanea BTC, ETH, SOL y altcoins líquidas configurables.
- Calcula EMAs, RSI, MACD, ATR, Bollinger, VWAP, volumen relativo, soportes, resistencias, momentum y volatilidad.
- Detecta régimen: `TREND_UP`, `TREND_DOWN`, `RANGE`, `HIGH_VOLATILITY`, `BREAKOUT_POSSIBLE`, `CHOPPY_MARKET`, `RISK_ON`, `RISK_OFF`.
- Elige automáticamente entre trend following, pullback, breakout, momentum rápido, reversión controlada, rechazo de soporte/resistencia o `NO_TRADE`.
- Decide si abrir una operación, dos o ninguna con `PortfolioAllocator`.
- Bloquea operaciones sin stop, sin take profit, con mal R:R, spread alto, tamaño mínimo peligroso o pérdida diaria/semanal excedida.
- Envía alertas por Telegram si se configuran credenciales.
- Expone `/health` para Railway.

## Qué No Hace

- No usa martingala, grid infinito ni DCA agresivo.
- No usa leverage superior a 5x.
- No opera sin stop loss y take profit.
- No inventa noticias ni abre trades por titulares no verificados.
- No necesita permisos de retirada.
- No garantiza rentabilidad.

## Instalación Local

```bash
cd bitget-ai-trading-bot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python -m app.main
```

Por defecto arranca seguro: `PAPER_TRADING=true`, `LIVE_TRADING=false`, `DRY_RUN=true`.

## Configurar `.env`

Rellena solo lo necesario. No subas `.env` a GitHub; ya está en `.gitignore`.

Para paper trading:

```env
PAPER_TRADING=true
LIVE_TRADING=false
DRY_RUN=true
```

Para dry-run:

```env
PAPER_TRADING=false
LIVE_TRADING=false
DRY_RUN=true
```

Para live real:

```env
PAPER_TRADING=false
LIVE_TRADING=true
DRY_RUN=false
BITGET_API_KEY=...
BITGET_API_SECRET=...
BITGET_PASSPHRASE=...
```

Si `LIVE_TRADING=true` pero `DRY_RUN=true`, el bot no enviará órdenes reales.

## Uso con 40 USDT

El perfil `aggressive_small_account` limita el riesgo por operación al 2.5%, pérdida diaria al 8%, pérdida semanal al 18%, máximo 2 posiciones y normalmente 1 si el balance es menor de 30 USDT. Si Bitget exige un mínimo que obligue a arriesgar más de lo permitido, la operación se bloquea.

Configuracion recomendada para 40 USDT:

```env
MARGIN_MODE=isolated
FORCE_ISOLATED_MARGIN=true
DISALLOW_CROSSED_MARGIN=true
AUTO_MARGIN=false
USE_FIXED_TRADE_MARGIN=true
TRADE_MARGIN_USDT=12.00
MAX_TRADE_MARGIN_USDT=15.00
SMALL_ACCOUNT_MAX_OPEN_POSITIONS=1
MAX_OPEN_POSITIONS=1
```

El bot opera siempre en `isolated margin`. `cross`/`crossed` queda prohibido. Con 40 USDT usa margen fijo de 12 USDT por operacion: con 3x abre unos 36 USDT nocionales, y con 5x unos 60 USDT. No se recomienda subir `MAX_TRADE_MARGIN_USDT` al principio.

Si Bitget no permite cambiar un simbolo a isolated porque hay ordenes o posiciones abiertas, el bot bloquea ese simbolo y no abre una operacion nueva hasta revision.

## API de Bitget

Crea una API key con permisos mínimos de trading futures. No actives permisos de retirada. Este bot no necesita withdrawals/retiros y nunca guarda ni imprime claves.

Permisos recomendados:

- Futures orders/trading.
- Futures positions/holdings.
- Sin withdrawal.
- IP whitelist si puedes usar una IP estable.

## Telegram

Crea un bot con BotFather, obtén `TELEGRAM_BOT_TOKEN` y tu `TELEGRAM_CHAT_ID`. Alertas incluidas: inicio, modo, balance, señales, operaciones, protecciones, cierres, PnL, errores críticos y circuit breakers.

## Railway

1. Sube este proyecto a GitHub.
2. Crea un servicio en Railway desde el repo.
3. Para probar 24 horas en paper + research, copia el contenido de `.env.railway.paper.example` en Railway > Variables > RAW Editor.
4. Mantén `PAPER_TRADING=true` al principio.
5. Revisa logs.
6. Cambia a live solo cuando hayas probado paper/dry-run.

No subas tu `.env` real a GitHub. `.env.railway.paper.example` no contiene claves privadas y sirve como plantilla segura para Railway.

Railway usará `Dockerfile` o `Procfile` y ejecutará:

```bash
python -m app.main
```

Health check:

```text
/health
```

Devuelve modo, uptime, posiciones abiertas, PnL diario, último escaneo y circuit breaker.

En `PAPER_TRADING=true`, el bot no requiere `BITGET_API_KEY`, `BITGET_API_SECRET` ni `BITGET_PASSPHRASE`; usa datos publicos de mercado, no envia ordenes reales y sigue guardando `signal_observations` y `signal_labels`. Con `META_MODEL_MODE=observe_only`, el meta-model no bloquea senales.

Si `ENABLE_RESEARCH_AUTO_REPORT=true`, el bot imprime el informe de investigacion en los logs cada `RESEARCH_REPORT_INTERVAL_MINUTES`. En Railway puedes buscar:

```text
RESEARCH REPORT START
```

para ver el bloque completo sin abrir una shell.

## GitHub Seguro

```bash
git init
git add .
git commit -m "Initial Bitget trading bot"
git remote add origin <tu-repo>
git push -u origin main
```

Antes de subir:

```bash
git status
```

Comprueba que `.env`, bases SQLite, logs y `.venv` no aparecen.

## Backtest

`app/backtester.py` incluye un baseline conservador para probar datos OHLCV. Antes de live, ejecuta backtests por símbolo/timeframe y revisa win rate, profit factor, drawdown, rachas de pérdidas, comisiones y slippage.

Si activas live sin backtest validado, el bot mostrará advertencia: riesgo elevado.

## Research Engine

El bot puede guardar cada senal generada aunque no se opere. Esto permite investigar que senales habrian funcionado y cuales conviene filtrar sin cambiar la estrategia base por una "caja magica".

Variables principales:

```env
ENABLE_FEATURE_LOGGING=true
ENABLE_SIGNAL_LABELING=true
ENABLE_META_MODEL=false
ENABLE_RESEARCH_AUTO_REPORT=true
RESEARCH_REPORT_INTERVAL_MINUTES=60
META_MODEL_MODE=observe_only
META_MODEL_MIN_SAMPLES=300
META_MODEL_MIN_POSITIVES=50
META_MODEL_MIN_NEGATIVES=50
META_MIN_PROBABILITY=0.58
MAX_HOLDING_BARS=48
LABEL_USE_TP2=false
```

`FeatureLogger` guarda observaciones en `signal_observations`: simbolo, lado, estrategia, score, regimen, entrada, stop, TPs, R:R, spread, volumen, funding, open interest, indicadores tecnicos, contexto BTC/ETH, si fue seleccionada por el allocator, si RiskManager la aprobo, si se opero y por que se bloqueo.

`TripleBarrierLabeler` etiqueta senales con triple barrera:

- `+1`: TP antes de SL.
- `-1`: SL antes de TP.
- `0`: no resolvio antes de la barrera temporal.

`MetaModel` no decide LONG/SHORT desde cero. Solo actua como filtro de segunda capa sobre senales que el bot ya genero. Nunca puede quitar stop, quitar take profit, subir leverage, saltarse isolated margin, saltarse RiskManager ni abrir una operacion bloqueada por riesgo.

Modos:

- `META_MODEL_MODE=off`: desactivado.
- `META_MODEL_MODE=observe_only`: calcula/observa si hay modelo, pero no bloquea.
- `META_MODEL_MODE=filter`: puede bloquear senales si el modelo esta validado out-of-sample.

No actives `filter` en live hasta tener al menos 300 senales etiquetadas, 50 positivas, 50 negativas y validacion walk-forward favorable. El riesgo principal aqui es el overfitting: un modelo puede parecer brillante sobre el pasado y fallar en mercado real.

Reporte:

```bash
python -m app.research_engine report
```

El reporte muestra total de senales, conteos reales por tabla, ultimas trades, ultimas observaciones operadas, ultimas labels, win rate y profit factor por estrategia, simbolo y regimen, mejores buckets de RSI, volumen relativo, ATR, spread y distancia a EMA, ademas de recomendaciones para filtrar o potenciar estrategias.

Exportar datos:

```bash
python -m app.research_engine export
```

Genera `exports/research_export_<timestamp>/` con CSV y JSON de `signal_observations`, `signal_labels`, `trades` y resumenes por simbolo, estrategia, regimen, RSI, volumen relativo, ATR y spread. La carpeta `exports/` esta ignorada por Git.

Variantes shadow:

```bash
python -m app.research_engine variants
```

El bot guarda variantes hipoteticas que nunca se operan: umbrales de score, ratios TP/SL, filtros de regimen, long-only, short-only y reverse shadow. Reverse comprueba si una senal LONG habria funcionado mejor como SHORT, o viceversa, manteniendo entry, timestamp y features, pero invirtiendo SL/TP de forma coherente. Estas variantes solo existen para investigacion.

Interpretacion de labels:

- `TP1`/`TP2`: la barrera de beneficio se alcanzo antes que el stop.
- `SL`: el stop se alcanzo antes que el take profit.
- `TIME`: no resolvio dentro de `MAX_HOLDING_BARS`; muchas `TIME` suelen indicar poca direccionalidad o targets/stops mal calibrados.

Guardrails: no consideres una variante como prometedora con menos de 100 labels. Si profit factor < 1.2, win rate muy bajo o hay demasiadas `TIME`, no actives live. Aunque una variante reverse tenga mejor profit factor, si tiene pocas labels o demasiadas `TIME`, queda marcada como evidencia debil.

Kronos research-only:

```bash
python -m app.research_lab kronos-once --limit 100
python -m app.research_lab kronos-evaluate
python -m app.research_lab reconcile-paper
```

Kronos queda apagado por defecto con `ENABLE_KRONOS_RESEARCH=false`. Si lo activas, descarga/carga el modelo opcional configurado, genera predicciones de velas y las cruza con `signal_labels`, shadow/reverse y virtual portfolio. No aprueba ordenes, no toca RiskManager, no usa ExecutionEngine y siempre reporta `NO LIVE`.

`reconcile-paper` limpia solo operaciones simuladas `PAPER_OPEN`: cierra fantasmas antiguos por label o por tiempo y deja el conteo de paper abierto coherente. No toca trades live ni APIs del exchange. El arranque automatico existe pero esta apagado por defecto con `ENABLE_PAPER_RECONCILE_ON_START=false`.

Variables principales:

```env
ENABLE_KRONOS_RESEARCH=false
KRONOS_MODEL_NAME=NeoQuasar/Kronos-mini
KRONOS_TOKENIZER_NAME=NeoQuasar/Kronos-Tokenizer-base
KRONOS_LOOKBACK=256
KRONOS_PRED_LEN=12
KRONOS_MAX_SYMBOLS_PER_RUN=5
```

`walkforward.py` separa train, validation y test en ventanas temporales rolling. El meta-model solo deberia activarse si mejora profit factor fuera de muestra, reduce drawdown, mantiene precision minima y no elimina demasiadas operaciones.

## Checklist antes de Railway Paper

```bash
python -m compileall app tests
python -m pytest -q
python -m app.main
python -m app.research_engine report
python -m app.research_engine variants
python -m app.research_engine export
```

Para Railway en paper, confirma:

- `PAPER_TRADING=true`
- `LIVE_TRADING=false`
- `DRY_RUN=true`
- `ENABLE_FEATURE_LOGGING=true`
- `ENABLE_SIGNAL_LABELING=true`
- `ENABLE_META_MODEL=true`
- `META_MODEL_MODE=observe_only`
- `MAX_OPEN_POSITIONS=1`
- `SMALL_ACCOUNT_MAX_OPEN_POSITIONS=1`

## Parar el Bot

Local: `Ctrl+C`.

Railway: pausa o elimina el deployment, o cambia variables:

```env
LIVE_TRADING=false
DRY_RUN=true
```

## Revisar Operaciones

Localmente se guarda `bot_state.db` con trades, eventos y estado. En Railway puedes usar `DATABASE_URL` para PostgreSQL.

## Cambiar Riesgo

Variables principales:

- `MAX_RISK_PER_TRADE`
- `MAX_DAILY_LOSS`
- `MAX_WEEKLY_LOSS`
- `MAX_MARGIN_USAGE_PER_TRADE`
- `MAX_TOTAL_MARGIN_USAGE`
- `MARGIN_SAFETY_BUFFER_USDT`
- `MIN_FREE_MARGIN_AFTER_TRADE`
- `MIN_STOP_DISTANCE_PCT`
- `MAX_NOTIONAL_PER_TRADE_SMALL_ACCOUNT`
- `MAX_OVER_NOTIONAL_DEVIATION_PCT`
- `MAX_UNDER_NOTIONAL_DEVIATION_PCT`
- `USE_FIXED_TRADE_MARGIN`
- `TRADE_MARGIN_USDT`
- `MAX_TRADE_MARGIN_USDT`
- `SMALL_ACCOUNT_MAX_OPEN_POSITIONS`
- `MAX_OPEN_POSITIONS`
- `MIN_SCORE_TO_TRADE`
- `DEFAULT_LEVERAGE`
- `MAX_LEVERAGE`

No pongas `MAX_LEVERAGE` por encima de 5; el código lo limita igualmente.

## Añadir Símbolos

Edita:

```env
SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT,...
```

Antes de operar, el bot consulta contratos reales de Bitget y bloquea símbolos no activos o sin reglas válidas.

## Si Algo Falla

- Si falla un símbolo, sigue con los demás.
- Si falla la API repetidamente, pausa trading.
- Si detecta una posición real sin protección, alerta y puede cerrar si `CLOSE_IF_PROTECTION_FAILS=true`.
- Si llega a pérdida diaria/semanal máxima, bloquea entradas.
- Si Railway reinicia, sincroniza posiciones reales antes de operar.

Opera pequeño, revisa logs y no des permisos de retirada. La parte más rentable de muchos bots es saber quedarse quieto.
