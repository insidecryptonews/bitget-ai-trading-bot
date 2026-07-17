$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "stop_local_stack.ps1")
Start-Sleep -Seconds 3
& (Join-Path $PSScriptRoot "start_local_stack.ps1")
