param([switch]$TraceTiming)
$ErrorActionPreference = "Continue"
$script:StatusWatch = [Diagnostics.Stopwatch]::StartNew()
function Write-StatusTrace([string]$Stage) {
    if ($TraceTiming) {
        $line = "STATUS_TRACE {0:N3}s {1}" -f $script:StatusWatch.Elapsed.TotalSeconds, $Stage
        Add-Content -LiteralPath $script:StatusTracePath -Value $line -Encoding UTF8
    }
}
. (Join-Path $PSScriptRoot "local_stack_common.ps1")
$script:StatusTracePath = Join-Path $script:RuntimeRoot "status_trace.log"
if ($TraceTiming) {
    New-Item -ItemType Directory -Force -Path $script:RuntimeRoot | Out-Null
    Set-Content -LiteralPath $script:StatusTracePath -Value "" -Encoding UTF8
}
$definitions = Get-LocalStackDefinitions
$processes = @(Get-ManagedProjectProcesses)
Write-StatusTrace "managed-processes"
$allProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
$allListeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue)
Write-StatusTrace "processes-and-listeners"
$childrenByParent = @{}
$processById = @{}
foreach ($process in $allProcesses) {
    $processById[[int]$process.ProcessId] = $process
    $parentPid = [int]$process.ParentProcessId
    if (-not $childrenByParent.ContainsKey($parentPid)) {
        $childrenByParent[$parentPid] = New-Object System.Collections.ArrayList
    }
    [void]$childrenByParent[$parentPid].Add([int]$process.ProcessId)
}
Write-StatusTrace "process-maps"
$now = [DateTime]::UtcNow
$artifactMap = @{
    continuous_microstructure = "external_data\staging\continuous_forward_v10_27\dataset\manifest.json"
    bybit_microstructure = "external_data\staging\bybit_microstructure_v10_32\dataset\manifest.json"
    persistent_ws_trades = "external_data\staging\bybit_trades_ws_persistent_v10_43c\health.json"
    shadow_scanner_p11 = "reports\research\p11_short_forward_observer\observer_status.json"
    ati_shadow = "reports\research\ati\ati_health.json"
    ati_paper_executor = "data\runtime\ati_paper\executor_status.json"
    dashboard_watcher = "reports\research\dashboard_v10_43c\dashboard_watch_status_v1043c.json"
    research_health_server = "reports\research\dashboard_v10_43c\index.html"
    heavy_research_scheduler = "data\runtime\heavy_research\scheduler_status.json"
    cross_venue_bitget = "external_data\staging\cross_venue_v1\bitget\health.json"
    cross_venue_binance = "external_data\staging\cross_venue_v1\binance\health.json"
    cross_venue_bybit = "external_data\staging\cross_venue_v1\bybit\health.json"
    cross_venue_okx = "external_data\staging\cross_venue_v1\okx\health.json"
    cross_venue_hyperliquid = "external_data\staging\cross_venue_v1\hyperliquid\health.json"
    cross_venue_engine = "data\runtime\cross_venue\engine_status.json"
    storage_edge_scheduler = "data\runtime\storage_efficiency_v2\scheduler_status.json"
}

function Get-DescendantPids([int[]]$RootPids) {
    $seen = [System.Collections.Generic.HashSet[int]]::new()
    $queue = New-Object System.Collections.Queue
    foreach ($rootPid in $RootPids) { $queue.Enqueue($rootPid); [void]$seen.Add($rootPid) }
    while ($queue.Count -gt 0) {
        $parent = [int]$queue.Dequeue()
        if ($childrenByParent.ContainsKey($parent)) {
            foreach ($childPid in @($childrenByParent[$parent])) {
                $childPid = [int]$childPid
                if ($seen.Add($childPid)) { $queue.Enqueue($childPid) }
            }
        }
    }
    return @($seen)
}

