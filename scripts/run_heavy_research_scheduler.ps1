param(
    [double]$IntervalHours = 6.0,
    [int]$InitialDelaySeconds = 60
)
$ErrorActionPreference = "Continue"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { $Python = "python" }
$Runtime = Join-Path $Repo "data\runtime\heavy_research"
$Status = Join-Path $Runtime "scheduler_status.json"
$LogDir = Join-Path $Repo "data\runtime\local_stack\logs"
$Log = Join-Path $LogDir "heavy_research_scheduler.log"
New-Item -ItemType Directory -Force -Path $Runtime | Out-Null
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Add-Content -LiteralPath $Log -Value ("{0} heavy scheduler started; research-only; NO LIVE" -f [DateTime]::UtcNow.ToString("o")) -Encoding UTF8
Set-Location -LiteralPath $Repo
$host.UI.RawUI.WindowTitle = "BitgetBot Heavy Research Scheduler (NO LIVE)"
Write-Host "HEAVY RESEARCH SCHEDULER - isolated from 30s watcher" -ForegroundColor Cyan
Write-Host "RESEARCH ONLY | NO LIVE | no strategy activation"
$mtx = New-Object System.Threading.Mutex($false, "Local\BitgetBotHeavyResearchSchedulerV1044")
try { $acquired = $mtx.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $acquired = $true }
if (-not $acquired) { Write-Host "Heavy scheduler already running."; return }
try {
    $initialSleep = [double][Math]::Max(0, $InitialDelaySeconds)
    if (Test-Path -LiteralPath $Status) {
        try {
            $previous = Get-Content -Raw -LiteralPath $Status | ConvertFrom-Json
            if ($previous.status -eq "COMPLETED" -and $previous.next_run_at) {
                $remaining = ([datetime]$previous.next_run_at).ToUniversalTime() - [DateTime]::UtcNow
                if ($remaining.TotalSeconds -gt $initialSleep) {
                    $initialSleep = [Math]::Ceiling($remaining.TotalSeconds)
                    $waitMessage = "Previous heavy refresh is current; next run remains $($previous.next_run_at)."
                    Write-Host $waitMessage
                    Add-Content -LiteralPath $Log -Value ("{0} {1}" -f [DateTime]::UtcNow.ToString("o"), $waitMessage) -Encoding UTF8
                }
            }
        } catch {
            Write-Host "Scheduler state unreadable; using controlled initial delay." -ForegroundColor Yellow
        }
    }
    Start-Sleep -Seconds ([int]$initialSleep)
    while ($true) {
        $started = [DateTime]::UtcNow
        $state = [ordered]@{ status="RUNNING"; started_at=$started.ToString("o"); research_only=$true; can_send_real_orders=$false; final_recommendation="NO LIVE" }
        $state | ConvertTo-Json | Set-Content -LiteralPath ($Status + ".tmp") -Encoding UTF8
        Move-Item -LiteralPath ($Status + ".tmp") -Destination $Status -Force
        & $Python -m app.research_lab research-heavy-run-v1044 --symbols BTCUSDT --data-source ws_persistent --max-runtime-minutes 90 2>&1 | Tee-Object -FilePath $Log -Append
        $exitCode = $LASTEXITCODE
        $finished = [DateTime]::UtcNow
        $state = [ordered]@{
            status = $(if ($exitCode -eq 0) { "COMPLETED" } else { "ERROR" })
            started_at = $started.ToString("o")
            finished_at = $finished.ToString("o")
            duration_seconds = [Math]::Round(($finished - $started).TotalSeconds, 3)
            exit_code = $exitCode
            next_run_at = $finished.AddHours([Math]::Max(1.0, $IntervalHours)).ToString("o")
            research_only = $true
            can_send_real_orders = $false
            final_recommendation = "NO LIVE"
        }
        $state | ConvertTo-Json | Set-Content -LiteralPath ($Status + ".tmp") -Encoding UTF8
        Move-Item -LiteralPath ($Status + ".tmp") -Destination $Status -Force
        Start-Sleep -Seconds ([int]([Math]::Max(1.0, $IntervalHours) * 3600))
    }
} finally {
    $mtx.ReleaseMutex()
}
