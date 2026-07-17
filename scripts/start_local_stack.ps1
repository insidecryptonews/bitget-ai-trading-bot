$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "local_stack_common.ps1")
Set-Location -LiteralPath $script:RepoRoot

Write-Host "Validating SAFE_PAPER_ONLY before starting local research stack..." -ForegroundColor Cyan
$audit = & $script:Python -m app.research_lab security-audit 2>&1 | Out-String
if ($LASTEXITCODE -ne 0 -or $audit -notmatch "SAFE_PAPER_ONLY") {
    throw "LOCAL_STACK_BLOCKED: security-audit did not return SAFE_PAPER_ONLY"
}

$rows = New-Object System.Collections.ArrayList
foreach ($definition in Get-LocalStackDefinitions) {
    if (Test-DefinitionRunning $definition) {
        Write-Host "ALREADY RUNNING: $($definition.Name)" -ForegroundColor DarkYellow
        [void]$rows.Add([ordered]@{ name=$definition.Name; script=$definition.Script; launcher_pid=$null; state="ALREADY_RUNNING" })
        continue
    }
    $process = Start-StackDefinition $definition
    Write-Host "STARTED: $($definition.Name) pid=$($process.Id)" -ForegroundColor Green
    [void]$rows.Add([ordered]@{ name=$definition.Name; script=$definition.Script; launcher_pid=$process.Id; state="STARTED" })
    Start-Sleep -Milliseconds 350
}
Write-StackRegistry $rows
Write-Host "LOCAL RESEARCH STACK STARTED" -ForegroundColor Cyan
Write-Host "Dashboard: http://127.0.0.1:8765/research-dashboard"
Write-Host "SIMULATION ONLY | NO LIVE | can_send_real_orders=false"
