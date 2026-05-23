# Microstructure Roadmap — Phase 7.4B Future Track

Status: **DESIGN ONLY**. No implementation. No WebSocket. No market making.

## Goal (years out, NOT phase 7.4B)

If — and only if — the bot demonstrates an edge with the current 5m OHLCV
infrastructure and Phase 7.4B labs find a real signal, then microstructure-
grade features become the natural next research target. Until that point, NO
WebSocket runtime is active, NO order book ingestion is built, NO market
making logic exists.

## Why microstructure later, not now

1. The bot has not demonstrated edge on classical-indicator 5m signals
   (Phase 7.2 findings: net_EV negative across all score buckets at 137k
   samples).
2. Adding microstructure inputs is hard: requires WebSocket reliability,
   latency budgeting, OBI/TFI computation, model that handles fast tick
   updates without drift.
3. Adding alpha sources before fixing the basic pipeline (TIME% ~88%,
   labels mostly TIME, no actionable candidates) would compound problems
   rather than solve them.
4. Live order book and trade flow data is expensive in compute and
   storage. We should not start until we have an edge to amplify.

## Features that would be valuable when this track activates

### Order Book Imbalance (OBI)
- Definition: `(bid_volume_topN - ask_volume_topN) / total_topN`.
- Time horizons: 100 ms, 1 s, 5 s.
- Signal: persistent imbalance + price following = bull/bear pressure.
- Risk: spoofed orders cancel before fill; OBI alone is noisy.

### Trade Flow Imbalance (TFI)
- Definition: `aggressor_buy_volume - aggressor_sell_volume` over window.
- Aggressor side from tick rule (uptick = buy, downtick = sell) or from
  exchange-provided side flag.
- Signal: TFI persists in same direction as bigger moves.
- Risk: thin books amplify TFI without real conviction.

### Microprice
- Definition: `(bid * ask_volume + ask * bid_volume) / (bid_volume + ask_volume)`.
- Better short-term price target than mid.
- Useful for entry timing and execution slippage estimation.

### Maker Fill Probability Model
- Given current order book depth + recent fill rate, estimate probability
  that a limit order at price X gets filled within window W.
- Enables maker-only strategies (lower fees) when probability is high.
- Requires high-quality WebSocket book feed.

### Liquidation Cascades
- Bitget exposes liquidation feeds. Liquidation clusters tend to mark
  short-term local extremes.
- Signal: after large liquidation cascade, mean-reversion bias.
- Risk: false signals during sustained trends.

## Required infrastructure

Before any of this is implementable:

1. **OHLCV foundation complete**. Track D persists 5m for all symbols.
2. **One candidate validated end-to-end**. Phase 7.4B labs + shadow monitor
   demonstrate at least one setup with positive net_EV over multiple months.
3. **Cost model audited and trusted**. Track F closes the double-counting
   risk. Real Bitget fees reconciled vs estimated.
4. **WebSocket roadmap unblocked**. See `docs/WEBSOCKET_ROADMAP.md`. Today
   WebSocket is explicitly NOT enabled.
5. **Tick-level storage decided**. Order book snapshots and trade flows
   are large; needs either a separate timeseries DB or aggressive
   downsampling. SQLite WAL is not the right destination.
6. **Latency budget defined**. Microstructure signals decay in seconds. A
   live bot that reads them must execute within tens of ms; current bot
   scan_interval is 30s. Architecture must change.
7. **Maker order routing**. Microstructure rewards passive fills. Current
   `ExecutionEngine` uses market orders for high-score signals. Maker
   routing is a separate workstream.

## Explicit non-goals for Phase 7.4B

- NO WebSocket runtime.
- NO live order book ingestion.
- NO market making.
- NO HFT-style strategies.
- NO tick-level storage.
- NO maker-only routing.
- NO sub-second cycle loop.

## When this track CAN activate

After the following gates are all green:

- [ ] Phase 7.4B labs produce at least one validated candidate.
- [ ] Candidate survives 30+ days of shadow monitor forward.
- [ ] Candidate survives 30+ days of paper filter ON (in shadow mode).
- [ ] Operator decides to invest in microstructure dev (months of work).
- [ ] WebSocket runtime path designed, tested, audited for safety.
- [ ] Latency / storage / cost budget approved.

Until then, this document is the entirety of microstructure work in repo.

FINAL_RECOMMENDATION: **NO LIVE**
