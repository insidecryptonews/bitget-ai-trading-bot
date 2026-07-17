"""Conservative forward-only simulation for cross-venue research candidates."""

from __future__ import annotations

import hashlib
import math
from typing import Any

from . import safety_envelope
from .ledger import CrossVenueLedger
from .models import finite


def choose_bar_exit(direction: str, *, high: float, low: float, stop: float, take_profit: float) -> str | None:
    """Conservative ambiguity contract: stop wins if TP and stop share a bar."""
    direction = str(direction).upper()
    if direction == "LONG":
        stop_hit, tp_hit = low <= stop, high >= take_profit
    elif direction == "SHORT":
        stop_hit, tp_hit = high >= stop, low <= take_profit
    else:
        raise ValueError("CROSS_VENUE_DIRECTION_INVALID")
    if stop_hit:
        return "STOP_BEFORE_TP"
    if tp_hit:
        return "TAKE_PROFIT"
    return None


class PaperSimulator:
    def __init__(self, config: dict[str, Any], ledger: CrossVenueLedger):
        self.config = config
        self.ledger = ledger
        self.ledger.initialize(float(config.get("paper_initial_balance_usdt", 50.0)))

    def on_signal(self, signal: dict[str, Any]) -> bool:
        return self.ledger.record_signal(signal)

    def on_bitget_quote(self, event: dict[str, Any]) -> dict[str, Any]:
        if event.get("venue") != "bitget":
            return {"opened": [], "closed": [], **safety_envelope()}
        symbol = str(event.get("canonical_symbol") or "")
        mono_ns = int(event.get("local_receive_monotonic_ns") or 0)
        wall_ts = str(event.get("local_receive_wall_ts") or "")
        bid, ask = finite(event.get("best_bid")), finite(event.get("best_ask"))
        if not bid or not ask or ask < bid or mono_ns <= 0:
            return {"opened": [], "closed": [], "status": "NEED_VALID_BITGET_L1", **safety_envelope()}
        closed = self._manage_positions(symbol, mono_ns, wall_ts, bid, ask)
        opened = self._open_pending(symbol, mono_ns, wall_ts, bid, ask, event)
        return {"opened": opened, "closed": closed, "status": "PAPER_FORWARD_SIMULATION", **safety_envelope()}

    def _open_pending(self, symbol: str, mono_ns: int, wall_ts: str, bid: float, ask: float,
                      event: dict[str, Any]) -> list[dict[str, Any]]:
        if len(self.ledger.open_positions()) >= int(self.config.get("paper_max_positions", 1)):
            return []
        opened: list[dict[str, Any]] = []
        required_delay_ns = (
            int(self.config.get("decision_latency_ms", 100))
            + int(self.config.get("simulated_send_latency_ms", 75))
        ) * 1_000_000
        for signal in self.ledger.pending_signals():
            if signal["symbol"] != symbol or mono_ns <= int(signal["decision_monotonic_ns"]) + required_delay_ns:
                continue
            if float(signal.get("unlevered_net_edge_bps") or 0) <= 0:
                self.ledger.update_signal_status(signal["signal_id"], "REJECTED_COSTS", "non_positive_unlevered_net_edge")
                continue
            direction = signal["direction"]
            reference = ask if direction == "LONG" else bid
            decision_price = finite((signal.get("bitget_state_at_decision") or {}).get("price"))
            expected = float(signal.get("expected_remaining_move_bps") or 0)
            consumed = 0.0
            if decision_price:
                consumed = ((reference / decision_price - 1) * 10_000) * (1 if direction == "LONG" else -1)
            if consumed >= expected:
                self.ledger.update_signal_status(signal["signal_id"], "REJECTED_MOVE_CONSUMED", "move_consumed_before_fill")
                continue
            requested_notional = float(self.config.get("paper_notional_usdt", 5.0))
            displayed_size = finite(event.get("ask_size" if direction == "LONG" else "bid_size"))
            if displayed_size is None:
                self.ledger.update_signal_status(signal["signal_id"], "REJECTED_L1_SIZE_MISSING", "observable_l1_size_required")
                continue
            displayed_notional = displayed_size * reference
            fill_fraction = min(1.0, displayed_notional / requested_notional)
            if fill_fraction < float(self.config.get("paper_min_fill_fraction", 0.2)):
                self.ledger.update_signal_status(signal["signal_id"], "REJECTED_PARTIAL_FILL_TOO_SMALL", "insufficient_l1_size")
                continue
            quantity_step = float(self.config.get("paper_quantity_step", 0.000001))
            if quantity_step <= 0 or not math.isfinite(quantity_step):
                raise ValueError("CROSS_VENUE_QUANTITY_STEP_INVALID")
            quantity = math.floor((requested_notional * fill_fraction / reference) / quantity_step) * quantity_step
            notional = quantity * reference
            if notional < float(self.config.get("paper_min_notional_usdt", 1.0)):
                self.ledger.update_signal_status(signal["signal_id"], "REJECTED_MIN_NOTIONAL", "simulated_fill_below_configured_minimum")
                continue
            # Spread is already represented by crossing the observable ask/bid.
            # Slippage is an explicit cash cost and therefore must not also be
            # embedded in the stored fill price.
            fill = reference
            slip_bps = float(self.config.get("adverse_slippage_bps_each_side", 1.5))
            fee = notional * float(self.config.get("round_trip_taker_fee_bps", 12.0)) / 20_000
            impact_each_side = float(self.config.get("market_impact_bps", 0.5)) / 2.0
            slippage = notional * (slip_bps + impact_each_side) / 10_000
            stop_bps = float(self.config.get("paper_stop_bps", 18.0)); tp_bps = float(self.config.get("paper_take_profit_bps", 28.0))
            stop = fill * (1 - stop_bps / 10_000 if direction == "LONG" else 1 + stop_bps / 10_000)
            tp = fill * (1 + tp_bps / 10_000 if direction == "LONG" else 1 - tp_bps / 10_000)
            position_id = "cvp_" + hashlib.sha256(signal["signal_id"].encode("utf-8")).hexdigest()[:24]
            payload = {
                "position_id": position_id, "signal_id": signal["signal_id"], "symbol": symbol,
                "direction": direction, "entry_ts": wall_ts, "entry_monotonic_ns": mono_ns,
                "entry_price": fill, "quantity": quantity, "notional": notional,
                "stop_price": stop, "take_profit_price": tp, "entry_fee": fee,
                "entry_slippage": slippage, "fill_fraction": fill_fraction,
                "simulation_only": True, "source": "BITGET_PUBLIC_L1_AFTER_DECISION",
            }
            if self.ledger.open_simulated_position(payload):
                opened.append(payload)
                break
        return opened

    def _manage_positions(self, symbol: str, mono_ns: int, wall_ts: str, bid: float, ask: float) -> list[dict[str, Any]]:
        closed: list[dict[str, Any]] = []
        for position in self.ledger.open_positions():
            if position["symbol"] != symbol:
                continue
            direction = position["direction"]; executable = bid if direction == "LONG" else ask
            best = max(float(position["best_price"]), executable) if direction == "LONG" else min(float(position["best_price"]), executable)
            worst = min(float(position["worst_price"]), executable) if direction == "LONG" else max(float(position["worst_price"]), executable)
            entry = float(position["entry_price"]); trailing = finite(position.get("trailing_stop"))
            activation = float(self.config.get("paper_trailing_activation_bps", 16.0)); distance = float(self.config.get("paper_trailing_distance_bps", 10.0))
            favorable = ((best / entry - 1) if direction == "LONG" else (entry / best - 1)) * 10_000
            if favorable >= activation:
                candidate = best * (1 - distance / 10_000 if direction == "LONG" else 1 + distance / 10_000)
                trailing = max(trailing or 0, candidate) if direction == "LONG" else min(trailing or float("inf"), candidate)
            self.ledger.mark_position(position["position_id"], executable, best, worst, trailing)
            stop_hit = executable <= float(position["stop_price"]) if direction == "LONG" else executable >= float(position["stop_price"])
            tp_hit = executable >= float(position["take_profit_price"]) if direction == "LONG" else executable <= float(position["take_profit_price"])
            trail_hit = trailing is not None and (executable <= trailing if direction == "LONG" else executable >= trailing)
            holding = max(0.0, (mono_ns - int(position["entry_monotonic_ns"])) / 1_000_000_000)
            reason = "STOP" if stop_hit else "TAKE_PROFIT" if tp_hit else "TRAILING" if trail_hit else "TIME_EXIT" if holding >= float(self.config.get("paper_max_holding_seconds", 30)) else None
            if reason is None:
                continue
            slip_bps = float(self.config.get("adverse_slippage_bps_each_side", 1.5))
            fill = executable
            notional = float(position["quantity"]) * fill
            exit_fee = notional * float(self.config.get("round_trip_taker_fee_bps", 12.0)) / 20_000
            impact_each_side = float(self.config.get("market_impact_bps", 0.5)) / 2.0
            exit_slippage = notional * (slip_bps + impact_each_side) / 10_000
            funding_reserve = float(position["notional"]) * float(self.config.get("funding_cost_reserve_bps", 0.5)) / 10_000
            closed.append(self.ledger.close_simulated_position(position["position_id"], {
                "exit_price": fill, "exit_fee": exit_fee, "exit_slippage": exit_slippage,
                "funding": funding_reserve,
                "funding_status": "CONSERVATIVE_RESERVE_NOT_ACTUAL_PAYMENT",
                "exit_ts": wall_ts, "exit_reason": reason, "holding_seconds": holding,
            }))
        return closed
