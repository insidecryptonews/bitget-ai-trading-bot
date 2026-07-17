param(
    [Parameter(Mandatory=$true)]
    [ValidateSet("bitget", "binance", "bybit", "okx", "hyperliquid")]
    [string]$Venue
)
$ErrorActionPreference = "Continue"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { $Python = "python" }
Set-Location -LiteralPath $Repo
$LogDir = Join-Path $Repo "data\runtime\local_stack\logs"
$Log = Join-Path $LogDir ("cross_venue_{0}.log" -f $Venue)
$StopFile = Join-Path $Repo "data\runtime\local_stack\stack.stop"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Mutex = New-Object System.Threading.Mutex($false, ("Local\BitgetBotCrossVenue_{0}" -f $Venue))
try { $Acquired = $Mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $Acquired = $true }
if (-not $Acquired) { Write-Host "CROSS-VENUE $Venue already active; exiting duplicate launcher."; exit 0 }
$host.UI.RawUI.WindowTitle = "CROSS-VENUE $($Venue.ToUpper()) PUBLIC FEED - RESEARCH ONLY"
Write-Host "CROSS-VENUE INTELLIGENCE | $($Venue.ToUpper()) PUBLIC WEBSOCKET" -ForegroundColor Cyan
Write-Host "RESEARCH ONLY | SIMULATION ONLY | NO KEYS | NO ORDERS | NO LIVE" -ForegroundColor Yellow
Write-Host "Ctrl+C stops this public collector cleanly."
try {
    while (-not (Test-Path -LiteralPath $StopFile)) {
        if ((Test-Path -LiteralPath $Log) -and ((Get-Item -LiteralPath $Log).Length -gt 5MB)) {
            Move-Item -LiteralPath $Log -Destination ($Log + ".1") -Force
        }
        & $Python -m app.labs.cross_venue.cli collect --venue $Venue --symbols BTCUSDT,ETHUSDT --stop-file $StopFile 2>&1 |
            Tee-Object -FilePath $Log -Append
        if (Test-Path -LiteralPath $StopFile) { break }
        Write-Host "Collector $Venue returned; controlled restart in 10 seconds." -ForegroundColor Yellow
        Start-Sleep -Seconds 10
    }
} finally {
    if ($Acquired) { $Mutex.ReleaseMutex() }
    Write-Host "CROSS-VENUE $Venue stopped cooperatively. Persisted append-only data remains intact." -ForegroundColor Yellow
}
