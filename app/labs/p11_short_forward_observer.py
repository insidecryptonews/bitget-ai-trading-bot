"""Continuous deterministic P11_SHORT forward-shadow observer.

This module is deliberately isolated from the execution stack.  It consumes
closed public Bitget candles, evaluates the preregistered V10.47 P11_SHORT
policy and persists an auditable simulated lifecycle.  It never imports the
application config, credentials, wallets, private clients or order modules.

SQLite is the source of truth.  JSON/CSV files are atomic read-only exports for
humans and the local research dashboard.
"""

from __future__ import annotations

import csv
import hashlib
import inspect
import json
import math
import os
import sqlite3
import subprocess
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from app.labs import public_data_backfill_v10_45_1 as bitget_data
from app.labs.v10_46 import causal_stats
from app.labs.v10_46 import contracts
from app.labs.v10_46 import event_clock
from app.labs.v10_46 import families
from app.labs.v10_46 import sim_oms


SCHEMA_VERSION = "p11_short_forward_observer.v1"
SYMBOL = "BTCUSDT"
VENUE = "bitget"
TIMEFRAME = "15m"
HYPOTHESIS_ID = "P11_SHORT"
SIDE = "SHORT"
INTERVAL_MS = 900_000
MAX_POSITION = 1
MONEY_SCENARIO = "5eur"
COST_SCENARIO = "observed"
SOURCE = "bitget_public_1m_strict_15m_closed"
AUTHORITY_PATH = Path(__file__).with_name("v10_46") / "campaign_authority_v10_47_25.json"
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "research" / "p11_short_forward_observer"
DEFAULT_DB_NAME = "observer.sqlite3"
LEASE_TTL_MS = 180_000
GENESIS_HASH = "GENESIS"
NA = "N/A"

EXPECTED_EXIT = {"stop_frac": 0.008, "tp_frac": 0.012, "time_exit": 15}
EXPECTED_PARTICIPANT_SPEC_HASH = (
    "d30173f89fe94144ff055e9f1a419ceb3172b407f8a77464583c816d47278571"
)
EXPECTED_REGISTRY_HASH = (
    "c4d99ad0bf4eb41c9d3df58de41d08a8d28da7400d98fbd91d16347b86dab9a9"
)
EXPECTED_REFERENCE_GENERATION = "cdf24067e4241157"

FINAL_STATES = {"REJECTED_FINAL", "LABEL_FINALIZED", "ERROR_FINAL"}
ACTIVE_STATES = {"ENTRY_PLANNED", "OPEN_SHADOW"}
FINAL_EVENT_TYPES = {"SIGNAL_REJECTED", "OUTCOME_FINALIZED", "LABEL_FINALIZED"}
ALL_STATES = {
    "OBSERVED", "ELIGIBLE", "ENTRY_PLANNED", "OPEN_SHADOW", "EXITED",
    "OUTCOME_FINALIZED", "LABEL_FINALIZED", "REJECTED_FINAL", "ERROR_FINAL",
}
VALID_TRANSITIONS = {
    ("OBSERVED", "ELIGIBLE"),
    ("OBSERVED", "REJECTED_FINAL"),
    ("OBSERVED", "ERROR_FINAL"),
    ("ELIGIBLE", "ENTRY_PLANNED"),
    ("ELIGIBLE", "ERROR_FINAL"),
    ("ENTRY_PLANNED", "OPEN_SHADOW"),
    ("ENTRY_PLANNED", "ERROR_FINAL"),
    ("OPEN_SHADOW", "OPEN_SHADOW"),
    ("OPEN_SHADOW", "EXITED"),
    ("OPEN_SHADOW", "ERROR_FINAL"),
    ("EXITED", "OUTCOME_FINALIZED"),
    ("EXITED", "ERROR_FINAL"),
    ("OUTCOME_FINALIZED", "LABEL_FINALIZED"),
    ("OUTCOME_FINALIZED", "ERROR_FINAL"),
}


class ObserverError(RuntimeError):
    """Base error for fail-closed observer failures."""


class ObserverAlreadyRunning(ObserverError):
    """Raised when a non-expired fenced lease belongs to another instance."""


class ObserverDataError(ObserverError):
    """Raised for malformed, conflicting or non-causal market data."""


class PublicDataUnavailable(ObserverDataError):
    """Transient public-source outage: visible and retryable, never silent."""


