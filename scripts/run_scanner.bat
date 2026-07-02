@echo off
REM BitgetBot V10.28 - Multi-Symbol Shadow Opportunity Scanner (VISIBLE window)
REM RESEARCH ONLY. Public OHLCV only. NO API keys, NO orders, NO live, NO paper.
REM Scans a universe of liquid USDT-perps, ranks candidate setups, and either
REM proposes SHADOW (simulated) entries or STAYS OUT. Makes NO real/paper trades.
title BitgetBot Shadow Scanner (RESEARCH ONLY - NO LIVE)
cd /d "%~dp0.."
echo ============================================================
echo  BitgetBot V10.28 Multi-Symbol Shadow Opportunity Scanner
echo  RESEARCH ONLY. Public data. NO keys, NO orders, NO live.
echo  Close cleanly: press Ctrl+C, or type  q / quit / exit / stop
echo ============================================================
echo  DASHBOARD (pegalo en el navegador; se refresca solo):
echo  file:///C:/Users/Adrian/Documents/New%%20project/bitget-ai-trading-bot/reports/research/v10_29/status.html
echo ============================================================
echo.
python -m app.research_lab opportunity-scanner-run-v1028 ^
  --universe BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,ADAUSDT,AVAXUSDT,LINKUSDT,DOGEUSDT,LTCUSDT,BCHUSDT,DOTUSDT,NEARUSDT,APTUSDT,ARBUSDT,OPUSDT,SUIUSDT,INJUSDT,ATOMUSDT ^
  --timeframe 15m --days 7 --interval-seconds 60 --max-scans 0 --request-budget 2
echo.
echo Scanner stopped. Press any key to close this window.
pause >nul
