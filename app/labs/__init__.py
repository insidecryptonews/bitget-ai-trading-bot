"""ResearchOps V8.2 — Bidirectional Forensics + Campaign + Exit Lab.

All labs in this package are research-only. They never:

- open orders,
- mutate ``LIVE_TRADING`` / ``ENABLE_PAPER_POLICY_FILTER`` / ``can_send_real_orders``,
- call private endpoints,
- modify leverage / margin / sizing / slots,
- touch the live PaperTrader / ExecutionEngine / Database write paths.

Every public function returns a dataclass with the invariants:

- ``research_only = True``
- ``paper_filter_enabled = False``
- ``can_send_real_orders = False``
- ``final_recommendation = "NO LIVE"``
"""

from __future__ import annotations

FINAL_RECOMMENDATION_NO_LIVE = "NO LIVE"

# Status / decision constants reused across labs.
STATUS_OK = "OK"
STATUS_NEED_DATA = "NEED_DATA"
STATUS_PARTIAL = "PARTIAL"

SIDE_LONG = "LONG"
SIDE_SHORT = "SHORT"
SIDE_NO_TRADE = "NO_TRADE"

REGIME_RISK_OFF = "RISK_OFF"
REGIME_RISK_ON = "RISK_ON"
REGIME_TREND_UP = "TREND_UP"
REGIME_TREND_DOWN = "TREND_DOWN"
REGIME_RANGE = "RANGE"
REGIME_CHOPPY = "CHOPPY_MARKET"
REGIME_HIGH_VOLATILITY = "HIGH_VOLATILITY"
REGIME_BREAKOUT_POSSIBLE = "BREAKOUT_POSSIBLE"
