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
$HeavyStatus = Join-Path $Repo "data\runtime\heavy_research\scheduler_status.json"
$LogDir = Join-Path $Repo "data\runtime\local_stack\logs"
$LogPath = Join-Path $LogDir "storage_edge_scheduler.log"
$CollectorRoot = Join-Path $Repo "external_data\staging\cross_venue_v1"
$CollectorVenues = @("bitget", "binance", "bybit", "okx", "hyperliquid")

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

        & $Python -m app.research_lab storage-efficiency-cycle-v2 --apply 2>&1 |
            Tee-Object -FilePath $LogPath -Append | Out-Host
        $storageExit = $LASTEXITCODE
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
            $storageExit -eq 0 -and $diskOk -and $collectorsHealthy -and
            -not $heavyRunning -and $intervalOk -and $newPartitions -ge $minNew
        )
        $challengerExit = $null
        $lastChallengerAt = if ($previous) { $previous.last_challenger_at } else { $null }
        if ($challengerEligible) {
            & $Python -m app.research_lab continuous-edge-challenger-v2 `
                --symbols BTCUSDT,ETHUSDT --max-runtime-minutes 30 2>&1 |
                Tee-Object -FilePath $LogPath -Append | Out-Host
            $challengerExit = $LASTEXITCODE
            $lastChallengerAt = [DateTime]::UtcNow.ToString("o")
            if ($challengerExit -eq 0) { $lastFeatureCount = $featureCount }
        }
        $finished = [DateTime]::UtcNow
        $state = [ordered]@{
            schema = "storage_edge_scheduler.v1"
            status = if ($storageExit -eq 0 -and ($null -eq $challengerExit -or $challengerExit -eq 0)) { "COMPLETED" } else { "ERROR" }
            cycle = $cycle
            started_at = $started.ToString("o")
            finished_at = $finished.ToString("o")
            duration_seconds = [Math]::Round(($finished - $started).TotalSeconds, 3)
            storage_exit_code = $storageExit
            challenger_eligible = $challengerEligible
            challenger_exit_code = $challengerExit
            challenger_skip_reason = if ($challengerEligible) { $null } elseif ($heavyRunning) { "HEAVY_RESEARCH_RUNNING" } elseif (-not $collectorsHealthy) { "COLLECTORS_NOT_HEALTHY" } elseif (-not $diskOk) { "DISK_GUARD" } elseif (-not $intervalOk) { "MINIMUM_INTERVAL" } else { "NO_NEW_VERIFIED_PARTITIONS" }
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