class InvalidTransition(ObserverError):
    """Raised when the lifecycle state machine is violated."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=str)


def _sha(value: Any) -> str:
    raw = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _deterministic_id(kind: str, *parts: Any) -> str:
    return f"{kind}_{_sha([SCHEMA_VERSION, kind, *parts])[:32]}"


def _utc_iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ceil_boundary(now_ms: int) -> int:
    return ((int(now_ms) + INTERVAL_MS - 1) // INTERVAL_MS) * INTERVAL_MS


def _git_value(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=REPO_ROOT, check=True, capture_output=True,
            text=True, timeout=5,
        )
        return result.stdout.strip()
    except Exception:
        return "UNAVAILABLE"


def _repo_provenance() -> dict[str, str]:
    return {
        "head": _git_value("rev-parse", "HEAD"),
        "tree": _git_value("rev-parse", "HEAD^{tree}"),
    }


def _bar_id(ts_ms: int) -> str:
    return f"{VENUE}:{SYMBOL}:{TIMEFRAME}:{int(ts_ms)}"


def _bar_payload(bar: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": int(bar["ts"]),
        "open": float(bar["open"]),
        "high": float(bar["high"]),
        "low": float(bar["low"]),
        "close": float(bar["close"]),
        "volume": float(bar.get("volume", 0.0)),
    }


def _validate_bar(bar: Any) -> dict[str, Any]:
    if not isinstance(bar, dict):
        raise ObserverDataError("BAR_NOT_OBJECT")
    required = ("ts", "open", "high", "low", "close")
    if any(key not in bar for key in required):
        raise ObserverDataError("BAR_FIELD_MISSING")
    if type(bar["ts"]) is not int or bar["ts"] < 0 \
            or bar["ts"] % INTERVAL_MS != 0:
        raise ObserverDataError("BAR_TIMESTAMP_INVALID_OR_MISALIGNED")
    values: list[float] = []
    for field in ("open", "high", "low", "close", "volume"):
        try:
            value = float(bar.get(field, 0.0))
        except (TypeError, ValueError):
            raise ObserverDataError(f"BAR_{field.upper()}_INVALID") from None
        if not math.isfinite(value):
            raise ObserverDataError(f"BAR_{field.upper()}_NONFINITE")
        values.append(value)
    op, hi, lo, close, volume = values
    if min(op, hi, lo, close) <= 0 or volume < 0 or hi < max(op, close) \
            or lo > min(op, close) or lo > hi:
        raise ObserverDataError("BAR_OHLC_INVALID")
    return {"ts": int(bar["ts"]), "open": op, "high": hi, "low": lo,
            "close": close, "volume": volume}


def _resample_closed_15m(minute_bars: list[dict[str, Any]], *,
                         as_of_ms: int) -> list[dict[str, Any]]:
    """Strict local adapter: exactly 15 aligned, consecutive closed 1m bars.

    This small adapter intentionally lives here so the operational observer's
    import graph never reaches the broad discovery/holdout research engine.
    """
    groups: dict[int, list[dict[str, Any]]] = {}
    for bar in minute_bars:
        ts_ms = int(bar["ts"])
        groups.setdefault(ts_ms // INTERVAL_MS, []).append(bar)
    result: list[dict[str, Any]] = []
    for bucket in sorted(groups):
        bucket_open = bucket * INTERVAL_MS
        if bucket_open + INTERVAL_MS > int(as_of_ms):
            continue
        group = sorted(groups[bucket], key=lambda item: int(item["ts"]))
        expected = [bucket_open + index * 60_000 for index in range(15)]
        if len(group) != 15 or [int(item["ts"]) for item in group] != expected:
            continue
        result.append({
            "ts": bucket_open,
            "available_at": bucket_open + INTERVAL_MS,
            "open": float(group[0]["open"]),
            "high": max(float(item["high"]) for item in group),
            "low": min(float(item["low"]) for item in group),
            "close": float(group[-1]["close"]),
            "volume": sum(float(item.get("volume", 0.0)) for item in group),
            "turnover": sum(float(item.get("turnover", 0.0)) for item in group),
            "symbol": SYMBOL, "venue": VENUE,
        })
    return result


def load_policy_binding() -> dict[str, Any]:
    """Bind the live observer to the frozen P11_SHORT scientific contract."""
    exit_params = dict(families.FAMILIES["P11"]["exit"])
    if exit_params != EXPECTED_EXIT:
        raise ObserverError("P11_EXIT_SPEC_MISMATCH")
    participant_hash = contracts.canonical_hash(
        {"participant": HYPOTHESIS_ID, "exit": exit_params}
    )
    if participant_hash != EXPECTED_PARTICIPANT_SPEC_HASH:
        raise ObserverError("P11_PARTICIPANT_FINGERPRINT_MISMATCH")
    try:
        authority = json.loads(AUTHORITY_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ObserverError(f"P11_AUTHORITY_UNREADABLE:{type(exc).__name__}") from exc
    authority_hash = (authority.get("participant_spec_hashes") or {}).get(
        HYPOTHESIS_ID
    )
    entry = next(
        (item for item in authority.get("entries", [])
         if item.get("key") == f"{SYMBOL}:{TIMEFRAME}"),
        None,
    )
    if authority_hash != participant_hash or not entry:
        raise ObserverError("P11_AUTHORITY_BINDING_MISSING")
    if str(entry.get("venue", "")).lower() != VENUE \
            or entry.get("tournament_registry_hash") != EXPECTED_REGISTRY_HASH \
            or entry.get("dataset_source_generation_id") != EXPECTED_REFERENCE_GENERATION:
        raise ObserverError("P11_AUTHORITY_SCOPE_MISMATCH")
    callable_fingerprint = _sha(inspect.getsource(families.FAMILIES["P11"]["fn"]))
    contract = {
        "schema_version": SCHEMA_VERSION,
        "participant": HYPOTHESIS_ID,
        "family": "P11",
        "direction": SIDE,
        "symbol": SYMBOL,
        "venue": VENUE,
        "timeframe": TIMEFRAME,
        "signal": "atr_pct>0.85 and ret_15>0.01 and upper_wick>0.001",
        "upper_wick_unit": "absolute_price",
        "entry": "next_closed_bar_open_first_causal",
        "exit": exit_params,
        "intrabar_priority": "STOP_FIRST",
        "max_simulated_positions": MAX_POSITION,
        "scenario_money": MONEY_SCENARIO,
        "scenario_cost": COST_SCENARIO,
        "participant_spec_hash": participant_hash,
        "callable_fingerprint": callable_fingerprint,
        "authority_registry_hash": entry["tournament_registry_hash"],
        "authority_reference_generation_id": entry["dataset_source_generation_id"],
        "authority_role": "historical_parent_reference_not_forward_dataset",
        "source": SOURCE,
        "research_only": True,
        "orders_allowed": False,
        "holdout_access": False,
    }
    return {**contract, "policy_fingerprint": _sha(contract)}


def _config_contract(policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "symbol": SYMBOL, "venue": VENUE, "timeframe": TIMEFRAME,
        "hypothesis_id": HYPOTHESIS_ID, "side": SIDE,
        "source": SOURCE, "strict_1m_aggregation_factor": 15,
        "closed_bars_only": True, "max_position": MAX_POSITION,
        "money_scenario": MONEY_SCENARIO, "cost_scenario": COST_SCENARIO,
        "policy_fingerprint": policy["policy_fingerprint"],
        "observer_code_fingerprint": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
    }


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS p11_runs (
    run_id TEXT PRIMARY KEY,
    singleton INTEGER NOT NULL UNIQUE CHECK (singleton = 1),
    schema_version TEXT NOT NULL,
    forward_start_ms INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    repo_head TEXT NOT NULL,
    repo_tree TEXT NOT NULL,
    policy_fingerprint TEXT NOT NULL,
    participant_spec_hash TEXT NOT NULL,
    callable_fingerprint TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    authority_registry_hash TEXT NOT NULL,
    authority_reference_generation_id TEXT NOT NULL,
    market_source_generation_id TEXT NOT NULL,
    source TEXT NOT NULL,
    symbol TEXT NOT NULL,
    venue TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    side TEXT NOT NULL,
    contract_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS p11_bars (
    run_id TEXT NOT NULL,
    bar_open_ms INTEGER NOT NULL,
    availability_ms INTEGER NOT NULL,
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
    close REAL NOT NULL, volume REAL NOT NULL,
    payload_hash TEXT NOT NULL,
    is_forward INTEGER NOT NULL CHECK (is_forward IN (0, 1)),
    first_seen_ms INTEGER NOT NULL,
    PRIMARY KEY (run_id, bar_open_ms),
    FOREIGN KEY (run_id) REFERENCES p11_runs(run_id)
);

CREATE TABLE IF NOT EXISTS p11_lifecycles (
    lifecycle_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    opportunity_id TEXT NOT NULL UNIQUE,
    signal_id TEXT NOT NULL UNIQUE,
    candidate_trade_id TEXT NOT NULL UNIQUE,
    hypothesis_id TEXT NOT NULL,
    global_event_id TEXT NOT NULL,
    dependency_cluster_id TEXT NOT NULL,
    underlying_trade_id TEXT UNIQUE,
    signal_bar_ms INTEGER NOT NULL,
    availability_ms INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
      'OBSERVED','ELIGIBLE','ENTRY_PLANNED','OPEN_SHADOW','EXITED',
      'OUTCOME_FINALIZED','LABEL_FINALIZED','REJECTED_FINAL','ERROR_FINAL')),
    version INTEGER NOT NULL DEFAULT 1,
    canonical_signal INTEGER NOT NULL CHECK (canonical_signal IN (0, 1)),
    decision_action TEXT NOT NULL,
    rejection_reason TEXT,
    regime TEXT NOT NULL,
    planned_entry_bar_ms INTEGER,
    entry_bar_id TEXT,
    entry_ts_ms INTEGER,
    exit_bar_id TEXT,
    exit_ts_ms INTEGER,
    last_bar_ms INTEGER,
    bars_observed INTEGER NOT NULL DEFAULT 0,
    mfe_frac REAL,
    mae_frac REAL,
    position_slot INTEGER CHECK (position_slot = 1 OR position_slot IS NULL),
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    UNIQUE (run_id, signal_bar_ms),
    FOREIGN KEY (run_id) REFERENCES p11_runs(run_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS p11_one_active_position
ON p11_lifecycles(run_id, position_slot) WHERE position_slot IS NOT NULL;

CREATE TABLE IF NOT EXISTS p11_events (
    event_id TEXT PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    schema_version TEXT NOT NULL,
    event_type TEXT NOT NULL,
    lifecycle_id TEXT NOT NULL,
    opportunity_id TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    candidate_trade_id TEXT NOT NULL,
    hypothesis_id TEXT NOT NULL,
    global_event_id TEXT NOT NULL,
    dependency_cluster_id TEXT NOT NULL,
    underlying_trade_id TEXT,
    entry_bar_id TEXT,
    exit_bar_id TEXT,
    from_state TEXT,
    to_state TEXT NOT NULL,
    finalization_status TEXT NOT NULL,
    bar_open_ms INTEGER NOT NULL,
    event_timestamp_ms INTEGER NOT NULL,
    availability_timestamp_ms INTEGER NOT NULL,
    processing_timestamp_ms INTEGER NOT NULL,
    symbol TEXT NOT NULL, venue TEXT NOT NULL, timeframe TEXT NOT NULL,
    side TEXT NOT NULL, regime TEXT NOT NULL,
    policy_fingerprint TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    source TEXT NOT NULL,
    market_source_generation_id TEXT NOT NULL,
    repo_head TEXT NOT NULL, repo_tree TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    hash_basis_json TEXT NOT NULL,
    prev_event_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL,
    UNIQUE (run_id, seq),
    FOREIGN KEY (run_id) REFERENCES p11_runs(run_id),
    FOREIGN KEY (lifecycle_id) REFERENCES p11_lifecycles(lifecycle_id)
);

CREATE TABLE IF NOT EXISTS p11_outcomes (
    outcome_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    lifecycle_id TEXT NOT NULL UNIQUE,
    opportunity_id TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    candidate_trade_id TEXT NOT NULL UNIQUE,
    underlying_trade_id TEXT NOT NULL UNIQUE,
    hypothesis_id TEXT NOT NULL,
    global_event_id TEXT NOT NULL,
    dependency_cluster_id TEXT NOT NULL,
    signal_bar_ms INTEGER NOT NULL,
    entry_bar_ms INTEGER NOT NULL,
    exit_bar_ms INTEGER NOT NULL,
    entry_bar_id TEXT NOT NULL,
    exit_bar_id TEXT NOT NULL,
    entry_ts_ms INTEGER NOT NULL,
    exit_ts_ms INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    exit_reason TEXT NOT NULL,
    bars_held INTEGER NOT NULL,
    gross_pnl_eur REAL NOT NULL,
    net_pnl_eur REAL NOT NULL,
    fee_eur REAL NOT NULL,
    spread_eur REAL NOT NULL,
    slippage_eur REAL NOT NULL,
    funding_eur REAL NOT NULL,
    mfe_frac REAL NOT NULL,
    mae_frac REAL NOT NULL,
    finalization_status TEXT NOT NULL CHECK (finalization_status='FINAL'),
    censored INTEGER NOT NULL DEFAULT 0 CHECK (censored IN (0,1)),
    finalized_at_ms INTEGER NOT NULL,
    provenance_json TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES p11_runs(run_id),
    FOREIGN KEY (lifecycle_id) REFERENCES p11_lifecycles(lifecycle_id)
);

CREATE TABLE IF NOT EXISTS p11_labels (
    label_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    outcome_id TEXT NOT NULL UNIQUE,
    lifecycle_id TEXT NOT NULL UNIQUE,
    opportunity_id TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    candidate_trade_id TEXT NOT NULL,
    underlying_trade_id TEXT NOT NULL,
    hypothesis_id TEXT NOT NULL,
    global_event_id TEXT NOT NULL,
    dependency_cluster_id TEXT NOT NULL,
    entry_bar_id TEXT NOT NULL,
    exit_bar_id TEXT NOT NULL,
    label INTEGER CHECK (label IN (0,1) OR label IS NULL),
    label_name TEXT NOT NULL,
    label_method TEXT NOT NULL,
    finalization_status TEXT NOT NULL CHECK (finalization_status='FINAL'),
    finalized_at_ms INTEGER NOT NULL,
    provenance_json TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES p11_runs(run_id),
    FOREIGN KEY (outcome_id) REFERENCES p11_outcomes(outcome_id),
    FOREIGN KEY (lifecycle_id) REFERENCES p11_lifecycles(lifecycle_id)
);

CREATE TABLE IF NOT EXISTS p11_diagnostics (
    diagnostic_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    correlation_key TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN ('DETECTED','RESOLVED')),
    severity TEXT NOT NULL CHECK (severity IN ('INFO','WARNING','ERROR')),
    code TEXT NOT NULL,
    bar_open_ms INTEGER,
    lifecycle_id TEXT,
    details_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    UNIQUE (run_id, correlation_key, phase),
    FOREIGN KEY (run_id) REFERENCES p11_runs(run_id)
);

CREATE TABLE IF NOT EXISTS p11_checkpoints (
    run_id TEXT PRIMARY KEY,
    last_processed_bar_ms INTEGER,
    last_event_seq INTEGER NOT NULL DEFAULT 0,
    last_event_hash TEXT NOT NULL DEFAULT 'GENESIS',
    observer_status TEXT NOT NULL,
    observer_instance_id TEXT,
    lease_epoch INTEGER,
    heartbeat_ms INTEGER,
    last_error TEXT,
    updated_at_ms INTEGER NOT NULL,
    FOREIGN KEY (run_id) REFERENCES p11_runs(run_id)
);

CREATE TABLE IF NOT EXISTS p11_leases (
    run_id TEXT PRIMARY KEY,
    holder_id TEXT NOT NULL,
    lease_token TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    heartbeat_ms INTEGER NOT NULL,
    expires_ms INTEGER NOT NULL,
    FOREIGN KEY (run_id) REFERENCES p11_runs(run_id)
);

CREATE TRIGGER IF NOT EXISTS p11_runs_no_update
BEFORE UPDATE ON p11_runs BEGIN SELECT RAISE(ABORT, 'P11_RUN_IMMUTABLE'); END;
CREATE TRIGGER IF NOT EXISTS p11_runs_no_delete
BEFORE DELETE ON p11_runs BEGIN SELECT RAISE(ABORT, 'P11_RUN_IMMUTABLE'); END;
CREATE TRIGGER IF NOT EXISTS p11_bars_no_update
BEFORE UPDATE ON p11_bars BEGIN SELECT RAISE(ABORT, 'P11_BAR_IMMUTABLE'); END;
CREATE TRIGGER IF NOT EXISTS p11_bars_no_delete
BEFORE DELETE ON p11_bars BEGIN SELECT RAISE(ABORT, 'P11_BAR_IMMUTABLE'); END;
CREATE TRIGGER IF NOT EXISTS p11_events_no_update
BEFORE UPDATE ON p11_events BEGIN SELECT RAISE(ABORT, 'P11_EVENT_APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS p11_events_no_delete
BEFORE DELETE ON p11_events BEGIN SELECT RAISE(ABORT, 'P11_EVENT_APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS p11_outcomes_no_update
BEFORE UPDATE ON p11_outcomes BEGIN SELECT RAISE(ABORT, 'P11_OUTCOME_APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS p11_outcomes_no_delete
BEFORE DELETE ON p11_outcomes BEGIN SELECT RAISE(ABORT, 'P11_OUTCOME_APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS p11_labels_no_update
BEFORE UPDATE ON p11_labels BEGIN SELECT RAISE(ABORT, 'P11_LABEL_APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS p11_labels_no_delete
BEFORE DELETE ON p11_labels BEGIN SELECT RAISE(ABORT, 'P11_LABEL_APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS p11_diagnostics_no_update
BEFORE UPDATE ON p11_diagnostics BEGIN SELECT RAISE(ABORT, 'P11_DIAGNOSTIC_APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS p11_diagnostics_no_delete
BEFORE DELETE ON p11_diagnostics BEGIN SELECT RAISE(ABORT, 'P11_DIAGNOSTIC_APPEND_ONLY'); END;
"""


