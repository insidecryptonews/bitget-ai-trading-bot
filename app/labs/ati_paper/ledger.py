"""Transactional, restart-safe ledger for the isolated ATI paper account."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import ACCOUNT_ID, DEFAULT_DB_PATH, POLICY_VERSION, SOURCE_POLICY_VERSION, safety_envelope
from .config import AtiPaperConfig


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def stable_id(prefix: str, *parts: Any) -> str:
    payload = "|".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:28]}"


def _finite(value: Any, label: str, *, allow_zero: bool = True) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"ATI_PAPER_NON_FINITE:{label}") from exc
    if not math.isfinite(number) or (number < 0 if allow_zero else number <= 0):
        raise ValueError(f"ATI_PAPER_NON_FINITE:{label}")
    return number


SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    account_id TEXT PRIMARY KEY,
    initial_balance REAL NOT NULL,
    cash_balance REAL NOT NULL,
    realized_equity REAL NOT NULL,
    unrealized_pnl REAL NOT NULL,
    total_equity REAL NOT NULL,
    equity_peak REAL NOT NULL,
    drawdown_abs REAL NOT NULL,
    drawdown_pct REAL NOT NULL,
    max_drawdown_pct REAL NOT NULL,
    realized_pnl_total REAL NOT NULL,
    fees_total REAL NOT NULL,
    slippage_total REAL NOT NULL,
    funding_total REAL NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY,
    setup_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    decision_ts TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    pending_after_ts TEXT NOT NULL,
    ati_score REAL,
    score_components_json TEXT NOT NULL,
    support REAL,
    resistance REAL,
    invalidation REAL NOT NULL,
    policy_version TEXT NOT NULL,
    feature_version TEXT,
    source_payload_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    rejection_reason TEXT,
    accepted_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS simulated_orders (
    sim_order_id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    position_id TEXT,
    order_type TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    requested_price REAL NOT NULL,
    filled_price REAL NOT NULL,
    quantity REAL NOT NULL,
    notional REAL NOT NULL,
    fee REAL NOT NULL,
    slippage REAL NOT NULL,
    source_ts TEXT NOT NULL,
    created_at TEXT NOT NULL,
    filled_at TEXT NOT NULL,
    status TEXT NOT NULL,
    FOREIGN KEY(signal_id) REFERENCES signals(signal_id)
);
CREATE TABLE IF NOT EXISTS positions (
    position_id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL UNIQUE,
    setup_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_ts TEXT NOT NULL,
    entry_source_ts TEXT NOT NULL,
    entry_reference_price REAL NOT NULL,
    entry_price REAL NOT NULL,
    quantity REAL NOT NULL,
    notional REAL NOT NULL,
    reserved_notional REAL NOT NULL,
    stop_price REAL NOT NULL,
    take_profit_price REAL NOT NULL,
    trailing_stop REAL,
    risk_distance REAL NOT NULL,
    risk_money REAL NOT NULL,
    configured_sizing_fraction REAL NOT NULL,
    effective_sizing_fraction REAL NOT NULL,
    equity_before REAL NOT NULL,
    entry_fee REAL NOT NULL,
    entry_slippage REAL NOT NULL,
    unrealized_pnl REAL NOT NULL,
    mfe REAL NOT NULL,
    mae REAL NOT NULL,
    last_price REAL NOT NULL,
    last_market_ts TEXT NOT NULL,
    last_processed_bar_ms INTEGER,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(signal_id) REFERENCES signals(signal_id)
);
CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL UNIQUE,
    signal_id TEXT NOT NULL UNIQUE,
    setup_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_ts TEXT NOT NULL,
    exit_ts TEXT NOT NULL,
    entry_reference_price REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_reference_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    quantity REAL NOT NULL,
    notional REAL NOT NULL,
    stop_price REAL NOT NULL,
    take_profit_price REAL NOT NULL,
    gross_pnl REAL NOT NULL,
    fees REAL NOT NULL,
    slippage REAL NOT NULL,
    funding REAL NOT NULL,
    funding_status TEXT NOT NULL,
    net_pnl REAL NOT NULL,
    return_pct REAL NOT NULL,
    equity_before REAL NOT NULL,
    equity_after REAL NOT NULL,
    exit_reason TEXT NOT NULL,
    holding_seconds REAL NOT NULL,
    mfe REAL NOT NULL,
    mae REAL NOT NULL,
    policy_version TEXT NOT NULL,
    sizing_policy TEXT NOT NULL,
    sizing_fraction REAL NOT NULL,
    configured_sizing_fraction REAL NOT NULL,
    source_ts TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(position_id) REFERENCES positions(position_id)
);
CREATE TABLE IF NOT EXISTS equity_curve (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    cash REAL NOT NULL,
    realized_equity REAL NOT NULL,
    unrealized_pnl REAL NOT NULL,
    total_equity REAL NOT NULL,
    drawdown_pct REAL NOT NULL,
    open_exposure REAL NOT NULL,
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    account_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    signal_id TEXT,
    position_id TEXT,
    previous_state TEXT,
    new_state TEXT,
    reason TEXT,
    source_ts TEXT,
    policy_version TEXT NOT NULL,
    commit_hash TEXT,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS market_bars (
    symbol TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    available_at_ms INTEGER NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    PRIMARY KEY(symbol, timestamp_ms)
);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status, observed_at);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status, symbol);
CREATE INDEX IF NOT EXISTS idx_trades_exit ON trades(exit_ts);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_equity_time ON equity_curve(timestamp);
"""


