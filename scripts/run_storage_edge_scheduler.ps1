param(
    [int]$IntervalSeconds = 300,
    [int]$MaxCycles = 0
)
$ErrorActionPreference = "Continue"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { $Python = "python" }
$ConfigPath = Join-Path $Repo "config\research\STORAGE_EFFICIENCY_V2.json"
$Runtime = Join-Path $Repo "data\runtime\storage_efficiency_v2"
$StatusPath = Join-Path $Runtime "scheduler_status.json"
$FeatureManifest = Join-Path $Runtime "feature_manifest.json"
$ContractStatus = Join-Path $Repo "data\runtime\project_memory\contract_state.json"
$DiskGuardStatus = Join-Path $Runtime "disk_guard_status.json"
$SprintStatus = Join-Path $Repo "data\runtime\edge_sprint_48h\sprint_status.json"
$HeavyStatus = Join-Path $Repo "data\runtime\heavy_research\scheduler_status.json"
$LogDir = Join-Path $Repo "data\runtime\local_stack\logs"
$LogPath = Join-Path $LogDir "storage_edge_scheduler.log"
$CollectorRoot = Join-Path $Repo "external_data\staging\cross_venue_v1"
$CollectorVenues = @("bitget", "binance", "bybit", "okx", "hyperliquid")
$SprintHeartbeatSeconds = 300

New-Item -ItemType Directory -Force -Path $Runtime | Out-Null
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Set-Location -LiteralPath $Repo
try { (Get-Process -Id $PID).PriorityClass = "BelowNormal" } catch { }
$host.UI.RawUI.WindowTitle = "BitgetBot Storage + Edge Scheduler (RESEARCH ONLY)"
Write-Host "STORAGE EFFICIENCY V2 + CONTINUOUS EDGE CHALLENGER" -ForegroundColor Cyan
Write-Host "RESEARCH ONLY | SIMULATION ONLY | NO LIVE | no orders"

function Write-SchedulerStatus($Payload) {
    $Payload["research_only"] = $true
    $Payload["simulation_only"] = $true
    $Payload["paper_filter_enabled"] = $false
    $Payload["can_send_real_orders"] = $false
    $Payload["final_recommendation"] = "NO LIVE"
    $tmp = $StatusPath + ".tmp"
    $json = $Payload | ConvertTo-Json -Depth 8
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($tmp, $json, $utf8NoBom)
    Move-Item -LiteralPath $tmp -Destination $StatusPath -Force
}

