# Strategy Replay Backtester — Technical Contract (V10.2.2)

**Status: CONTRACT ONLY. No strategy engine, no optimizer, no UI implemented.**
This document specifies a FUTURE bar-by-bar replay backtester. It is a design
contract + acceptance criteria, not an implementation. A minimal safe **stub**
CLI (`strategy-replay-backtest-v103`) exists and only returns guard statuses
(`NEED_LONG_HISTORY` / `UNDERCOVERAGE_BLOCK` / `MISSING_OI_RISK` /
`RESEARCH_ONLY`) — it never simulates trades yet.

> ⚠️ NO LIVE. NO paper. NO order placement. NO optimizer / no auto-search for a
> "perfect strategy". NO threshold tuning on the test window. Research-only.

Future engine name: **`strategy-replay-backtest-v103`**.

## Goal
Replay history candle-by-candle, **without lookahead**, using only data
available up to each timestamp, opening/closing historical trades with
candidate rules, to see which would have worked. Start with **`ETHUSDT SHORT
crowded_longs_flush_z15`**, then a few controlled variants — never a wild search.

## A. Inputs
OHLCV, funding, open interest, liquidations, optional long/short ratio, fees,
slippage, funding cost, spread proxy, bucket signals, regime/context. All read
from clean external data + `ohlcv_candles` (read-only). No network, no DB writes.

## B. Rules (per candidate)
entry rule, invalidation rule, stop loss, take profit, time exit, max holding
time, cooldown, no-trade zones, one position per symbol, no overlapping trades
unless explicitly allowed.

## C. Anti-lookahead (mandatory)
- Entry only on the **next bar** after the signal bar.
- Never use a trade's future high/low before the trade is open.
- **Same-bar SL/TP ambiguity resolved conservatively**: if both SL and TP are
  touched within the same candle, assume the **worst case** (SL first).
- Features computed with **trailing windows only**.
- Labels/outcomes are never used in the entry/exit decision.

## D. Costs
Configurable maker/taker fees; slippage stress **x1/x2/x3**; funding
paid/received; **worst-case same-bar execution**; optional latency delay.
Gross-only results never promote — everything must clear costs.

## E. Walk-forward
Train / validation / test windows + rolling windows. **No threshold tuning on
the test window.** Report train and test **separately**. **Reject if the edge
only works in-sample.**

## F. Metrics
trades, winrate, avg win/loss, expectancy, net EV, profit factor (PF), max
drawdown, exposure time, time-in-trade, return by month, return by regime,
Sharpe-like (if meaningful), worst streak, dependency on top-1 / top-5 trades,
cost sensitivity, stability by split.

## G. Rejection criteria
fewer than minimum trades; PF only positive before costs; one-trade dominance;
one-week/one-month dominance; OOS fail; cost x2 fail; drawdown too high;
same-bar ambiguity too frequent; unresolved missing-OI dependency; edge
disappears outside ETH.

## H. Promotion ladder (strictly gated, never skips)
`RESEARCH_ONLY` → `BACKTEST_CANDIDATE` → `WALK_FORWARD_CANDIDATE` →
`SHADOW_RESEARCH_ONLY_FUTURE` → `PAPER_ELIGIBLE_FUTURE` (only after later gates).
**NEVER live directly.**

## I. Hard blockers (the engine MUST return these before simulating)
- If no **180d+** clean data → `NEED_LONG_HISTORY`.
- If missing OI **> 10%** and the strategy uses OI directly → `MISSING_OI_RISK`.
- If the latest fetch shows undercoverage → `UNDERCOVERAGE_BLOCK`.

## J. Dashboard relation
A future **Dashboard Trader Read-Only** should visualize replay trades, equity
curve, drawdown, signal markers and the safety panel — but the dashboard must
remain **read-only** (no order buttons, no go-live, no leverage/margin/sizing
controls, no `.env` edits, no DB writes).

## Out of scope for now (explicitly NOT implemented)
No UI, no strategy optimizer, no auto-search for a perfect strategy, no paper,
no live. Only this contract + the guard stub.

**FINAL_RECOMMENDATION: NO LIVE.**
