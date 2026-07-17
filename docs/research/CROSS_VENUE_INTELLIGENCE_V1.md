# Cross-Venue Intelligence V1

## Scope

Cross-Venue Intelligence is a local, public-data, research-only subsystem. It
observes whether equivalent linear perpetual markets appear to move before
Bitget, records causal research candidates, and can forward-simulate those
candidates in the isolated `CROSS_VENUE_PAPER_50` account.

It cannot submit an exchange order. It has no private client, key loader,
balance reader, transfer route, withdrawal route, live switch, or paper-policy
switch. Its HTTP surface is GET-only. Its leverage table is an offline
counterfactual over identical simulated fills and never changes exchange or bot
configuration.

Permanent safety contract:

```text
CROSS_VENUE_MODE=RESEARCH_PAPER_ONLY
PAPER_TRADING=True
LIVE_TRADING=False
DRY_RUN=True
paper_filter_enabled=false
can_send_real_orders=false
FINAL_RECOMMENDATION=NO LIVE
```

## Official provider inventory

The versioned inventory is
`config/cross_venue/providers_v1.json`. The source of truth is official vendor
documentation, not community endpoint lists.

| Tier | Provider | Public endpoint used | Initial role |
|---|---|---|---|
| 1 | Bitget | `wss://ws.bitget.com/v2/ws/public` | Target venue |
| 1 | Binance USD-M | `wss://fstream.binance.com/stream` | Eligible leader |
| 1 | Bybit Linear | `wss://stream.bybit.com/v5/public/linear` | Eligible leader |
| 1 | OKX V5 | `wss://ws.okx.com:8443/ws/v5/public` | Eligible leader |
| 1 | Hyperliquid | `wss://api.hyperliquid.xyz/ws` | Observation only |
| 2 disabled | Coinbase Exchange | Official public feed | USD spot research |
| 2 disabled | Kraken | Official public feed | Contract audit pending |

Official documentation references are recorded per provider in the inventory.
Commercial-use status deliberately remains `NEEDS_MANUAL_TERMS_REVIEW`; a
working public feed is not a legal conclusion.

Hyperliquid is observation-only because its USD/USDC collateral and product
contract are not silently treated as equivalent to Bitget USDT linear
perpetuals. Coinbase and Kraken are disabled until the contract/basis layer is
explicitly validated.

## Architecture

### Public adapters

`app/labs/cross_venue/adapters.py` implements a common interface:

- `connect()`
- `subscribe()`
- `receive()`
- `normalize()`
- `health()`
- `reconnect()`
- `close()`
- `capabilities()`
- `provenance()`

The allowlist is exact, WSS-only, and rejects credentials and sensitive query
parameters. Subscription messages contain public channel names only.

### Canonical event

Each event carries venue/source symbol, canonical symbol, product and quote
contract, event type, exchange timestamps, wall receive time, local monotonic
receive time, sequence/trade identifiers, market fields, connection identity,
reconnect count, schema provenance, and source status. Missing unsupported
fields remain `null`; zero is never fabricated.

### Storage

Generated data is ignored by Git and stored under:

```text
external_data/staging/cross_venue_v1/<venue>/
```

Raw frames and normalized JSONL are append-only and partitioned by venue,
symbol, event type and UTC date. Each venue has an atomic manifest, chained
hash, bounded restart dedup, health artifact, and atomic exclusive writer
lease. Partial JSONL lines are not silently consumed. The root must resolve to
the exact allowlisted staging directory and cannot be a symlink. Atomic health
replacement uses a bounded Windows retry for transient reader locks and never
falls back to an in-place partial write.

On the first productive engine start, byte offsets are frozen at the current
end of every venue stream. Existing rows remain research history and are never
replayed into `CROSS_VENUE_PAPER_50`. Versioned offsets resume only new rows;
stream replacement or truncation freezes a new boundary instead of replaying
from zero. A system monotonic-clock reset is detected while persisted byte
offsets remain authoritative.

### Causal lead-lag engine

The engine performs a durable k-way merge of the next unread row from every
venue by `local_receive_monotonic_ns`. Its global cycle budget cannot advance a
quiet venue beyond an unread busy-venue backlog. Events genuinely older than
the persisted causal frontier are dropped and counted; they cannot rewrite a
past decision. Exchange clocks are retained for diagnostics but are never used
alone to claim leadership. A configured 250 ms causal reorder buffer absorbs
normal cross-process flush jitter before the frontier advances.

Decision and outcome price history uses only observable L1 bid/ask midpoints.
Trades feed order-flow diagnostics, while mark price, index price, funding and
open interest remain diagnostics; none can create a lead or resolve an outcome.

The initial research policy requires:

