"""Two isolated research demos with no exchange or productive-policy imports.

The diagnostic account exists only to exercise causal simulation mechanics. Its
PnL is never evidence of edge. The candidate account remains uninitialized until
a strict research gate passes and a separate human review occurs.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .cross_venue import REPO_ROOT


RUNTIME_ROOT = REPO_ROOT / "data" / "runtime" / "edge_sprint_48h"
DIAGNOSTIC_DB_PATH = RUNTIME_ROOT / "operability_diagnostic_demo.sqlite"
DIAGNOSTIC_STATUS_PATH = RUNTIME_ROOT / "operability_diagnostic_demo_status.json"
EDGE_DEMO_STATUS_PATH = RUNTIME_ROOT / "edge_candidate_demo_status.json"
DIAGNOSTIC_ACCOUNT_ID = "OPERABILITY_DIAGNOSTIC_DEMO_50"
EDGE_ACCOUNT_ID = "EDGE_CANDIDATE_DEMO_50"

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
 account_id TEXT PRIMARY KEY, initial_balance REAL NOT NULL, cash REAL NOT NULL,
 realized_pnl REAL NOT NULL, fees REAL NOT NULL, created_at TEXT NOT NULL,
 forward_boundary_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
 signal_id TEXT PRIMARY KEY, observed_at_ms INTEGER NOT NULL, decision_ms INTEGER NOT NULL,
 symbol TEXT NOT NULL, side TEXT NOT NULL, reason TEXT NOT NULL, payload_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
 trade_id TEXT PRIMARY KEY, signal_id TEXT NOT NULL UNIQUE, symbol TEXT NOT NULL,
 side TEXT NOT NULL, entry_ms INTEGER NOT NULL, exit_ms INTEGER NOT NULL,
 entry_price REAL NOT NULL, exit_price REAL NOT NULL, gross_bps REAL NOT NULL,
 cost_bps REAL NOT NULL, net_bps REAL NOT NULL, pnl REAL NOT NULL,
 exit_reason TEXT NOT NULL, FOREIGN KEY(signal_id) REFERENCES signals(signal_id)
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
 event_type TEXT NOT NULL, correlation_id TEXT NOT NULL, payload_json TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safety() -> dict[str, Any]:
    return {
        "research_only": True, "simulation_only": True, "shadow_only": True,
        "diagnostic_only": True, "edge_validated": False, "not_actionable": True,
        "paper_filter_enabled": False, "can_send_real_orders": False,
        "private_endpoints_used": False, "orders_sent": 0,
        "active_policy_modified": False, "auto_promotion": False,
        "final_recommendation": "NO LIVE",
    }


def _finite(value: Any, label: str, *, positive: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"DIAGNOSTIC_DEMO_NON_FINITE:{label}") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError(f"DIAGNOSTIC_DEMO_NON_FINITE:{label}")
    return number


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("DIAGNOSTIC_DEMO_SYMLINK_BLOCKED")
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class DiagnosticDemoLedger:
    def __init__(self, path: Path | str = DIAGNOSTIC_DB_PATH) -> None:
        self.path = Path(path)

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            connection = sqlite3.connect(
                f"file:{self.path.resolve().as_posix()}?mode=ro", uri=True, timeout=5,
            )
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_symlink() or self.path.parent.is_symlink():
                raise ValueError("DIAGNOSTIC_DEMO_SYMLINK_BLOCKED")
            connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self, *, forward_boundary_ms: int, initial_balance: float = 50.0) -> dict[str, Any]:
        boundary = int(forward_boundary_ms)
        balance = _finite(initial_balance, "initial_balance", positive=True)
        if boundary <= 0:
            raise ValueError("DIAGNOSTIC_DEMO_BOUNDARY_REQUIRED")
        connection = self._connect()
        try:
            connection.executescript(SCHEMA)
            row = connection.execute("SELECT * FROM account").fetchall()
            if not row:
                connection.execute(
                    "INSERT INTO account VALUES(?,?,?,?,?,?,?)",
                    (DIAGNOSTIC_ACCOUNT_ID, balance, balance, 0.0, 0.0, utc_now(), boundary),
                )
                self._event(connection, "ACCOUNT_CREATED", DIAGNOSTIC_ACCOUNT_ID, {"boundary_ms": boundary})
            elif len(row) != 1:
                raise ValueError("DIAGNOSTIC_DEMO_ACCOUNT_CARDINALITY")
            else:
                account = dict(row[0])
                if (
                    account.get("account_id") != DIAGNOSTIC_ACCOUNT_ID
                    or float(account.get("initial_balance")) != balance
                    or int(account.get("forward_boundary_ms")) != boundary
                ):
                    raise ValueError("DIAGNOSTIC_DEMO_ACCOUNT_RESET_BLOCKED")
            connection.commit()
        finally:
            connection.close()
        return self.status()

    @staticmethod
    def _event(connection: sqlite3.Connection, event: str, correlation: str, payload: dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO events(timestamp,event_type,correlation_id,payload_json) VALUES(?,?,?,?)",
            (utc_now(), event, correlation, json.dumps(payload, sort_keys=True, allow_nan=False)),
        )

    def record_simulation(self, signal: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        if result.get("status") != "CLOSED":
            raise ValueError("DIAGNOSTIC_DEMO_CLOSED_RESULT_REQUIRED")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            account = connection.execute("SELECT * FROM account").fetchone()
            if account is None:
                raise ValueError("DIAGNOSTIC_DEMO_NOT_INITIALIZED")
            signal_id = str(signal.get("signal_id") or "")
            decision_ms = int(signal.get("decision_ms") or 0)
            observed_at_ms = int(signal.get("observed_at_ms") or decision_ms)
            if not signal_id or decision_ms < int(account["forward_boundary_ms"]):
                raise ValueError("DIAGNOSTIC_DEMO_PRE_BOUNDARY_SIGNAL_BLOCKED")
            existing = connection.execute("SELECT signal_id FROM signals WHERE signal_id=?", (signal_id,)).fetchone()
            if existing is not None:
                connection.rollback()
                return {"status": "DUPLICATE_IGNORED", "signal_id": signal_id}
            payload_hash = hashlib.sha256(
                json.dumps(signal, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
            ).hexdigest()
            connection.execute(
                "INSERT INTO signals VALUES(?,?,?,?,?,?,?)",
                (signal_id, observed_at_ms, decision_ms, str(signal["symbol"]), str(signal["side"]),
                 str(signal.get("reason") or "PREDEFINED_DIAGNOSTIC_NEAR_MISS"), payload_hash),
            )
            trade_id = "diag_" + hashlib.sha256(signal_id.encode("utf-8")).hexdigest()[:24]
            fields = (
                trade_id, signal_id, str(signal["symbol"]), str(signal["side"]),
                int(result["entry_ms"]), int(result["exit_ms"]), float(result["entry_price"]),
                float(result["exit_price"]), float(result["gross_bps"]), float(result["cost_bps"]),
                float(result["net_bps"]), float(result["pnl"]), str(result["exit_reason"]),
            )
            connection.execute("INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", fields)
            connection.execute(
                "UPDATE account SET cash=cash+?,realized_pnl=realized_pnl+?,fees=fees+? WHERE account_id=?",
                (float(result["pnl"]), float(result["pnl"]), float(result["fee_amount"]), DIAGNOSTIC_ACCOUNT_ID),
            )
            self._event(connection, "DIAGNOSTIC_TRADE_CLOSED", trade_id, {
                "signal_id": signal_id, "exit_reason": result["exit_reason"],
                "not_edge": True, "not_candidate": True,
            })
            connection.commit()
            return {"status": "RECORDED", "trade_id": trade_id}
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def status(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"status": "NOT_STARTED", "account": None, "trades": 0, **safety()}
        connection = self._connect(read_only=True)
        try:
            account_row = connection.execute("SELECT * FROM account").fetchone()
            trades = int(connection.execute("SELECT COUNT(*) FROM trades").fetchone()[0])
            signals = int(connection.execute("SELECT COUNT(*) FROM signals").fetchone()[0])
            duplicate_trades = int(connection.execute(
                "SELECT COUNT(*) FROM (SELECT signal_id,COUNT(*) n FROM trades GROUP BY signal_id HAVING n>1)"
            ).fetchone()[0])
            account = dict(account_row) if account_row else None
            expected_cash = (
                float(account["initial_balance"])
                + float(connection.execute("SELECT COALESCE(SUM(pnl),0) FROM trades").fetchone()[0])
                if account else None
            )
            reconciliation = bool(
                account and duplicate_trades == 0
                and abs(float(account["cash"]) - float(expected_cash)) <= 1e-9
            )
            return {
                "status": "OPERABILITY DIAGNOSTIC ACTIVE - NOT EDGE" if account else "NOT_STARTED",
                "account": account, "signals": signals, "trades": trades,
                "open_positions": 0, "duplicate_trades": duplicate_trades,
                "reconciliation": "PASS" if reconciliation else "FAIL",
                "labels": ["DIAGNOSTIC ONLY", "NOT EDGE", "NOT A CANDIDATE", "DO NOT USE FOR PROFIT CLAIMS", "NO LIVE"],
                **safety(),
            }
        finally:
            connection.close()


def simulate_causal_trade(
    signal: dict[str, Any], quotes: Iterable[dict[str, Any]], *,
    notional: float = 5.0, cost_bps: float = 15.5,
    stop_bps: float = 20.0, take_profit_bps: float = 30.0,
    max_quotes: int = 20,
) -> dict[str, Any]:
    side = str(signal.get("side") or "").upper()
    if side not in {"LONG", "SHORT"}:
        raise ValueError("DIAGNOSTIC_DEMO_SIDE_INVALID")
    available_at = int(signal.get("observed_at_ms") or signal.get("decision_ms") or 0)
    ordered = sorted((dict(row) for row in quotes), key=lambda row: int(row.get("timestamp_ms") or 0))
    future = [row for row in ordered if int(row.get("timestamp_ms") or 0) > available_at]
    if not future:
        return {"status": "NEED_MORE_DATA", "reason": "NEXT_OBSERVABLE_QUOTE_MISSING"}
    first = future[0]
    entry = _finite(first.get("ask") if side == "LONG" else first.get("bid"), "entry", positive=True)
    entry_ms = int(first["timestamp_ms"])
    stop = entry * (1 - stop_bps / 10_000) if side == "LONG" else entry * (1 + stop_bps / 10_000)
    target = entry * (1 + take_profit_bps / 10_000) if side == "LONG" else entry * (1 - take_profit_bps / 10_000)
    exit_price = None
    exit_ms = None
    reason = None
    for row in future[1:max(2, int(max_quotes) + 1)]:
        timestamp = int(row.get("timestamp_ms") or 0)
        executable = _finite(row.get("bid") if side == "LONG" else row.get("ask"), "exit", positive=True)
        low = _finite(row.get("low_bid", executable), "low", positive=True)
        high = _finite(row.get("high_bid", executable), "high", positive=True)
        if side == "SHORT":
            low = _finite(row.get("low_ask", executable), "low", positive=True)
            high = _finite(row.get("high_ask", executable), "high", positive=True)
        stop_hit = low <= stop if side == "LONG" else high >= stop
        tp_hit = high >= target if side == "LONG" else low <= target
        if stop_hit:
            exit_price, exit_ms, reason = stop, timestamp, "STOP_BEFORE_TP"
            break
        if tp_hit:
            exit_price, exit_ms, reason = target, timestamp, "TAKE_PROFIT"
            break
        exit_price, exit_ms, reason = executable, timestamp, "TIME"
    if exit_price is None or exit_ms is None:
        return {"status": "NEED_MORE_DATA", "reason": "EXIT_QUOTE_MISSING"}
    gross_bps = (
        (exit_price / entry - 1) * 10_000
        if side == "LONG" else (entry / exit_price - 1) * 10_000
    )
    total_cost = _finite(cost_bps, "cost_bps")
    amount = _finite(notional, "notional", positive=True)
    net_bps = gross_bps - total_cost
    pnl = amount * net_bps / 10_000
    return {
        "status": "CLOSED", "entry_ms": entry_ms, "exit_ms": exit_ms,
        "entry_price": entry, "exit_price": exit_price,
        "gross_bps": gross_bps, "cost_bps": total_cost, "net_bps": net_bps,
        "pnl": pnl, "fee_amount": amount * total_cost / 10_000,
        "exit_reason": reason, "same_bar_policy": "STOP_BEFORE_TP",
        "causal_entry": entry_ms > available_at, "not_edge": True,
    }


def ensure_diagnostic_demo(
    *, now_ms: int | None = None, ledger: DiagnosticDemoLedger | None = None,
    write_status: bool = True,
) -> dict[str, Any]:
    target = ledger or DiagnosticDemoLedger()
    existing = target.status()
    if existing.get("account"):
        boundary = int(existing["account"]["forward_boundary_ms"])
    else:
        boundary = int(now_ms or time.time() * 1000)
        target.initialize(forward_boundary_ms=boundary)
    result = target.status()
    result.update({
        "forward_boundary_ms": boundary,
        "source_populations_excluded": [
            "ATI_PAPER", "P11", "CROSS_VENUE_PAPER", "CANDIDATE_INCUBATOR", "FORWARD_EDGE_EVIDENCE",
        ],
    })
    if write_status:
        _atomic_json(DIAGNOSTIC_STATUS_PATH, result)
    return result


def edge_candidate_gate(challenger: dict[str, Any], holdout: dict[str, Any] | None = None) -> dict[str, Any]:
    candidates = challenger.get("candidates") if isinstance(challenger.get("candidates"), list) else []
    best = candidates[0] if candidates else {}
    validation = ((best.get("validation") or {}).get("cost_scenarios") or {}).get("15.5") or {}
    stress = ((best.get("validation") or {}).get("cost_scenarios") or {}).get("18.0") or {}
    folds = (best.get("walk_forward") or {}).get("folds") or []
    nonnegative = sum(
        1 for fold in folds
        if ((fold.get("metrics") or {}).get("net_ev_bps") is not None
            and float((fold.get("metrics") or {}).get("net_ev_bps")) >= 0)
    )
    baseline = ((best.get("validation") or {}).get("exposure_matched_baselines") or {}).get("15.5") or {}
    best_baseline = max(
        float((baseline.get(name) or {}).get("net_ev_bps") or 0.0)
        for name in ("no_trade", "opposite_direction", "deterministic_random_sign")
    ) if baseline else 0.0
    blockers: list[str] = []
    if not challenger.get("dataset_hash"):
        blockers.append("FROZEN_DATASET_REQUIRED")
    if best.get("state") != "WATCH_ONLY":
        blockers.append("WATCH_ONLY_CANDIDATE_REQUIRED")
    if float(validation.get("net_ev_bps") or -math.inf) <= 0:
        blockers.append("VALIDATION_NET_EV_NOT_POSITIVE")
    if float(validation.get("net_ev_lower_bound_bps") or -math.inf) < 0:
        blockers.append("LOWER_BOUND_NEGATIVE")
    if float(validation.get("n_eff") or 0) < 100:
        blockers.append("N_EFF_BELOW_100")
    if int(validation.get("trades") or 0) < 100:
        blockers.append("EPISODE_SAMPLE_INSUFFICIENT")
    if len(folds) < 4 or nonnegative < 3:
        blockers.append("WALK_FORWARD_3_OF_4_NOT_MET")
    if float(stress.get("net_ev_bps") or -math.inf) <= -5:
        blockers.append("CONSERVATIVE_COST_CATASTROPHIC")
    if float(validation.get("net_ev_bps") or -math.inf) <= best_baseline:
        blockers.append("EXPOSURE_MATCHED_BASELINE_NOT_BEATEN")
    if float(validation.get("single_symbol_profit_concentration") or 1.0) > 0.80:
        blockers.append("SINGLE_SYMBOL_DOMINATION")
    holdout_state = (holdout or {}).get("status") or "NEED_MORE_DATA"
    if holdout_state not in {"PASS", "NEED_MORE_DATA"}:
        blockers.append("HOLDOUT_NOT_POSITIVE_OR_NEED_DATA")
    return {
        "status": "ELIGIBLE_PENDING_HUMAN_REVIEW" if not blockers else "NO_DEFENSIBLE_CANDIDATE",
        "candidate": best.get("trial_id"), "family": best.get("family"),
        "blockers": blockers, "human_review_required": True,
        "automatic_start": False, "paper_filter_enabled": False,
        "can_send_real_orders": False, "final_recommendation": "NO LIVE",
    }


def edge_demo_status(
    challenger: dict[str, Any] | None = None, *, write_status: bool = False,
) -> dict[str, Any]:
    if challenger is None:
        try:
            challenger = json.loads(
                (REPO_ROOT / "data" / "runtime" / "storage_efficiency_v2" / "challenger_status.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            challenger = {}
    gate = edge_candidate_gate(challenger)
    result = {
        "schema": "edge_candidate_demo_status.v1",
        "status": "NO DEFENSIBLE CANDIDATE - DEMO NOT STARTED",
        "account_id": EDGE_ACCOUNT_ID, "account_initialized": False,
        "positions": 0, "trades": 0, "reconciliation": "NOT_STARTED",
        "gate": gate, "activation": "disabled",
        **safety(),
    }
    if write_status:
        _atomic_json(EDGE_DEMO_STATUS_PATH, result)
    return result
