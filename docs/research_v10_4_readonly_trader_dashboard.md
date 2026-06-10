# ResearchOps V10.4 — Near-Real-Time Read-Only Trader Terminal

**Status:** read-only · near-real-time (GET polling) · NO LIVE
**Module:** `app/labs/trader_dashboard_v104.py`
**Server:** `app/health_server.py` (additive routes; existing `/dashboard` untouched)
**CLI:** `python -m app.research_lab trader-dashboard-contract-v104`

## Route

`GET /trader-terminal` — self-contained dark cyber/trader terminal page.
Uses the same `enable_training_dashboard` flag and the same
`dashboard_auth_token` auth (`?token=` or `X-Dashboard-Token`) as the
existing dashboard.

## Read-only API (GET only — there are NO mutable routes in V10.4)

| endpoint | content |
|---|---|
| `/api/researchops/v104/overview` | banner, mode, security, data classification |
| `/api/researchops/v104/safety` | flags derived from real config (never invented) |
| `/api/researchops/v104/data-readiness` | V10.3 data source audit (clean days, OI policy) — TTL cache 120s |
| `/api/researchops/v104/provider-readiness` | provider registry snapshot — TTL 600s |
| `/api/researchops/v104/provider-verification` | V10.4 manual verification report — TTL 600s |
| `/api/researchops/v104/candidates` | candidate-ranking lab payload — TTL 600s |
| `/api/researchops/v104/net-edge` | net-edge lab payload — TTL 600s |
| `/api/researchops/v104/paper-monitor` | open paper positions, paper PnL (labelled NOT real) |
| `/api/researchops/v104/signal-monitor` | top signals + top EdgeGuard blocks from the training pulse |
| `/api/researchops/v104/dashboard-state` | aggregate of all of the above (the polling target) |

Every handler is lazy-imported and wrapped in try/except: a labs failure can
never break `/health` or the existing dashboard.

## V10.4.1 hardening (Codex P2)

- **Unknown v104 endpoints return HTTP 404** with the sanitized payload
  `{"error": "unknown_researchops_v104_endpoint", "final_recommendation": "NO LIVE"}`
  (auth is still checked first when a token is configured).
- **Errors are sanitized**: public payloads only ever say
  `component_unavailable` / `data_temporarily_unavailable` /
  `research_endpoint_error` — no paths, no stack traces, no exception text.
  The real error goes to the internal logger (`app.health_server.v104`).
- **The 7s polling endpoint never computes heavy work** (single-threaded
  `HTTPServer` protection): `dashboard-state` composes from existing caches
  only. Cold/expired heavy sections answer `data_status: STALE_OR_PENDING`
  or `STALE` instead of computing.
- Heavy builders (`data-readiness` TTL 300s; `candidates`/`net-edge` TTL
  600s) run at most once concurrently (non-blocking lock): a second request
  during a build gets the stale copy or a pending placeholder.
- Light builders (`safety`, `overview`, `paper-monitor`, `signal-monitor`,
  `provider-readiness`, `provider-verification` — pure/in-memory) stay
  synchronous and cheap; `/health` stays fast.

## Near-real-time behaviour

- The page polls `GET /api/researchops/v104/dashboard-state` every **7s**
  (server-configurable via `dashboard_refresh_seconds`, clamped 3–60s) —
  ultra-light cache-peek on the server.
- A slow warm loop calls the heavy read-only endpoints (`data-readiness`,
  `candidates`, `net-edge`) on load and every **300s** so caches stay warm
  without making the fast polling expensive.
- ALL JavaScript network traffic goes through one `getJSON()` helper that
  hard-rejects any path outside `/api/researchops/v104/` and only issues GET.
  No WebSocket, no POST.
- Connection badge states: `LOADING` → `LIVE-POLL` → `STALE` (a poll failed
  but the last success is recent) → `ERROR` (no success for >3 intervals),
  plus a visible "data may be outdated" warning.
- "last update" timestamp (UTC) on every successful poll.
- Server renders an initial snapshot so the page is meaningful even before
  the first poll.

## Panels

Mission Control · Safety Panel (semaphores for LIVE_TRADING/DRY_RUN/
PAPER_TRADING/paper_filter) · Data Readiness (SVG progress ring: clean days /
180 + readiness bar + OI policy) · Provider Readiness (8-provider table) ·
Data Blockers · Candidate/Edge funnel · Net Edge Lab · Paper Monitor
(paper/shadow PnL explicitly labelled NOT real money) · Signal Monitor ·
Strategy/Research panel · Disabled Controls.

If any safety flag leaves its safe position the terminal shows a red
"SAFETY REVIEW REQUIRED" banner — the dashboard derives safety from real
flags instead of hardcoding green.

## Disabled future controls (visual only)

`Enable Live · Enable Paper Filter · Run Paid Download · Promote Candidate ·
Start Backtester Operational · Re-ingest Data · Replace Raw Data`

All rendered with the HTML `disabled` attribute, **no click handlers, no
backend routes**, tooltip: `Locked: requires explicit human approval + audit
+ gates`.

## Guarantees (tested)

- No `<form>`, no POST/PUT/DELETE routes, no JS fetch to anything outside
  `/api/researchops/v104/`.
- `can_send_real_orders` is derived (live && !dry_run) and shown honestly.
- Output ceiling everywhere: `final_recommendation: NO LIVE`.
