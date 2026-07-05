# LEGACY WRAPPER (V10.36): this script now runs the FULL Bybit microstructure
# loop (trades/orderbook/OI/funding + liquidations), not only liquidations.
# Kept so old shortcuts keep working. RESEARCH ONLY - NO LIVE - NO ORDERS.
Write-Host "legacy wrapper: now runs full Bybit microstructure V10.32 loop" -ForegroundColor Yellow
& (Join-Path $PSScriptRoot "collect_bybit_microstructure_forever.ps1")
