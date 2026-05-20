# WebSocket Roadmap

Status: future proposal only.

No WebSocket runtime is active in Fase 7.1. This phase only stabilizes dashboard reports, HTTP tests, and the local OHLCV replay loader for the real strategy backtester.

WebSocket work should wait until these requirements are true:

- OHLCV replay loader is `OK` with persisted candles and continuity checks.
- Dashboard short/full reports return `OK` or controlled `PARTIAL_REPORT` without hanging.
- Full test suite passes consistently.
- Single-worker lock and heartbeat are stable.
- Candle storage is idempotent and rejects duplicate candles safely.
- Reconnect, backoff, and rate-limit behavior are tested.
- `LIVE_TRADING=false`, `DRY_RUN=true`, `PAPER_TRADING=true`.
- `ENABLE_PAPER_POLICY_FILTER=false`.
- No live orders, no leverage/margin/sizing changes, no slot expansion.

Until then, WebSocket remains a research roadmap item, not an active runtime feature.
