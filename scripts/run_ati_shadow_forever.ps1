param(
    [int]$RefreshSeconds = 900,
    [int]$HeartbeatSeconds = 60
)

$ErrorActionPreference = "Continue"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { $Python = "python" }
Set-Location -LiteralPath $Repo

Write-Host "ATI SHADOW SUPERVISOR - RESEARCH ONLY"
Write-Host "NO LIVE | NO PAPER FILTER | NO ORDERS | can_send_real_orders=false"
Write-Host "Public Bitget OHLCV refresh is isolated from the ATI engine."
Write-Host "Stop cleanly with Ctrl+C."

$NextRefresh = Get-Date
while ($true) {
    $CycleStart = Get-Date
    if ($CycleStart -ge $NextRefresh) {
        & $Python -m scripts.refresh_ati_public_data --symbols BTCUSDT,ETHUSDT --days 90
        if ($LASTEXITCODE -ne 0) {
            Write-Host "ATI DATA REFRESH ERROR: observer will retain the last verified snapshot." -ForegroundColor Red
        }
        $NextRefresh = (Get-Date).AddSeconds([Math]::Max(300, $RefreshSeconds))
    }

    & $Python -m app.research_lab ati-shadow-run-v2 `
        --symbols BTCUSDT,ETHUSDT --interval-seconds 60 --max-scans 1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ATI OBSERVER ERROR: inspect reports/research/ati/ati_health.json" -ForegroundColor Red
    }

    $Elapsed = ((Get-Date) - $CycleStart).TotalSeconds
    $Sleep = [Math]::Max(5, $HeartbeatSeconds - [int][Math]::Ceiling($Elapsed))
    Start-Sleep -Seconds $Sleep
}
