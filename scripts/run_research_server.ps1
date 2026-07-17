$ErrorActionPreference = "Continue"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { $Python = "python" }
Set-Location -LiteralPath $Repo
$LogDir = Join-Path $Repo "data\runtime\local_stack\logs"
$Log = Join-Path $LogDir "research_health_server.log"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Add-Content -LiteralPath $Log -Value ("{0} loopback research server wrapper started; NO LIVE" -f [DateTime]::UtcNow.ToString("o")) -Encoding UTF8
$host.UI.RawUI.WindowTitle = "BitgetBot Local Research Server (NO LIVE)"
Write-Host "LOCAL READ-ONLY RESEARCH SERVER" -ForegroundColor Cyan
Write-Host "http://127.0.0.1:8765/research-dashboard" -ForegroundColor Green
Write-Host "PAPER_TRADING=True | LIVE_TRADING=False | can_send_real_orders=false"
while ($true) {
    & $Python -m app.labs.ati_paper.server --host 127.0.0.1 --port 8765 2>&1 | Tee-Object -FilePath $Log -Append
    Write-Host "Research server returned; retry in 5 seconds." -ForegroundColor Yellow
    Start-Sleep -Seconds 5
}
