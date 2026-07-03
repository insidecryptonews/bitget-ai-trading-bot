# BitgetBot - Continuous Forward Data Collector (ResearchOps V10.29.2) - VISIBLE console
# RESEARCH ONLY. Public data only. NO API keys, NO orders, NO live, NO paper.
# Each cycle: collect (bounded) -> RE-ASSEMBLE the live sample -> readiness ->
# refresh the status page and print its link. So the dashboard always reflects
# the LIVE dataset (Codex V10.29.2 stale-dashboard hotfix).
# Stop safely with Ctrl+C: state is saved at the end of every cycle.
# Single-instance guarded so two logons never write the dataset concurrently.

$ErrorActionPreference = "Continue"

# repo root = parent of this script's folder (scripts/)
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

# prefer the repo venv python when it exists; fall back to PATH python
$py = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$dir = Join-Path $repo "external_data\staging\continuous_forward_v10_27"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$log = Join-Path $dir "collector.log"

# single instance per user session (no admin needed). An abandoned mutex
# (previous holder killed) still grants ownership -- treat it as acquired.
$mtx = New-Object System.Threading.Mutex($false, "Local\BitgetBotCollectorV1027")
try { $acquired = $mtx.WaitOne(0) }
catch [System.Threading.AbandonedMutexException] { $acquired = $true }
if (-not $acquired) {
    Write-Host "Otro colector ya esta corriendo; cierro esta ventana en 10s." -ForegroundColor Yellow
    "$(Get-Date -Format s) another collector already running; exiting" | Add-Content $log
    Start-Sleep -Seconds 10
    return
}

$host.UI.RawUI.WindowTitle = "BitgetBot Collector (RESEARCH ONLY - NO LIVE)"
Write-Host "==============================================================" -ForegroundColor Cyan
Write-Host " BitgetBot - Colector continuo de microestructura (V10.29.2)" -ForegroundColor Cyan
Write-Host " RESEARCH ONLY. Datos publicos. Sin claves, sin ordenes, NO LIVE." -ForegroundColor Cyan
Write-Host " Para PARAR de forma segura: pulsa Ctrl+C." -ForegroundColor Yellow
Write-Host " El estado se guarda al final de CADA ciclo (nada se pierde)." -ForegroundColor Yellow
Write-Host "==============================================================" -ForegroundColor Cyan
"$(Get-Date -Format s) collector started (research-only, NO LIVE, visible console)" | Add-Content $log

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
        Write-Host ">>> CICLO $cycle  $(Get-Date -Format s)  (recolectando ~5 min de datos publicos...)" -ForegroundColor Green
        try {
            & $py -m app.research_lab continuous-collection-run-cycle-v1027 `
                --symbols BTCUSDT --apply --max-runtime-seconds 300 --max-events 100000 |
                Tee-Object -FilePath $log -Append
        } catch {
            Write-Host "ERROR ciclo: $($_.Exception.Message)" -ForegroundColor Red
            "$(Get-Date -Format s) ERROR $($_.Exception.Message)" | Add-Content $log
            Start-Sleep -Seconds 15
        }
        Write-Host ""
        Write-Host ">>> RE-ENSAMBLANDO el sample vivo (para que el dashboard NO quede stale)..." -ForegroundColor Green
        try {
            & $py -m app.research_lab free-microstructure-assemble-sample-v1029 `
                --symbols BTCUSDT --apply --run-label latest |
                Select-String -Pattern "mode:|trades:|orderbook:|oi:|funding:|liquidations:|gaps:|readiness_verdict" |
                ForEach-Object { Write-Host ("  " + $_.Line) }
        } catch {
            Write-Host "ERROR assemble: $($_.Exception.Message)" -ForegroundColor Red
            "$(Get-Date -Format s) ERROR assemble $($_.Exception.Message)" | Add-Content $log
        }
        Write-Host ""
        Write-Host ">>> PROGRESO HACIA MICROSTRUCTURE_RESEARCH_READY:" -ForegroundColor Green
        try {
            & $py -m app.research_lab free-microstructure-readiness-status-v1029 |
                Select-String -Pattern "readiness_verdict|stale_assembled_warning|WARNING|trades:|orderbook:|oi:|funding:|liquidations:|estimated|estimate_unknown" |
                ForEach-Object { Write-Host ("  " + $_.Line) }
        } catch {}
        try {
            $page = & $py -m app.research_lab free-microstructure-status-page-v1029 |
                Select-String -Pattern "DASHBOARD:"
            Write-Host ""
            Write-Host ">>> $($page.Line.Trim())" -ForegroundColor Magenta
            Write-Host "    (copia ese enlace file:/// en tu navegador; se refresca solo)" -ForegroundColor Magenta
        } catch {}
        Write-Host ""
        Write-Host "Siguiente ciclo en 60s... (Ctrl+C para parar de forma segura)" -ForegroundColor DarkGray
        Start-Sleep -Seconds 60
    }
} finally {
    $mtx.ReleaseMutex()
    "$(Get-Date -Format s) collector stopped cleanly" | Add-Content $log
    Write-Host ""
    Write-Host "COLECTOR DETENIDO LIMPIAMENTE. Todo el estado quedo guardado." -ForegroundColor Yellow
}
