$ErrorActionPreference = "Continue"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { $Python = "python" }
Set-Location -LiteralPath $Repo
$LogDir = Join-Path $Repo "data\runtime\local_stack\logs"
$Log = Join-Path $LogDir "dashboard_watcher.log"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Add-Content -LiteralPath $Log -Value ("{0} dashboard watcher started; artifact-only; NO LIVE" -f [DateTime]::UtcNow.ToString("o")) -Encoding UTF8
$host.UI.RawUI.WindowTitle = "BitgetBot Dashboard Watcher (ARTIFACT ONLY - NO LIVE)"
Write-Host "DASHBOARD WATCHER - ARTIFACT ONLY" -ForegroundColor Cyan
Write-Host "Heavy research is not executed here. NO LIVE."
while ($true) {
    & $Python -m app.research_lab research-dashboard-watch-v1043c --symbols BTCUSDT --interval-seconds 30 2>&1 | Tee-Object -FilePath $Log -Append
    Write-Host "Dashboard watcher returned; retry in 5 seconds." -ForegroundColor Yellow
    Start-Sleep -Seconds 5
}
