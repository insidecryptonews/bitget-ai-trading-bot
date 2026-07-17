$ErrorActionPreference = "Continue"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { $Python = "python" }
Set-Location -LiteralPath $Repo
$host.UI.RawUI.WindowTitle = "BitgetBot Local Research Server (NO LIVE)"
Write-Host "LOCAL READ-ONLY RESEARCH SERVER" -ForegroundColor Cyan
Write-Host "http://127.0.0.1:8765/research-dashboard" -ForegroundColor Green
Write-Host "PAPER_TRADING=True | LIVE_TRADING=False | can_send_real_orders=false"
while ($true) {
    & $Python -m app.labs.ati_paper.server --host 127.0.0.1 --port 8765
    Write-Host "Research server returned; retry in 5 seconds." -ForegroundColor Yellow
    Start-Sleep -Seconds 5
}