- equivalent USDT linear perpetual contracts;
- fresh Bitget L1;
- at least two eligible leader venues aligned within the configured window;
- a leader move above the frozen threshold;
- remaining Bitget movement after the observed target move;
- estimated remaining movement above spread, round-trip taker fees, adverse
  slippage, latency cost, market impact, funding reserve, basis-risk reserve,
  and the safety margin.

Rejected observations are retained with reasons and counted separately from
candidate signals. Activity is never forced.
Observed feed resolution controls which horizons are measurable; the engine
does not interpolate 10 ms results from a slower feed.

Initial states are `NEED_MORE_DATA`, `WAITING_FOR_SIGNAL`, or explicit rejection.
No result is edge validation. Proper train/validation/test, walk-forward,
multiple-testing control, cost sensitivity, day/regime stability, and adequate
sample size remain mandatory before any later paper-policy discussion.

### Isolated paper account

`CROSS_VENUE_PAPER_50` has one persistent 50 USDT simulated credit in:

```text
data/runtime/cross_venue/cross_venue_paper.sqlite
```

The account is not shared with ATI, P11, or another strategy. Restarting does
not credit it again. A candidate can only receive a simulated fill after its
decision timestamp plus decision/send latency. Entry and exit include spread,
adverse slippage, and fees. Spread is represented by crossing observable L1;
slippage is deducted exactly once as an explicit cash cost. Missing L1 size is
rejected rather than replaced with an invented fill. Insufficient observed L1
size produces a partial fill or rejection. Consumed or non-positive estimated
edge is rejected. Funding is
`UNKNOWN_NOT_CLAIMED` until verified. Ambiguous bars use `STOP_BEFORE_TP`.

### Leverage lab

Scenarios 1x, 2x, 3x, 5x, 10x, 20x and 50x consume the same closed simulated
trade, fill, path, and cost rate. The current maintenance-margin model is
explicitly conservative and unverified, so it is not productive readiness.
Negative unlevered net edge remains negative at every leverage. No exchange
leverage, margin, sizing, or slots are changed.

## Local processes

The stack adds five public collectors and one engine process:

```text
cross_venue_bitget
cross_venue_binance
cross_venue_bybit
cross_venue_okx
cross_venue_hyperliquid
cross_venue_engine
```

They are managed by the existing `start_local_stack.ps1`,
`stop_local_stack.ps1`, `restart_local_stack.ps1`, and
`status_local_stack.ps1`. PowerShell mutexes and venue writer leases prevent
duplicate writers. Ctrl+C closes sockets and preserves append-only data and the
paper ledger. Cross-Venue runners also honor a cooperative stop file so public
sockets and writer leases are released before any forced fallback.

Standalone commands:

```powershell
python -m app.labs.cross_venue.cli verify-inventory
python -m app.labs.cross_venue.cli providers
python -m app.labs.cross_venue.cli collect --venue bybit --symbols BTCUSDT,ETHUSDT
python -m app.labs.cross_venue.cli engine
python -m app.labs.cross_venue.cli status
python -m app.labs.cross_venue.cli snapshot
```

## Read-only API and dashboard

The authenticated dashboard GET surface exposes:

```text
/api/cross-venue/status
/api/cross-venue/providers
/api/cross-venue/venues
/api/cross-venue/prices
/api/cross-venue/orderflow
/api/cross-venue/leadlag
/api/cross-venue/signals
/api/cross-venue/account
/api/cross-venue/positions
/api/cross-venue/trades
/api/cross-venue/equity
/api/cross-venue/events
/api/cross-venue/leverage
/api/cross-venue/health
/api/cross-venue/performance
```

There is no API to open, close, reset, configure, set leverage, add a key, or
activate anything. The dashboard labels the system `SIMULATION ONLY`,
`RESEARCH ONLY`, `NOT ACTIONABLE`, and `NO LIVE`; it shows `N/A`/`NEED_DATA`
instead of decorative values.

## Initial local verification, 2026-07-17

Public, no-auth WebSocket smokes received normalized real events from all Tier-1
venues. A forward boundary first excluded all pre-existing rows. Events appended
after that boundary were then consumed causally: 193 events, seven venue/symbol
states, zero positions, zero trades, and ledger reconciliation `PASS`. It
produced no candidate signal. This proves plumbing, forward isolation and
public-feed availability only. It does not prove lead-lag edge, profitability,
latency advantage, OOS stability, or paper readiness.

## Stop conditions

- A stale/missing Bitget quote blocks a candidate.
- A non-equivalent product blocks signal eligibility.
- Fewer than two aligned eligible venues blocks consensus.
- Estimated net edge below the cost/safety threshold blocks the candidate.
- Late events are excluded from causal decisions.
- Fewer than 200 closed observations remains `NEED_MORE_DATA`.
- No automatic promotion exists at any sample size.
- `FINAL_RECOMMENDATION` remains `NO LIVE`.
