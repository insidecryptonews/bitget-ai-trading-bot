# BitgetBot - Bybit PERSISTENT public-trade WEBSOCKET collector (ResearchOps V10.43C)
# RESEARCH ONLY. Public Bybit v5 linear websocket (publicTrade). NO API keys, NO orders,
# NO live, NO paper. ONE long-lived connection (reconnect handled inside Python, with
# backoff) so the tape stays contiguous instead of the V10.42 60s-cycle holes.
# Writes to a SEPARATE dataset (external_data/staging/bybit_trades_ws_persistent_v10_43c)
# so it never touches the V10.32 REST or the V10.42 WS datasets. Stop safely with Ctrl+C.

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$py = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$symbols = "BTCUSDT"

$dir = Join-Path $repo "external_data\staging\bybit_trades_ws_persistent_v10_43c"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$log = Join-Path $dir "ws_persistent_collector.log"

# single instance per user session (own mutex; the Python writer-lock is the 2nd guard)
$mtx = New-Object System.Threading.Mutex($false, "Local\BitgetBotBybitTradesWsPersistentV1043C")
try { $acquired = $mtx.WaitOne(0) }
catch [System.Threading.AbandonedMutexException] { $acquired = $true }
if (-not $acquired) {
    Write-Host "Otro colector WS PERSISTENTE ya esta corriendo; cierro en 10s." -ForegroundColor Yellow
    Start-Sleep -Seconds 10
    return
}

$host.UI.RawUI.WindowTitle = "BitgetBot Bybit TRADES WS PERSISTENT (RESEARCH ONLY - NO LIVE)"
Write-Host "==============================================================" -ForegroundColor Cyan
Write-Host " Bybit PERSISTENT trades WEBSOCKET collector (V10.43C)" -ForegroundColor Cyan
Write-Host " Una conexion larga. Sin huecos de reconexion cada 60s." -ForegroundColor Cyan
Write-Host " Publico. Sin claves. Sin ordenes. NO LIVE." -ForegroundColor Cyan
Write-Host " Dataset separado: $dir" -ForegroundColor Cyan
Write-Host " Ctrl+C para parar de forma segura." -ForegroundColor Yellow
Write-Host "==============================================================" -ForegroundColor Cyan
"$(Get-Date -Format s) bybit trades ws PERSISTENT collector started (research-only, NO LIVE)" | Add-Content $log

$cycle = 0
try {
    while ($true) {
        $cycle += 1
        if ((Test-Path $log) -and ((Get-Item $log).Length -gt 5MB)) {
            Move-Item -Force $log ($log + ".1")
        }
        Write-Host ""
        Write-Host ">>> SESION $cycle  $(Get-Date -Format s)  (streaming persistente, reconnect interno...)" -ForegroundColor Green
        try {
            & $py -m app.research_lab bybit-trades-ws-persistent-v1043c --symbols $symbols |
                Tee-Object -FilePath $log -Append |
                Select-String -Pattern "status|trades_count|messages_count|reconnect_count|uptime" |
                ForEach-Object { Write-Host ("  " + $_.Line) }
        } catch {
            Write-Host "ERROR sesion: $($_.Exception.Message)" -ForegroundColor Red
            "$(Get-Date -Format s) ERROR $($_.Exception.Message)" | Add-Content $log
            Start-Sleep -Seconds 15
        }
        # the Python handles its own long run + reconnects; if it returns, wait a bit and relaunch
        Write-Host "Sesion terminada; relanzo en 10s... (Ctrl+C para parar)" -ForegroundColor DarkGray
        Start-Sleep -Seconds 10
    }
} finally {
    $mtx.ReleaseMutex()
    "$(Get-Date -Format s) bybit trades ws PERSISTENT collector stopped cleanly" | Add-Content $log
    Write-Host "COLECTOR WS PERSISTENTE DETENIDO LIMPIAMENTE." -ForegroundColor Yellow
}
