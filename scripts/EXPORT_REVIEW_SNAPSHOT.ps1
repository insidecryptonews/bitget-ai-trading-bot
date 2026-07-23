$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { $Python = "python" }
Set-Location -LiteralPath $Repo

$branch = (& git branch --show-current).Trim()
if ($branch -ne "backup/ati-wip-cdb0cee") {
    throw "REVIEW_EXPORT_BLOCKED_WRONG_BRANCH:$branch"
}
$audit = & $Python -m app.research_lab security-audit 2>&1 | Out-String
if ($LASTEXITCODE -ne 0 -or $audit -notmatch "SAFE_PAPER_ONLY") {
    throw "REVIEW_EXPORT_BLOCKED_SECURITY_AUDIT"
}

& $Python -m app.research_lab export-review-snapshot-v1 --apply
if ($LASTEXITCODE -ne 0) { throw "REVIEW_EXPORT_FAILED" }
Write-Host "SANITIZED REVIEW SNAPSHOT COMPLETE" -ForegroundColor Green
Write-Host "RESEARCH ONLY | NO LIVE | no holdout access | no orders"
