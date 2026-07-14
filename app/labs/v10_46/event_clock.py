"""V10.46 EventClock + data adapters (RESEARCH ONLY, causal & deterministic).

The EventClock replays a time-ordered stream of events and guarantees
causality: at any decision time T a consumer can only see events whose
`available_time_ms <= T`. It is the single time authority shared by replay,
shadow and paper research, so every strategy/agent/tournament participant
sees exactly the same information at exactly the same instants.

`bars_to_events` adapts the existing content-addressed dataset generations
(app.labs.public_data_backfill) into MarketEvents, so the whole V10.46
architecture REUSES the verified data layer instead of duplicating it. A bar
with open time T becomes an event available at T + timeframe (its close) —
never earlier — preserving the no-lookahead contract.
"""

from __future__ import annotations

from typing import Any, Iterator

from . import contracts as C

BAR_MS = 60_000

# V10.47.8: the clock is timeframe-aware. A fixed 1-minute step (BAR_MS) is only
# correct for 1m; every other timeframe MUST use its own interval so candle
# close, causal cutoff, entry/exit timestamps, bars_held, funding-settlement
# crossing and stale/time exits are computed correctly.
TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000,
         "1h": 3_600_000, "4h": 14_400_000}


def interval_ms_for(timeframe: str) -> int:
    """Milliseconds per bar for a timeframe. Raises on an unknown timeframe so a
    caller can never silently fall back to a wrong (1-minute) step."""
    if timeframe not in TF_MS:
        raise ValueError(f"unknown timeframe {timeframe!r}; known={sorted(TF_MS)}")
    return TF_MS[timeframe]


def cluster_id(symbol: str, ts_ms: int, block_ms: int = 30 * BAR_MS) -> str:
    """Deterministic temporal cluster used for cooldown and PAIRED tournament
    comparison: events in the same block on the same symbol share a cluster."""
    return f"{symbol}:{ts_ms // block_ms}"


def cluster_block_ms(timeframe: str) -> int:
    """Timeframe-aware cluster block for cooldown + n_eff: a wall-clock hour for
    intraday (1m/5m/15m), and one bar for 1h/4h. Never smaller than one bar."""
    return max(3_600_000, interval_ms_for(timeframe))


def cluster_id_tf(symbol: str, ts_ms: int, timeframe: str) -> str:
    """Timeframe-aware cluster id (see cluster_block_ms)."""
    return f"{symbol}:{ts_ms // cluster_block_ms(timeframe)}"


def session_id(symbol: str, ts_ms: int, session_ms: int = 8 * 3_600_000) -> str:
    """8-hour funding session block, used for session-level n_eff dependence."""
    return f"{symbol}:S{ts_ms // session_ms}"


def day_id(symbol: str, ts_ms: int) -> str:
    return f"{symbol}:D{ts_ms // 86_400_000}"


def bars_to_events(bars: list[dict], *, symbol: str, venue: str,
                   timeframe: str, data_generation_id: str | None,
                   repo_commit: str | None = None,
                   interval_ms: int = BAR_MS) -> list[dict]:
    """Adapt canonical bar dicts to MarketEvents. available_time = bar close
    (open ts + interval): a bar is NEVER visible before it closes."""
    out = []
    for b in bars:
        ts = int(b["ts"])
        close = ts + interval_ms
        eid = f"{symbol}:{ts}"
        out.append(C.make(
            "MarketEvent", symbol=symbol, venue=venue, timeframe=timeframe,
            event_id=eid, event_cluster_id=cluster_id(symbol, ts),
            causal_cutoff_ms=close, data_generation_id=data_generation_id,
            repo_commit=repo_commit, created_at_ms=close,
            event_type="BAR", ts_ms=ts, available_time_ms=close,
            payload={"open": float(b["open"]), "high": float(b["high"]),
                     "low": float(b["low"]), "close": float(b["close"]),
                     "volume": float(b.get("volume", 0.0))}))
    return out


class EventClock:
    """Causal, deterministic clock over a list of events with
    `available_time_ms`. Decision times advance monotonically; `visible_at`
    returns only causally-available events."""

    def __init__(self, events: list[dict]):
        # sort by availability then event time — ties broken deterministically
        self._events = sorted(
            events, key=lambda e: (int(e["available_time_ms"]),
                                   int(e.get("ts_ms", e["available_time_ms"])),
                                   str(e["event_id"])))
        self._avail = [int(e["available_time_ms"]) for e in self._events]
        self._last_decision = None

    def __len__(self) -> int:
        return len(self._events)

    @property
    def decision_times(self) -> list[int]:
        """The distinct instants at which new information becomes available."""
        seen, out = set(), []
        for t in self._avail:
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out

    def visible_at(self, decision_time_ms: int) -> list[dict]:
        """All events with available_time_ms <= decision_time (no lookahead).
        Enforces monotonic non-decreasing decision times within one stream."""
        import bisect
        if self._last_decision is not None and decision_time_ms < self._last_decision:
            raise ValueError("EventClock: decision time went backwards "
                             "(non-causal access)")
        self._last_decision = decision_time_ms
        hi = bisect.bisect_right(self._avail, decision_time_ms)
        return self._events[:hi]

    def latest_visible(self, decision_time_ms: int) -> dict | None:
        vis = self.visible_at(decision_time_ms)
        return vis[-1] if vis else None

    def stream(self, warmup: int = 0) -> Iterator[tuple[int, list[dict]]]:
        """Yield (decision_time_ms, visible_events) at every distinct
        availability instant after `warmup` events. Deterministic and causal;
        a fresh clock instance is used for the monotonic guard so callers can
        re-stream."""
        clk = EventClock(self._events)
        for idx, t in enumerate(clk.decision_times):
            vis = clk.visible_at(t)
            if len(vis) <= warmup:
                continue
            yield t, vis

    def window_close_only(self, decision_time_ms: int) -> list[dict]:
        """Bars usable for a decision at `decision_time_ms`: those whose close
        (available_time) is <= the decision time. Convenience for bar-based
        strategies that must never peek at the forming bar."""
        return [e for e in self.visible_at(decision_time_ms)
                if e.get("event_type") == "BAR"]
