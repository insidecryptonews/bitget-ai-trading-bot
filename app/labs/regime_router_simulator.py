"""V8.2 — Regime Router Simulator (research-only).

Implements a research-only bidirectional router and replays it over a
historical timeline. Never connected to ``signal_engine``; the production
``regime_detector`` is untouched.

States:

- ``LONG_ONLY_RESEARCH``
- ``SHORT_ONLY_RESEARCH``
- ``BOTH_ALLOWED_RESEARCH``
- ``NO_TRADE``
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from . import (
    FINAL_RECOMMENDATION_NO_LIVE,
    REGIME_CHOPPY,
    REGIME_HIGH_VOLATILITY,
    REGIME_RANGE,
    REGIME_RISK_OFF,
    REGIME_RISK_ON,
    REGIME_TREND_DOWN,
    REGIME_TREND_UP,
    SIDE_LONG,
    SIDE_SHORT,
    STATUS_NEED_DATA,
    STATUS_OK,
)


STATE_LONG_ONLY = "LONG_ONLY_RESEARCH"
STATE_SHORT_ONLY = "SHORT_ONLY_RESEARCH"
STATE_BOTH = "BOTH_ALLOWED_RESEARCH"
STATE_NO_TRADE = "NO_TRADE"

VALID_STATES: tuple[str, ...] = (STATE_LONG_ONLY, STATE_SHORT_ONLY, STATE_BOTH, STATE_NO_TRADE)


@dataclass
class RouterInputs:
    timestamp: str
    btc_bias_1h: str = "neutral"
    btc_bias_4h: str = "neutral"
    eth_bias_1h: str = "neutral"
    pct_universe_up: float = 0.5
    pct_universe_down: float = 0.5
    regime_current: str = REGIME_RANGE
    atr_norm_avg: float | None = None
    spread_bps_avg: float | None = None
    funding_avg: float | None = None
    oi_delta_24h_pct: float | None = None
    liquidations_24h_usd: float | None = None
    has_high_severity_event: bool = False
    news_risk_red: bool = False
    universe_volume_avg_usd: float | None = None


@dataclass
class RouterDecision:
    timestamp: str
    state: str
    allowed_sides: list[str] = field(default_factory=list)
    bias_strength: float = 0.0
    override_reason: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RouterSimulationReport:
    hours: int
    samples: int
    by_state: dict[str, int] = field(default_factory=dict)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    coverage_pct: dict[str, float] = field(default_factory=dict)
    status: str = STATUS_NEED_DATA
    need_data_reasons: list[str] = field(default_factory=list)
    research_only: bool = True
    paper_filter_enabled: bool = False
    can_send_real_orders: bool = False
    final_recommendation: str = FINAL_RECOMMENDATION_NO_LIVE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---- Decision logic --------------------------------------------------------

def _override(inputs: RouterInputs) -> tuple[bool, str]:
    """Hard overrides to ``NO_TRADE``."""
    if inputs.has_high_severity_event:
        return True, "macro_or_event_high_severity_active"
    if inputs.news_risk_red:
        return True, "news_risk_gate_red"
    if inputs.universe_volume_avg_usd is not None and inputs.universe_volume_avg_usd < 20_000_000:
        return True, "universe_liquidity_too_low"
    if inputs.regime_current == REGIME_CHOPPY:
        return True, "choppy_market_confirmed"
    if (
        inputs.atr_norm_avg is not None and inputs.atr_norm_avg > 0.035
        and inputs.spread_bps_avg is not None and inputs.spread_bps_avg > 25
    ):
        return True, "panic_extreme_atr_and_spread"
    return False, ""


def decide(inputs: RouterInputs) -> RouterDecision:
    override, reason = _override(inputs)
    if override:
        return RouterDecision(
            timestamp=inputs.timestamp,
            state=STATE_NO_TRADE,
            allowed_sides=[],
            bias_strength=0.0,
            override_reason=reason,
            inputs=asdict(inputs),
        )
    btc1h = inputs.btc_bias_1h.lower()
    btc4h = inputs.btc_bias_4h.lower()
    regime = inputs.regime_current.upper()
    # Determine direction signal
    if regime in {REGIME_RISK_ON, REGIME_TREND_UP} or (btc1h == "bullish" and btc4h in {"bullish", "neutral"}):
        if inputs.pct_universe_up >= 0.70:
            return RouterDecision(
                timestamp=inputs.timestamp, state=STATE_LONG_ONLY,
                allowed_sides=[SIDE_LONG],
                bias_strength=min(1.0, inputs.pct_universe_up),
                inputs=asdict(inputs),
            )
        if inputs.pct_universe_up >= 0.50:
            return RouterDecision(
                timestamp=inputs.timestamp, state=STATE_LONG_ONLY,
                allowed_sides=[SIDE_LONG],
                bias_strength=inputs.pct_universe_up,
                inputs=asdict(inputs),
            )
        return RouterDecision(
            timestamp=inputs.timestamp, state=STATE_BOTH,
            allowed_sides=[SIDE_LONG, SIDE_SHORT],
            bias_strength=0.3,
            inputs=asdict(inputs),
        )
    if regime in {REGIME_RISK_OFF, REGIME_TREND_DOWN} or (btc1h == "bearish" and btc4h in {"bearish", "neutral"}):
        if inputs.pct_universe_down >= 0.70:
            return RouterDecision(
                timestamp=inputs.timestamp, state=STATE_SHORT_ONLY,
                allowed_sides=[SIDE_SHORT],
                bias_strength=min(1.0, inputs.pct_universe_down),
                inputs=asdict(inputs),
            )
        if inputs.pct_universe_down >= 0.50:
            return RouterDecision(
                timestamp=inputs.timestamp, state=STATE_SHORT_ONLY,
                allowed_sides=[SIDE_SHORT],
                bias_strength=inputs.pct_universe_down,
                inputs=asdict(inputs),
            )
        return RouterDecision(
            timestamp=inputs.timestamp, state=STATE_BOTH,
            allowed_sides=[SIDE_LONG, SIDE_SHORT],
            bias_strength=0.3,
            inputs=asdict(inputs),
        )
    # Neutral
    if regime == REGIME_HIGH_VOLATILITY:
        return RouterDecision(
            timestamp=inputs.timestamp, state=STATE_BOTH,
            allowed_sides=[SIDE_LONG, SIDE_SHORT],
            bias_strength=0.2,
            inputs=asdict(inputs),
        )
    if inputs.pct_universe_up < 0.30 and inputs.pct_universe_down < 0.30:
        return RouterDecision(
            timestamp=inputs.timestamp, state=STATE_NO_TRADE,
            allowed_sides=[],
            bias_strength=0.0,
            override_reason="universe_too_neutral",
            inputs=asdict(inputs),
        )
    return RouterDecision(
        timestamp=inputs.timestamp, state=STATE_BOTH,
        allowed_sides=[SIDE_LONG, SIDE_SHORT],
        bias_strength=0.3,
        inputs=asdict(inputs),
    )


# ---- Simulation over historical snapshots ----------------------------------

def _safe_call(db: Any, method: str, *args, **kwargs) -> tuple[bool, Any]:
    if db is None:
        return False, None
    fn = getattr(db, method, None)
    if fn is None or not callable(fn):
        return False, None
    try:
        return True, fn(*args, **kwargs)
    except Exception:
        return False, None


def simulate_router(
    db: Any,
    *,
    hours: int = 168,
    inputs_stream: Iterable[RouterInputs] | None = None,
) -> RouterSimulationReport:
    """Replay the router over an inputs timeline.

    For tests, pass ``inputs_stream`` directly. For VPS, the function will
    call ``db.fetch_router_inputs(hours=...)`` which is expected to return an
    iterable of dict rows that map to ``RouterInputs``. If absent, the
    function returns ``NEED_DATA``.
    """
    report = RouterSimulationReport(hours=int(hours), samples=0)
    if inputs_stream is None:
        ok, value = _safe_call(db, "fetch_router_inputs", hours=int(hours))
        if not ok or not value:
            report.need_data_reasons.append("fetch_router_inputs_method_missing_or_empty")
            return report
        stream_list: list[RouterInputs] = []
        for raw in value:
            if isinstance(raw, RouterInputs):
                stream_list.append(raw)
                continue
            if isinstance(raw, dict):
                try:
                    stream_list.append(RouterInputs(**{k: v for k, v in raw.items() if k in RouterInputs.__dataclass_fields__}))
                except Exception:
                    continue
        inputs_stream = stream_list
    decisions = [decide(inp) for inp in inputs_stream]
    counts: dict[str, int] = {s: 0 for s in VALID_STATES}
    for d in decisions:
        counts[d.state] = counts.get(d.state, 0) + 1
    total = max(len(decisions), 1)
    report.samples = len(decisions)
    report.by_state = counts
    report.coverage_pct = {s: counts.get(s, 0) / total for s in VALID_STATES}
    # Persist a sample of decisions (max 200) to keep output bounded.
    report.decisions = [d.as_dict() for d in decisions[:200]]
    report.status = STATUS_OK if decisions else STATUS_NEED_DATA
    return report
