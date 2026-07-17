"""Isolated causal broker for ATI paper simulation.

The module deliberately has no import path to the productive paper trader,
execution engine, exchange client, credentials or environment configuration.
It accepts only public market observations supplied by the executor.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any

from . import ACCOUNT_ID, POLICY_VERSION
from .config import AtiPaperConfig, InstrumentRule
from .ledger import AtiPaperLedger, stable_id, utc_now
from .public_market import MarketBar, MarketTick


ENTRY_EVENT = "SIM_MARKET_ENTRY"
EXIT_EVENTS = {"SIM_STOP", "SIM_TAKE_PROFIT", "SIM_TRAILING_STOP", "SIM_TIME_EXIT", "SIM_MANUAL_RESEARCH_CLOSE"}


@dataclass(frozen=True)
class FillDecision:
    exit_event: str | None
    exit_reason: str | None
    reference_price: float | None
    ambiguity_rule: str = "NONE"


def _finite(value: Any, label: str, *, positive: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"ATI_PAPER_INVALID_{label}") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError(f"ATI_PAPER_INVALID_{label}")
    return number


def _parse_utc(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("ATI_PAPER_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_from_ms(value: int) -> str:
    return datetime.fromtimestamp(int(value) / 1000.0, tz=timezone.utc).isoformat()


def _floor_step(value: float, step: float, places: int) -> float:
    if value <= 0 or step <= 0:
        return 0.0
    units = (Decimal(str(value)) / Decimal(str(step))).to_integral_value(rounding=ROUND_DOWN)
    quantized = units * Decimal(str(step))
    quantum = Decimal(1).scaleb(-max(0, int(places)))
    return float(quantized.quantize(quantum, rounding=ROUND_DOWN))


def _side_gross(direction: str, entry: float, exit_price: float, quantity: float) -> float:
    return (exit_price - entry) * quantity if direction == "LONG" else (entry - exit_price) * quantity


def _adverse_fill(direction: str, reference: float, fraction: float, *, entry: bool) -> float:
    buying = (direction == "LONG" and entry) or (direction == "SHORT" and not entry)
    return reference * (1.0 + fraction if buying else 1.0 - fraction)


def decide_bar_exit(position: dict[str, Any], bar: MarketBar) -> FillDecision:
    """Evaluate one closed bar using only levels fixed before that bar.

    The existing stop (including a previously activated trailing stop) is
    checked before any favorable excursion from this bar can move trailing.
    """
    direction = str(position["direction"]).upper()
    stop = _finite(position["stop_price"], "STOP", positive=True)
    trailing = position.get("trailing_stop")
    if trailing is not None:
        trailing = _finite(trailing, "TRAILING", positive=True)
        stop = max(stop, trailing) if direction == "LONG" else min(stop, trailing)
    target = _finite(position["take_profit_price"], "TARGET", positive=True)
    if direction == "LONG":
        if bar.open <= stop:
            return FillDecision("SIM_STOP", "GAP_STOP", bar.open)
        if bar.open >= target:
            return FillDecision("SIM_TAKE_PROFIT", "TP", target)
        hit_stop, hit_target = bar.low <= stop, bar.high >= target
    else:
        if bar.open >= stop:
            return FillDecision("SIM_STOP", "GAP_STOP", bar.open)
        if bar.open <= target:
            return FillDecision("SIM_TAKE_PROFIT", "TP", target)
        hit_stop, hit_target = bar.high >= stop, bar.low <= target
    if hit_stop and hit_target:
        return FillDecision("SIM_STOP", "STOP_BEFORE_TP", stop, "STOP_BEFORE_TP")
    if hit_stop:
        event = "SIM_TRAILING_STOP" if trailing is not None and abs(stop - trailing) <= 1e-12 else "SIM_STOP"
        return FillDecision(event, "TRAIL" if event == "SIM_TRAILING_STOP" else "SL", stop)
    if hit_target:
        return FillDecision("SIM_TAKE_PROFIT", "TP", target)
    return FillDecision(None, None, None)


class AtiPaperBroker:
    """Ledger-backed broker that can only create simulated fills."""

    def __init__(self, ledger: AtiPaperLedger, config: AtiPaperConfig, *, commit_hash: str = "unknown"):
        self.ledger = ledger
        self.config = config
        self.commit_hash = str(commit_hash or "unknown")

    @staticmethod
    def _update_account(
        conn: sqlite3.Connection, *, cash: float, realized: float, unrealized: float,
        fees_delta: float = 0.0, slippage_delta: float = 0.0,
        funding_delta: float = 0.0, realized_pnl_delta: float = 0.0,
    ) -> None:
        row = conn.execute("SELECT * FROM account WHERE account_id=?", (ACCOUNT_ID,)).fetchone()
        if row is None:
            raise ValueError("ATI_PAPER_ACCOUNT_MISSING")
        total = realized + unrealized
        peak = max(float(row["equity_peak"]), total)
        drawdown_abs = max(0.0, peak - total)
        drawdown_pct = drawdown_abs / peak if peak > 0 else 0.0
        max_drawdown = max(float(row["max_drawdown_pct"]), drawdown_pct)
        values = (cash, realized, unrealized, total, peak, drawdown_abs, drawdown_pct,
                  max_drawdown, float(row["realized_pnl_total"]) + realized_pnl_delta,
                  float(row["fees_total"]) + fees_delta,
                  float(row["slippage_total"]) + slippage_delta,
                  float(row["funding_total"]) + funding_delta, utc_now(), ACCOUNT_ID)
        if not all(math.isfinite(float(value)) for value in values[:-2]):
            raise ValueError("ATI_PAPER_ACCOUNT_NON_FINITE")
        conn.execute(
            """UPDATE account SET cash_balance=?,realized_equity=?,unrealized_pnl=?,
               total_equity=?,equity_peak=?,drawdown_abs=?,drawdown_pct=?,
               max_drawdown_pct=?,realized_pnl_total=?,fees_total=?,
               slippage_total=?,funding_total=?,updated_at=? WHERE account_id=?""",
            values,
        )

    def open_from_signal(self, signal_id: str, tick: MarketTick, rule: InstrumentRule) -> dict[str, Any]:
        now = utc_now()
        reference = _finite(tick.price, "ENTRY_PRICE", positive=True)
        source_ts = _iso_from_ms(tick.source_ts_ms)
        with self.ledger.transaction() as conn:
            signal = conn.execute("SELECT * FROM signals WHERE signal_id=?", (signal_id,)).fetchone()
            if signal is None:
                raise ValueError("ATI_PAPER_SIGNAL_MISSING")
            if signal["status"] != "ATI_SIGNAL_OBSERVED":
                return {"status": str(signal["status"]), "signal_id": signal_id, "idempotent": True}
            observed_at = _parse_utc(signal["observed_at"])
            tick_observed = _parse_utc(tick.observed_at)
            if tick_observed <= observed_at:
                raise ValueError("ATI_PAPER_ENTRY_NOT_AFTER_OBSERVATION")
            source_age = (datetime.now(timezone.utc) - datetime.fromtimestamp(tick.source_ts_ms / 1000.0, tz=timezone.utc)).total_seconds()
            if source_age < -5 or source_age > self.config.market_data_stale_after_seconds:
                raise ValueError("ATI_PAPER_MARKET_DATA_STALE")
            direction = str(signal["direction"]).upper()
            invalidation = _finite(signal["invalidation"], "INVALIDATION", positive=True)
            if direction == "LONG":
                if reference <= invalidation:
                    raise ValueError("ATI_PAPER_GAP_INVALIDATED")
                risk_distance = reference - invalidation
                target = reference + self.config.target_r_multiple * risk_distance
            elif direction == "SHORT":
                if reference >= invalidation:
                    raise ValueError("ATI_PAPER_GAP_INVALIDATED")
                risk_distance = invalidation - reference
                target = reference - self.config.target_r_multiple * risk_distance
            else:
                raise ValueError("ATI_PAPER_DIRECTION_INVALID")
            risk_fraction = risk_distance / reference
            if risk_fraction < 0.0002 or risk_fraction > 0.05:
                raise ValueError("ATI_PAPER_STRUCTURAL_RISK_INVALID")
            account = conn.execute("SELECT * FROM account WHERE account_id=?", (ACCOUNT_ID,)).fetchone()
            if account is None:
                raise ValueError("ATI_PAPER_ACCOUNT_MISSING")
            equity_before = _finite(account["realized_equity"], "REALIZED_EQUITY", positive=True)
            cash = max(0.0, _finite(account["cash_balance"], "CASH"))
            requested_notional = equity_before * self.config.position_fraction
            reserve_fraction = self.config.entry_fee_fraction + self.config.adverse_slippage_fraction
            affordable_notional = cash / (1.0 + reserve_fraction)
            reference_notional = min(requested_notional, affordable_notional)
            quantity = _floor_step(reference_notional / reference, rule.quantity_step, rule.volume_place)
            if quantity < rule.min_trade_num:
                raise ValueError("ATI_PAPER_MINIMUM_QUANTITY")
            reference_notional = quantity * reference
            fill = _adverse_fill(direction, reference, self.config.adverse_slippage_fraction, entry=True)
            fill_notional = quantity * fill
            if fill_notional < rule.min_trade_usdt:
                raise ValueError("ATI_PAPER_MINIMUM_NOTIONAL")
            slippage = abs(fill - reference) * quantity
            fee = fill_notional * self.config.entry_fee_fraction
            entry_cost = fee + slippage
            if reference_notional + entry_cost > cash + 1e-9:
                raise ValueError("ATI_PAPER_INSUFFICIENT_SIMULATED_CASH")
            effective_fraction = reference_notional / equity_before
            previous = conn.execute("SELECT notional FROM trades ORDER BY exit_ts DESC LIMIT 1").fetchone()
            previous_notional = float(previous["notional"]) if previous else None
            size_change_pct = (
                (reference_notional / previous_notional - 1.0) * 100.0
                if previous_notional and previous_notional > 0 else None
            )
            position_id = stable_id("pos", signal_id)
            order_id = stable_id("ord", signal_id, ENTRY_EVENT)
            realized = float(account["realized_equity"]) - entry_cost
            new_cash = float(account["cash_balance"]) - reference_notional - entry_cost
            other_unrealized = conn.execute(
                "SELECT COALESCE(SUM(unrealized_pnl),0) FROM positions WHERE status='OPEN'"
            ).fetchone()[0]
            self._update_account(
                conn, cash=new_cash, realized=realized, unrealized=float(other_unrealized or 0.0),
                fees_delta=fee, slippage_delta=slippage,
            )
            conn.execute(
                """INSERT INTO positions
                   (position_id,signal_id,setup_id,symbol,direction,entry_ts,entry_source_ts,
                    entry_reference_price,entry_price,quantity,notional,reserved_notional,
                    stop_price,take_profit_price,trailing_stop,risk_distance,risk_money,
                    configured_sizing_fraction,effective_sizing_fraction,equity_before,
                    entry_fee,entry_slippage,unrealized_pnl,mfe,mae,last_price,last_market_ts,
                    last_processed_bar_ms,status,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (position_id, signal_id, signal["setup_id"], signal["symbol"], direction,
                 now, source_ts, reference, fill, quantity, reference_notional,
                 reference_notional, invalidation, target, None, risk_distance,
                 quantity * risk_distance, self.config.position_fraction,
                 effective_fraction, equity_before, fee, slippage, 0.0, 0.0, 0.0,
                 reference, source_ts, None, "OPEN", now, now),
            )
            conn.execute(
                """INSERT INTO simulated_orders
                   (sim_order_id,signal_id,position_id,order_type,symbol,side,
                    requested_price,filled_price,quantity,notional,fee,slippage,
                    source_ts,created_at,filled_at,status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (order_id, signal_id, position_id, ENTRY_EVENT, signal["symbol"], direction,
                 reference, fill, quantity, reference_notional, fee, slippage,
                 source_ts, now, now, "FILLED_SIMULATION"),
            )
            conn.execute(
                """UPDATE signals SET status='ATI_PAPER_POSITION_OPEN',accepted_at=?,updated_at=?
                   WHERE signal_id=?""",
                (now, now, signal_id),
            )
            common = dict(correlation_id=signal_id, signal_id=signal_id,
                          position_id=position_id, source_ts=source_ts,
                          commit_hash=self.commit_hash)
            self.ledger._event(
                conn, event_type="ATI_SIGNAL_ACCEPTED_FOR_PAPER",
                previous_state="ATI_SIGNAL_OBSERVED", new_state="ATI_SIGNAL_ACCEPTED_FOR_PAPER",
                reason="FIRST_PUBLIC_TICK_AFTER_LIVE_OBSERVATION", payload={
                    "entry_event": ENTRY_EVENT, "simulation_only": True,
                    "configured_sizing_fraction": self.config.position_fraction,
                    "effective_sizing_fraction": effective_fraction,
                }, **common,
            )
            self.ledger._event(
                conn, event_type="SIMULATED_ORDER_FILLED", previous_state="CREATED",
                new_state="FILLED_SIMULATION", reason=ENTRY_EVENT,
                payload={"order_id": order_id, "fee": fee, "slippage": slippage}, **common,
            )
            self.ledger._event(
                conn, event_type="ATI_PAPER_POSITION_OPEN", previous_state="ATI_SIGNAL_ACCEPTED_FOR_PAPER",
                new_state="ATI_PAPER_POSITION_OPEN", reason=ENTRY_EVENT,
                payload={
                    "equity_before": equity_before, "notional": reference_notional,
                    "quantity": quantity, "risk_money": quantity * risk_distance,
                    "stop_distance": risk_distance, "previous_closed_notional": previous_notional,
                    "size_change_pct": size_change_pct, "rule_source": rule.source,
                }, **common,
            )
            self.ledger._append_equity(conn, reason="ATI_PAPER_POSITION_OPEN")
        return {"status": "ATI_PAPER_POSITION_OPEN", "position_id": position_id,
                "signal_id": signal_id, "notional": reference_notional,
                "quantity": quantity, "equity_before": equity_before,
                "configured_sizing_fraction": self.config.position_fraction,
                "effective_sizing_fraction": effective_fraction,
                "size_change_pct": size_change_pct}

    def _close_in_transaction(
        self, conn: sqlite3.Connection, position: sqlite3.Row, *, event_type: str,
        exit_reason: str, reference_price: float, source_ts: str,
        ambiguity_rule: str = "NONE", funding: float = 0.0,
        funding_status: str = "UNKNOWN",
    ) -> dict[str, Any]:
        if event_type not in EXIT_EVENTS:
            raise ValueError("ATI_PAPER_EXIT_EVENT_BLOCKED")
        reference = _finite(reference_price, "EXIT_PRICE", positive=True)
        funding = _finite(funding, "FUNDING")
        direction = str(position["direction"])
        quantity = float(position["quantity"])
        fill = _adverse_fill(direction, reference, self.config.adverse_slippage_fraction, entry=False)
        slippage = abs(fill - reference) * quantity
        fee = fill * quantity * self.config.exit_fee_fraction
        gross = _side_gross(direction, float(position["entry_reference_price"]), reference, quantity)
        known_mfe = max(float(position["mfe"]), max(0.0, gross))
        known_mae = max(float(position["mae"]), max(0.0, -gross))
        total_fees = float(position["entry_fee"]) + fee
        total_slippage = float(position["entry_slippage"]) + slippage
        net = gross - total_fees - total_slippage - funding
        account = conn.execute("SELECT * FROM account WHERE account_id=?", (ACCOUNT_ID,)).fetchone()
        if account is None:
            raise ValueError("ATI_PAPER_ACCOUNT_MISSING")
        realized = float(account["realized_equity"]) + gross - fee - slippage - funding
        cash = float(account["cash_balance"]) + float(position["reserved_notional"]) + gross - fee - slippage - funding
        other_unrealized = conn.execute(
            "SELECT COALESCE(SUM(unrealized_pnl),0) FROM positions WHERE status='OPEN' AND position_id<>?",
            (position["position_id"],),
        ).fetchone()[0]
        self._update_account(
            conn, cash=cash, realized=realized, unrealized=float(other_unrealized or 0.0),
            fees_delta=fee, slippage_delta=slippage, funding_delta=funding,
            realized_pnl_delta=net,
        )
        now = utc_now()
        holding_seconds = max(0.0, (_parse_utc(source_ts) - _parse_utc(position["entry_source_ts"])).total_seconds())
        equity_after = realized + float(other_unrealized or 0.0)
        trade_id = stable_id("trd", position["position_id"])
        order_id = stable_id("ord", position["signal_id"], event_type, source_ts)
        conn.execute(
            """INSERT INTO trades
               (trade_id,position_id,signal_id,setup_id,symbol,direction,entry_ts,exit_ts,
                entry_reference_price,entry_price,exit_reference_price,exit_price,quantity,
                notional,stop_price,take_profit_price,gross_pnl,fees,slippage,funding,
                funding_status,net_pnl,return_pct,equity_before,equity_after,exit_reason,
                holding_seconds,mfe,mae,policy_version,sizing_policy,sizing_fraction,
                configured_sizing_fraction,source_ts,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (trade_id, position["position_id"], position["signal_id"], position["setup_id"],
             position["symbol"], direction, position["entry_ts"], now,
             position["entry_reference_price"], position["entry_price"], reference, fill,
             quantity, position["notional"], position["stop_price"],
             position["take_profit_price"], gross, total_fees, total_slippage, funding,
             funding_status, net, net / float(position["notional"]) * 100.0,
             position["equity_before"], equity_after, exit_reason, holding_seconds,
             known_mfe, known_mae, POLICY_VERSION,
             self.config.sizing_method, position["effective_sizing_fraction"],
             position["configured_sizing_fraction"], source_ts, now),
        )
        conn.execute(
            """INSERT INTO simulated_orders
               (sim_order_id,signal_id,position_id,order_type,symbol,side,requested_price,
                filled_price,quantity,notional,fee,slippage,source_ts,created_at,filled_at,status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (order_id, position["signal_id"], position["position_id"], event_type,
             position["symbol"], direction, reference, fill, quantity,
             reference * quantity, fee, slippage, source_ts, now, now, "FILLED_SIMULATION"),
        )
        conn.execute(
            """UPDATE positions SET status='CLOSED',unrealized_pnl=0,last_price=?,
               last_market_ts=?,updated_at=? WHERE position_id=?""",
            (reference, source_ts, now, position["position_id"]),
        )
        conn.execute(
            "UPDATE signals SET status='ATI_PAPER_POSITION_CLOSED',updated_at=? WHERE signal_id=?",
            (now, position["signal_id"]),
        )
        common = dict(correlation_id=position["signal_id"], signal_id=position["signal_id"],
                      position_id=position["position_id"], source_ts=source_ts,
                      commit_hash=self.commit_hash)
        self.ledger._event(
            conn, event_type="SIMULATED_ORDER_FILLED", previous_state="CREATED",
            new_state="FILLED_SIMULATION", reason=event_type,
            payload={"order_id": order_id, "fee": fee, "slippage": slippage,
                     "funding": funding, "funding_status": funding_status}, **common,
        )
        self.ledger._event(
            conn, event_type="ATI_PAPER_POSITION_CLOSED",
            previous_state="ATI_PAPER_POSITION_OPEN", new_state="ATI_PAPER_POSITION_CLOSED",
            reason=exit_reason, payload={
                "exit_event": event_type, "gross_pnl": gross, "net_pnl": net,
                "fees": total_fees, "slippage": total_slippage, "funding": funding,
                "ambiguity_rule": ambiguity_rule, "equity_after": equity_after,
            }, **common,
        )
        self.ledger._append_equity(conn, reason="ATI_PAPER_POSITION_CLOSED")
        return {"status": "ATI_PAPER_POSITION_CLOSED", "trade_id": trade_id,
                "position_id": position["position_id"], "exit_reason": exit_reason,
                "gross_pnl": gross, "net_pnl": net, "equity_after": equity_after,
                "ambiguity_rule": ambiguity_rule}

    def close_position(
        self, position_id: str, *, event_type: str, exit_reason: str,
        reference_price: float, source_ts: str, ambiguity_rule: str = "NONE",
        funding: float = 0.0, funding_status: str = "UNKNOWN",
    ) -> dict[str, Any]:
        with self.ledger.transaction() as conn:
            position = conn.execute("SELECT * FROM positions WHERE position_id=?", (position_id,)).fetchone()
            if position is None:
                raise ValueError("ATI_PAPER_POSITION_MISSING")
            if position["status"] != "OPEN":
                trade = conn.execute("SELECT * FROM trades WHERE position_id=?", (position_id,)).fetchone()
                return {"status": "ATI_PAPER_POSITION_CLOSED", "idempotent": True,
                        "trade_id": trade["trade_id"] if trade else None}
            return self._close_in_transaction(
                conn, position, event_type=event_type, exit_reason=exit_reason,
                reference_price=reference_price, source_ts=source_ts,
                ambiguity_rule=ambiguity_rule, funding=funding, funding_status=funding_status,
            )

    def process_closed_bar(self, position_id: str, bar: MarketBar) -> dict[str, Any]:
        with self.ledger.transaction() as conn:
            position = conn.execute("SELECT * FROM positions WHERE position_id=?", (position_id,)).fetchone()
            if position is None or position["status"] != "OPEN":
                return {"status": "NO_OPEN_POSITION", "idempotent": True}
            last_ms = position["last_processed_bar_ms"]
            if last_ms is not None and int(bar.timestamp_ms) <= int(last_ms):
                return {"status": "BAR_ALREADY_PROCESSED", "idempotent": True}
            entry_ms = int(_parse_utc(position["entry_source_ts"]).timestamp() * 1000)
            first_full_bar_ms = ((entry_ms + 59_999) // 60_000) * 60_000
            if int(bar.timestamp_ms) < first_full_bar_ms:
                conn.execute(
                    "UPDATE positions SET last_processed_bar_ms=?,updated_at=? WHERE position_id=?",
                    (int(bar.timestamp_ms), utc_now(), position_id),
                )
                return {"status": "ENTRY_PARTIAL_BAR_SKIPPED"}
            decision = decide_bar_exit(dict(position), bar)
            source_ts = _iso_from_ms(bar.available_at_ms)
            if decision.exit_event:
                return self._close_in_transaction(
                    conn, position, event_type=decision.exit_event,
                    exit_reason=str(decision.exit_reason),
                    reference_price=float(decision.reference_price), source_ts=source_ts,
                    ambiguity_rule=decision.ambiguity_rule,
                )
            direction = str(position["direction"])
            quantity = float(position["quantity"])
            favorable = (
                max(0.0, (bar.high - float(position["entry_reference_price"])) * quantity)
                if direction == "LONG" else
                max(0.0, (float(position["entry_reference_price"]) - bar.low) * quantity)
            )
            adverse = (
                max(0.0, (float(position["entry_reference_price"]) - bar.low) * quantity)
                if direction == "LONG" else
                max(0.0, (bar.high - float(position["entry_reference_price"])) * quantity)
            )
            mfe = max(float(position["mfe"]), favorable)
            mae = max(float(position["mae"]), adverse)
            unrealized = _side_gross(direction, float(position["entry_reference_price"]), bar.close, quantity)
            trailing = position["trailing_stop"]
            trail_activated = False
            if self.config.trailing_enabled:
                favorable_r = mfe / max(float(position["risk_money"]), 1e-12)
                if favorable_r >= self.config.trailing_activation_r:
                    candidate = (
                        bar.high - self.config.trailing_distance_r * float(position["risk_distance"])
                        if direction == "LONG" else
                        bar.low + self.config.trailing_distance_r * float(position["risk_distance"])
                    )
                    if direction == "LONG":
                        new_trailing = max(float(trailing), candidate) if trailing is not None else candidate
                    else:
                        new_trailing = min(float(trailing), candidate) if trailing is not None else candidate
                    if trailing is None or abs(float(new_trailing) - float(trailing)) > 1e-12:
                        trailing, trail_activated = new_trailing, True
            other_unrealized = conn.execute(
                "SELECT COALESCE(SUM(unrealized_pnl),0) FROM positions WHERE status='OPEN' AND position_id<>?",
                (position_id,),
            ).fetchone()[0]
            account = conn.execute("SELECT * FROM account WHERE account_id=?", (ACCOUNT_ID,)).fetchone()
            self._update_account(
                conn, cash=float(account["cash_balance"]), realized=float(account["realized_equity"]),
                unrealized=float(other_unrealized or 0.0) + unrealized,
            )
            conn.execute(
                """UPDATE positions SET trailing_stop=?,unrealized_pnl=?,mfe=?,mae=?,
                   last_price=?,last_market_ts=?,last_processed_bar_ms=?,updated_at=?
                   WHERE position_id=?""",
                (trailing, unrealized, mfe, mae, bar.close, source_ts,
                 int(bar.timestamp_ms), utc_now(), position_id),
            )
            if trail_activated:
                self.ledger._event(
                    conn, event_type="TRAILING_STOP_UPDATED", correlation_id=position["signal_id"],
                    signal_id=position["signal_id"], position_id=position_id,
                    previous_state=str(position["trailing_stop"]), new_state=str(trailing),
                    reason="PRIOR_CLOSED_BAR_FAVORABLE_EXCURSION", source_ts=source_ts,
                    commit_hash=self.commit_hash,
                    payload={"applies_from_next_bar": True, "mfe": mfe},
                )
            elapsed = (_parse_utc(source_ts) - _parse_utc(position["entry_source_ts"])).total_seconds()
            if elapsed >= self.config.max_holding_minutes * 60:
                refreshed = conn.execute("SELECT * FROM positions WHERE position_id=?", (position_id,)).fetchone()
                return self._close_in_transaction(
                    conn, refreshed, event_type="SIM_TIME_EXIT", exit_reason="TIME",
                    reference_price=bar.close, source_ts=source_ts,
                )
            self.ledger._append_equity(conn, reason="MARK_TO_MARKET_CLOSED_BAR")
            return {"status": "OPEN", "position_id": position_id,
                    "unrealized_pnl": unrealized, "mfe": mfe, "mae": mae,
                    "trailing_stop": trailing, "bar_timestamp_ms": bar.timestamp_ms}

    def mark_tick(self, position_id: str, tick: MarketTick) -> dict[str, Any]:
        reference = _finite(tick.price, "MARK_PRICE", positive=True)
        source_ts = _iso_from_ms(tick.source_ts_ms)
        with self.ledger.transaction() as conn:
            position = conn.execute("SELECT * FROM positions WHERE position_id=?", (position_id,)).fetchone()
            if position is None or position["status"] != "OPEN":
                return {"status": "NO_OPEN_POSITION"}
            unrealized = _side_gross(
                str(position["direction"]), float(position["entry_reference_price"]),
                reference, float(position["quantity"]),
            )
            other = conn.execute(
                "SELECT COALESCE(SUM(unrealized_pnl),0) FROM positions WHERE status='OPEN' AND position_id<>?",
                (position_id,),
            ).fetchone()[0]
            account = conn.execute("SELECT * FROM account WHERE account_id=?", (ACCOUNT_ID,)).fetchone()
            self._update_account(
                conn, cash=float(account["cash_balance"]), realized=float(account["realized_equity"]),
                unrealized=float(other or 0.0) + unrealized,
            )
            conn.execute(
                "UPDATE positions SET unrealized_pnl=?,last_price=?,last_market_ts=?,updated_at=? WHERE position_id=?",
                (unrealized, reference, source_ts, utc_now(), position_id),
            )
            return {"status": "OPEN", "unrealized_pnl": unrealized, "last_price": reference}
