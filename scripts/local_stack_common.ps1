$ErrorActionPreference = "Stop"

$script:RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$script:RuntimeRoot = Join-Path $script:RepoRoot "data\runtime\local_stack"
$script:RegistryPath = Join-Path $script:RuntimeRoot "process_registry.json"
$script:LogsRoot = Join-Path $script:RuntimeRoot "logs"
$script:Python = Join-Path $script:RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $script:Python)) { $script:Python = "python" }

$script:ManagedMarkers = @(
    "collect_forever.ps1",
    "collect_bybit_microstructure_forever.ps1",
    "collect_bybit_trades_ws_forever.ps1",
    "collect_bybit_trades_ws_persistent_forever.ps1",
    "run_scanner.bat",
    "run_ati_shadow_forever.ps1",
    "run_ati_paper_forever.ps1",
    "run_dashboard_watcher_forever.ps1",
    "run_research_server.ps1",
    "run_heavy_research_scheduler.ps1",
    "p11-forward-observer-run",
    "continuous-collection-run-cycle-v1027",
    "bybit-liquidations-ws-collect-v1030",
    "bybit-microstructure-run-cycle-v1032",
    "bybit-trades-ws-collect-v1042",
    "bybit-trades-ws-persistent-v1043c",
    "opportunity-scanner-run-v1028",
    "ati-shadow-run-v2",
    "refresh_ati_public_data",
    "research-dashboard-watch-v1043c",
    "app.labs.ati_paper.cli run",
    "app.labs.ati_paper.server",
    "research-heavy-run-v1044"
)

function Get-LocalStackDefinitions {
    return @(
        [pscustomobject]@{ Name="continuous_microstructure"; Kind="powershell"; Script="collect_forever.ps1" },
        [pscustomobject]@{ Name="bybit_microstructure"; Kind="powershell"; Script="collect_bybit_microstructure_forever.ps1" },
        [pscustomobject]@{ Name="persistent_ws_trades"; Kind="powershell"; Script="collect_bybit_trades_ws_persistent_forever.ps1" },
        [pscustomobject]@{ Name="shadow_scanner_p11"; Kind="batch"; Script="run_scanner.bat" },
        [pscustomobject]@{ Name="ati_shadow"; Kind="powershell"; Script="run_ati_shadow_forever.ps1" },
        [pscustomobject]@{ Name="ati_paper_executor"; Kind="powershell"; Script="run_ati_paper_forever.ps1" },
        [pscustomobject]@{ Name="dashboard_watcher"; Kind="powershell"; Script="run_dashboard_watcher_forever.ps1" },
        [pscustomobject]@{ Name="research_health_server"; Kind="powershell"; Script="run_research_server.ps1" },
        [pscustomobject]@{ Name="heavy_research_scheduler"; Kind="powershell"; Script="run_heavy_research_scheduler.ps1" }
    )
}

function Get-ManagedProjectProcesses {
    $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    return @($all | Where-Object {
        $line = [string]$_.CommandLine
        if ([string]::IsNullOrWhiteSpace($line)) { return $false }
        $inRepo = $line.IndexOf($script:RepoRoot, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
        if (-not $inRepo) { return $false }
        foreach ($marker in $script:ManagedMarkers) {
            if ($line.IndexOf($marker, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true }
        }
        return $false
    })
}

function Stop-ManagedProcessTree([int]$RootPid) {
    $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $byParent = @{}
    foreach ($item in $all) {
        $parent = [int]$item.ParentProcessId
        if (-not $byParent.ContainsKey($parent)) { $byParent[$parent] = New-Object System.Collections.ArrayList }
        [void]$byParent[$parent].Add([int]$item.ProcessId)
    }
    $ordered = New-Object System.Collections.ArrayList
    function Add-Descendants([int]$ProcessId) {
        if ($byParent.ContainsKey($ProcessId)) {
            foreach ($child in @($byParent[$ProcessId])) { Add-Descendants $child }
        }
        [void]$ordered.Add($ProcessId)
    }
    Add-Descendants $RootPid
    foreach ($pidValue in $ordered) {
        Stop-Process -Id $pidValue -Force -ErrorAction SilentlyContinue
    }
}

function Test-DefinitionRunning($Definition) {
    $needle = [string]$Definition.Script
    return @((Get-ManagedProjectProcesses) | Where-Object {
        ([string]$_.CommandLine).IndexOf($needle, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    }).Count -gt 0
}

function Start-StackDefinition($Definition) {
    $path = Join-Path $script:RepoRoot ("scripts\" + $Definition.Script)
    if (-not (Test-Path -LiteralPath $path)) { throw "STACK_SCRIPT_MISSING:$path" }
    if ($Definition.Kind -eq "batch") {
        return Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", ('"{0}"' -f $path)) -WorkingDirectory $script:RepoRoot -WindowStyle Normal -PassThru
    }
    return Start-Process -FilePath "powershell.exe" -ArgumentList @(
        "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ('"{0}"' -f $path)
    ) -WorkingDirectory $script:RepoRoot -WindowStyle Normal -PassThru
}

function Write-StackRegistry($Rows) {
    New-Item -ItemType Directory -Force -Path $script:RuntimeRoot | Out-Null
    New-Item -ItemType Directory -Force -Path $script:LogsRoot | Out-Null
    $payload = [ordered]@{
        schema = "local_research_stack.v1"
        repo = $script:RepoRoot
        generated_at = [DateTime]::UtcNow.ToString("o")
        research_only = $true
        paper_trading = $true
        live_trading = $false
        paper_filter_enabled = $false
        can_send_real_orders = $false
        processes = @($Rows)
        final_recommendation = "NO LIVE"
    }
    $tmp = $script:RegistryPath + ".tmp"
    $payload | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $tmp -Encoding UTF8
    Move-Item -LiteralPath $tmp -Destination $script:RegistryPath -Force
}
