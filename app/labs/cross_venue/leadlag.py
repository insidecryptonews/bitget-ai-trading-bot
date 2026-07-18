"""Causal, receive-time cross-venue lead/lag detector.

The engine never compares unsynchronised exchange clocks to claim leadership.
Decision ordering is based exclusively on the local monotonic receive clock.
Exchange timestamps are retained for diagnostics and provenance.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import defaultdict, deque
from typing import Any

from . import POLICY_VERSION, code_revision, safety_envelope
from .models import comparable_to_bitget, finite


def _bps(new: float, old: float) -> float:
    return (new / old - 1.0) * 10_000.0 if old > 0 else 0.0


class LeadLagEngine:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.target = str(config.get("target_venue") or "bitget")
        self.history: dict[tuple[str, str], deque[tuple[int, float]]] = defaultdict(lambda: deque(maxlen=5000))
        self.quotes: dict[tuple[str, str], dict[str, Any]] = {}
        self.trade_flow: dict[tuple[str, str], deque[tuple[int, float]]] = defaultdict(lambda: deque(maxlen=20_000))
        self.intervals_ms: dict[tuple[str, str], deque[float]] = defaultdict(lambda: deque(maxlen=1000))
        self.last_mono: dict[tuple[str, str], int] = {}
        self.recent_leads: dict[tuple[str, str], tuple[int, float, str | None]] = {}
        self.last_signal_at: dict[str, int] = {}
        self.pending_outcomes: dict[str, dict[str, Any]] = {}
        self.observations: deque[dict[str, Any]] = deque(maxlen=1000)
        self.episodes: deque[dict[str, Any]] = deque(maxlen=500)
        self._active_episodes: dict[tuple[Any, ...], dict[str, Any]] = {}
        self.counters: dict[str, int] = defaultdict(int)
        self.horizon_stats: dict[tuple[str, str, int], dict[str, float]] = defaultdict(
            lambda: {"count": 0.0, "continuations": 0.0, "reversals": 0.0, "sum_target_bps": 0.0}
        )
        self.code_commit = code_revision()

    def _price_at_or_before(self, key: tuple[str, str], mono_ns: int) -> float | None:
        for observed_ns, price in reversed(self.history[key]):
            if observed_ns <= mono_ns:
                return price
        return None

    def _venue_resolution_ms(self, key: tuple[str, str]) -> float | None:
        values = sorted(self.intervals_ms[key])
        if not values:
            return None
        return values[len(values) // 2]

    def process(self, event: dict[str, Any]) -> dict[str, Any]:
        venue = str(event.get("venue") or "").lower()
        symbol = str(event.get("canonical_symbol") or "").upper()
        mono_ns = int(event.get("local_receive_monotonic_ns") or 0)
        result: dict[str, Any] = {"signal": None, "outcomes": []}
        if not venue or not symbol or mono_ns <= 0:
            return result
        key = (venue, symbol)
        state = self.quotes.setdefault(key, {})
        for field in ("best_bid", "best_ask", "bid_size", "ask_size", "mark_price",
                      "index_price", "funding_rate", "open_interest"):
            if event.get(field) is not None:
                state[field] = event[field]
        state["last_receive_monotonic_ns"] = mono_ns
        if event.get("event_type") == "trade":
            trade_price = finite(event.get("price"))
            if trade_price is not None and trade_price > 0:
                state["last_trade_price"] = trade_price
                state["last_trade_receive_monotonic_ns"] = mono_ns
        if event.get("event_type") == "trade" and finite(event.get("size")) is not None:
            signed = float(event["size"]) * (1.0 if event.get("taker_side") == "BUY" else -1.0 if event.get("taker_side") == "SELL" else 0.0)
            self.trade_flow[key].append((mono_ns, signed))

        bid, ask = finite(event.get("best_bid")), finite(event.get("best_ask"))
        price = (bid + ask) / 2.0 if bid is not None and ask is not None and bid > 0 and ask >= bid else None
        if price is None:
            return result
        state["quote_receive_monotonic_ns"] = mono_ns
        previous_mono = self.last_mono.get(key)
        if previous_mono is not None and mono_ns > previous_mono:
            self.intervals_ms[key].append((mono_ns - previous_mono) / 1_000_000.0)
        self.last_mono[key] = max(mono_ns, previous_mono or 0)
        self.history[key].append((mono_ns, price))

        if venue == self.target:
            result["outcomes"] = self._resolve_outcomes(symbol, mono_ns, price)
            return result
        if venue not in set(self.config.get("signal_eligible_venues") or []):
            return result
        if not comparable_to_bitget(event):
            return result

        window_ns = int(self.config.get("leader_return_window_ms", 500)) * 1_000_000
        old = self._price_at_or_before(key, mono_ns - window_ns)
        if old is None:
            return result
        move_bps = _bps(price, old)
        if abs(move_bps) < float(self.config.get("minimum_leader_move_bps", 4.0)):
            return result
        self.recent_leads[(venue, symbol)] = (
            mono_ns, move_bps, event.get("local_receive_wall_ts"),
        )
        signal = self._candidate(symbol, mono_ns, event)
        result["signal"] = signal
        if signal is not None:
            signal = self._attach_episode(signal)
            result["signal"] = signal
            self.observations.append(signal)
            if signal["status"] == "CANDIDATE_RESEARCH_ONLY":
                self.pending_outcomes[signal["signal_id"]] = signal
        return result

    def _candidate(self, symbol: str, decision_ns: int, event: dict[str, Any]) -> dict[str, Any] | None:
        cooldown_ns = 2_000_000_000
        last_signal = self.last_signal_at.get(symbol)
        if last_signal is not None and decision_ns - last_signal < cooldown_ns:
            self.counters["cooldown_suppressions"] += 1
            return None
        consensus_window_ns = int(self.config.get("consensus_window_ms", 750)) * 1_000_000
        leads = [
            (venue, ts, move, wall_ts)
            for (venue, candidate_symbol), (ts, move, wall_ts) in self.recent_leads.items()
            if candidate_symbol == symbol and 0 <= decision_ns - ts <= consensus_window_ns
        ]
        positives = [(v, t, m, wall_ts) for v, t, m, wall_ts in leads if m > 0]
        negatives = [(v, t, m, wall_ts) for v, t, m, wall_ts in leads if m < 0]
        aligned = positives if len(positives) >= len(negatives) else negatives
        direction = "LONG" if aligned is positives else "SHORT"
        min_venues = int(self.config.get("minimum_consensus_venues", 2))
        target_key = (self.target, symbol)
        target_price = self.history[target_key][-1][1] if self.history[target_key] else None
        target_quote = self.quotes.get(target_key)
        status = "CANDIDATE_RESEARCH_ONLY"
        rejection: str | None = None
        if len(aligned) < min_venues:
            status, rejection = "REJECTED_INSUFFICIENT_CONSENSUS", "minimum_consensus_not_met"
        if target_price is None or target_quote is None:
            status, rejection = "REJECTED_TARGET_NEED_DATA", "bitget_quote_missing"
        target_age_ms = None
        if target_quote is not None:
            target_age_ms = (decision_ns - int(target_quote.get("quote_receive_monotonic_ns") or 0)) / 1_000_000
            if target_age_ms < 0 or target_age_ms > int(self.config.get("stale_after_ms", 5000)):
                status, rejection = "REJECTED_FEED_STALE", "bitget_quote_stale"
        average_lead = sum(item[2] for item in aligned) / len(aligned) if aligned else 0.0
        target_old = self._price_at_or_before(target_key, decision_ns - int(self.config.get("leader_return_window_ms", 500)) * 1_000_000)
        target_move = _bps(target_price, target_old) if target_price is not None and target_old is not None else 0.0
        remaining_signed = average_lead - target_move
        expected_remaining = max(0.0, remaining_signed if direction == "LONG" else -remaining_signed)
        bid = finite((target_quote or {}).get("best_bid")); ask = finite((target_quote or {}).get("best_ask"))
        spread_bps = ((ask - bid) / ((ask + bid) / 2.0) * 10_000.0) if bid and ask and ask >= bid else math.inf
        round_trip_fee = float(self.config.get("round_trip_taker_fee_bps", 12.0))
        slippage_each = float(self.config.get("adverse_slippage_bps_each_side", 1.5))
        latency = float(self.config.get("latency_cost_bps", 1.0))
        impact = float(self.config.get("market_impact_bps", 0.5))
        funding = float(self.config.get("funding_cost_reserve_bps", 0.5))
        basis = float(self.config.get("basis_risk_reserve_bps", 1.0))
        fixed_cost = round_trip_fee + 2.0 * slippage_each + latency + impact + funding + basis
        total_cost = fixed_cost + spread_bps
        net_edge = expected_remaining - total_cost
        # Cost is the final gate.  It must never overwrite an earlier data,
        # freshness or consensus rejection because that corrupts the research
        # funnel and makes the true bottleneck impossible to measure.
        if status == "CANDIDATE_RESEARCH_ONLY":
            if not math.isfinite(net_edge):
                status, rejection = "REJECTED_TARGET_NEED_DATA", "bitget_spread_missing"
            elif net_edge < float(self.config.get("minimum_net_edge_bps", 3.0)):
                status, rejection = "REJECTED_COSTS", "estimated_move_does_not_clear_costs"
        if status == "CANDIDATE_RESEARCH_ONLY":
            self.last_signal_at[symbol] = decision_ns
        identity = {
            "symbol": symbol, "direction": direction, "decision_ns": decision_ns,
            "leaders": sorted(item[0] for item in aligned), "policy": POLICY_VERSION,
        }
        signal_id = "cvs_" + hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:28]
        first_lead = min(aligned, key=lambda item: item[1], default=None)
        first_lead_ns = first_lead[1] if first_lead is not None else decision_ns
        first_lead_ts = first_lead[3] if first_lead is not None else None
        cost_breakdown = {
            "entry_fee_bps": round_trip_fee / 2.0,
            "exit_fee_bps": round_trip_fee / 2.0,
            "spread_bps": spread_bps if math.isfinite(spread_bps) else None,
            "entry_slippage_bps": slippage_each,
            "exit_slippage_bps": slippage_each,
            "latency_reserve_bps": latency,
            "basis_reserve_bps": basis,
            "funding_reserve_bps": funding,
            "impact_reserve_bps": impact,
            "total_bps": total_cost if math.isfinite(total_cost) else None,
        }
        return {
            "signal_id": signal_id, "strategy_id": "CONSENSUS_LEAD_V1", "symbol": symbol,
            "leader_venues": sorted(item[0] for item in aligned), "target_venue": self.target,
            "direction": direction, "decision_ts": event.get("local_receive_wall_ts"),
            "decision_monotonic_ns": decision_ns, "first_lead_event_monotonic_ns": first_lead_ns,
            "first_lead_event_ts": first_lead_ts,
            "bitget_state_at_decision": {"price": target_price, "bid": bid, "ask": ask, "move_bps": target_move},
            "expected_remaining_move_bps": expected_remaining, "measured_latency_ms": (decision_ns - first_lead_ns) / 1_000_000,
            "estimated_total_cost_bps": total_cost, "unlevered_net_edge_bps": net_edge,
            "estimated_cost_breakdown_bps": cost_breakdown,
            "confidence": min(0.95, len(aligned) / max(1, len(set(self.config.get("signal_eligible_venues") or [])))),
            "features": {"average_leader_move_bps": average_lead, "target_move_bps": target_move,
                         "spread_bps": spread_bps, "target_age_ms": target_age_ms,
                         "funding_cost_reserve_bps": float(self.config.get("funding_cost_reserve_bps", 0.5)),
                         "basis_risk_reserve_bps": float(self.config.get("basis_risk_reserve_bps", 1.0)),
                         "ordering_clock": "LOCAL_MONOTONIC_RECEIVE"},
            "regime": "UNCLASSIFIED_FORWARD", "status": status, "rejection_reason": rejection,
            "policy_version": POLICY_VERSION, "feature_version": "CROSS_VENUE_CAUSAL_FEATURES_V1",
            "code_commit": self.code_commit, "research_only": True, "edge_validated": False,
            "not_actionable": True, "final_recommendation": "NO LIVE",
        }

    def _attach_episode(self, signal: dict[str, Any]) -> dict[str, Any]:
        leaders = tuple(sorted(str(item) for item in signal.get("leader_venues") or []))
        core = (
            str(signal.get("symbol") or ""), str(signal.get("direction") or ""),
            leaders, str(signal.get("regime") or "UNCLASSIFIED_FORWARD"), self.target,
        )
        decision_ns = int(signal.get("decision_monotonic_ns") or 0)
        episode = self._active_episodes.get(core)
        max_gap_ns = max(
            5_000_000_000,
            int(self.config.get("consensus_window_ms", 750)) * 4_000_000,
        )
        if episode is None or decision_ns - int(episode["last_observed_monotonic_ns"]) > max_gap_ns:
            identity = {
                "symbol": core[0], "direction": core[1], "leaders": leaders,
                "regime": core[3], "target": self.target,
                "first_lead_event_monotonic_ns": signal.get("first_lead_event_monotonic_ns"),
                "first_observed_monotonic_ns": decision_ns,
                "policy": POLICY_VERSION,
            }
            episode = {
                "episode_id": "cve_" + hashlib.sha256(
                    json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()[:28],
                "symbol": core[0], "direction": core[1], "leader_venues": list(leaders),
                "regime": core[3], "target_venue": self.target,
                "first_observed_at": signal.get("decision_ts"),
                "last_observed_at": signal.get("decision_ts"),
                "first_observed_monotonic_ns": decision_ns,
                "last_observed_monotonic_ns": decision_ns,
                "evaluations": 0, "candidate_evaluations": 0,
                "last_status": None, "last_rejection_reason": None,
            }
            self._active_episodes[core] = episode
            self.episodes.append(episode)
            self.counters["unique_market_episodes"] += 1
        else:
            self.counters["duplicate_evaluations"] += 1
        episode["evaluations"] += 1
        episode["last_observed_at"] = signal.get("decision_ts")
        episode["last_observed_monotonic_ns"] = decision_ns
        episode["last_status"] = signal.get("status")
        episode["last_rejection_reason"] = signal.get("rejection_reason")
        episode["last_expected_remaining_move_bps"] = signal.get("expected_remaining_move_bps")
        episode["last_estimated_total_cost_bps"] = signal.get("estimated_total_cost_bps")
        episode["last_unlevered_net_edge_bps"] = signal.get("unlevered_net_edge_bps")
        self.counters["raw_evaluations"] += 1
        status = str(signal.get("status") or "")
        if status == "CANDIDATE_RESEARCH_ONLY":
            episode["candidate_evaluations"] += 1
            self.counters["candidate_signals"] += 1
        elif status == "REJECTED_COSTS":
            self.counters["rejected_costs"] += 1
        elif status == "REJECTED_FEED_STALE":
            self.counters["rejected_stale"] += 1
        elif status == "REJECTED_INSUFFICIENT_CONSENSUS":
            self.counters["rejected_no_consensus"] += 1
        elif status == "REJECTED_TARGET_NEED_DATA":
            self.counters["rejected_need_data"] += 1
        else:
            self.counters["rejected_contract_mismatch"] += 1
        return {
            **signal,
            "episode_id": episode["episode_id"],
            "episode_evaluation": episode["evaluations"],
            "episode_first_observed_at": episode["first_observed_at"],
            "raw_evaluation_preserved": True,
        }

    def _resolve_outcomes(self, symbol: str, now_ns: int, target_price: float) -> list[dict[str, Any]]:
        outcomes: list[dict[str, Any]] = []
        for signal_id, signal in list(self.pending_outcomes.items()):
            if signal["symbol"] != symbol:
                continue
            elapsed_ms = (now_ns - int(signal["decision_monotonic_ns"])) / 1_000_000
            if elapsed_ms < 1000:
                continue
            start = finite((signal.get("bitget_state_at_decision") or {}).get("price"))
            if start is None or start <= 0:
                del self.pending_outcomes[signal_id]; continue
            move = _bps(target_price, start)
            signed = move if signal["direction"] == "LONG" else -move
            outcomes.append({
                "signal_id": signal_id, "symbol": symbol, "horizon_ms": 1000,
                "target_move_bps": move, "directional_move_bps": signed,
                "resolved_monotonic_ns": now_ns, "counterfactual_outcome_only": True,
                "no_lookahead_status": "OK_FORWARD_ONLY", **safety_envelope(),
            })
            for venue in signal["leader_venues"]:
                stats = self.horizon_stats[(venue, symbol, 1000)]
                stats["count"] += 1; stats["sum_target_bps"] += signed
                stats["continuations"] += 1 if signed > 0 else 0
                stats["reversals"] += 1 if signed < 0 else 0
            del self.pending_outcomes[signal_id]
        return outcomes

    def snapshot(self) -> dict[str, Any]:
        venues: list[dict[str, Any]] = []
        orderflow: list[dict[str, Any]] = []
        series: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(dict)
        for (venue, symbol), history in sorted(self.history.items()):
            quote = self.quotes.get((venue, symbol), {})
            price = history[-1][1] if history else None
            resolution = self._venue_resolution_ms((venue, symbol))
            receive_age_ms = max(
                0.0, (time.monotonic_ns() - int(self.last_mono.get((venue, symbol)) or 0)) / 1_000_000,
            )
            bid, ask = finite(quote.get("best_bid")), finite(quote.get("best_ask"))
            venues.append({
                "venue": venue, "symbol": symbol, "price": price, "best_bid": bid, "best_ask": ask,
                "bid_size": finite(quote.get("bid_size")), "ask_size": finite(quote.get("ask_size")),
                "mark_price": finite(quote.get("mark_price")), "index_price": finite(quote.get("index_price")),
                "funding_rate": finite(quote.get("funding_rate")), "open_interest": finite(quote.get("open_interest")),
                "last_trade_price": finite(quote.get("last_trade_price")),
                "price_basis": "L1_MIDPOINT_ONLY",
                "spread_bps": ((ask - bid) / ((ask + bid) / 2) * 10_000) if bid and ask and ask >= bid else None,
                "resolution_ms_observed_median": resolution,
                "measurable_horizons_ms": [h for h in self.config.get("horizons_ms", []) if resolution is not None and resolution <= h],
                "last_receive_monotonic_ns": self.last_mono.get((venue, symbol)),
                "receive_age_ms": receive_age_ms,
                "freshness_status": "FRESH" if receive_age_ms <= int(self.config.get("stale_after_ms", 5000)) else "STALE",
                "signal_eligible": venue in set(self.config.get("signal_eligible_venues") or []),
            })
            cutoff = self.last_mono.get((venue, symbol), 0) - 1_000_000_000
            flow = [signed for observed_ns, signed in self.trade_flow[(venue, symbol)] if observed_ns >= cutoff]
            buy_volume = sum(value for value in flow if value > 0); sell_volume = abs(sum(value for value in flow if value < 0))
            bid_size, ask_size = finite(quote.get("bid_size")), finite(quote.get("ask_size"))
            imbalance = ((bid_size - ask_size) / (bid_size + ask_size)) if bid_size is not None and ask_size is not None and bid_size + ask_size > 0 else None
            microprice = ((ask * bid_size + bid * ask_size) / (bid_size + ask_size)) if bid and ask and bid_size is not None and ask_size is not None and bid_size + ask_size > 0 else None
            orderflow.append({"venue": venue, "symbol": symbol, "trade_events_1s": len(flow),
                              "buy_volume_1s": buy_volume, "sell_volume_1s": sell_volume,
                              "net_aggressor_volume_1s": buy_volume - sell_volume,
                              "book_imbalance_l1": imbalance, "microprice": microprice,
                              "volume_unit_status": "SOURCE_NATIVE_NOT_CROSS_VENUE_COMPARABLE"})
            series[symbol][venue] = [
                {"monotonic_ns": observed_ns, "price": observed_price}
                for observed_ns, observed_price in list(history)[-240:]
            ]
        leaderboard = []
        for (venue, symbol, horizon), stats in sorted(self.horizon_stats.items()):
            count = int(stats["count"])
            leaderboard.append({
                "venue": venue, "symbol": symbol, "horizon_ms": horizon, "sample_size": count,
                "continuation_probability": stats["continuations"] / count if count else None,
                "reversal_probability": stats["reversals"] / count if count else None,
                "average_target_move_bps": stats["sum_target_bps"] / count if count else None,
                "validated": False, "status": "NEED_MORE_DATA" if count < 200 else "RESEARCH_ONLY",
            })
        return {
            "schema": "cross_venue_leadlag_snapshot.v1", "venues": venues,
            "orderflow": orderflow,
            "leaderboard": leaderboard, "recent_signals": list(self.observations)[-100:],
            "recent_episodes": [dict(row) for row in list(self.episodes)[-100:]],
            "evaluation_counts": {
                name: int(self.counters.get(name, 0)) for name in (
                    "raw_evaluations", "unique_market_episodes", "candidate_signals",
                    "rejected_costs", "rejected_stale", "rejected_contract_mismatch",
                    "rejected_no_consensus", "rejected_need_data",
                    "duplicate_evaluations", "cooldown_suppressions",
                )
            },
            "normalized_price_series": dict(series),
            "pending_outcomes": len(self.pending_outcomes),
            "strategy_research_status": [
                {"strategy_id": "CONSENSUS_LEAD_V1", "status": "ACTIVE_RESEARCH_ONLY"},
                {"strategy_id": "SINGLE_VENUE_LEAD", "status": "NEED_MORE_DATA_NOT_SIGNAL_ENABLED"},
                {"strategy_id": "ORDER_FLOW_LEAD", "status": "DIAGNOSTIC_ONLY_NEED_MORE_DATA"},
                {"strategy_id": "BOOK_PRESSURE_LEAD", "status": "DIAGNOSTIC_ONLY_NEED_MORE_DATA"},
                {"strategy_id": "SPOT_PERPETUAL_LEAD", "status": "DISABLED_NO_EQUIVALENT_SPOT_FEED"},
                {"strategy_id": "FUNDING_OI_DIVERGENCE", "status": "DIAGNOSTIC_ONLY_NEED_MORE_DATA"},
                {"strategy_id": "CEX_DEX_DIVERGENCE", "status": "OBSERVATION_ONLY_CONTRACT_NOT_EQUIVALENT"},
            ],
            "ordering_clock": "LOCAL_MONOTONIC_RECEIVE",
            "exchange_clock_leadership_claims_allowed": False, **safety_envelope(),
        }
