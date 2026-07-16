# ATI V2 Automated Shadow Learning Plan

## Purpose

The 20 blind cases are seed evidence, not proof of edge and not an ML training
set. They define four deterministic hypotheses that can generate a much larger
causal sample from validated OHLCV:

- `SHORT_R1`: repeated resistance rejection.
- `SHORT_S1`: fatigued support, confirmed break, and non-recovery.
- `LONG_R1`: resistance break followed by a hold or defended retest.
- `LONG_S1`: objective support defence and reclaim.

Every decision uses closed data only. Entries are at the next 15m open. The
replay measures 15m, 30m, 1h, 2h, and 4h returns, MFE/MAE, costs, structural
TP/SL/TIME, and ten predeclared trailing variants.

## Statistical prior

The seed posterior is `Beta(12,6)`, mean `0.6667`, with a wide 95% credible
interval around `0.4404..0.8579`. This only justifies automated research.
Directional prior weights are metadata and never modify the deterministic
score or promotion gate.

## Promotion gate

A rule cannot even be described as promising unless it has at least 40 trades,
positive net EV after costs, PF >= 1.15, a positive bootstrap lower bound,
chronological validation and test stability, and no more than 40% of positive
PnL concentrated in the top three trades. It still remains shadow-only and
requires at least 30 days of forward evidence and human audit.

The available local BTCUSDT/ETHUSDT snapshot spans 90 days. It is suitable for
initial research but below the 180/365-day validation requirement.

## Invariants

```text
SHADOW_RESEARCH_ONLY
paper_filter_enabled=false
can_send_real_orders=false
paper_ready=false
live_ready=false
FINAL_RECOMMENDATION=NO LIVE
```

Leverage values 3 and 5 are diagnostic MAE/liquidation scenarios only. They do
not modify bot leverage, sizing, margin, slots, or simulated underlying edge.