function Read-JsonSafe([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try { return Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json } catch { return $null }
}

function Test-CollectorsHealthy {
    $now = [DateTime]::UtcNow
    foreach ($venue in $CollectorVenues) {
        $path = Join-Path $CollectorRoot ($venue + "\health.json")
        if (-not (Test-Path -LiteralPath $path)) { return $false }
        $file = Get-Item -LiteralPath $path
        if (($now - $file.LastWriteTimeUtc).TotalSeconds -gt 180) { return $false }
        $health = Read-JsonSafe $path
        if ($null -eq $health) { return $false }
        if ([string]$health.status -in @("ERROR", "FAILED", "HALTED")) { return $false }
    }
    return $true
}

function Get-VerifiedFeatureCount {
    $manifest = Read-JsonSafe $FeatureManifest
    if ($null -eq $manifest -or $null -eq $manifest.segments) { return 0 }
    return @($manifest.segments.PSObject.Properties | Where-Object {
        $_.Value.status -eq "VERIFIED_FEATURES"
    }).Count
}

function Invoke-SprintCycle {
    & $Python -m app.research_lab edge-sprint-cycle-v1 --apply 2>&1 |
        Tee-Object -FilePath $LogPath -Append | Out-Host
    return $LASTEXITCODE
}

function Invoke-ChallengerWithSprintHeartbeats {
    $runId = [Guid]::NewGuid().ToString("N")
    $stdoutPath = Join-Path $Runtime ("challenger_" + $runId + ".stdout.tmp")
    $stderrPath = Join-Path $Runtime ("challenger_" + $runId + ".stderr.tmp")
    $process = $null
    $heartbeatExit = 0
    try {
        $arguments = @(
            "-m", "app.research_lab", "continuous-edge-challenger-v2",
            "--symbols", "BTCUSDT,ETHUSDT", "--max-runtime-minutes", "30"
        )
        $process = Start-Process -FilePath $Python -ArgumentList $arguments -NoNewWindow `
            -PassThru -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
        try { $process.PriorityClass = "BelowNormal" } catch { }
        while (-not $process.WaitForExit($SprintHeartbeatSeconds * 1000)) {
            $cycleExit = Invoke-SprintCycle
            if ($cycleExit -ne 0) { $heartbeatExit = $cycleExit }
        }
        $process.WaitForExit()
        foreach ($path in @($stdoutPath, $stderrPath)) {
            if (Test-Path -LiteralPath $path) {
                Get-Content -LiteralPath $path | Tee-Object -FilePath $LogPath -Append | Out-Host
            }
        }
        return [pscustomobject]@{
            challenger_exit_code = $process.ExitCode
            heartbeat_exit_code = $heartbeatExit
        }
    } finally {
        if ($null -ne $process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }
}

$mutex = New-Object System.Threading.Mutex($false, "Local\BitgetBotStorageEdgeSchedulerV2")
try { $acquired = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $acquired = $true }
if (-not $acquired) { Write-Host "Storage scheduler already running."; return }

try {
    $cycle = 0
    while ($MaxCycles -le 0 -or $cycle -lt $MaxCycles) {
        $cycle += 1
        $started = [DateTime]::UtcNow
        $config = Read-JsonSafe $ConfigPath
        $previous = Read-JsonSafe $StatusPath
        $heavy = Read-JsonSafe $HeavyStatus
        $disk = Get-PSDrive -Name ([IO.Path]::GetPathRoot($Repo).Substring(0, 1))
        $minimumFree = if ($config) { [double]$config.minimum_free_disk_bytes } else { 5368709120.0 }
        $diskOk = [double]$disk.Free -gt $minimumFree
        $collectorsHealthy = Test-CollectorsHealthy
        $heavyRunning = $null -ne $heavy -and [string]$heavy.status -eq "RUNNING"
        $state = [ordered]@{
            schema = "storage_edge_scheduler.v1"
            status = "RUNNING"
            cycle = $cycle
            started_at = $started.ToString("o")
            process_priority = "BelowNormal"
            collectors_healthy = $collectorsHealthy
            heavy_research_running = $heavyRunning
            disk_guard_ok = $diskOk
            free_disk_bytes = [double]$disk.Free
        }
        Write-SchedulerStatus $state

        if (Test-Path -LiteralPath $LogPath) {
            $logInfo = Get-Item -LiteralPath $LogPath
            if ($logInfo.Length -gt 5MB) {
                Move-Item -LiteralPath $LogPath -Destination ($LogPath + ".1") -Force
            }
        }

        & $Python -m app.research_lab project-memory-contract-v1 --apply 2>&1 |
            Tee-Object -FilePath $LogPath -Append | Out-Host
        $contractExit = $LASTEXITCODE
        $contractState = Read-JsonSafe $ContractStatus
        $contractPass = (
            $contractExit -eq 0 -and $null -ne $contractState -and
            [string]$contractState.guardrails_status -eq "PASS"
        )

        # Keep active-runtime accounting independent from storage/challenger duration.
        $earlySprintExit = Invoke-SprintCycle

        & $Python -m app.research_lab storage-efficiency-cycle-v2 --apply 2>&1 |
            Tee-Object -FilePath $LogPath -Append | Out-Host
        $storageExit = $LASTEXITCODE
        & $Python -m app.research_lab storage-disk-guard-v1 --apply 2>&1 |
            Tee-Object -FilePath $LogPath -Append | Out-Host
        $diskGuardExit = $LASTEXITCODE
        $diskGuard = Read-JsonSafe $DiskGuardStatus
        $allowChallenger = (
            $diskGuardExit -eq 0 -and $null -ne $diskGuard -and
            [bool]$diskGuard.allow_challenger
        )
        $featureCount = Get-VerifiedFeatureCount
        $lastFeatureCount = if ($previous) { [int]$previous.last_challenger_feature_count } else { 0 }
        $lastRun = if ($previous -and $previous.last_challenger_at) {
            try { ([datetime]$previous.last_challenger_at).ToUniversalTime() } catch { [datetime]::MinValue }
        } else { [datetime]::MinValue }
        $minHours = if ($config) { [double]$config.challenger_min_interval_hours } else { 6.0 }
        $minNew = if ($config) { [int]$config.challenger_min_new_partitions } else { 1 }
        $intervalOk = ([DateTime]::UtcNow - $lastRun).TotalHours -ge $minHours
        $newPartitions = $featureCount - $lastFeatureCount
        $challengerEligible = (
            $contractPass -and $storageExit -eq 0 -and $diskOk -and
            $allowChallenger -and $collectorsHealthy -and
            -not $heavyRunning -and $intervalOk -and $newPartitions -ge $minNew
        )
        $sprintExit = Invoke-SprintCycle
        if ($earlySprintExit -ne 0) { $sprintExit = $earlySprintExit }
        $sprintState = Read-JsonSafe $SprintStatus
        $sprintPass = (
            $sprintExit -eq 0 -and $null -ne $sprintState -and
            [string]$sprintState.status -in @("ACTIVE", "PAUSED", "COMPLETED")
        )
        $challengerExit = $null
        $lastChallengerAt = if ($previous) { $previous.last_challenger_at } else { $null }
        if ($challengerEligible) {
            $challengerResult = Invoke-ChallengerWithSprintHeartbeats
            $challengerExit = $challengerResult.challenger_exit_code
            if ($challengerResult.heartbeat_exit_code -ne 0) {
                $sprintExit = $challengerResult.heartbeat_exit_code
            }
            $sprintState = Read-JsonSafe $SprintStatus
            $sprintPass = (
                $sprintExit -eq 0 -and $null -ne $sprintState -and
                [string]$sprintState.status -in @("ACTIVE", "PAUSED", "COMPLETED")
            )
            $lastChallengerAt = [DateTime]::UtcNow.ToString("o")
            if ($challengerExit -eq 0) { $lastFeatureCount = $featureCount }
        }
        $handoffStatus = "NOT_DUE"
        $handoffExit = $null
        $handoffPath = $null
        if ($sprintState -and [string]$sprintState.status -eq "COMPLETED") {
            $handoffPath = Join-Path $Repo (
                "reports\research\48h_edge_sprint\" + [string]$sprintState.sprint_id +
                "\HANDOFF_REVIEW_PACK.zip"
            )
            if (Test-Path -LiteralPath $handoffPath) {
                $handoffStatus = "ALREADY_PRESENT"
            } else {
                & $Python -m app.research_lab edge-sprint-final-handoff-v1 --apply 2>&1 |
                    Tee-Object -FilePath $LogPath -Append | Out-Host
                $handoffExit = $LASTEXITCODE
                $handoffStatus = if ($handoffExit -eq 0 -and (Test-Path -LiteralPath $handoffPath)) {
                    "CREATED"
                } else {
                    "ERROR"
                }
            }
        }
        $finished = [DateTime]::UtcNow
        $state = [ordered]@{
            schema = "storage_edge_scheduler.v1"
            status = if ($contractPass -and $storageExit -eq 0 -and $diskGuardExit -eq 0 -and $sprintPass -and ($null -eq $challengerExit -or $challengerExit -eq 0)) { "COMPLETED" } else { "ERROR" }
            cycle = $cycle
            started_at = $started.ToString("o")
            finished_at = $finished.ToString("o")
            duration_seconds = [Math]::Round(($finished - $started).TotalSeconds, 3)
            storage_exit_code = $storageExit
            contract_exit_code = $contractExit
            contract_guardrails_pass = $contractPass
            disk_guard_exit_code = $diskGuardExit
            disk_guard_level = if ($diskGuard) { [string]$diskGuard.level } else { "UNKNOWN" }
            sprint_exit_code = $sprintExit
            sprint_status = if ($sprintState) { [string]$sprintState.status } else { "UNKNOWN" }
            sprint_runtime_state = if ($sprintState) { [string]$sprintState.runtime_state } else { "UNKNOWN" }
            sprint_active_runtime_seconds = if ($sprintState) { [int64]$sprintState.accumulated_active_runtime_seconds } else { 0 }
            sprint_active_runtime_remaining_seconds = if ($sprintState) { [int64]$sprintState.active_runtime_remaining_seconds } else { 172800 }
            sprint_handoff_status = $handoffStatus
            sprint_handoff_exit_code = $handoffExit
            sprint_handoff_path = $handoffPath
            challenger_eligible = $challengerEligible
            challenger_exit_code = $challengerExit
            challenger_skip_reason = if ($challengerEligible) { $null } elseif (-not $contractPass) { "PROJECT_MEMORY_CONTRACT" } elseif ($heavyRunning) { "HEAVY_RESEARCH_RUNNING" } elseif (-not $collectorsHealthy) { "COLLECTORS_NOT_HEALTHY" } elseif (-not $diskOk -or -not $allowChallenger) { "DISK_GUARD" } elseif (-not $intervalOk) { "MINIMUM_INTERVAL" } else { "NO_NEW_VERIFIED_PARTITIONS" }
            verified_feature_count = $featureCount
            new_verified_partitions = $newPartitions
            last_challenger_feature_count = $lastFeatureCount
            last_challenger_at = $lastChallengerAt
            collectors_healthy = $collectorsHealthy
            heavy_research_running = $heavyRunning
            disk_guard_ok = $diskOk
            free_disk_bytes = [double]$disk.Free
            next_cycle_at = $finished.AddSeconds([Math]::Max(30, $IntervalSeconds)).ToString("o")
        }
        Write-SchedulerStatus $state
        if ($MaxCycles -le 0 -or $cycle -lt $MaxCycles) {
            Start-Sleep -Seconds ([Math]::Max(30, $IntervalSeconds))
        }
    }
} finally {
    if ($acquired) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
