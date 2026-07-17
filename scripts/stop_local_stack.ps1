$ErrorActionPreference = "Continue"
. (Join-Path $PSScriptRoot "local_stack_common.ps1")
Set-Location -LiteralPath $script:RepoRoot
$stackStop = Join-Path $script:RuntimeRoot "stack.stop"
New-Item -ItemType Directory -Force -Path $script:RuntimeRoot | Out-Null
"controlled local stack stop" | Set-Content -LiteralPath $stackStop -Encoding ASCII

$atiStop = Join-Path $script:RepoRoot "data\runtime\ati_paper\executor.stop"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $atiStop) | Out-Null
"controlled local stack stop" | Set-Content -LiteralPath $atiStop -Encoding ASCII
Start-Sleep -Seconds 2
Write-Host "Waiting for cooperative Cross-Venue socket/ledger shutdown..." -ForegroundColor Cyan
Start-Sleep -Seconds 9

$managed = @(Get-ManagedProjectProcesses)
$roots = @($managed | Where-Object {
    $parent = [int]$_.ParentProcessId
    -not ($managed.ProcessId -contains $parent)
})
foreach ($process in $roots) {
    Write-Host "STOPPING: pid=$($process.ProcessId) $($process.Name)" -ForegroundColor Yellow
    Stop-ManagedProcessTree ([int]$process.ProcessId)
}
Start-Sleep -Seconds 2
$remaining = @(Get-ManagedProjectProcesses)
if ($remaining.Count -gt 0) {
    foreach ($process in $remaining) { Stop-ManagedProcessTree ([int]$process.ProcessId) }
}
Write-StackRegistry @()
Write-Host "LOCAL RESEARCH STACK STOPPED. Runtime ledgers and checkpoints preserved." -ForegroundColor Cyan
Write-Host "NO LIVE | NO PAPER FILTER | NO REAL ORDERS"
