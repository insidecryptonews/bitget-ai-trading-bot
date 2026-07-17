@echo off
REM Adrian Trading Intelligence V2 - visible forward-shadow observer.
REM RESEARCH ONLY. Local validated snapshots. NO keys, NO orders, NO live, NO paper.
title ATI Shadow Observer (RESEARCH ONLY - NO LIVE)
cd /d "%~dp0.."
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo ============================================================
echo  ADRIAN TRADING INTELLIGENCE V2 - SHADOW OBSERVER
echo  RESEARCH ONLY. NO LIVE. NO PAPER FILTER. NO ORDERS.
echo  can_send_real_orders=false
echo  Close cleanly with Ctrl+C.
echo ============================================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_ati_shadow_forever.ps1"
echo.
echo ATI shadow observer stopped. Press any key to close this window.
pause >nul
