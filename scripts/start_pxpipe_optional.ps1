# pxpipe OPTIONAL launcher (V10.37) - MANUAL ONLY, OFF BY DEFAULT.
# Token-saving proxy that renders bulky context as an image (OCR risk!).
# NEVER called by the bot. NO autostart. NO .env. NO keys. NO exchange. NO DB.
Write-Host "=================================================================" -ForegroundColor Yellow
Write-Host " PXPIPE OPCIONAL - AVISO OCR" -ForegroundColor Yellow
Write-Host " El bloque visual puede contener ERRORES DE LECTURA." -ForegroundColor Yellow
Write-Host " Para hashes, comandos, flags, rutas, seguridad, push y decisiones" -ForegroundColor Yellow
Write-Host " criticas usa UNICAMENTE texto normal (CRITICAL_TEXT_BLOCK)." -ForegroundColor Yellow
Write-Host " Ver docs/pxpipe_optional_usage.md. Ctrl+C para cerrar." -ForegroundColor Yellow
Write-Host "=================================================================" -ForegroundColor Yellow
npx pxpipe-proxy
