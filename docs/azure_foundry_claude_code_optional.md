# Azure AI Foundry como vía opcional para Claude Code (NO CONFIGURADO)

Objetivo: que Adrián decida con datos si registrarse merece la pena para
ahorrar límites/créditos de Claude Code. Este doc NO configura nada, NO pide
keys y NO guarda endpoints. Todo manual y reversible.

## Qué es
Azure AI Foundry permite desplegar modelos Claude facturando por Azure. Claude
Code puede apuntar a ese endpoint mediante variables de entorno temporales, de
modo que el consumo sale del crédito/facturación de Azure en vez de la
suscripción de Anthropic.

## Qué hace falta (todo del lado de Adrián, nada en el repo)
1. Cuenta Azure (suele pedir tarjeta; a veces hay crédito temporal de bienvenida).
2. Crear un recurso AI Foundry y desplegar un modelo Claude (la disponibilidad
   de modelos depende de región y cuenta — verificar antes de contar con ello).
3. Obtener endpoint + API key del recurso.

## Cómo probarlo (manual, PowerShell, variables TEMPORALES de sesión)
```powershell
# SOLO en la sesión actual; jamás en .env ni en config global:
$env:ANTHROPIC_BASE_URL = "<endpoint-del-recurso>"
$env:ANTHROPIC_API_KEY  = "<key-del-recurso>"
claude   # arrancar Claude Code y comprobar con /status que el proveedor es el esperado
```
Al terminar:
```powershell
Remove-Item Env:ANTHROPIC_BASE_URL, Env:ANTHROPIC_API_KEY
```
Y apagar/eliminar el recurso de Azure si no se va a usar (evita coste pasivo).

## Reglas duras
- NO guardar la key en `.env`, en el repo ni en config global.
- NO usarlo en tareas críticas (audits, push, seguridad) hasta validar que el
  modelo/latencia/calidad son equivalentes.
- Vigilar costes en el portal de Azure desde el primer día.
- El repo no sabe ni debe saber nada de esto: es tooling de sesión.

## ¿Merece la pena?
- SÍ potencialmente, si los límites de Code son el cuello de botella real y
  hay crédito Azure disponible.
- NO, si implica fricción de facturación por un ahorro pequeño; el trabajo del
  bot (colectores/tests) no consume créditos de Code — solo las sesiones.

FINAL_RECOMMENDATION: NO LIVE (esto es tooling de sesión, no toca el bot).
