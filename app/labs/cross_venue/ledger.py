"""Transactional ledger for the isolated CROSS_VENUE_PAPER_50 account."""

from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import ACCOUNT_ID, LEDGER_PATH, POLICY_VERSION, safety_envelope

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
 account_id TEXT PRIMARY KEY, initial_balance REAL NOT NULL, cash REAL NOT NULL,
 realized_pnl REAL NOT NULL, unrealized_pnl REAL NOT NULL, total_equity REAL NOT NULL,
 equity_peak REAL NOT NULL, max_drawdown_pct REAL NOT NULL, fees REAL NOT NULL,
 slippage REAL NOT NULL, funding REAL NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
 signal_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, direction TEXT NOT NULL,
 decision_ts TEXT NOT NULL, decision_monotonic_ns INTEGER NOT NULL, status TEXT NOT NULL,
 rejection_reason TEXT, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
 position_id TEXT PRIMARY KEY, signal_id TEXT NOT NULL UNIQUE, symbol TEXT NOT NULL,
 direction TEXT NOT NULL, entry_ts TEXT NOT NULL, entry_monotonic_ns INTEGER NOT NULL,
 entry_price REAL NOT NULL, quantity REAL NOT NULL, notional REAL NOT NULL,
 stop_price REAL NOT NULL, take_profit_price REAL NOT NULL, trailing_stop REAL,
 best_price REAL NOT NULL, worst_price REAL NOT NULL, entry_fee REAL NOT NULL,
 entry_slippage REAL NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
 trade_id TEXT PRIMARY KEY, position_id TEXT NOT NULL UNIQUE, signal_id TEXT NOT NULL UNIQUE,
 symbol TEXT NOT NULL, direction TEXT NOT NULL, entry_ts TEXT NOT NULL, exit_ts TEXT NOT NULL,
 entry_price REAL NOT NULL, exit_price REAL NOT NULL, quantity REAL NOT NULL, notional REAL NOT NULL,
 gross_pnl REAL NOT NULL, fees REAL NOT NULL, slippage REAL NOT NULL, funding REAL NOT NULL,
 funding_status TEXT NOT NULL, net_pnl REAL NOT NULL, gross_return_bps REAL NOT NULL,
 total_cost_bps REAL NOT NULL, net_return_bps REAL NOT NULL, mae_bps REAL NOT NULL,
 mfe_bps REAL NOT NULL, exit_reason TEXT NOT NULL, holding_seconds REAL NOT NULL,
 policy_version TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equity (
 id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, cash REAL NOT NULL,
 unrealized_pnl REAL NOT NULL, total_equity REAL NOT NULL, drawdown_pct REAL NOT NULL,
 reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, event_type TEXT NOT NULL,
 correlation_id TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leverage_results (
 trade_id TEXT NOT NULL, leverage INTEGER NOT NULL, pnl REAL NOT NULL, equity_after REAL NOT NULL,
 liquidated INTEGER NOT NULL, liquidation_distance_bps REAL NOT NULL, payload_json TEXT NOT NULL,
 PRIMARY KEY(trade_id, leverage)
);
CREATE INDEX IF NOT EXISTS cv_positions_status ON positions(status, symbol);
CREATE INDEX IF NOT EXISTS cv_trades_exit ON trades(exit_ts);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def checked(value: Any, label: str, *, positive: bool = False, nonnegative: bool = False) -> float:
    try: number = float(value)
    except (TypeError, ValueError) as exc: raise ValueError(f"CROSS_VENUE_NON_FINITE:{label}") from exc
    if not math.isfinite(number) or (positive and number <= 0) or (nonnegative and number < 0):
        raise ValueError(f"CROSS_VENUE_NON_FINITE:{label}")
    return number


class CrossVenueLedger:
    TABLES = {"signals", "positions", "trades", "equity", "events", "leverage_results"}

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else LEDGER_PATH

    def _connect(self, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            if not self.path.is_file(): raise FileNotFoundError(self.path)
            conn = sqlite3.connect(f"file:{self.path.resolve().as_posix()}?mode=ro", uri=True, timeout=5)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row; conn.execute("PRAGMA foreign_keys=ON"); conn.execute("PRAGMA busy_timeout=5000")
        if not read_only:
            conn.execute("PRAGMA journal_mode=WAL"); conn.execute("PRAGMA synchronous=FULL")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE"); yield conn; conn.commit()
        except Exception:
            conn.rollback(); raise
        finally: conn.close()

    def initialize(self, initial_balance: float = 50.0) -> dict[str, Any]:
        balance = checked(initial_balance, "initial_balance", positive=True)
        conn = self._connect(); conn.executescript(SCHEMA); conn.commit(); conn.close()
        now = utc_now(); created = False
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM account WHERE account_id=?", (ACCOUNT_ID,)).fetchone()
            if row is None:
                created = True
                conn.execute("INSERT INTO account VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (ACCOUNT_ID, balance, balance, 0.0, 0.0, balance, balance, 0.0, 0.0, 0.0, 0.0, now, now))
                self._event(conn, "ACCOUNT_CREATED", ACCOUNT_ID, {"initial_balance": balance, "credit_once": True})
                self._equity(conn, "ACCOUNT_CREATED")
            elif abs(float(row["initial_balance"]) - balance) > 1e-9:
                raise ValueError("CROSS_VENUE_ACCOUNT_CONTRACT_MISMATCH")
            else:
                self._event(conn, "ACCOUNT_RESUMED", ACCOUNT_ID, {"recredited": False})
        return {"created": created, "account": self.account(), **safety_envelope()}

    def _event(self, conn: sqlite3.Connection, kind: str, correlation: str, payload: dict[str, Any]) -> None:
        conn.execute("INSERT INTO events(timestamp,event_type,correlation_id,payload_json) VALUES (?,?,?,?)",
                     (utc_now(), kind, correlation, json.dumps(payload, sort_keys=True, default=str)))

    def _equity(self, conn: sqlite3.Connection, reason: str) -> None:
        row = conn.execute("SELECT * FROM account WHERE account_id=?", (ACCOUNT_ID,)).fetchone()
        conn.execute("INSERT INTO equity(timestamp,cash,unrealized_pnl,total_equity,drawdown_pct,reason) VALUES (?,?,?,?,?,?)",
                     (utc_now(), row["cash"], row["unrealized_pnl"], row["total_equity"],
                      max(0.0, 1.0 - row["total_equity"] / max(row["equity_peak"], 1e-12)), reason))

    def account(self) -> dict[str, Any] | None:
        try: conn = self._connect(True)
        except FileNotFoundError: return None
        try:
            row = conn.execute("SELECT * FROM account WHERE account_id=?", (ACCOUNT_ID,)).fetchone()
            return dict(row) if row else None
        finally: conn.close()

    def rows(self, table: str, limit: int = 500) -> list[dict[str, Any]]:
        if table not in self.TABLES: raise ValueError("CROSS_VENUE_TABLE_NOT_ALLOWED")
        try: conn = self._connect(True)
        except FileNotFoundError: return []
        try:
            key = "id" if table in {"equity", "events"} else "rowid"
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY {key} DESC LIMIT ?", (max(1, min(int(limit), 5000)),)).fetchall()
            return [dict(row) for row in rows]
        finally: conn.close()

    def record_signal(self, signal: dict[str, Any]) -> bool:
        with self.transaction() as conn:
            exists = conn.execute("SELECT 1 FROM signals WHERE signal_id=?", (signal["signal_id"],)).fetchone()
            if exists: return False
            conn.execute("INSERT INTO signals VALUES (?,?,?,?,?,?,?,?,?)", (
                signal["signal_id"], signal["symbol"], signal["direction"], signal["decision_ts"],
                int(signal["decision_monotonic_ns"]), signal["status"], signal.get("rejection_reason"),
                json.dumps(signal, sort_keys=True, default=str), utc_now(),
            ))
            self._event(conn, "SIGNAL_RECORDED", signal["signal_id"], {"status": signal["status"]})
        return True

    def pending_signals(self) -> list[dict[str, Any]]:
        try: conn = self._connect(True)
        except FileNotFoundError: return []
        try:
            rows = conn.execute("SELECT payload_json FROM signals WHERE status='CANDIDATE_RESEARCH_ONLY' ORDER BY decision_monotonic_ns").fetchall()
            return [json.loads(row["payload_json"]) for row in rows]
        finally: conn.close()

    def open_positions(self) -> list[dict[str, Any]]:
        try: conn = self._connect(True)
        except FileNotFoundError: return []
        try: return [dict(row) for row in conn.execute("SELECT * FROM positions WHERE status='OPEN' ORDER BY entry_ts").fetchall()]
        finally: conn.close()

    def update_signal_status(self, signal_id: str, status: str, reason: str | None = None) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE signals SET status=?, rejection_reason=COALESCE(?,rejection_reason) WHERE signal_id=?",
                         (status, reason, signal_id))
            self._event(conn, "SIGNAL_STATUS", signal_id, {"status": status, "reason": reason})

    def open_simulated_position(self, payload: dict[str, Any]) -> bool:
        fields = ("entry_price", "quantity", "notional", "stop_price", "take_profit_price", "entry_fee", "entry_slippage")
        values = {
            field: checked(
                payload[field], field,
                positive=field not in {"entry_fee", "entry_slippage"},
                nonnegative=field in {"entry_fee", "entry_slippage"},
            )
            for field in fields
        }
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM positions WHERE signal_id=?", (payload["signal_id"],)).fetchone(): return False
            conn.execute("""INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                payload["position_id"], payload["signal_id"], payload["symbol"], payload["direction"], payload["entry_ts"],
                int(payload["entry_monotonic_ns"]), values["entry_price"], values["quantity"], values["notional"],
                values["stop_price"], values["take_profit_price"], None, values["entry_price"], values["entry_price"],
                values["entry_fee"], values["entry_slippage"], "OPEN", utc_now(),
            ))
            conn.execute("UPDATE signals SET status='SIMULATED_POSITION_OPEN' WHERE signal_id=?", (payload["signal_id"],))
            self._event(conn, "SIMULATED_POSITION_OPENED", payload["position_id"], payload)
        return True

    def mark_position(self, position_id: str, price: float, best: float, worst: float, trailing: float | None) -> None:
        price = checked(price, "mark_price", positive=True)
        with self.transaction() as conn:
            conn.execute("UPDATE positions SET best_price=?,worst_price=?,trailing_stop=?,updated_at=? WHERE position_id=? AND status='OPEN'",
                         (checked(best, "best", positive=True), checked(worst, "worst", positive=True), trailing, utc_now(), position_id))
            positions = conn.execute("SELECT * FROM positions WHERE status='OPEN'").fetchall()
            unrealized = sum(((price - p["entry_price"]) if p["direction"] == "LONG" else (p["entry_price"] - price)) * p["quantity"] for p in positions)
            account = conn.execute("SELECT * FROM account WHERE account_id=?", (ACCOUNT_ID,)).fetchone()
            total = account["cash"] + unrealized; peak = max(account["equity_peak"], total)
            dd = max(0.0, 1.0 - total / max(peak, 1e-12))
            conn.execute("UPDATE account SET unrealized_pnl=?,total_equity=?,equity_peak=?,max_drawdown_pct=?,updated_at=? WHERE account_id=?",
                         (unrealized, total, peak, max(account["max_drawdown_pct"], dd), utc_now(), ACCOUNT_ID))

    def close_simulated_position(self, position_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.transaction() as conn:
            position = conn.execute("SELECT * FROM positions WHERE position_id=? AND status='OPEN'", (position_id,)).fetchone()
            if position is None: raise ValueError("CROSS_VENUE_POSITION_NOT_OPEN")
            exit_price = checked(payload["exit_price"], "exit_price", positive=True)
            direction = position["direction"]; quantity = float(position["quantity"])
            gross = ((exit_price - position["entry_price"]) if direction == "LONG" else (position["entry_price"] - exit_price)) * quantity
            exit_fee = checked(payload["exit_fee"], "exit_fee", nonnegative=True)
            exit_slippage = checked(payload["exit_slippage"], "exit_slippage", nonnegative=True)
            fees = float(position["entry_fee"]) + exit_fee; slippage = float(position["entry_slippage"]) + exit_slippage
            funding = checked(payload.get("funding", 0), "funding"); net = gross - fees - slippage - funding
            notional = float(position["notional"]); gross_bps = gross / notional * 10_000; cost_bps = (fees + slippage + funding) / notional * 10_000
            best = float(position["best_price"]); worst = float(position["worst_price"]); entry = float(position["entry_price"])
            mfe = ((best / entry - 1) if direction == "LONG" else (entry / best - 1)) * 10_000
            mae = ((entry / worst - 1) if direction == "LONG" else (worst / entry - 1)) * 10_000
            trade = {
                "trade_id": f"cvt_{position_id}", "position_id": position_id, "signal_id": position["signal_id"],
                "symbol": position["symbol"], "direction": direction, "entry_ts": position["entry_ts"],
                "exit_ts": payload["exit_ts"], "entry_price": entry, "exit_price": exit_price,
                "quantity": quantity, "notional": notional, "gross_pnl": gross, "fees": fees,
                "slippage": slippage, "funding": funding, "funding_status": payload.get("funding_status", "UNKNOWN"),
                "net_pnl": net, "gross_return_bps": gross_bps, "total_cost_bps": cost_bps,
                "net_return_bps": gross_bps - cost_bps, "mae_bps": max(0.0, mae), "mfe_bps": max(0.0, mfe),
                "exit_reason": payload["exit_reason"], "holding_seconds": float(payload["holding_seconds"]),
                "policy_version": POLICY_VERSION, "created_at": utc_now(),
            }
            columns = tuple(trade)
            if len(columns) != 26:
                raise ValueError("CROSS_VENUE_TRADE_SCHEMA_MISMATCH")
            conn.execute(
                f"INSERT INTO trades ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                tuple(trade[column] for column in columns),
            )
            conn.execute("UPDATE positions SET status='CLOSED',updated_at=? WHERE position_id=?", (utc_now(), position_id))
            conn.execute("UPDATE signals SET status='SIMULATED_CLOSED' WHERE signal_id=?", (position["signal_id"],))
            account = conn.execute("SELECT * FROM account WHERE account_id=?", (ACCOUNT_ID,)).fetchone()
            cash = account["cash"] + net; peak = max(account["equity_peak"], cash); dd = max(0.0, 1.0 - cash / max(peak, 1e-12))
            conn.execute("""UPDATE account SET cash=?,realized_pnl=realized_pnl+?,unrealized_pnl=0,total_equity=?,equity_peak=?,max_drawdown_pct=?,fees=fees+?,slippage=slippage+?,funding=funding+?,updated_at=? WHERE account_id=?""",
                         (cash, net, cash, peak, max(account["max_drawdown_pct"], dd), fees, slippage, funding, utc_now(), ACCOUNT_ID))
            self._event(conn, "SIMULATED_POSITION_CLOSED", position_id, trade); self._equity(conn, "TRADE_CLOSED")
        return trade

    def reconcile(self) -> dict[str, Any]:
        account = self.account()
        if account is None: return {"status": "NO_LEDGER", **safety_envelope()}
        trades = self.rows("trades", 5000); positions = self.open_positions()
        expected_cash = float(account["initial_balance"]) + sum(float(row["net_pnl"]) for row in trades)
        errors = []
        if abs(expected_cash - float(account["cash"])) > 1e-8: errors.append("CASH_LEDGER_MISMATCH")
        if len({row["signal_id"] for row in trades}) != len(trades): errors.append("DUPLICATE_SIGNAL_TRADE")
        if len({row["signal_id"] for row in positions}) != len(positions): errors.append("DUPLICATE_OPEN_SIGNAL")
        return {"status": "PASS" if not errors else "FAIL", "errors": errors,
                "expected_cash": expected_cash, "stored_cash": account["cash"],
                "open_positions": len(positions), "closed_trades": len(trades), **safety_envelope()}