class AtiPaperLedger:
    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            if not self.db_path.is_file():
                raise FileNotFoundError(self.db_path)
            conn = sqlite3.connect(
                f"file:{self.db_path.resolve().as_posix()}?mode=ro",
                uri=True, timeout=5.0,
            )
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        if not read_only:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ensure_schema(self) -> None:
        """Create only the isolated ATI-paper schema.

        ``sqlite3.executescript`` may commit an already-open transaction.  Run
        DDL before the account-credit transaction so the one-time credit and
        its audit event remain a single explicit transaction.
        """
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def initialize(self, config: AtiPaperConfig, *, commit_hash: str = "unknown") -> dict[str, Any]:
        self.ensure_schema()
        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM account WHERE account_id=?", (config.account_id,)).fetchone()
            created = row is None
            if created:
                balance = _finite(config.initial_balance_usdt, "initial_balance", allow_zero=False)
                conn.execute(
                    """INSERT INTO account VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (config.account_id, balance, balance, balance, 0.0, balance,
                     balance, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, now, now),
                )
                self._event(
                    conn, event_type="ACCOUNT_CREATED", correlation_id=config.account_id,
                    new_state="ACTIVE", reason="INITIAL_CREDIT_ONCE", source_ts=now,
                    commit_hash=commit_hash,
                    payload={"initial_balance": balance, "simulation_only": True},
                )
                self._append_equity(conn, reason="ACCOUNT_CREATED")
            else:
                if str(row["account_id"]) != ACCOUNT_ID or abs(float(row["initial_balance"]) - config.initial_balance_usdt) > 1e-9:
                    raise ValueError("ATI_PAPER_ACCOUNT_CONTRACT_MISMATCH")
                self._event(
                    conn, event_type="EXECUTOR_RESTARTED", correlation_id=config.account_id,
                    previous_state="PERSISTED", new_state="RESUMED",
                    reason="NO_RECREDIT", source_ts=now, commit_hash=commit_hash,
                    payload={"realized_equity": row["realized_equity"]},
                )
        account = self.account()
        return {"created": created, "account": account, **safety_envelope()}

    def signal(self, signal_id: str) -> dict[str, Any] | None:
        try:
            conn = self._connect(read_only=True)
        except FileNotFoundError:
            return None
        try:
            row = conn.execute("SELECT * FROM signals WHERE signal_id=?", (signal_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def pending_signals(self, *, limit: int = 200) -> list[dict[str, Any]]:
        try:
            conn = self._connect(read_only=True)
        except FileNotFoundError:
            return []
        try:
            rows = conn.execute(
                """SELECT * FROM signals WHERE status='ATI_SIGNAL_OBSERVED'
                   ORDER BY observed_at ASC LIMIT ?""",
                (max(1, min(5000, int(limit))),),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def open_positions(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        try:
            conn = self._connect(read_only=True)
        except FileNotFoundError:
            return []
        try:
            if symbol:
                rows = conn.execute(
                    "SELECT * FROM positions WHERE status='OPEN' AND symbol=? ORDER BY created_at",
                    (str(symbol).upper(),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM positions WHERE status='OPEN' ORDER BY created_at"
                ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def _event(self, conn: sqlite3.Connection, *, event_type: str,
               correlation_id: str, signal_id: str | None = None,
               position_id: str | None = None, previous_state: str | None = None,
               new_state: str | None = None, reason: str | None = None,
               source_ts: str | None = None, commit_hash: str = "unknown",
               payload: dict[str, Any] | None = None, event_key: str | None = None) -> str:
        timestamp = utc_now()
        key = event_key or stable_id(
            "evt", event_type, correlation_id, signal_id or "", position_id or "",
            source_ts or timestamp, reason or "",
        )
        conn.execute(
            """INSERT OR IGNORE INTO events
               (event_id,timestamp,event_type,account_id,correlation_id,signal_id,
                position_id,previous_state,new_state,reason,source_ts,policy_version,
                commit_hash,payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (key, timestamp, event_type, ACCOUNT_ID, correlation_id, signal_id,
             position_id, previous_state, new_state, reason, source_ts,
             POLICY_VERSION, commit_hash, canonical_json(payload or {})),
        )
        return key

    def record_event(self, **kwargs: Any) -> str:
        with self.transaction() as conn:
            return self._event(conn, **kwargs)

    def _append_equity(self, conn: sqlite3.Connection, *, reason: str) -> None:
        account = conn.execute("SELECT * FROM account WHERE account_id=?", (ACCOUNT_ID,)).fetchone()
        exposure = conn.execute(
            "SELECT COALESCE(SUM(notional),0) FROM positions WHERE status='OPEN'"
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO equity_curve
               (timestamp,cash,realized_equity,unrealized_pnl,total_equity,
                drawdown_pct,open_exposure,reason) VALUES (?,?,?,?,?,?,?,?)""",
            (utc_now(), account["cash_balance"], account["realized_equity"],
             account["unrealized_pnl"], account["total_equity"],
             account["drawdown_pct"], float(exposure or 0.0), reason),
        )

    def observe_signal(self, signal: dict[str, Any], *, observed_at: str | None = None,
                       commit_hash: str = "unknown") -> bool:
        signal_id = str(signal.get("signal_id") or "")
        direction = str(signal.get("direction") or "").upper()
        symbol = str(signal.get("symbol") or "").upper()
        decision_ts = str(signal.get("decision_ts") or "")
        invalidation = _finite(signal.get("invalidation_level"), "invalidation", allow_zero=False)
        if not signal_id or not decision_ts or direction not in {"LONG", "SHORT"} or not symbol.endswith("USDT"):
            raise ValueError("ATI_PAPER_SIGNAL_CONTRACT_INVALID")
        if signal.get("decision") != "SHADOW_CANDIDATE" or signal.get("exact_trigger") is not True:
            raise ValueError("ATI_PAPER_SIGNAL_NOT_FORWARD_CANDIDATE")
        if str(signal.get("policy_version") or "") != SOURCE_POLICY_VERSION:
            raise ValueError("ATI_PAPER_SOURCE_POLICY_NOT_FROZEN_V2")
        observed = observed_at or utc_now()
        allowed = {
            key: signal.get(key) for key in (
                "signal_id", "setup_id", "setup_variant", "symbol", "direction",
                "decision_ts", "ati_score", "score_components", "support_level",
                "resistance_level", "invalidation_level", "policy_version",
                "feature_version", "regime", "atr15",
            )
        }
        payload_hash = hashlib.sha256(canonical_json(allowed).encode("utf-8")).hexdigest()
        with self.transaction() as conn:
            existing = conn.execute("SELECT * FROM signals WHERE signal_id=?", (signal_id,)).fetchone()
            if existing:
                if str(existing["source_payload_hash"]) != payload_hash:
                    raise ValueError("ATI_PAPER_SIGNAL_ID_COLLISION")
                return False
            conn.execute(
                """INSERT INTO signals
                   (signal_id,setup_id,symbol,direction,decision_ts,observed_at,
                    pending_after_ts,ati_score,score_components_json,support,
                    resistance,invalidation,policy_version,feature_version,
                    source_payload_hash,status,rejection_reason,accepted_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (signal_id, str(signal.get("setup_id") or "UNKNOWN"), symbol,
                 direction, decision_ts, observed, observed,
                 float(signal.get("ati_score") or 0.0),
                 canonical_json(signal.get("score_components") or {}),
                 signal.get("support_level"), signal.get("resistance_level"),
                 invalidation, str(signal.get("policy_version") or ""),
                 str(signal.get("feature_version") or ""), payload_hash,
                 "ATI_SIGNAL_OBSERVED", None, None, observed),
            )
            self._event(
                conn, event_type="ATI_SIGNAL_OBSERVED", correlation_id=signal_id,
                signal_id=signal_id, previous_state=None,
                new_state="ATI_SIGNAL_OBSERVED", reason="FORWARD_LEDGER_INPUT",
                source_ts=decision_ts, commit_hash=commit_hash,
                payload={"symbol": symbol, "direction": direction,
                         "observed_at": observed, "entry_is_not_retroactive": True},
                event_key=stable_id("evt", "ATI_SIGNAL_OBSERVED", signal_id),
            )
        return True

    def reject_signal(self, signal_id: str, reason: str, *, source_ts: str | None = None,
                      commit_hash: str = "unknown") -> bool:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM signals WHERE signal_id=?", (signal_id,)).fetchone()
            if row is None or row["status"] in {"ATI_SIGNAL_REJECTED", "ATI_PAPER_POSITION_OPEN", "ATI_PAPER_POSITION_CLOSED"}:
                return False
            conn.execute(
                "UPDATE signals SET status='ATI_SIGNAL_REJECTED', rejection_reason=?, updated_at=? WHERE signal_id=?",
                (reason, utc_now(), signal_id),
            )
            self._event(
                conn, event_type="ATI_SIGNAL_REJECTED", correlation_id=signal_id,
                signal_id=signal_id, previous_state=str(row["status"]),
                new_state="ATI_SIGNAL_REJECTED", reason=reason,
                source_ts=source_ts, commit_hash=commit_hash,
                payload={"mechanical_rejection": True},
                event_key=stable_id("evt", "ATI_SIGNAL_REJECTED", signal_id, reason),
            )
        return True

    def persist_market_bars(self, bars: list[Any]) -> int:
        if not bars:
            return 0
        with self.transaction() as conn:
            before = conn.total_changes
            for bar in bars:
                conn.execute(
                    """INSERT OR IGNORE INTO market_bars
                       (symbol,timestamp_ms,available_at_ms,open,high,low,close,volume)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (bar.symbol, int(bar.timestamp_ms), int(bar.available_at_ms),
                     float(bar.open), float(bar.high), float(bar.low),
                     float(bar.close), float(bar.volume)),
                )
            return conn.total_changes - before

    def account(self, *, read_only: bool = True) -> dict[str, Any] | None:
        try:
            conn = self._connect(read_only=read_only)
        except FileNotFoundError:
            return None
        try:
            row = conn.execute("SELECT * FROM account WHERE account_id=?", (ACCOUNT_ID,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def rows(self, table: str, *, limit: int = 200, status: str | None = None,
             symbol: str | None = None) -> list[dict[str, Any]]:
        allowed = {
            "signals": "observed_at", "simulated_orders": "created_at",
            "positions": "created_at", "trades": "exit_ts",
            "equity_curve": "id", "events": "timestamp", "market_bars": "timestamp_ms",
        }
        if table not in allowed:
            raise ValueError("ATI_PAPER_TABLE_NOT_ALLOWED")
        try:
            conn = self._connect(read_only=True)
        except FileNotFoundError:
            return []
        try:
            clauses: list[str] = []
            params: list[Any] = []
            if status is not None and table in {"signals", "positions", "simulated_orders"}:
                clauses.append("status=?")
                params.append(status)
            if symbol is not None and table in {"signals", "positions", "trades", "market_bars"}:
                clauses.append("symbol=?")
                params.append(symbol.upper())
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            params.append(max(1, min(5000, int(limit))))
            result = conn.execute(
                f"SELECT * FROM {table}{where} ORDER BY {allowed[table]} DESC LIMIT ?", params,
            ).fetchall()
            return [dict(row) for row in result]
        finally:
            conn.close()

    def reconcile(self) -> dict[str, Any]:
        try:
            conn = self._connect(read_only=True)
        except FileNotFoundError:
            return {"status": "NO_LEDGER", "blockers": ["ledger_missing"]}
        try:
            account = conn.execute("SELECT * FROM account WHERE account_id=?", (ACCOUNT_ID,)).fetchone()
            if account is None:
                return {"status": "FAIL", "blockers": ["account_missing"]}
            blockers: list[str] = []
            open_rows = conn.execute("SELECT * FROM positions WHERE status='OPEN'").fetchall()
            closed_trades = conn.execute("SELECT * FROM trades").fetchall()
            duplicate_open = conn.execute(
                "SELECT signal_id,COUNT(*) n FROM positions WHERE status='OPEN' GROUP BY signal_id HAVING n>1"
            ).fetchall()
            if duplicate_open:
                blockers.append("duplicate_open_signal")
            open_entry_cost = sum(float(row["entry_fee"]) + float(row["entry_slippage"]) for row in open_rows)
            expected_realized = float(account["initial_balance"]) + sum(float(row["net_pnl"]) for row in closed_trades) - open_entry_cost
            expected_cash = expected_realized - sum(float(row["reserved_notional"]) for row in open_rows)
            expected_unrealized = sum(float(row["unrealized_pnl"]) for row in open_rows)
            expected_total = expected_realized + expected_unrealized
            if abs(float(account["realized_equity"]) - expected_realized) > 1e-6:
                blockers.append("realized_equity_mismatch")
            if abs(float(account["cash_balance"]) - expected_cash) > 1e-6:
                blockers.append("cash_balance_mismatch")
            if abs(float(account["unrealized_pnl"]) - expected_unrealized) > 1e-6:
                blockers.append("unrealized_pnl_mismatch")
            if abs(float(account["total_equity"]) - expected_total) > 1e-6:
                blockers.append("total_equity_mismatch")
            if float(account["cash_balance"]) < -1e-6:
                blockers.append("negative_cash")
            return {
                "status": "PASS" if not blockers else "FAIL",
                "blockers": blockers,
                "account_id": ACCOUNT_ID,
                "open_positions": len(open_rows),
                "closed_trades": len(closed_trades),
                "expected_realized_equity": expected_realized,
                "actual_realized_equity": float(account["realized_equity"]),
                "expected_cash_balance": expected_cash,
                "actual_cash_balance": float(account["cash_balance"]),
            }
        finally:
            conn.close()
