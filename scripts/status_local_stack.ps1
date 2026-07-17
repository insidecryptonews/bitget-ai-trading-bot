$ErrorActionPreference = "Continue"
. (Join-Path $PSScriptRoot "local_stack_common.ps1")
$definitions = Get-LocalStackDefinitions
$processes = @(Get-ManagedProjectProcesses)
$rows = foreach ($definition in $definitions) {
    $matching = @($processes | Where-Object {
        ([string]$_.CommandLine).IndexOf($definition.Script, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    })
    [ordered]@{ name=$definition.Name; running=($matching.Count -gt 0); pids=@($matching.ProcessId); script=$definition.Script }
}
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" -Method Get -TimeoutSec 5
} catch {
    $health = [ordered]@{ status="UNREACHABLE"; error=$_.Exception.Message; final_recommendation="NO LIVE" }
}
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
$payload | ConvertTo-Json -Depth 10