class ObserverStore:
    """Fenced SQLite store.  Every domain write is explicit and fail-closed."""

    def __init__(self, output_dir: Path | str = DEFAULT_OUTPUT_DIR, *,
                 now_ms: int | None = None, forward_start_ms: int | None = None,
                 provenance: dict[str, str] | None = None,
                 lease_ttl_ms: int = LEASE_TTL_MS) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.output_dir / DEFAULT_DB_NAME
        self.policy = load_policy_binding()
        self.config = _config_contract(self.policy)
        self.config_hash = _sha(self.config)
        self.market_source_generation_id = _sha({
            "source": SOURCE, "aggregation": "strict_1m_to_15m",
            "closed_only": True, "schema": SCHEMA_VERSION,
        })[:16]
        self.provenance = dict(provenance or _repo_provenance())
        self.instance_id = f"observer-{os.getpid()}-{uuid.uuid4().hex}"
        self.lease_token = uuid.uuid4().hex
        self.lease_epoch: int | None = None
        self.lease_ttl_ms = max(10_000, int(lease_ttl_ms))
        self._init_schema()
        frozen_at = int(now_ms if now_ms is not None else _now_ms())
        proposed_boundary = int(
            forward_start_ms if forward_start_ms is not None
            else _ceil_boundary(frozen_at)
        )
        if proposed_boundary % INTERVAL_MS != 0:
            raise ObserverError("FORWARD_BOUNDARY_MISALIGNED")
        self.run = self._freeze_or_load_run(proposed_boundary, frozen_at)
        self.run_id = str(self.run["run_id"])

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA_SQL)

    def _freeze_or_load_run(self, boundary: int, created_ms: int) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT * FROM p11_runs WHERE singleton=1"
                ).fetchone()
                if existing is not None:
                    row = dict(existing)
                    immutable_expected = {
                        "schema_version": SCHEMA_VERSION,
                        "policy_fingerprint": self.policy["policy_fingerprint"],
                        "participant_spec_hash": self.policy["participant_spec_hash"],
                        "callable_fingerprint": self.policy["callable_fingerprint"],
                        "config_hash": self.config_hash,
                        "repo_head": self.provenance.get("head", "UNAVAILABLE"),
                        "repo_tree": self.provenance.get("tree", "UNAVAILABLE"),
                    }
                    mismatch = [key for key, value in immutable_expected.items()
                                if row.get(key) != value]
                    if mismatch:
                        raise ObserverError(
                            "FROZEN_RUN_CONTRACT_MISMATCH:" + ",".join(mismatch)
                        )
                    conn.commit()
                    return row
                boundary_contract = {
                    "forward_start_ms": boundary,
                    "head": self.provenance.get("head", "UNAVAILABLE"),
                    "tree": self.provenance.get("tree", "UNAVAILABLE"),
                    "policy_fingerprint": self.policy["policy_fingerprint"],
                    "config_hash": self.config_hash,
                    "source": SOURCE,
                    "schema_version": SCHEMA_VERSION,
                }
                run_id = _deterministic_id("run", boundary_contract)
                contract_json = _canonical({
                    "boundary": boundary_contract,
                    "policy": self.policy,
                    "config": self.config,
                })
                conn.execute(
                    """INSERT INTO p11_runs (
                      run_id,singleton,schema_version,forward_start_ms,created_at_ms,
                      repo_head,repo_tree,policy_fingerprint,participant_spec_hash,
                      callable_fingerprint,config_hash,authority_registry_hash,
                      authority_reference_generation_id,market_source_generation_id,
                      source,symbol,venue,timeframe,side,contract_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, 1, SCHEMA_VERSION, boundary, created_ms,
                     boundary_contract["head"], boundary_contract["tree"],
                     self.policy["policy_fingerprint"],
                     self.policy["participant_spec_hash"],
                     self.policy["callable_fingerprint"], self.config_hash,
                     self.policy["authority_registry_hash"],
                     self.policy["authority_reference_generation_id"],
                     self.market_source_generation_id, SOURCE, SYMBOL, VENUE,
                     TIMEFRAME, SIDE, contract_json),
                )
                conn.execute(
                    """INSERT INTO p11_checkpoints (
                      run_id,observer_status,updated_at_ms
                    ) VALUES (?,?,?)""",
                    (run_id, "WAITING_FOR_FIRST_CLOSED_BAR", created_ms),
                )
                conn.commit()
                return dict(conn.execute(
                    "SELECT * FROM p11_runs WHERE run_id=?", (run_id,)
                ).fetchone())
            except Exception:
                conn.rollback()
                raise

    @property
    def forward_start_ms(self) -> int:
        return int(self.run["forward_start_ms"])

    def acquire_or_renew_lease(self, now_ms: int) -> int:
        now_ms = int(now_ms)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM p11_leases WHERE run_id=?", (self.run_id,)
                ).fetchone()
                if row is None:
                    epoch = 1
                    conn.execute(
                        "INSERT INTO p11_leases VALUES (?,?,?,?,?,?)",
                        (self.run_id, self.instance_id, self.lease_token, epoch,
                         now_ms, now_ms + self.lease_ttl_ms),
                    )
                elif row["holder_id"] == self.instance_id \
                        and row["lease_token"] == self.lease_token \
                        and int(row["epoch"]) == int(self.lease_epoch or row["epoch"]):
                    epoch = int(row["epoch"])
                    conn.execute(
                        """UPDATE p11_leases SET heartbeat_ms=?,expires_ms=?
                           WHERE run_id=? AND holder_id=? AND lease_token=? AND epoch=?""",
                        (now_ms, now_ms + self.lease_ttl_ms, self.run_id,
                         self.instance_id, self.lease_token, epoch),
                    )
                elif int(row["expires_ms"]) <= now_ms:
                    epoch = int(row["epoch"]) + 1
                    conn.execute(
                        """UPDATE p11_leases SET holder_id=?,lease_token=?,epoch=?,
                           heartbeat_ms=?,expires_ms=? WHERE run_id=? AND epoch=?""",
                        (self.instance_id, self.lease_token, epoch, now_ms,
                         now_ms + self.lease_ttl_ms, self.run_id, int(row["epoch"])),
                    )
                else:
                    raise ObserverAlreadyRunning(
                        f"OBSERVER_ALREADY_RUNNING:{row['holder_id']}:epoch={row['epoch']}"
                    )
                self.lease_epoch = epoch
                conn.execute(
                    """UPDATE p11_checkpoints SET observer_instance_id=?,
                       lease_epoch=?,heartbeat_ms=?,updated_at_ms=? WHERE run_id=?""",
                    (self.instance_id, epoch, now_ms, now_ms, self.run_id),
                )
                conn.commit()
                return epoch
            except Exception:
                conn.rollback()
                raise

    def _assert_lease(self, conn: sqlite3.Connection, now_ms: int) -> None:
        if self.lease_epoch is None:
            raise ObserverAlreadyRunning("OBSERVER_LEASE_NOT_ACQUIRED")
        row = conn.execute(
            "SELECT * FROM p11_leases WHERE run_id=?", (self.run_id,)
        ).fetchone()
        if row is None or row["holder_id"] != self.instance_id \
                or row["lease_token"] != self.lease_token \
                or int(row["epoch"]) != self.lease_epoch \
                or int(row["expires_ms"]) <= int(now_ms):
            raise ObserverAlreadyRunning("OBSERVER_FENCED_OR_LEASE_EXPIRED")

    @contextmanager
    def transaction(self, now_ms: int) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._assert_lease(conn, int(now_ms))
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def release_lease(self, now_ms: int | None = None) -> None:
        if self.lease_epoch is None:
            return
        released_ms = int(now_ms if now_ms is not None else _now_ms())
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            deleted = conn.execute(
                """DELETE FROM p11_leases WHERE run_id=? AND holder_id=?
                   AND lease_token=? AND epoch=?""",
                (self.run_id, self.instance_id, self.lease_token,
                 self.lease_epoch),
            )
            if deleted.rowcount == 1:
                conn.execute(
                    """UPDATE p11_checkpoints SET heartbeat_ms=?,updated_at_ms=?
                       WHERE run_id=?""",
                    (released_ms, released_ms, self.run_id),
                )
            conn.commit()
        self.lease_epoch = None

    def checkpoint(self) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM p11_checkpoints WHERE run_id=?", (self.run_id,)
            ).fetchone()
            return dict(row)

    def record_diagnostic(self, *, correlation_key: str, phase: str,
                          severity: str, code: str, now_ms: int,
                          bar_open_ms: int | None = None,
                          lifecycle_id: str | None = None,
                          details: dict[str, Any] | None = None) -> None:
        diagnostic_id = _deterministic_id(
            "diagnostic", self.run_id, correlation_key, phase
        )
        with self.transaction(now_ms) as conn:
            conn.execute(
                """INSERT OR IGNORE INTO p11_diagnostics (
                  diagnostic_id,run_id,correlation_key,phase,severity,code,
                  bar_open_ms,lifecycle_id,details_json,created_at_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (diagnostic_id, self.run_id, correlation_key, phase, severity,
                 code, bar_open_ms, lifecycle_id,
                 _canonical(details or {}), int(now_ms)),
            )

    def ingest_bars(self, bars: Iterable[dict[str, Any]], now_ms: int) -> int:
        inserted = 0
        with self.transaction(now_ms) as conn:
            for bar in bars:
                payload = _bar_payload(bar)
                payload_hash = _sha(payload)
                existing = conn.execute(
                    """SELECT payload_hash FROM p11_bars
                       WHERE run_id=? AND bar_open_ms=?""",
                    (self.run_id, payload["ts"]),
                ).fetchone()
                if existing is not None:
                    if existing["payload_hash"] != payload_hash:
                        raise ObserverDataError(
                            f"BAR_PAYLOAD_CONFLICT:{payload['ts']}"
                        )
                    continue
                conn.execute(
                    """INSERT INTO p11_bars (
                      run_id,bar_open_ms,availability_ms,open,high,low,close,
                      volume,payload_hash,is_forward,first_seen_ms
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (self.run_id, payload["ts"], payload["ts"] + INTERVAL_MS,
                     payload["open"], payload["high"], payload["low"],
                     payload["close"], payload["volume"], payload_hash,
                     int(payload["ts"] >= self.forward_start_ms), int(now_ms)),
                )
                inserted += 1
        return inserted

    def bars_between(self, start_ms: int, end_ms: int,
                     conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        own = conn is None
        connection = conn or self.connect()
        try:
            rows = connection.execute(
                """SELECT bar_open_ms AS ts,open,high,low,close,volume
                   FROM p11_bars WHERE run_id=? AND bar_open_ms BETWEEN ? AND ?
                   ORDER BY bar_open_ms""",
                (self.run_id, int(start_ms), int(end_ms)),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            if own:
                connection.close()

    @staticmethod
    def lifecycle_ids(run_id: str, policy_fingerprint: str,
                      signal_bar_ms: int) -> dict[str, str]:
        global_event_id = f"{SYMBOL}:{int(signal_bar_ms)}"
        opportunity_id = _deterministic_id(
            "opportunity", run_id, policy_fingerprint, global_event_id
        )
        lifecycle_id = _deterministic_id("lifecycle", opportunity_id)
        return {
            "global_event_id": global_event_id,
            "opportunity_id": opportunity_id,
            "signal_id": _deterministic_id("signal", opportunity_id),
            "lifecycle_id": lifecycle_id,
            "candidate_trade_id": _deterministic_id("candidate", lifecycle_id),
            "hypothesis_id": HYPOTHESIS_ID,
            "dependency_cluster_id": event_clock.cluster_id_tf(
                SYMBOL, int(signal_bar_ms), TIMEFRAME
            ),
        }

    def _event_key(self, lifecycle_id: str, event_type: str,
                   bar_open_ms: int) -> str:
        return _sha([self.run_id, lifecycle_id, event_type, int(bar_open_ms)])

    def _append_event(
            self, conn: sqlite3.Connection, lifecycle: dict[str, Any], *,
            event_type: str, from_state: str | None, to_state: str,
            bar_open_ms: int, event_timestamp_ms: int,
            availability_timestamp_ms: int, processing_timestamp_ms: int,
            payload: dict[str, Any]) -> bool:
        event_key = self._event_key(
            lifecycle["lifecycle_id"], event_type, bar_open_ms
        )
        existing = conn.execute(
            "SELECT event_id FROM p11_events WHERE event_key=?", (event_key,)
        ).fetchone()
        if existing is not None:
            return False
        checkpoint = conn.execute(
            "SELECT last_event_seq,last_event_hash FROM p11_checkpoints WHERE run_id=?",
            (self.run_id,),
        ).fetchone()
        seq = int(checkpoint["last_event_seq"]) + 1
        prev_hash = str(checkpoint["last_event_hash"])
        payload_json = _canonical(payload)
        basis = {
            "event_key": event_key, "run_id": self.run_id, "seq": seq,
            "schema_version": SCHEMA_VERSION, "event_type": event_type,
            "lifecycle_id": lifecycle["lifecycle_id"],
            "opportunity_id": lifecycle["opportunity_id"],
            "signal_id": lifecycle["signal_id"],
            "candidate_trade_id": lifecycle["candidate_trade_id"],
            "hypothesis_id": lifecycle["hypothesis_id"],
            "global_event_id": lifecycle["global_event_id"],
            "dependency_cluster_id": lifecycle["dependency_cluster_id"],
            "underlying_trade_id": lifecycle.get("underlying_trade_id"),
            "entry_bar_id": lifecycle.get("entry_bar_id"),
            "exit_bar_id": lifecycle.get("exit_bar_id"),
            "from_state": from_state, "to_state": to_state,
            "finalization_status": (
                "FINAL" if event_type in FINAL_EVENT_TYPES else "INTERMEDIATE"
            ),
            "bar_open_ms": int(bar_open_ms),
            "event_timestamp_ms": int(event_timestamp_ms),
            "availability_timestamp_ms": int(availability_timestamp_ms),
            "processing_timestamp_ms": int(processing_timestamp_ms),
            "symbol": SYMBOL, "venue": VENUE, "timeframe": TIMEFRAME,
            "side": SIDE, "regime": lifecycle["regime"],
            "policy_fingerprint": self.run["policy_fingerprint"],
            "config_hash": self.run["config_hash"],
            "source": self.run["source"],
            "market_source_generation_id": self.run[
                "market_source_generation_id"
            ],
            "repo_head": self.run["repo_head"],
            "repo_tree": self.run["repo_tree"],
            "payload": payload,
        }
        hash_basis_json = _canonical(basis)
        event_hash = _sha(prev_hash + "|" + hash_basis_json)
        event_id = _deterministic_id("event", event_key)
        conn.execute(
            """INSERT INTO p11_events (
              event_id,event_key,run_id,seq,schema_version,event_type,
              lifecycle_id,opportunity_id,signal_id,candidate_trade_id,
              hypothesis_id,global_event_id,dependency_cluster_id,
              underlying_trade_id,entry_bar_id,exit_bar_id,from_state,to_state,
              finalization_status,bar_open_ms,event_timestamp_ms,availability_timestamp_ms,
              processing_timestamp_ms,symbol,venue,timeframe,side,regime,
              policy_fingerprint,config_hash,source,market_source_generation_id,
              repo_head,repo_tree,payload_json,hash_basis_json,prev_event_hash,event_hash
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, event_key, self.run_id, seq, SCHEMA_VERSION, event_type,
             lifecycle["lifecycle_id"], lifecycle["opportunity_id"],
             lifecycle["signal_id"], lifecycle["candidate_trade_id"],
             lifecycle["hypothesis_id"], lifecycle["global_event_id"],
             lifecycle["dependency_cluster_id"],
             lifecycle.get("underlying_trade_id"), lifecycle.get("entry_bar_id"),
             lifecycle.get("exit_bar_id"), from_state, to_state,
             basis["finalization_status"], int(bar_open_ms), int(event_timestamp_ms),
             int(availability_timestamp_ms), int(processing_timestamp_ms),
             SYMBOL, VENUE, TIMEFRAME, SIDE, lifecycle["regime"],
             self.run["policy_fingerprint"], self.run["config_hash"],
             self.run["source"], self.run["market_source_generation_id"],
             self.run["repo_head"], self.run["repo_tree"], payload_json,
             hash_basis_json, prev_hash, event_hash),
        )
        conn.execute(
            """UPDATE p11_checkpoints SET last_event_seq=?,last_event_hash=?,
               updated_at_ms=? WHERE run_id=?""",
            (seq, event_hash, int(processing_timestamp_ms), self.run_id),
        )
        return True

    def _advance(
            self, conn: sqlite3.Connection, lifecycle_id: str, *,
            to_state: str, event_type: str, bar_open_ms: int,
            event_timestamp_ms: int, availability_timestamp_ms: int,
            processing_timestamp_ms: int, payload: dict[str, Any],
            updates: dict[str, Any] | None = None) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM p11_lifecycles WHERE lifecycle_id=?",
            (lifecycle_id,),
        ).fetchone()
        if row is None:
            raise InvalidTransition("LIFECYCLE_NOT_FOUND")
        lifecycle = dict(row)
        existing_event = conn.execute(
            """SELECT to_state,event_timestamp_ms,availability_timestamp_ms,
                      payload_json FROM p11_events WHERE event_key=?""",
            (self._event_key(lifecycle_id, event_type, bar_open_ms),),
        ).fetchone()
        if existing_event is not None:
            exact_replay = (
                existing_event["to_state"] == to_state
                and int(existing_event["event_timestamp_ms"])
                == int(event_timestamp_ms)
                and int(existing_event["availability_timestamp_ms"])
                == int(availability_timestamp_ms)
                and existing_event["payload_json"] == _canonical(payload)
            )
            if not exact_replay:
                raise ObserverError(
                    f"IDEMPOTENCY_CONFLICT:{event_type}:{bar_open_ms}"
                )
            return lifecycle
        from_state = str(lifecycle["state"])
        if (from_state, to_state) not in VALID_TRANSITIONS:
            raise InvalidTransition(f"INVALID_TRANSITION:{from_state}->{to_state}")
        allowed_updates = {
            "rejection_reason", "planned_entry_bar_ms", "underlying_trade_id",
            "entry_bar_id", "entry_ts_ms", "exit_bar_id", "exit_ts_ms",
            "last_bar_ms", "bars_observed", "mfe_frac", "mae_frac",
            "position_slot", "decision_action",
        }
        extra = dict(updates or {})
        unknown = set(extra) - allowed_updates
        if unknown:
            raise ObserverError("UNSAFE_LIFECYCLE_UPDATE:" + ",".join(sorted(unknown)))
        assignments = ["state=?", "version=version+1", "updated_at_ms=?"]
        values: list[Any] = [to_state, int(processing_timestamp_ms)]
        for key, value in extra.items():
            assignments.append(f"{key}=?")
            values.append(value)
        values.append(lifecycle_id)
        conn.execute(
            f"UPDATE p11_lifecycles SET {','.join(assignments)} WHERE lifecycle_id=?",
            tuple(values),
        )
        lifecycle = dict(conn.execute(
            "SELECT * FROM p11_lifecycles WHERE lifecycle_id=?",
            (lifecycle_id,),
        ).fetchone())
        self._append_event(
            conn, lifecycle, event_type=event_type, from_state=from_state,
            to_state=to_state, bar_open_ms=bar_open_ms,
            event_timestamp_ms=event_timestamp_ms,
            availability_timestamp_ms=availability_timestamp_ms,
            processing_timestamp_ms=processing_timestamp_ms, payload=payload,
        )
        return lifecycle

    def create_observation(self, conn: sqlite3.Connection, *, bar: dict[str, Any],
                           signal: dict[str, Any], decision: dict[str, Any],
                           processing_ms: int) -> dict[str, Any]:
        ts_ms = int(bar["ts"])
        ids = self.lifecycle_ids(
            self.run_id, self.run["policy_fingerprint"], ts_ms
        )
        canonical_signal = int(
            decision.get("decision_action") == "TRADE"
            and decision.get("side") == SIDE
        )
        regime = str(decision.get("regime") or "UNSPECIFIED")
        conn.execute(
            """INSERT INTO p11_lifecycles (
              lifecycle_id,run_id,opportunity_id,signal_id,candidate_trade_id,
              hypothesis_id,global_event_id,dependency_cluster_id,signal_bar_ms,
              availability_ms,state,canonical_signal,decision_action,regime,
              created_at_ms,updated_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ids["lifecycle_id"], self.run_id, ids["opportunity_id"],
             ids["signal_id"], ids["candidate_trade_id"], HYPOTHESIS_ID,
             ids["global_event_id"], ids["dependency_cluster_id"], ts_ms,
             ts_ms + INTERVAL_MS, "OBSERVED", canonical_signal,
             str(decision.get("decision_action") or "ABSTAIN_DATA_QUALITY"),
             regime, int(processing_ms), int(processing_ms)),
        )
        lifecycle = dict(conn.execute(
            "SELECT * FROM p11_lifecycles WHERE lifecycle_id=?",
            (ids["lifecycle_id"],),
        ).fetchone())
        self._append_event(
            conn, lifecycle, event_type="SIGNAL_OBSERVED", from_state=None,
            to_state="OBSERVED", bar_open_ms=ts_ms,
            event_timestamp_ms=ts_ms + INTERVAL_MS,
            availability_timestamp_ms=ts_ms + INTERVAL_MS,
            processing_timestamp_ms=processing_ms,
            payload={
                "canonical_signal": bool(canonical_signal),
                "decision_action": lifecycle["decision_action"],
                "decision_side": decision.get("side"),
                "reason_codes": decision.get("reason_codes") or [],
                "calibrated_probability": decision.get("calibrated_probability"),
                "market_bar": _bar_payload(bar),
                "cost_model": COST_SCENARIO,
                "signal_features": {
                    key: signal.get(key) for key in (
                        "ok", "atr_pct", "ret_15", "upper_wick", "slope"
                    )
                },
                "entry_bar_id": None, "exit_bar_id": None,
                "underlying_trade_id": None,
                "not_applicable_reason": (
                    None if canonical_signal else "P11_CONDITION_FALSE"
                ),
            },
        )
        return lifecycle

    def mark_checkpoint(self, conn: sqlite3.Connection, *, bar_open_ms: int,
                        processing_ms: int, status: str = "OBSERVER_CONNECTED",
                        last_error: str | None = None) -> None:
        conn.execute(
            """UPDATE p11_checkpoints SET last_processed_bar_ms=?,
               observer_status=?,heartbeat_ms=?,last_error=?,updated_at_ms=?
               WHERE run_id=?""",
            (int(bar_open_ms), status, int(processing_ms), last_error,
             int(processing_ms), self.run_id),
        )

    def set_heartbeat(self, now_ms: int, *, status: str,
                      last_error: str | None = None) -> None:
        with self.transaction(now_ms) as conn:
            conn.execute(
                """UPDATE p11_checkpoints SET observer_status=?,heartbeat_ms=?,
                   last_error=?,updated_at_ms=? WHERE run_id=?""",
                (status, int(now_ms), last_error, int(now_ms), self.run_id),
            )


class BitgetClosedBarProvider:
    """Public-only Bitget 1m adapter with strict causal 15m aggregation."""

    def __init__(self, log: Callable[[str], None] | None = None) -> None:
        self.log = log or (lambda _message: None)

    def fetch(self, *, now_ms: int, since_ms: int) -> list[dict[str, Any]]:
        span_ms = max(0, int(now_ms) - int(since_ms))
        days = max(1, math.ceil(span_ms / 86_400_000))
        transport_messages: list[str] = []

        def capture(message: str) -> None:
            text = str(message)
            transport_messages.append(text)
            self.log(text)

        rows = bitget_data.fetch_bitget_1m(
            SYMBOL, days=days, end_ms=int(now_ms), log=capture
        )
        http_errors = [message for message in transport_messages
                       if " HTTP " in f" {message} "]
        if http_errors:
            raise PublicDataUnavailable(
                "BITGET_PUBLIC_HTTP_ERROR:" + http_errors[-1][:200]
            )
        if not rows:
            raise PublicDataUnavailable("BITGET_PUBLIC_DATA_EMPTY")
        minute_by_ts: dict[int, dict[str, Any]] = {}
        for row in rows:
            if not bitget_data.validate_raw_candle(row):
                continue
            ts_ms = int(row[0])
            if ts_ms + 60_000 > int(now_ms):
                continue
            bar = {
                "ts": ts_ms, "open": float(row[1]), "high": float(row[2]),
                "low": float(row[3]), "close": float(row[4]),
                "volume": float(row[5]),
                "turnover": float(row[6]) if len(row) > 6 else 0.0,
                "available_at": ts_ms + 60_000,
                "symbol": SYMBOL, "venue": VENUE,
            }
            prior = minute_by_ts.get(ts_ms)
            if prior is not None and _sha(prior) != _sha(bar):
                raise ObserverDataError(f"BITGET_1M_CONFLICT:{ts_ms}")
            minute_by_ts[ts_ms] = bar
        minute_bars = [minute_by_ts[key] for key in sorted(minute_by_ts)]
        aggregated = _resample_closed_15m(minute_bars, as_of_ms=int(now_ms))
        if not aggregated:
            raise PublicDataUnavailable("BITGET_STRICT_15M_DATA_UNAVAILABLE")
        return aggregated


class P11ShortForwardObserver:
    """One-position continuous forward state machine for canonical P11_SHORT."""

    def __init__(
            self, output_dir: Path | str = DEFAULT_OUTPUT_DIR, *,
            now_ms: int | None = None, forward_start_ms: int | None = None,
            provenance: dict[str, str] | None = None,
            provider: Any | None = None,
            lease_ttl_ms: int = LEASE_TTL_MS) -> None:
        self.store = ObserverStore(
            output_dir, now_ms=now_ms, forward_start_ms=forward_start_ms,
            provenance=provenance, lease_ttl_ms=lease_ttl_ms,
        )
        self.output_dir = self.store.output_dir
        self.provider = provider or BitgetClosedBarProvider()
        self._decider = families.family_decider(
            "P11", symbol=SYMBOL, venue=VENUE, timeframe=TIMEFRAME,
            gen_id=self.store.market_source_generation_id, direction=SIDE,
        )
        self._closed = False

    @property
    def run_id(self) -> str:
        return self.store.run_id

    @property
    def forward_start_ms(self) -> int:
        return self.store.forward_start_ms

    def _signal_and_decision(
            self, history: list[dict[str, Any]], bar_ts: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        recent = history[-families.SIG_LOOKBACK:]
        continuous = all(
            int(recent[index]["ts"]) - int(recent[index - 1]["ts"])
            == INTERVAL_MS
            for index in range(1, len(recent))
        )
        if len(recent) < 60 or not continuous:
            signal: dict[str, Any] = {
                "ok": False,
                "data_quality_reason": (
                    "WARMUP_LT_60" if len(recent) < 60 else "FEATURE_WINDOW_GAP"
                ),
            }
        else:
            signal = families._sig(recent)
        cluster = event_clock.cluster_id_tf(SYMBOL, bar_ts, TIMEFRAME)
        decision = self._decider(
            {"_sig": signal, "ts": bar_ts}, f"{SYMBOL}:{bar_ts}",
            bar_ts + INTERVAL_MS, cluster,
        )
        return signal, decision

    def _active_lifecycle(self, conn: sqlite3.Connection) -> dict[str, Any] | None:
        row = conn.execute(
            """SELECT * FROM p11_lifecycles
               WHERE run_id=? AND position_slot=1""",
            (self.run_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _entry_cost_snapshot(raw_entry: float) -> dict[str, float]:
        cost = sim_oms.COST_SCENARIOS[COST_SCENARIO]
        notional = float(sim_oms.MONEY_SCENARIOS[MONEY_SCENARIO]["notional_eur"])
        half_spread = float(cost["spread_bps"]) / 2 / 10_000.0
        slippage = float(cost["slippage_bps"]) / 10_000.0
        fee = notional * float(cost["taker_fee_bps"]) / 10_000.0
        return {
            "raw_entry_price": float(raw_entry),
            "shadow_fill_price": round(
                float(raw_entry) * (1 - half_spread - slippage), 8
            ),
            "entry_fee_eur": round(fee, 8),
            "entry_spread_eur": round(notional * half_spread, 8),
            "entry_slippage_eur": round(notional * slippage, 8),
        }

    @staticmethod
    def _ensure_finite_result(result: dict[str, Any]) -> None:
        required = (
            "entry_price", "exit_price", "gross_pnl_eur", "net_pnl_eur",
            "fee_eur", "spread_eur", "slippage_eur", "funding_eur",
            "mfe_frac", "mae_frac",
        )
        for key in required:
            value = result.get(key)
            if type(value) not in (int, float) or isinstance(value, bool) \
                    or not math.isfinite(float(value)):
                raise ObserverError(f"NONFINITE_OUTCOME_FIELD:{key}")

    def _simulate_active(self, conn: sqlite3.Connection,
                         lifecycle: dict[str, Any], bar: dict[str, Any],
                         processing_ms: int) -> dict[str, Any]:
        entry_ts = int(lifecycle["entry_ts_ms"])
        lifecycle_bars = self.store.bars_between(
            entry_ts, int(bar["ts"]), conn=conn
        )
        expected_count = (int(bar["ts"]) - entry_ts) // INTERVAL_MS + 1
        if len(lifecycle_bars) != expected_count:
            raise ObserverDataError("OPEN_POSITION_BAR_GAP")
        result = sim_oms.simulate_trade(
            side=SIDE, entry_bar=lifecycle_bars[0],
            exit_bars=lifecycle_bars[1:], entry_ts_ms=entry_ts,
            stop_frac=float(EXPECTED_EXIT["stop_frac"]),
            tp_frac=float(EXPECTED_EXIT["tp_frac"]),
            time_exit=int(EXPECTED_EXIT["time_exit"]),
            scenario_money=MONEY_SCENARIO, scenario_cost=COST_SCENARIO,
            interval_ms=INTERVAL_MS,
        )
        if result.get("status") != "OK":
            raise ObserverError(f"SIM_OMS_{result.get('status')}:{result.get('reason')}")
        exit_reason = str(result.get("exit_reason"))
        complete = exit_reason in {"SL", "TP", "TIME"}
        if exit_reason == "END" and len(lifecycle_bars) >= EXPECTED_EXIT["time_exit"]:
            raise ObserverError("SIM_OMS_END_AFTER_FULL_HORIZON")
        if exit_reason not in {"SL", "TP", "TIME", "END"}:
            raise ObserverError(f"NONCANONICAL_EXIT_REASON:{exit_reason}")
        lifecycle = self.store._advance(
            conn, lifecycle["lifecycle_id"], to_state="OPEN_SHADOW",
            event_type="SHADOW_POSITION_UPDATED",
            bar_open_ms=int(bar["ts"]),
            event_timestamp_ms=int(bar["ts"]) + INTERVAL_MS,
            availability_timestamp_ms=int(bar["ts"]) + INTERVAL_MS,
            processing_timestamp_ms=processing_ms,
            payload={
                "bars_observed": len(lifecycle_bars),
                "position_open": not complete,
                "provisional_exit_reason": None if complete else "END_NOT_FINAL",
                "last_close": float(bar["close"]),
                "stop_price": result.get("stop_price"),
                "tp_price": result.get("tp_price"),
                "mfe_frac": result.get("mfe_frac"),
                "mae_frac": result.get("mae_frac"),
            },
            updates={
                "last_bar_ms": int(bar["ts"]),
                "bars_observed": len(lifecycle_bars),
                "mfe_frac": float(result["mfe_frac"]),
                "mae_frac": float(result["mae_frac"]),
            },
        )
        if not complete:
            return lifecycle
        self._ensure_finite_result(result)
        exit_bar_ms = int(bar["ts"])
        exit_bar_id = _bar_id(exit_bar_ms)
        lifecycle = self.store._advance(
            conn, lifecycle["lifecycle_id"], to_state="EXITED",
            event_type="SHADOW_EXITED", bar_open_ms=exit_bar_ms,
            event_timestamp_ms=int(result["exit_ts_ms"]),
            availability_timestamp_ms=exit_bar_ms + INTERVAL_MS,
            processing_timestamp_ms=processing_ms,
            payload={
                "exit_reason": exit_reason,
                "exit_price": float(result["exit_price"]),
                "stop_first": True,
                "gap_aware": True,
                "bars_held": int(result["bars_held"]),
            },
            updates={"exit_bar_id": exit_bar_id,
                     "exit_ts_ms": int(result["exit_ts_ms"]),
                     "position_slot": None},
        )
        outcome_id = _deterministic_id("outcome", lifecycle["lifecycle_id"])
        provenance = {
            "schema_version": SCHEMA_VERSION,
            "repo_head": self.store.run["repo_head"],
            "repo_tree": self.store.run["repo_tree"],
            "policy_fingerprint": self.store.run["policy_fingerprint"],
            "config_hash": self.store.run["config_hash"],
            "source": SOURCE,
            "market_source_generation_id": self.store.run[
                "market_source_generation_id"
            ],
        }
        conn.execute(
            """INSERT INTO p11_outcomes (
              outcome_id,run_id,lifecycle_id,opportunity_id,signal_id,
              candidate_trade_id,underlying_trade_id,hypothesis_id,
              global_event_id,dependency_cluster_id,signal_bar_ms,
              entry_bar_ms,exit_bar_ms,entry_bar_id,exit_bar_id,entry_ts_ms,exit_ts_ms,
              entry_price,exit_price,exit_reason,bars_held,gross_pnl_eur,
              net_pnl_eur,fee_eur,spread_eur,slippage_eur,funding_eur,
              mfe_frac,mae_frac,finalization_status,censored,finalized_at_ms,
              provenance_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (outcome_id, self.run_id, lifecycle["lifecycle_id"],
             lifecycle["opportunity_id"], lifecycle["signal_id"],
             lifecycle["candidate_trade_id"], lifecycle["underlying_trade_id"],
             lifecycle["hypothesis_id"], lifecycle["global_event_id"],
             lifecycle["dependency_cluster_id"], lifecycle["signal_bar_ms"],
             lifecycle["entry_ts_ms"], exit_bar_ms, lifecycle["entry_bar_id"],
             exit_bar_id,
             lifecycle["entry_ts_ms"], int(result["exit_ts_ms"]),
             float(result["entry_price"]), float(result["exit_price"]),
             exit_reason, int(result["bars_held"]),
             float(result["gross_pnl_eur"]), float(result["net_pnl_eur"]),
             float(result["fee_eur"]), float(result["spread_eur"]),
             float(result["slippage_eur"]), float(result["funding_eur"]),
             float(result["mfe_frac"]), float(result["mae_frac"]),
             "FINAL", 0, int(processing_ms), _canonical(provenance)),
        )
        lifecycle = self.store._advance(
            conn, lifecycle["lifecycle_id"], to_state="OUTCOME_FINALIZED",
            event_type="OUTCOME_FINALIZED", bar_open_ms=exit_bar_ms,
            event_timestamp_ms=int(result["exit_ts_ms"]),
            availability_timestamp_ms=exit_bar_ms + INTERVAL_MS,
            processing_timestamp_ms=processing_ms,
            payload={"outcome_id": outcome_id,
                     "finalization_status": "FINAL", **result},
        )
        label = 1 if float(result["net_pnl_eur"]) > 0 else 0
        label_id = _deterministic_id("label", outcome_id)
        conn.execute(
            """INSERT INTO p11_labels (
              label_id,run_id,outcome_id,lifecycle_id,opportunity_id,signal_id,
              candidate_trade_id,underlying_trade_id,hypothesis_id,global_event_id,
              dependency_cluster_id,entry_bar_id,exit_bar_id,label,label_name,
              label_method,finalization_status,finalized_at_ms,provenance_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (label_id, self.run_id, outcome_id, lifecycle["lifecycle_id"],
             lifecycle["opportunity_id"], lifecycle["signal_id"],
             lifecycle["candidate_trade_id"], lifecycle["underlying_trade_id"],
             lifecycle["hypothesis_id"], lifecycle["global_event_id"],
             lifecycle["dependency_cluster_id"], lifecycle["entry_bar_id"],
             lifecycle["exit_bar_id"], label, "WIN" if label else "NON_WIN",
             "NET_PNL_POSITIVE_V1",
             "FINAL", int(processing_ms), _canonical(provenance)),
        )
        lifecycle = self.store._advance(
            conn, lifecycle["lifecycle_id"], to_state="LABEL_FINALIZED",
            event_type="LABEL_FINALIZED", bar_open_ms=exit_bar_ms,
            event_timestamp_ms=int(result["exit_ts_ms"]),
            availability_timestamp_ms=exit_bar_ms + INTERVAL_MS,
            processing_timestamp_ms=processing_ms,
            payload={"outcome_id": outcome_id, "label_id": label_id,
                     "label": label, "label_name": "WIN" if label else "NON_WIN",
                     "finalization_status": "FINAL"},
        )
        return lifecycle

    def _update_or_open_active(self, conn: sqlite3.Connection,
                               bar: dict[str, Any], processing_ms: int) -> None:
        lifecycle = self._active_lifecycle(conn)
        if lifecycle is None:
            return
        if lifecycle["state"] == "ENTRY_PLANNED":
            expected = int(lifecycle["planned_entry_bar_ms"])
            if int(bar["ts"]) != expected:
                raise ObserverDataError(
                    f"PLANNED_ENTRY_BAR_MISSING:expected={expected}:got={bar['ts']}"
                )
            raw_entry = float(bar["open"])
            fill = self._entry_cost_snapshot(raw_entry)
            lifecycle = self.store._advance(
                conn, lifecycle["lifecycle_id"], to_state="OPEN_SHADOW",
                event_type="SHADOW_ENTRY_OPENED", bar_open_ms=int(bar["ts"]),
                event_timestamp_ms=int(bar["ts"]),
                availability_timestamp_ms=int(bar["ts"]) + INTERVAL_MS,
                processing_timestamp_ms=processing_ms,
                payload={
                    **fill, "entry_bar_id": lifecycle["entry_bar_id"],
                    "underlying_trade_id": lifecycle["underlying_trade_id"],
                    "notional_eur": sim_oms.MONEY_SCENARIOS[MONEY_SCENARIO][
                        "notional_eur"
                    ],
                    "leverage_simulated": sim_oms.MONEY_SCENARIOS[
                        MONEY_SCENARIO
                    ]["leverage"],
                    "orders_sent": 0,
                },
                updates={"entry_ts_ms": int(bar["ts"]),
                         "last_bar_ms": int(bar["ts"])},
            )
        if lifecycle["state"] != "OPEN_SHADOW":
            raise InvalidTransition(
                f"ACTIVE_SLOT_WITH_INVALID_STATE:{lifecycle['state']}"
            )
        self._simulate_active(conn, lifecycle, bar, processing_ms)

    def _evaluate_current(self, conn: sqlite3.Connection,
                          bar: dict[str, Any], history: list[dict[str, Any]],
                          processing_ms: int) -> None:
        ts_ms = int(bar["ts"])
        signal, decision = self._signal_and_decision(history, ts_ms)
        lifecycle = self.store.create_observation(
            conn, bar=bar, signal=signal, decision=decision,
            processing_ms=processing_ms,
        )
        canonical_signal = bool(lifecycle["canonical_signal"])
        reason: str | None = None
        if not canonical_signal:
            reason = str(
                signal.get("data_quality_reason") or "P11_CONDITION_FALSE"
            )
        elif self._active_lifecycle(conn) is not None:
            reason = "POSITION_ALREADY_OPEN"
        else:
            prior_cluster_entry = conn.execute(
                """SELECT 1 FROM p11_lifecycles WHERE run_id=?
                   AND dependency_cluster_id=? AND entry_ts_ms IS NOT NULL
                   LIMIT 1""",
                (self.run_id, lifecycle["dependency_cluster_id"]),
            ).fetchone()
            if prior_cluster_entry is not None:
                reason = "CLUSTER_COOLDOWN"
        if reason is not None:
            self.store._advance(
                conn, lifecycle["lifecycle_id"], to_state="REJECTED_FINAL",
                event_type="SIGNAL_REJECTED", bar_open_ms=ts_ms,
                event_timestamp_ms=ts_ms + INTERVAL_MS,
                availability_timestamp_ms=ts_ms + INTERVAL_MS,
                processing_timestamp_ms=processing_ms,
                payload={"reason": reason,
                         "canonical_signal": canonical_signal,
                         "finalization_status": "FINAL"},
                updates={"rejection_reason": reason},
            )
            return
        lifecycle = self.store._advance(
            conn, lifecycle["lifecycle_id"], to_state="ELIGIBLE",
            event_type="SIGNAL_ELIGIBLE", bar_open_ms=ts_ms,
            event_timestamp_ms=ts_ms + INTERVAL_MS,
            availability_timestamp_ms=ts_ms + INTERVAL_MS,
            processing_timestamp_ms=processing_ms,
            payload={"eligibility": "FIRST_CAUSAL_SIGNAL_SINGLE_POSITION",
                     "canonical_signal": True},
        )
        entry_ts = ts_ms + INTERVAL_MS
        underlying_trade_id = _deterministic_id(
            "underlying_trade", self.run_id, VENUE, SYMBOL, TIMEFRAME, SIDE,
            entry_ts,
        )
        entry_bar_id = _bar_id(entry_ts)
        self.store._advance(
            conn, lifecycle["lifecycle_id"], to_state="ENTRY_PLANNED",
            event_type="SHADOW_ENTRY_PLANNED", bar_open_ms=ts_ms,
            event_timestamp_ms=ts_ms + INTERVAL_MS,
            availability_timestamp_ms=ts_ms + INTERVAL_MS,
            processing_timestamp_ms=processing_ms,
            payload={
                "planned_entry_bar_ms": entry_ts,
                "entry_bar_id": entry_bar_id,
                "underlying_trade_id": underlying_trade_id,
                "entry_rule": "NEXT_BAR_OPEN_FIRST_CAUSAL",
                "stop_frac": EXPECTED_EXIT["stop_frac"],
                "tp_frac": EXPECTED_EXIT["tp_frac"],
                "time_exit": EXPECTED_EXIT["time_exit"],
                "trailing": None,
                "max_positions": MAX_POSITION,
                "orders_sent": 0,
            },
            updates={"planned_entry_bar_ms": entry_ts,
                     "underlying_trade_id": underlying_trade_id,
                     "entry_bar_id": entry_bar_id,
                     "position_slot": 1},
        )

    def _process_forward_bar(self, bar: dict[str, Any],
                             processing_ms: int) -> None:
        with self.store.transaction(processing_ms) as conn:
            checkpoint = conn.execute(
                "SELECT * FROM p11_checkpoints WHERE run_id=?", (self.run_id,)
            ).fetchone()
            last_processed = checkpoint["last_processed_bar_ms"]
            expected = self.forward_start_ms if last_processed is None \
                else int(last_processed) + INTERVAL_MS
            if int(bar["ts"]) < expected:
                return
            if int(bar["ts"]) != expected:
                raise ObserverDataError(
                    f"FORWARD_BAR_GAP:expected={expected}:got={bar['ts']}"
                )
            self._update_or_open_active(conn, bar, processing_ms)
            history_start = int(bar["ts"]) - (families.SIG_LOOKBACK - 1) * INTERVAL_MS
            history = self.store.bars_between(
                history_start, int(bar["ts"]), conn=conn
            )
            self._evaluate_current(conn, bar, history, processing_ms)
            self.store.mark_checkpoint(
                conn, bar_open_ms=int(bar["ts"]),
                processing_ms=processing_ms, status="OBSERVER_CONNECTED",
            )

    def _resolve_public_data_outages(self, now_ms: int) -> None:
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT detected.correlation_key FROM p11_diagnostics detected
                   LEFT JOIN p11_diagnostics resolved
                     ON resolved.run_id=detected.run_id
                    AND resolved.correlation_key=detected.correlation_key
                    AND resolved.phase='RESOLVED'
                   WHERE detected.run_id=? AND detected.phase='DETECTED'
                     AND detected.code LIKE 'BITGET_%'
                     AND resolved.diagnostic_id IS NULL""",
                (self.run_id,),
            ).fetchall()
        for row in rows:
            self.store.record_diagnostic(
                correlation_key=row["correlation_key"], phase="RESOLVED",
                severity="INFO", code="BITGET_PUBLIC_DATA_RECOVERED",
                now_ms=now_ms,
            )

    def poll_once(self, *, now_ms: int | None = None,
                  bars: Iterable[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Fetch/process every newly closed bar, then reconcile and export.

        `bars` is an injectable public-bar fixture used by deterministic tests;
        production always leaves it as ``None`` and uses the public provider.
        """
        if self._closed:
            raise ObserverError("OBSERVER_CLOSED")
        processing_ms = int(now_ms if now_ms is not None else _now_ms())
        self.store.acquire_or_renew_lease(processing_ms)
        checkpoint = self.store.checkpoint()
        last_processed = checkpoint.get("last_processed_bar_ms")
        next_expected_open = self.forward_start_ms if last_processed is None \
            else int(last_processed) + INTERVAL_MS
        since_ms = (
            self.forward_start_ms - families.SIG_LOOKBACK * INTERVAL_MS
            if last_processed is None else int(last_processed)
        )
        explicit = bars is not None
        if not explicit and processing_ms < next_expected_open + INTERVAL_MS:
            self.store.set_heartbeat(
                processing_ms,
                status=("WAITING_FOR_FIRST_CLOSED_BAR"
                        if last_processed is None else "OBSERVER_CONNECTED"),
            )
            return self._publish(processing_ms)
        try:
            incoming = list(bars) if explicit else list(self.provider.fetch(
                now_ms=processing_ms, since_ms=since_ms
            ))
            if not explicit:
                self._resolve_public_data_outages(processing_ms)
            validated: list[dict[str, Any]] = []
            previous_ts: int | None = None
            seen: dict[int, str] = {}
            for raw in incoming:
                bar = _validate_bar(raw)
                ts_ms = int(bar["ts"])
                if previous_ts is not None and ts_ms < previous_ts:
                    raise ObserverDataError("BAR_TIMESTAMP_OUT_OF_ORDER")
                previous_ts = ts_ms
                payload_hash = _sha(_bar_payload(bar))
                if ts_ms in seen:
                    if seen[ts_ms] != payload_hash:
                        raise ObserverDataError(f"DUPLICATE_BAR_CONFLICT:{ts_ms}")
                    continue
                seen[ts_ms] = payload_hash
                if ts_ms + INTERVAL_MS <= processing_ms:
                    validated.append(bar)
            if validated:
                self.store.ingest_bars(validated, processing_ms)
            checkpoint = self.store.checkpoint()
            expected = self.forward_start_ms \
                if checkpoint.get("last_processed_bar_ms") is None \
                else int(checkpoint["last_processed_bar_ms"]) + INTERVAL_MS
            # Drive from durable bars, not merely this fetch. If a later bar was
            # persisted while an earlier gap was pending, supplying the missing
            # bar once is enough to resume and catch up deterministically.
            latest_closed_open = (processing_ms // INTERVAL_MS - 1) * INTERVAL_MS
            durable_forward = self.store.bars_between(
                expected, latest_closed_open
            ) if latest_closed_open >= expected else []
            forward = {int(bar["ts"]): bar for bar in durable_forward}
            if not validated and not forward:
                status = (
                    "WAITING_FOR_FIRST_CLOSED_BAR"
                    if checkpoint.get("last_processed_bar_ms") is None
                    else "WAITING_FOR_DATA"
                )
                self.store.set_heartbeat(processing_ms, status=status)
                return self._publish(processing_ms)
            processed = 0
            while expected in forward:
                gap_key = f"forward-gap:{expected}"
                with self.store.connect() as conn:
                    detected = conn.execute(
                        """SELECT 1 FROM p11_diagnostics WHERE run_id=?
                           AND correlation_key=? AND phase='DETECTED'""",
                        (self.run_id, gap_key),
                    ).fetchone()
                    resolved = conn.execute(
                        """SELECT 1 FROM p11_diagnostics WHERE run_id=?
                           AND correlation_key=? AND phase='RESOLVED'""",
                        (self.run_id, gap_key),
                    ).fetchone()
                if detected and not resolved:
                    self.store.record_diagnostic(
                        correlation_key=gap_key, phase="RESOLVED",
                        severity="INFO", code="FORWARD_BAR_GAP_RECOVERED",
                        now_ms=processing_ms, bar_open_ms=expected,
                    )
                self._process_forward_bar(forward[expected], processing_ms)
                processed += 1
                expected += INTERVAL_MS
            later = [ts for ts in forward if ts > expected]
            if later:
                gap_key = f"forward-gap:{expected}"
                self.store.record_diagnostic(
                    correlation_key=gap_key, phase="DETECTED",
                    severity="ERROR", code="FORWARD_BAR_GAP_PENDING",
                    now_ms=processing_ms, bar_open_ms=expected,
                    details={"first_available_later_bar": min(later)},
                )
                self.store.set_heartbeat(
                    processing_ms, status="WAITING_FOR_DATA_GAP",
                    last_error=f"FORWARD_BAR_GAP:{expected}",
                )
            elif processed == 0 and self.store.checkpoint().get(
                    "last_processed_bar_ms") is None:
                self.store.set_heartbeat(
                    processing_ms, status="WAITING_FOR_FIRST_CLOSED_BAR"
                )
            else:
                self.store.set_heartbeat(
                    processing_ms, status="OBSERVER_CONNECTED"
                )
            return self._publish(processing_ms)
        except Exception as exc:
            if isinstance(exc, PublicDataUnavailable) and not explicit:
                correlation = f"public-source:{processing_ms // INTERVAL_MS}"
                try:
                    self.store.record_diagnostic(
                        correlation_key=correlation, phase="DETECTED",
                        severity="WARNING", code=str(exc).split(":", 1)[0],
                        now_ms=processing_ms,
                        details={"error_type": type(exc).__name__,
                                 "message": str(exc)[:500]},
                    )
                    self.store.set_heartbeat(
                        processing_ms, status="WAITING_FOR_DATA",
                        last_error=f"{type(exc).__name__}:{str(exc)[:400]}",
                    )
                    return self._publish(processing_ms)
                except Exception:
                    return {
                        "observer_status": "WAITING_FOR_DATA",
                        "last_error": f"{type(exc).__name__}:{str(exc)[:400]}",
                    }
            code = str(exc).split(":", 1)[0] or type(exc).__name__
            correlation = f"poll-error:{code}:{processing_ms // INTERVAL_MS}"
            try:
                self.store.record_diagnostic(
                    correlation_key=correlation, phase="DETECTED",
                    severity="ERROR", code=code, now_ms=processing_ms,
                    details={"error_type": type(exc).__name__,
                             "message": str(exc)[:500]},
                )
                self.store.set_heartbeat(
                    processing_ms, status="HALTED_FAIL_CLOSED",
                    last_error=f"{type(exc).__name__}:{str(exc)[:400]}",
                )
                self._publish(processing_ms)
            except Exception:
                pass
            if explicit:
                raise
            return self.read_status() or {
                "observer_status": "HALTED_FAIL_CLOSED",
                "last_error": f"{type(exc).__name__}:{str(exc)[:400]}",
            }

    def _publish(self, now_ms: int) -> dict[str, Any]:
        report = reconcile_store(self.store, now_ms=now_ms)
        metrics = calculate_forward_metrics(self.store, report, now_ms=now_ms)
        exports = write_exports(self.store, report=report, metrics=metrics,
                                now_ms=now_ms)
        checkpoint = self.store.checkpoint()
        first_bar = checkpoint.get("last_processed_bar_ms")
        observer_state = str(checkpoint["observer_status"])
        ready_to_observe = (
            report["status"] == "PASS"
            and observer_state in {
                "WAITING_FOR_FIRST_CLOSED_BAR", "OBSERVER_CONNECTED"
            }
        )
        activation_state = [
            "FORWARD_BOUNDARY_FROZEN",
            f"RECONCILIATION={report['status']}",
        ]
        if ready_to_observe:
            activation_state = [
                "OBSERVER_CONNECTED", *activation_state,
                "START_FORWARD_SHADOW_NOW",
            ]
            recommendation = "START_FORWARD_SHADOW_NOW"
        else:
            activation_state = [
                "OBSERVER_BLOCKED_FAIL_CLOSED", *activation_state,
                "WAIT_FOR_OBSERVER_RECOVERY",
            ]
            recommendation = "WAIT_FOR_OBSERVER_RECOVERY"
        status = {
            "schema_version": SCHEMA_VERSION,
            "observer_status": observer_state,
            "activation_state": activation_state,
            "recommendation": recommendation,
            "scientific_status": "NO_CONFIRMED_EDGE_RESEARCH_ONLY",
            "identity": {
                "run_id": self.run_id, "observer_instance_id": self.store.instance_id,
                "symbol": SYMBOL, "venue": VENUE, "timeframe": TIMEFRAME,
                "hypothesis_id": HYPOTHESIS_ID, "side": SIDE,
                "mode": "FORWARD_SHADOW", "max_simulated_positions": 1,
                "orders_allowed": False,
            },
            "boundary": {
                "forward_start_timestamp": _utc_iso(self.forward_start_ms),
                "forward_start_ms": self.forward_start_ms,
                "first_forward_bar_open_ms": self.forward_start_ms,
                "historical_snapshots_imported": 0,
                "bootstrap_is_feature_only": True,
            },
            "provenance": {
                "head": self.store.run["repo_head"],
                "tree": self.store.run["repo_tree"],
                "policy_fingerprint": self.store.run["policy_fingerprint"],
                "participant_spec_hash": self.store.run["participant_spec_hash"],
                "config_hash": self.store.run["config_hash"],
                "observer_code_fingerprint": self.store.config[
                    "observer_code_fingerprint"
                ],
                "source": SOURCE,
                "market_source_generation_id": self.store.run[
                    "market_source_generation_id"
                ],
                "authority_reference_generation_id": self.store.run[
                    "authority_reference_generation_id"
                ],
                "authority_role": "PARENT_REFERENCE_ONLY",
            },
            "heartbeat": {
                "observer_heartbeat": _utc_iso(checkpoint.get("heartbeat_ms")),
                "observer_heartbeat_ms": checkpoint.get("heartbeat_ms"),
                "observer_lag_seconds": metrics["observer_lag_seconds"],
                "last_closed_bar": metrics["last_closed_bar"],
                "last_error": checkpoint.get("last_error"),
            },
            "metrics": metrics,
            "reconciliation": report,
            "exports": exports,
            "safety": {
                "research_only": True, "paper_execution_enabled": False,
                "live_execution_enabled": False, "dry_run": True,
                "can_send_real_orders": False, "private_endpoints_used": False,
                "wallet_used": False, "holdout_opened": False,
            },
            "first_observation_status": (
                observer_state if first_bar is None
                else "FIRST_FORWARD_BAR_OBSERVED"
            ),
            "generated_at": _utc_iso(now_ms),
        }
        _atomic_write_text(
            self.output_dir / "observer_status.json",
            json.dumps(status, indent=2, ensure_ascii=False, allow_nan=False),
        )
        return status

    def read_status(self) -> dict[str, Any] | None:
        path = self.output_dir / "observer_status.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except Exception:
            return None

    def close(self) -> None:
        if self._closed:
            return
        self.store.release_lease()
        self._closed = True

    def __enter__(self) -> "P11ShortForwardObserver":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def _pending_diagnostics(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT detected.* FROM p11_diagnostics detected
           LEFT JOIN p11_diagnostics resolved
             ON resolved.run_id=detected.run_id
            AND resolved.correlation_key=detected.correlation_key
            AND resolved.phase='RESOLVED'
           WHERE detected.run_id=? AND detected.phase='DETECTED'
             AND detected.severity='ERROR' AND resolved.diagnostic_id IS NULL
           ORDER BY detected.created_at_ms""",
        (run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def reconcile_store(store: ObserverStore, *, now_ms: int | None = None) -> dict[str, Any]:
    """Rebuild invariants from append-only truth and compare projections."""
    generated_ms = int(now_ms if now_ms is not None else _now_ms())
    with store.connect() as conn:
        state_rows = conn.execute(
            """SELECT state,COUNT(*) AS n FROM p11_lifecycles
               WHERE run_id=? GROUP BY state""",
            (store.run_id,),
        ).fetchall()
        by_state = {row["state"]: int(row["n"]) for row in state_rows}
        for state in ALL_STATES:
            by_state.setdefault(state, 0)
        total = sum(by_state.values())
        partition = {
            "rejected_final": by_state["REJECTED_FINAL"],
            "observed_pending": by_state["OBSERVED"],
            "eligible_pending": by_state["ELIGIBLE"],
            "entry_planned": by_state["ENTRY_PLANNED"],
            "open_shadow": by_state["OPEN_SHADOW"],
            "exited_unfinalized": by_state["EXITED"],
            "outcome_finalized_unlabeled": by_state["OUTCOME_FINALIZED"],
            "label_finalized": by_state["LABEL_FINALIZED"],
            "structured_error_final": by_state["ERROR_FINAL"],
        }
        partition_sum = sum(partition.values())
        event_rows = conn.execute(
            "SELECT * FROM p11_events WHERE run_id=? ORDER BY seq",
            (store.run_id,),
        ).fetchall()
        chain_errors: list[str] = []
        previous_hash = GENESIS_HASH
        for expected_seq, row in enumerate(event_rows, 1):
            if int(row["seq"]) != expected_seq:
                chain_errors.append(
                    f"SEQ_GAP:expected={expected_seq}:got={row['seq']}"
                )
            if row["prev_event_hash"] != previous_hash:
                chain_errors.append(f"PREV_HASH_MISMATCH:seq={row['seq']}")
            recomputed = _sha(previous_hash + "|" + row["hash_basis_json"])
            if recomputed != row["event_hash"]:
                chain_errors.append(f"EVENT_HASH_MISMATCH:seq={row['seq']}")
            previous_hash = row["event_hash"]
        checkpoint = dict(conn.execute(
            "SELECT * FROM p11_checkpoints WHERE run_id=?", (store.run_id,)
        ).fetchone())
        last_bar_ms = checkpoint.get("last_processed_bar_ms")
        processed_bar_rows: list[sqlite3.Row] = []
        expected_processed_bars = 0
        if last_bar_ms is not None:
            last_bar_int = int(last_bar_ms)
            if last_bar_int < store.forward_start_ms \
                    or (last_bar_int - store.forward_start_ms) % INTERVAL_MS:
                chain_errors.append("CHECKPOINT_BAR_MISALIGNED")
            else:
                expected_processed_bars = (
                    (last_bar_int - store.forward_start_ms) // INTERVAL_MS + 1
                )
            processed_bar_rows = conn.execute(
                """SELECT bar_open_ms FROM p11_bars WHERE run_id=?
                   AND is_forward=1 AND bar_open_ms BETWEEN ? AND ?
                   ORDER BY bar_open_ms""",
                (store.run_id, store.forward_start_ms, last_bar_int),
            ).fetchall()
        processed_bar_count = len(processed_bar_rows)
        processed_bar_timestamps = [int(row["bar_open_ms"])
                                    for row in processed_bar_rows]
        expected_timestamps = [
            store.forward_start_ms + index * INTERVAL_MS
            for index in range(expected_processed_bars)
        ]
        bar_continuity_errors = (
            [] if processed_bar_timestamps == expected_timestamps
            else ["PROCESSED_FORWARD_BAR_CONTINUITY_MISMATCH"]
        )
        bars_without_lifecycle = int(conn.execute(
            """SELECT COUNT(*) FROM p11_bars b
               LEFT JOIN p11_lifecycles l ON l.run_id=b.run_id
                 AND l.signal_bar_ms=b.bar_open_ms
               WHERE b.run_id=? AND b.is_forward=1
                 AND (? IS NOT NULL AND b.bar_open_ms BETWEEN ? AND ?)
                 AND l.lifecycle_id IS NULL""",
            (store.run_id, last_bar_ms, store.forward_start_ms,
             int(last_bar_ms) if last_bar_ms is not None
             else store.forward_start_ms - INTERVAL_MS),
        ).fetchone()[0])
        lifecycles_without_processed_bar = int(conn.execute(
            """SELECT COUNT(*) FROM p11_lifecycles l
               LEFT JOIN p11_bars b ON b.run_id=l.run_id
                 AND b.bar_open_ms=l.signal_bar_ms AND b.is_forward=1
               WHERE l.run_id=? AND (b.bar_open_ms IS NULL
                 OR ? IS NULL OR l.signal_bar_ms>?)""",
            (store.run_id, last_bar_ms, last_bar_ms),
        ).fetchone()[0])
        if int(checkpoint["last_event_seq"]) != len(event_rows):
            chain_errors.append("CHECKPOINT_EVENT_SEQ_MISMATCH")
        if checkpoint["last_event_hash"] != previous_hash:
            chain_errors.append("CHECKPOINT_EVENT_HASH_MISMATCH")
        projection_errors: list[str] = []
        lifecycle_rows = conn.execute(
            "SELECT lifecycle_id,state FROM p11_lifecycles WHERE run_id=?",
            (store.run_id,),
        ).fetchall()
        for lifecycle in lifecycle_rows:
            last_event = conn.execute(
                """SELECT to_state FROM p11_events WHERE run_id=?
                   AND lifecycle_id=? ORDER BY seq DESC LIMIT 1""",
                (store.run_id, lifecycle["lifecycle_id"]),
            ).fetchone()
            if last_event is None or last_event["to_state"] != lifecycle["state"]:
                projection_errors.append(
                    f"PROJECTION_STATE_MISMATCH:{lifecycle['lifecycle_id']}"
                )
        invalid_transitions = 0
        for row in event_rows:
            pair = (row["from_state"], row["to_state"])
            if row["event_type"] == "SIGNAL_OBSERVED":
                valid = pair == (None, "OBSERVED")
            else:
                valid = pair in VALID_TRANSITIONS
            if not valid:
                invalid_transitions += 1
        orphan_events = int(conn.execute(
            """SELECT COUNT(*) FROM p11_events e LEFT JOIN p11_lifecycles l
               ON l.lifecycle_id=e.lifecycle_id WHERE e.run_id=?
               AND l.lifecycle_id IS NULL""",
            (store.run_id,),
        ).fetchone()[0])
        orphan_outcomes = int(conn.execute(
            """SELECT COUNT(*) FROM p11_outcomes o LEFT JOIN p11_lifecycles l
               ON l.lifecycle_id=o.lifecycle_id WHERE o.run_id=?
               AND l.lifecycle_id IS NULL""",
            (store.run_id,),
        ).fetchone()[0])
        orphan_labels = int(conn.execute(
            """SELECT COUNT(*) FROM p11_labels x LEFT JOIN p11_outcomes o
               ON o.outcome_id=x.outcome_id WHERE x.run_id=?
               AND o.outcome_id IS NULL""",
            (store.run_id,),
        ).fetchone()[0])
        orphan_count = orphan_events + orphan_outcomes + orphan_labels
        duplicate_count = 0
        for table, key in (
                ("p11_events", "event_key"),
                ("p11_lifecycles", "opportunity_id"),
                ("p11_outcomes", "lifecycle_id"),
                ("p11_labels", "outcome_id")):
            duplicate_count += int(conn.execute(
                f"""SELECT COALESCE(SUM(n-1),0) FROM (
                     SELECT COUNT(*) AS n FROM {table} WHERE run_id=?
                     GROUP BY {key} HAVING COUNT(*)>1)""",
                (store.run_id,),
            ).fetchone()[0])
        outcomes = int(conn.execute(
            "SELECT COUNT(*) FROM p11_outcomes WHERE run_id=?",
            (store.run_id,),
        ).fetchone()[0])
        labels = int(conn.execute(
            "SELECT COUNT(*) FROM p11_labels WHERE run_id=?",
            (store.run_id,),
        ).fetchone()[0])
        entries = int(conn.execute(
            """SELECT COUNT(*) FROM p11_events WHERE run_id=?
               AND event_type='SHADOW_ENTRY_OPENED'""",
            (store.run_id,),
        ).fetchone()[0])
        exits = int(conn.execute(
            """SELECT COUNT(*) FROM p11_events WHERE run_id=?
               AND event_type='SHADOW_EXITED'""",
            (store.run_id,),
        ).fetchone()[0])
        signals = int(conn.execute(
            """SELECT COUNT(*) FROM p11_lifecycles WHERE run_id=?
               AND canonical_signal=1""",
            (store.run_id,),
        ).fetchone()[0])
        pending_diagnostics = _pending_diagnostics(conn, store.run_id)
        event_pre_boundary = int(conn.execute(
            """SELECT COUNT(*) FROM p11_lifecycles WHERE run_id=?
               AND signal_bar_ms<?""",
            (store.run_id, store.forward_start_ms),
        ).fetchone()[0])
        cardinality_errors: list[str] = []
        if exits != outcomes:
            cardinality_errors.append(f"EXITS_OUTCOMES:{exits}!={outcomes}")
        if outcomes != labels:
            cardinality_errors.append(f"OUTCOMES_LABELS:{outcomes}!={labels}")
        if entries < outcomes or entries > signals:
            cardinality_errors.append(
                f"ENTRY_CARDINALITY:entries={entries}:signals={signals}:outcomes={outcomes}"
            )
        if by_state["OPEN_SHADOW"] + by_state["ENTRY_PLANNED"] > 1:
            cardinality_errors.append("MAX_POSITION_EXCEEDED")
        bar_lifecycle_errors: list[str] = []
        if processed_bar_count != expected_processed_bars:
            bar_lifecycle_errors.append(
                f"PROCESSED_BAR_COUNT:{processed_bar_count}!={expected_processed_bars}"
            )
        if processed_bar_count != total:
            bar_lifecycle_errors.append(
                f"BARS_LIFECYCLES:{processed_bar_count}!={total}"
            )
        if bars_without_lifecycle:
            bar_lifecycle_errors.append(
                f"BARS_WITHOUT_LIFECYCLE:{bars_without_lifecycle}"
            )
        if lifecycles_without_processed_bar:
            bar_lifecycle_errors.append(
                f"LIFECYCLES_WITHOUT_PROCESSED_BAR:{lifecycles_without_processed_bar}"
            )
        issues = (
            chain_errors + projection_errors + cardinality_errors
            + bar_continuity_errors + bar_lifecycle_errors
            + (["PARTITION_MISMATCH"]
               if processed_bar_count != partition_sum else [])
            + ([f"ORPHANS:{orphan_count}"] if orphan_count else [])
            + ([f"DUPLICATES:{duplicate_count}"] if duplicate_count else [])
            + ([f"INVALID_TRANSITIONS:{invalid_transitions}"]
               if invalid_transitions else [])
            + ([f"PRE_BOUNDARY_LIFECYCLES:{event_pre_boundary}"]
               if event_pre_boundary else [])
            + ([f"PENDING_DIAGNOSTICS:{len(pending_diagnostics)}"]
               if pending_diagnostics else [])
        )
        lag_seconds: float | str = NA
        if last_bar_ms is not None:
            lag_seconds = round(max(
                0.0, (generated_ms - (int(last_bar_ms) + INTERVAL_MS)) / 1000.0
            ), 3)
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "PASS" if not issues else "FAIL",
            "equation": (
                "forward_opportunities = rejected_final + observed_pending + "
                "eligible_pending + entry_planned + open_shadow + "
                "exited_unfinalized + outcome_finalized_unlabeled + "
                "label_finalized + structured_error_final"
            ),
            "lhs_forward_opportunities": processed_bar_count,
            "rhs_partition_total": partition_sum,
            "partition": partition,
            "snapshots_total": processed_bar_count,
            "lifecycle_total": total,
            "expected_processed_bars": expected_processed_bars,
            "bars_without_lifecycle": bars_without_lifecycle,
            "lifecycles_without_processed_bar": lifecycles_without_processed_bar,
            "signals_p11_short": signals,
            "rejections": by_state["REJECTED_FINAL"],
            "shadow_entries": entries,
            "open_positions": by_state["OPEN_SHADOW"],
            "closed_outcomes": outcomes,
            "closed_labels": labels,
            "duplicate_count": duplicate_count,
            "orphan_count": orphan_count,
            "invalid_transition_count": invalid_transitions,
            "pending_structured_errors": len(pending_diagnostics),
            "pending_errors": [row["code"] for row in pending_diagnostics],
            "event_chain_count": len(event_rows),
            "event_chain_head": previous_hash,
            "last_closed_bar": (
                _utc_iso(int(last_bar_ms) + INTERVAL_MS)
                if last_bar_ms is not None else NA
            ),
            "last_processed_bar_ms": last_bar_ms,
            "max_lag_seconds": lag_seconds,
            "issues": issues,
            "generated_at": _utc_iso(generated_ms),
        }


def calculate_forward_metrics(store: ObserverStore, report: dict[str, Any], *,
                              now_ms: int | None = None) -> dict[str, Any]:
    generated_ms = int(now_ms if now_ms is not None else _now_ms())
    with store.connect() as conn:
        outcomes = [dict(row) for row in conn.execute(
            """SELECT * FROM p11_outcomes WHERE run_id=? AND censored=0
               ORDER BY entry_ts_ms""",
            (store.run_id,),
        ).fetchall()]
    n_raw = len(outcomes)
    sample: dict[str, Any]
    if not outcomes:
        sample = {
            "forward_n_eff": NA, "gross_pnl": NA, "net_pnl": NA,
            "fees": NA, "spread": NA, "slippage": NA, "funding": NA,
            "MFE": NA, "MAE": NA, "win_rate": NA, "payoff": NA,
            "profit_factor": NA,
        }
    else:
        trades = []
        for outcome in outcomes:
            entry_ts = int(outcome["entry_ts_ms"])
            signal_ts = int(outcome["signal_bar_ms"])
            trades.append({
                "entry_ts": entry_ts,
                "cluster": str(outcome["dependency_cluster_id"]),
                "session": event_clock.session_id(SYMBOL, signal_ts),
                "day": event_clock.day_id(SYMBOL, signal_ts),
                "opportunity_bar": signal_ts // INTERVAL_MS,
                "entry_bar": int(outcome["entry_bar_ms"]) // INTERVAL_MS,
                "exit_index": int(outcome["exit_bar_ms"]) // INTERVAL_MS,
                "net_eur": float(outcome["net_pnl_eur"]),
                "dependency_cluster_id": str(outcome["dependency_cluster_id"]),
                "underlying_trade_id": str(outcome["underlying_trade_id"]),
            })
        n_eff = causal_stats.n_eff_estimate(trades, timeframe=TIMEFRAME)
        net = [float(row["net_pnl_eur"]) for row in outcomes]
        wins = [value for value in net if value > 0]
        losses = [value for value in net if value <= 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        payoff: float | str = NA
        if wins and losses and sum(abs(value) for value in losses) > 0:
            payoff = round(
                (sum(wins) / len(wins))
                / (sum(abs(value) for value in losses) / len(losses)), 8
            )
        profit_factor: float | str = NA
        if gross_loss > 0:
            profit_factor = round(gross_profit / gross_loss, 8)
        sample = {
            "forward_n_eff": n_eff["n_eff_final"],
            "gross_pnl": round(sum(float(x["gross_pnl_eur"]) for x in outcomes), 8),
            "net_pnl": round(sum(net), 8),
            "fees": round(sum(float(x["fee_eur"]) for x in outcomes), 8),
            "spread": round(sum(float(x["spread_eur"]) for x in outcomes), 8),
            "slippage": round(sum(float(x["slippage_eur"]) for x in outcomes), 8),
            "funding": round(sum(float(x["funding_eur"]) for x in outcomes), 8),
            "MFE": round(sum(float(x["mfe_frac"]) for x in outcomes) / n_raw, 8),
            "MAE": round(sum(float(x["mae_frac"]) for x in outcomes) / n_raw, 8),
            "win_rate": round(len(wins) / n_raw, 8),
            "payoff": payoff, "profit_factor": profit_factor,
        }
    checkpoint_last = report.get("last_processed_bar_ms")
    observer_lag: float | str = NA
    if checkpoint_last is not None:
        observer_lag = round(max(
            0.0, (generated_ms - (int(checkpoint_last) + INTERVAL_MS)) / 1000.0
        ), 3)
    return {
        "forward_opportunities": report["lhs_forward_opportunities"],
        "forward_signals": report["signals_p11_short"],
        "forward_rejections": report["rejections"],
        "forward_entries": report["shadow_entries"],
        "forward_open_positions": report["open_positions"],
        "forward_closed_outcomes": report["closed_outcomes"],
        "forward_finalized_labels": report["closed_labels"],
        "forward_n_raw": n_raw,
        **sample,
        "time_exits": sum(1 for row in outcomes if row["exit_reason"] == "TIME"),
        "duplicate_count": report["duplicate_count"],
        "orphan_count": report["orphan_count"],
        "reconciliation_status": report["status"],
        "last_closed_bar": report["last_closed_bar"],
        "observer_heartbeat": _utc_iso(generated_ms),
        "observer_lag_seconds": observer_lag,
    }


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="")
    os.replace(temporary, path)


def _csv_text(rows: list[dict[str, Any]], fields: list[str]) -> str:
    import io
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return stream.getvalue()


def write_exports(store: ObserverStore, *, report: dict[str, Any],
                  metrics: dict[str, Any], now_ms: int) -> dict[str, str]:
    with store.connect() as conn:
        events = [dict(row) for row in conn.execute(
            "SELECT * FROM p11_events WHERE run_id=? ORDER BY seq",
            (store.run_id,),
        ).fetchall()]
        outcomes = [dict(row) for row in conn.execute(
            "SELECT * FROM p11_outcomes WHERE run_id=? ORDER BY entry_ts_ms",
            (store.run_id,),
        ).fetchall()]
        labels = [dict(row) for row in conn.execute(
            "SELECT * FROM p11_labels WHERE run_id=? ORDER BY finalized_at_ms",
            (store.run_id,),
        ).fetchall()]
    event_lines = []
    for event in events:
        value = dict(event)
        value["payload"] = json.loads(value.pop("payload_json"))
        value.pop("hash_basis_json", None)
        event_lines.append(_canonical(value))
    ledger_text = "\n".join(event_lines) + ("\n" if event_lines else "")
    outcome_fields = [
        "outcome_id", "lifecycle_id", "opportunity_id", "signal_id",
        "candidate_trade_id", "underlying_trade_id", "hypothesis_id",
        "global_event_id", "dependency_cluster_id", "signal_bar_ms",
        "entry_bar_ms", "exit_bar_ms", "entry_bar_id", "exit_bar_id",
        "entry_ts_ms", "exit_ts_ms",
        "entry_price", "exit_price", "exit_reason", "bars_held",
        "gross_pnl_eur", "net_pnl_eur", "fee_eur", "spread_eur",
        "slippage_eur", "funding_eur", "mfe_frac", "mae_frac",
        "finalization_status", "censored", "finalized_at_ms",
    ]
    label_fields = [
        "label_id", "outcome_id", "lifecycle_id", "opportunity_id",
        "signal_id", "candidate_trade_id", "underlying_trade_id",
        "hypothesis_id", "global_event_id", "dependency_cluster_id",
        "entry_bar_id", "exit_bar_id", "label", "label_name", "label_method",
        "finalization_status", "finalized_at_ms",
    ]
    paths = {
        "lifecycle_ledger": store.output_dir / "lifecycle_ledger.jsonl",
        "outcomes": store.output_dir / "outcomes.csv",
        "labels": store.output_dir / "labels.csv",
        "reconciliation_report": store.output_dir / "reconciliation_report.json",
        "summary": store.output_dir / "summary.txt",
    }
    _atomic_write_text(paths["lifecycle_ledger"], ledger_text)
    _atomic_write_text(paths["outcomes"], _csv_text(outcomes, outcome_fields))
    _atomic_write_text(paths["labels"], _csv_text(labels, label_fields))
    _atomic_write_text(
        paths["reconciliation_report"],
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False),
    )
    summary = "\n".join([
        "P11_SHORT FORWARD OBSERVER",
        f"generated_at={_utc_iso(now_ms)}",
        f"run_id={store.run_id}",
        f"forward_start_timestamp={_utc_iso(store.forward_start_ms)}",
        f"observer=BTCUSDT Bitget 15m P11_SHORT FORWARD_SHADOW",
        f"opportunities={metrics['forward_opportunities']}",
        f"signals={metrics['forward_signals']}",
        f"entries={metrics['forward_entries']}",
        f"open_positions={metrics['forward_open_positions']}",
        f"closed_outcomes={metrics['forward_closed_outcomes']}",
        f"labels={metrics['forward_finalized_labels']}",
        f"forward_n_eff={metrics['forward_n_eff']}",
        f"reconciliation={report['status']}",
        "scientific_status=NO_CONFIRMED_EDGE_RESEARCH_ONLY",
        "orders_sent=0",
        "recommendation=START_FORWARD_SHADOW_NOW",
    ]) + "\n"
    _atomic_write_text(paths["summary"], summary)
    return {key: path.name for key, path in paths.items()}


def run_observer_forever(
        *, poll_seconds: float = 60.0,
        stop_requested: Callable[[], bool] | None = None,
        output_dir: Path | str = DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
    """Own internal polling clock; suitable for the public research CLI."""
    stop = stop_requested or (lambda: False)
    interval = max(1.0, min(60.0, float(poll_seconds)))
    observer = P11ShortForwardObserver(output_dir=output_dir)
    cycles = 0
    last: dict[str, Any] = {}
    try:
        while not stop():
            cycle_start = time.monotonic()
            last = observer.poll_once()
            cycles += 1
            if stop():
                break
            remaining = interval - (time.monotonic() - cycle_start)
            if remaining > 0:
                time.sleep(remaining)
    except KeyboardInterrupt:
        pass
    finally:
        observer.close()
    return {
        "cycles": cycles, "last_status": last,
        "mode": "FORWARD_SHADOW", "research_only": True,
        "can_send_real_orders": False,
    }
