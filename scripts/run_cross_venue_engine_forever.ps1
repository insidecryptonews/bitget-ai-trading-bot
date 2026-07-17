$ErrorActionPreference = "Continue"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { $Python = "python" }
Set-Location -LiteralPath $Repo
$LogDir = Join-Path $Repo "data\runtime\local_stack\logs"
$Log = Join-Path $LogDir "cross_venue_engine.log"
$StopFile = Join-Path $Repo "data\runtime\local_stack\stack.stop"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Mutex = New-Object System.Threading.Mutex($false, "Local\BitgetBotCrossVenueEngine")
try { $Acquired = $Mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $Acquired = $true }
if (-not $Acquired) { Write-Host "CROSS-VENUE engine already active; exiting duplicate launcher."; exit 0 }
$host.UI.RawUI.WindowTitle = "CROSS-VENUE ENGINE + PAPER 50 - SIMULATION ONLY"
Write-Host "CROSS-VENUE CAUSAL ENGINE | CROSS_VENUE_PAPER_50" -ForegroundColor Cyan
Write-Host "SIMULATION ONLY | PAPER FILTER OFF | NO REAL LEVERAGE | NO ORDERS | NO LIVE" -ForegroundColor Yellow
try {
    while (-not (Test-Path -LiteralPath $StopFile)) {
        & $Python -m app.labs.cross_venue.cli engine --interval-seconds 0.25 --stop-file $StopFile 2>&1 |
            Tee-Object -FilePath $Log -Append
        if (Test-Path -LiteralPath $StopFile) { break }
        Write-Host "Cross-venue engine returned; controlled restart in 10 seconds." -ForegroundColor Yellow
        Start-Sleep -Seconds 10
    }
} finally {
    if ($Acquired) { $Mutex.ReleaseMutex() }
    Write-Host "CROSS-VENUE engine stopped cooperatively; account ledger remains persisted." -ForegroundColor Yellow
}
