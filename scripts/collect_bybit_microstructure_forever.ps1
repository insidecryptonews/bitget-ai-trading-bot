# BitgetBot - Bybit FULL MICROSTRUCTURE Collector loop (ResearchOps V10.36) - VISIBLE console
# (renamed from collect_bybit_liquidations_forever.ps1: since V10.32 each cycle also
#  collects trades/orderbook/OI/funding, not only liquidations)
# RESEARCH ONLY. Public Bybit v5 linear websocket. NO API keys, NO orders, NO live, NO paper.
# SEPARATE alternative cross-exchange source (design OPTION A): its rows are NEVER merged
# into the Binance sample and NEVER produce MICROSTRUCTURE_RESEARCH_READY.
# This script is INDEPENDENT of the Binance collector loop and has NO autostart:
# launch it manually (double-click or from a console) only when you decide to.
# Stop safely with Ctrl+C: state is saved at the end of every cycle.

$ErrorActionPreference = "Continue"

# repo root = parent of this script's folder (scripts/)
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

# prefer the repo venv python when it exists; fall back to PATH python
$py = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$symbols = "BTCUSDT,ETHUSDT,SOLUSDT"

$dir = Join-Path $repo "external_data\staging\bybit_liquidations_v10_30"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$log = Join-Path $dir "collector.log"

# single instance per user session (own mutex, independent from the Binance loop)
$mtx = New-Object System.Threading.Mutex($false, "Local\BitgetBotBybitLiqV1030")
try { $acquired = $mtx.WaitOne(0) }
catch [System.Threading.AbandonedMutexException] { $acquired = $true }
if (-not $acquired) {
    Write-Host "Otro colector Bybit ya esta corriendo; cierro esta ventana en 10s." -ForegroundColor Yellow
    "$(Get-Date -Format s) another bybit collector already running; exiting" | Add-Content $log
    Start-Sleep -Seconds 10
    return
}

$host.UI.RawUI.WindowTitle = "BitgetBot Bybit Microstructure (RESEARCH ONLY - NO LIVE)"
Write-Host "==============================================================" -ForegroundColor Cyan
Write-Host " BitgetBot - Colector Bybit MICROESTRUCTURA COMPLETA (V10.32/V10.36)" -ForegroundColor Cyan
Write-Host " FUENTE ALTERNATIVA cross-exchange. NUNCA se mezcla con Binance." -ForegroundColor Cyan
Write-Host " NUNCA produce READY. Sin claves, sin ordenes, NO LIVE." -ForegroundColor Cyan
Write-Host " Simbolos: $symbols" -ForegroundColor Cyan
Write-Host " Para PARAR de forma segura: pulsa Ctrl+C." -ForegroundColor Yellow
Write-Host "==============================================================" -ForegroundColor Cyan
"$(Get-Date -Format s) bybit liquidations collector started (research-only, NO LIVE)" | Add-Content $log

$cycle = 0
try {
    while ($true) {
        $cycle += 1
        # simple log rotation: keep the live log under ~5 MB
        if ((Test-Path $log) -and ((Get-Item $log).Length -gt 5MB)) {
            Move-Item -Force $log ($log + ".1")
            "$(Get-Date -Format s) log rotated" | Add-Content $log
        }
        Write-Host ""
        Write-Host ">>> CICLO $cycle  $(Get-Date -Format s)  (escuchando liquidaciones Bybit ~5 min...)" -ForegroundColor Green
        try {
            & $py -m app.research_lab bybit-liquidations-ws-collect-v1030 `
                --symbols $symbols --apply --max-runtime-seconds 300 --max-events 5000 |
                Tee-Object -FilePath $log -Append
        } catch {
            Write-Host "ERROR ciclo: $($_.Exception.Message)" -ForegroundColor Red
            "$(Get-Date -Format s) ERROR $($_.Exception.Message)" | Add-Content $log
            Start-Sleep -Seconds 15
        }
        Write-Host ""
        Write-Host ">>> V10.32: sample completo bybit_linear (trades/OB/OI/funding + sync liq)..." -ForegroundColor Green
        try {
            & $py -m app.research_lab bybit-microstructure-run-cycle-v1032 `
                --symbols BTCUSDT --apply |
                Select-String -Pattern "added_this_cycle|cumulative_added|errors" |
                ForEach-Object { Write-Host ("  " + $_.Line) }
        } catch {
            Write-Host "ERROR v1032: $($_.Exception.Message)" -ForegroundColor Red
            "$(Get-Date -Format s) ERROR v1032 $($_.Exception.Message)" | Add-Content $log
        }
        try {
            $page = & $py -m app.research_lab free-microstructure-status-page-v1029 |
                Select-String -Pattern "DASHBOARD:"
            Write-Host ""
            Write-Host ">>> $($page.Line.Trim())" -ForegroundColor Magenta
        } catch {}
        Write-Host ""
        Write-Host "Siguiente ciclo en 30s... (Ctrl+C para parar de forma segura)" -ForegroundColor DarkGray
        Start-Sleep -Seconds 30
    }
} finally {
    $mtx.ReleaseMutex()
    "$(Get-Date -Format s) bybit collector stopped cleanly" | Add-Content $log
    Write-Host ""
    Write-Host "COLECTOR BYBIT DETENIDO LIMPIAMENTE. Estado guardado." -ForegroundColor Yellow
}
