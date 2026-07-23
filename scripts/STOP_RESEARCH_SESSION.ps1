$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { $Python = "python" }
Set-Location -LiteralPath $Repo
. (Join-Path $PSScriptRoot "local_stack_common.ps1")

$branch = (& git branch --show-current).Trim()
if ($branch -ne "backup/ati-wip-cdb0cee") {
    throw "RESEARCH_SESSION_STOP_BLOCKED_WRONG_BRANCH:$branch"
}
$audit = & $Python -m app.research_lab security-audit 2>&1 | Out-String
if ($LASTEXITCODE -ne 0 -or $audit -notmatch "SAFE_PAPER_ONLY") {
    throw "RESEARCH_SESSION_STOP_BLOCKED_SECURITY_AUDIT"
}

$pause = $null
$pauseDeadline = [DateTime]::UtcNow.AddMinutes(10)
do {
    $pauseRaw = & $Python -m app.research_lab edge-sprint-pause-v1 `
        --reason USER_REQUESTED_CONTROLLED_LOCAL_SHUTDOWN 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { throw "EDGE_SPRINT_PAUSE_FAILED`n$pauseRaw" }
    $pause = $pauseRaw | ConvertFrom-Json
    if ([string]$pause.status -in @("PAUSED", "NOT_STARTED")) { break }
    if ([string]$pause.status -ne "BLOCKED_SPRINT_CYCLE_IN_PROGRESS") {
        throw "EDGE_SPRINT_PAUSE_NOT_CONFIRMED:$($pause.status)"
    }
    Start-Sleep -Seconds 2
} while ([DateTime]::UtcNow -lt $pauseDeadline)
if ([string]$pause.status -notin @("PAUSED", "NOT_STARTED")) {
    throw "EDGE_SPRINT_PAUSE_TIMEOUT"
}

$schedulerStatusPath = Join-Path $Repo "data\runtime\storage_efficiency_v2\scheduler_status.json"
$schedulerDeadline = [DateTime]::UtcNow.AddMinutes(10)
do {
    $schedulerProcesses = @((Get-ManagedProjectProcesses) | Where-Object {
        ([string]$_.CommandLine).IndexOf("run_storage_edge_scheduler.ps1", [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    })
    $schedulerStatus = if (Test-Path -LiteralPath $schedulerStatusPath) {
        try { Get-Content -Raw -LiteralPath $schedulerStatusPath | ConvertFrom-Json } catch { $null }
    } else { $null }
    if ($schedulerProcesses.Count -eq 0 -or [string]$schedulerStatus.status -ne "RUNNING") { break }
    Start-Sleep -Seconds 2
} while ([DateTime]::UtcNow -lt $schedulerDeadline)
if ($schedulerProcesses.Count -gt 0 -and [string]$schedulerStatus.status -eq "RUNNING") {
    throw "SCHEDULER_DID_NOT_REACH_ATOMIC_CYCLE_BOUNDARY"
}
foreach ($process in $schedulerProcesses) {
    Stop-ManagedProcessTree ([int]$process.ProcessId)
}
Start-Sleep -Seconds 2

& $Python -m app.research_lab project-memory-contract-v1 --apply | Out-Host
if ($LASTEXITCODE -ne 0) { throw "PROJECT_MEMORY_FLUSH_FAILED" }
& $Python -m app.research_lab storage-efficiency-cycle-v2 --apply | Out-Host
if ($LASTEXITCODE -ne 0) { throw "STORAGE_FLUSH_FAILED" }
& $Python -m app.research_lab storage-disk-guard-v1 --apply | Out-Host
if ($LASTEXITCODE -ne 0) { throw "DISK_GUARD_FLUSH_FAILED" }

& (Join-Path $PSScriptRoot "stop_local_stack.ps1")
if ($LASTEXITCODE -ne 0) { throw "LOCAL_STACK_STOP_FAILED" }
$remaining = @(Get-ManagedProjectProcesses)
if ($remaining.Count -ne 0) { throw "MANAGED_PROCESSES_STILL_RUNNING:$($remaining.Count)" }

$status = & $Python -m app.research_lab edge-sprint-status-v1 | ConvertFrom-Json
$driveName = [IO.Path]::GetPathRoot($Repo).Substring(0, 1)
$drive = Get-PSDrive -Name $driveName
Write-Host "RESEARCH SESSION PAUSED AND STACK STOPPED" -ForegroundColor Cyan
Write-Host "active_seconds=$($status.accumulated_active_runtime_seconds) remaining_seconds=$($status.active_runtime_remaining_seconds)"
Write-Host "free_disk_bytes=$([int64]$drive.Free) errors=NONE safe_to_power_off=true"
Write-Host "PC OFF TIME DOES NOT COUNT | NO LIVE | no orders"
