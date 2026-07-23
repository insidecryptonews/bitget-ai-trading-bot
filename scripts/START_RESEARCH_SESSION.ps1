param([int]$WaitForGrowthSeconds = 900)
$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { $Python = "python" }
Set-Location -LiteralPath $Repo

$branch = (& git branch --show-current).Trim()
if ($branch -ne "backup/ati-wip-cdb0cee") {
    throw "RESEARCH_SESSION_START_BLOCKED_WRONG_BRANCH:$branch"
}
$head = (& git rev-parse HEAD).Trim()
if ([string]::IsNullOrWhiteSpace($head)) { throw "RESEARCH_SESSION_START_BLOCKED_HEAD_UNKNOWN" }
$audit = & $Python -m app.research_lab security-audit 2>&1 | Out-String
if ($LASTEXITCODE -ne 0 -or $audit -notmatch "SAFE_PAPER_ONLY") {
    throw "RESEARCH_SESSION_START_BLOCKED_SECURITY_AUDIT"
}

$contractRaw = & $Python -m app.research_lab project-memory-contract-v1 --apply 2>&1 | Out-String
if ($LASTEXITCODE -ne 0) { throw "PROJECT_MEMORY_CONTRACT_FAILED`n$contractRaw" }
$contract = $contractRaw | ConvertFrom-Json
if ([string]$contract.guardrails_status -ne "PASS") {
    throw "PROJECT_MEMORY_CONTRACT_NOT_PASS"
}
$diskRaw = & $Python -m app.research_lab storage-disk-guard-v1 --apply 2>&1 | Out-String
if ($LASTEXITCODE -ne 0) { throw "DISK_GUARD_FAILED`n$diskRaw" }
$disk = $diskRaw | ConvertFrom-Json
if ([string]$disk.level -eq "ABSOLUTE_PROTECTION") {
    throw "RESEARCH_SESSION_START_BLOCKED_DISK_ABSOLUTE_PROTECTION"
}

& (Join-Path $PSScriptRoot "start_local_stack.ps1")
if ($LASTEXITCODE -ne 0) { throw "LOCAL_STACK_START_FAILED" }
Start-Sleep -Seconds 15

$preResumeRaw = & $Python -m app.research_lab edge-sprint-status-v1 2>&1 | Out-String
if ($LASTEXITCODE -ne 0) { throw "EDGE_SPRINT_STATUS_FAILED`n$preResumeRaw" }
$preResume = $preResumeRaw | ConvertFrom-Json
$resume = $preResume
if ([string]$preResume.status -ne "ACTIVE" -or [bool]$preResume.explicit_pause) {
    $resumeDeadline = [DateTime]::UtcNow.AddMinutes(10)
    do {
        $resumeRaw = & $Python -m app.research_lab edge-sprint-resume-v1 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0) { throw "EDGE_SPRINT_RESUME_FAILED`n$resumeRaw" }
        $resume = $resumeRaw | ConvertFrom-Json
        if ([string]$resume.status -in @("ACTIVE", "NOT_STARTED")) { break }
        if ([string]$resume.status -ne "BLOCKED_SPRINT_CYCLE_IN_PROGRESS") {
            throw "EDGE_SPRINT_RESUME_NOT_CONFIRMED:$($resume.status)"
        }
        Start-Sleep -Seconds 2
    } while ([DateTime]::UtcNow -lt $resumeDeadline)
}
if ([string]$resume.status -notin @("ACTIVE", "NOT_STARTED")) {
    throw "EDGE_SPRINT_RESUME_NOT_CONFIRMED:$($resume.status)"
}

$growthStatus = "NOT_APPLICABLE_SPRINT_NOT_STARTED"
if ([string]$resume.status -eq "ACTIVE" -and $WaitForGrowthSeconds -gt 0) {
    $activeBefore = [int64]$resume.accumulated_active_runtime_seconds
    $growthDeadline = [DateTime]::UtcNow.AddSeconds($WaitForGrowthSeconds)
    do {
        Start-Sleep -Seconds 10
        $currentRaw = & $Python -m app.research_lab edge-sprint-status-v1 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0) { throw "EDGE_SPRINT_GROWTH_STATUS_FAILED`n$currentRaw" }
        $current = $currentRaw | ConvertFrom-Json
        if (
            [int64]$current.accumulated_active_runtime_seconds -gt $activeBefore -and
            [bool]$current.last_runtime_qualified
        ) {
            $resume = $current
            $growthStatus = "QUALIFIED_DATA_GROWTH_CONFIRMED"
            break
        }
    } while ([DateTime]::UtcNow -lt $growthDeadline)
    if ($growthStatus -ne "QUALIFIED_DATA_GROWTH_CONFIRMED") {
        throw "RESEARCH_SESSION_STARTED_BUT_QUALIFIED_GROWTH_NOT_CONFIRMED"
    }
}

Write-Host "RESEARCH SESSION STARTED" -ForegroundColor Cyan
Write-Host "branch=$branch head=$head"
Write-Host "active_seconds=$($resume.accumulated_active_runtime_seconds) remaining_seconds=$($resume.active_runtime_remaining_seconds)"
Write-Host "growth_status=$growthStatus; PC-off time was not counted."
Write-Host "PAPER/RESEARCH ONLY | NO LIVE | no orders"
