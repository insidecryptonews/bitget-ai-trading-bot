$ErrorActionPreference = "Continue"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { $Python = "python" }
Set-Location -LiteralPath $Repo
$LogDir = Join-Path $Repo "data\runtime\local_stack\logs"
$Log = Join-Path $LogDir "ati_paper_executor.log"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Add-Content -LiteralPath $Log -Value ("{0} ATI paper wrapper started; SIMULATION ONLY; NO LIVE" -f [DateTime]::UtcNow.ToString("o")) -Encoding UTF8
$host.UI.RawUI.WindowTitle = "ATI PAPER 50 (SIMULATION ONLY - NO LIVE)"
Write-Host "ATI PAPER TRADING - 50 USDT SIMULADOS" -ForegroundColor Cyan
Write-Host "SIMULATION ONLY | PAPER_TRADING=True | LIVE_TRADING=False" -ForegroundColor Yellow
Write-Host "NO PAPER FILTER | NO REAL ORDERS | can_send_real_orders=false" -ForegroundColor Yellow
Write-Host "Ctrl+C stops the executor; the ledger and open positions remain persisted."
while ($true) {
    & $Python -m app.labs.ati_paper.cli run 2>&1 | Tee-Object -FilePath $Log -Append
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ATI PAPER EXECUTOR ERROR. Controlled restart in 10 seconds." -ForegroundColor Red
    } else {
        Write-Host "ATI PAPER executor stopped; restart in 10 seconds unless this window is closed." -ForegroundColor Yellow
    }
    Start-Sleep -Seconds 10
}