$rows = foreach ($definition in $definitions) {
    Write-StatusTrace ("component-start:" + $definition.Name)
    $matching = @($processes | Where-Object {
        ([string]$_.CommandLine).IndexOf($definition.Script, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    })
    $rootPids = @($matching.ProcessId | ForEach-Object { [int]$_ })
    $treePids = if ($rootPids.Count -gt 0) { @(Get-DescendantPids $rootPids) } else { @() }
    $primary = $matching | Select-Object -First 1
    $created = if ($primary -and $primary.CreationDate) { ([datetime]$primary.CreationDate).ToUniversalTime() } else { $null }
    $memoryBytes = 0.0
    $cpuSeconds = 0.0
    foreach ($treePid in $treePids) {
        if ($processById.ContainsKey([int]$treePid)) {
            $proc = $processById[[int]$treePid]
            $memoryBytes += [double]$proc.WorkingSetSize
            $cpuSeconds += ([double]$proc.KernelModeTime + [double]$proc.UserModeTime) / 10000000.0
        }
    }
    $ports = @()
    if ($treePids.Count -gt 0) {
        $ports = @($allListeners |
            Where-Object { $treePids -contains [int]$_.OwningProcess } |
            ForEach-Object {
                [ordered]@{
                    local_address = [string]$_.LocalAddress
                    local_port = [int]$_.LocalPort
                    owning_process = [int]$_.OwningProcess
                }
            })
    }
    $logPath = Join-Path $script:LogsRoot ($definition.Name + ".log")
    $logInfo = if (Test-Path -LiteralPath $logPath) { Get-Item -LiteralPath $logPath } else { $null }
    $artifactPath = Join-Path $script:RepoRoot ([string]$artifactMap[$definition.Name])
    $artifact = if (Test-Path -LiteralPath $artifactPath) { Get-Item -LiteralPath $artifactPath } else { $null }
    [ordered]@{
        name=$definition.Name
        running=($matching.Count -gt 0)
        pids=$rootPids
        process_tree_pids=$treePids
        duplicate_launchers=($matching.Count -gt 1)
        script=$definition.Script
        command_line=if ($primary) { [string]$primary.CommandLine } else { $null }
        started_at=if ($created) { $created.ToString("o") } else { $null }
        uptime_seconds=if ($created) { [Math]::Round(($now - $created).TotalSeconds, 1) } else { $null }
        memory_mb=[Math]::Round($memoryBytes / 1MB, 2)
        cpu_seconds=[Math]::Round($cpuSeconds, 2)
        listening_ports=$ports
        log_path=if ($logInfo) { $logInfo.FullName } else { $null }
        log_mtime=if ($logInfo) { $logInfo.LastWriteTimeUtc.ToString("o") } else { $null }
        last_log_line=if ($logInfo) { Get-Content -LiteralPath $logInfo.FullName -Tail 1 -ErrorAction SilentlyContinue } else { $null }
        latest_artifact=if ($artifact) { $artifact.FullName } else { $null }
        artifact_mtime=if ($artifact) { $artifact.LastWriteTimeUtc.ToString("o") } else { $null }
        artifact_age_seconds=if ($artifact) { [Math]::Round(($now - $artifact.LastWriteTimeUtc).TotalSeconds, 1) } else { $null }
    }
    Write-StatusTrace ("component-done:" + $definition.Name)
}
Write-StatusTrace "components-complete"
try {
    $rawHealth = Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" -Method Get -TimeoutSec 5
    $shadow = $rawHealth.research_components.ati_shadow
    $health = [ordered]@{
        status = $rawHealth.overall_status
        http_status = $rawHealth.status
        mode = $rawHealth.mode
        safety = $rawHealth.research_components.safety
        collectors = $rawHealth.research_components.collectors
        datasets = $rawHealth.research_components.datasets
        dashboard_watcher = $rawHealth.research_components.dashboard_watcher
        heavy_research = $rawHealth.research_components.heavy_research
        ati_shadow = [ordered]@{
            status = $shadow.status
            observer_status = $shadow.observer_status
            reconciliation_status = $shadow.reconciliation_status
            last_run_at = $shadow.last_run_at
            dataset_last_bar_at = $shadow.dataset_last_bar_at
            dataset_age_seconds = $shadow.dataset_age_seconds
            forward_signals = $shadow.forward_signals
            open_positions = $shadow.open_positions
            closed_shadow_trades = $shadow.closed_shadow_trades
            can_send_real_orders = $shadow.can_send_real_orders
            final_recommendation = $shadow.final_recommendation
        }
        ati_paper_executor = $rawHealth.ati_paper_executor
        cross_venue = $rawHealth.cross_venue
        can_send_real_orders = $rawHealth.can_send_real_orders
        final_recommendation = $rawHealth.final_recommendation
    }
} catch {
    $health = [ordered]@{ status="UNREACHABLE"; error=$_.Exception.Message; final_recommendation="NO LIVE" }
}
Write-StatusTrace "health-complete"
$payload = [ordered]@{
    schema = "local_research_stack_status.v1"
    generated_at = [DateTime]::UtcNow.ToString("o")
    branch = (& git -C $script:RepoRoot branch --show-current).Trim()
    head = (& git -C $script:RepoRoot rev-parse HEAD).Trim()
    components = @($rows)
    health = $health
    paper_trading = $true
    live_trading = $false
    dry_run = $true
    paper_filter_enabled = $false
    can_send_real_orders = $false
    final_recommendation = "NO LIVE"
}
if ($TraceTiming) {
    $null = $payload.components | ConvertTo-Json -Depth 6
    Write-StatusTrace "components-json-complete"
    $null = $payload.health | ConvertTo-Json -Depth 6
    Write-StatusTrace "health-json-complete"
}
$payload | ConvertTo-Json -Depth 6
Write-StatusTrace "json-complete"
