$ErrorActionPreference = "Continue"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { $Python = "python" }
Set-Location -LiteralPath $Repo
$host.UI.RawUI.WindowTitle = "BitgetBot Dashboard Watcher (ARTIFACT ONLY - NO LIVE)"
Write-Host "DASHBOARD WATCHER - ARTIFACT ONLY" -ForegroundColor Cyan
Write-Host "Heavy research is not executed here. NO LIVE."
while ($true) {
    & $Python -m app.research_lab research-dashboard-watch-v1043c --symbols BTCUSDT --interval-seconds 30
    Write-Host "Dashboard watcher returned; retry in 5 seconds." -ForegroundColor Yellow
    Start-Sleep -Seconds 5
}
