# Dashboard V4 Research Pack

The Research Pack is a compact, secret-free status bundle for human or ChatGPT
review.

Endpoint:

```text
GET /api/research-pack?hours=24
GET /api/research-pack?hours=24&format=text
```

CLI:

```powershell
python -m app.research_lab research-pack --hours 24
```

## Included

- Git version.
- Safety flags.
- Final recommendation.
- Lightweight short report.
- Data freshness summary.
- Last 20 signals, labels, paper trades, and recent errors when tables exist.
- API 429 count.
- Worker lock summary when available.
- OHLCV row summary.
- Commands for heavier audits instead of running them inline.

## Excluded

- `.env` contents.
- API keys, auth tokens, passphrases, passwords.
- Database dumps.
- Backups, vaults, zips, or training exports.
- Heavy 720h replay labs.

## Heavy Research Guard

Dashboard Phase 9 buttons are intentionally light. Requests above 168h return
`HEAVY_RESEARCH_SKIPPED` with the exact CLI command unless `allow_heavy=true`
is explicitly supplied.

This prevents the dashboard from blocking the worker or presenting stale
long-running labs as live readiness.
