# BitgetBot - Bybit CONTINUOUS public-trade WEBSOCKET collector loop (ResearchOps V10.42)
# RESEARCH ONLY. Public Bybit v5 linear websocket (publicTrade). NO API keys, NO orders,
# NO live, NO paper. Writes to a SEPARATE dataset (external_data/staging/bybit_trades_ws_v10_42)
# so it never corrupts the V10.32 REST dataset. Fixes the DATA_GAP by collecting ticks
# continuously while the PC is on. Stop safely with Ctrl+C.

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$py = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$symbols = "BTCUSDT"

$dir = Join-Path $repo "external_data\staging\bybit_trades_ws_v10_42"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$log = Join-Path $dir "ws_collector.log"

# single instance per user session (own mutex)
$mtx = New-Object System.Threading.Mutex($false, "Local\BitgetBotBybitTradesWsV1042")
try { $acquired = $mtx.WaitOne(0) }
catch [System.Threading.AbandonedMutexException] { $acquired = $true }
if (-not $acquired) {
    Write-Host "Otro colector WS de trades ya esta corriendo; cierro en 10s." -ForegroundColor Yellow
    Start-Sleep -Seconds 10
    return
}

$host.UI.RawUI.WindowTitle = "BitgetBot Bybit TRADES WS (RESEARCH ONLY - NO LIVE)"
Write-Host "==============================================================" -ForegroundColor Cyan
Write-Host " Bybit CONTINUOUS trades WEBSOCKET collector (V10.42)" -ForegroundColor Cyan
Write-Host " Publico. Sin claves. Sin ordenes. NO LIVE." -ForegroundColor Cyan
Write-Host " Dataset separado: $dir" -ForegroundColor Cyan
Write-Host " Ctrl+C para parar de forma segura." -ForegroundColor Yellow
Write-Host "==============================================================" -ForegroundColor Cyan
"$(Get-Date -Format s) bybit trades ws collector started (research-only, NO LIVE)" | Add-Content $log

$cycle = 0
try {
    while ($true) {
        $cycle += 1
        if ((Test-Path $log) -and ((Get-Item $log).Length -gt 5MB)) {
            Move-Item -Force $log ($log + ".1")
        }
        Write-Host ""
        Write-Host ">>> CICLO $cycle  $(Get-Date -Format s)  (streaming publicTrade ~60s/ciclo...)" -ForegroundColor Green
        try {
            & $py -m app.research_lab bybit-trades-ws-collect-v1042 --symbols $symbols |
                Tee-Object -FilePath $log -Append |
                Select-String -Pattern "collect_status|rows_added|total_rows|unique_trades" |
                ForEach-Object { Write-Host ("  " + $_.Line) }
        } catch {
            Write-Host "ERROR ciclo: $($_.Exception.Message)" -ForegroundColor Red
            "$(Get-Date -Format s) ERROR $($_.Exception.Message)" | Add-Content $log
            Start-Sleep -Seconds 15
        }
        Write-Host "Siguiente ciclo en 5s... (Ctrl+C para parar)" -ForegroundColor DarkGray
        Start-Sleep -Seconds 5
    }
} finally {
    $mtx.ReleaseMutex()
    "$(Get-Date -Format s) bybit trades ws collector stopped cleanly" | Add-Content $log
    Write-Host "COLECTOR WS DE TRADES DETENIDO LIMPIAMENTE." -ForegroundColor Yellow
}
