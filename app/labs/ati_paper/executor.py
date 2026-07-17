"""Persistent ATI forward paper executor (simulation only)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import (
    ACCOUNT_ID,
    DEFAULT_RUNTIME_DIR,
    DEFAULT_SIGNAL_PATH,
    DEFAULT_STATUS_PATH,
    EXECUTION_MODE,
    MODE,
    safety_envelope,
)
from .broker import AtiPaperBroker
from .config import AtiPaperConfig, InstrumentRule, load_config
from .ledger import AtiPaperLedger, utc_now
from .public_market import AtiPublicMarketError, BitgetPublicMarket, MarketTick

LOCK_PATH = DEFAULT_RUNTIME_DIR / "executor.lock"
STOP_PATH = DEFAULT_RUNTIME_DIR / "executor.stop"


def _commit_hash() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
            check=True, timeout=5,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file() or path.is_symlink():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"ATI_FORWARD_LEDGER_INVALID_JSON_LINE:{line_number}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"ATI_FORWARD_LEDGER_NON_OBJECT:{line_number}")
        rows.append(value)
    return rows


class SingleInstanceLock:
    def __init__(self, path: Path = LOCK_PATH):
        self.path = path
        self.handle: Any | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0)
        self.handle.write(b"0")
        self.handle.flush()
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - exercised on Linux CI only
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("ATI_PAPER_EXECUTOR_ALREADY_RUNNING") from exc
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()).encode("ascii"))
        self.handle.flush()

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, *_: Any) -> None:
        self.release()


class AtiPaperExecutor:
    def __init__(
        self, *, config: AtiPaperConfig | None = None,
        ledger: AtiPaperLedger | None = None, market: Any | None = None,
        signal_path: Path | str | None = None, status_path: Path | str | None = None,
        commit_hash: str | None = None,
    ):
        self.config = config or load_config()
        self.ledger = ledger or AtiPaperLedger()
        self.market = market or BitgetPublicMarket()
        self.signal_path = Path(signal_path) if signal_path is not None else DEFAULT_SIGNAL_PATH
        self.status_path = Path(status_path) if status_path is not None else DEFAULT_STATUS_PATH
        self.commit_hash = commit_hash or _commit_hash()
        self.broker = AtiPaperBroker(self.ledger, self.config, commit_hash=self.commit_hash)
        self.started_at = utc_now()
        self.stop_requested = False
        self.cycle = 0
        self.last_market_ts: str | None = None
        self.last_market_observed_at: str | None = None
        self.last_error: str | None = None
        self.last_signal_id: str | None = None
        self.last_trade_id: str | None = None
        self._source_ids_at_start: set[str] = set()
        self._last_watch_refresh_monotonic = 0.0

    def _instrument_rule(self, symbol: str) -> InstrumentRule:
        try:
            return self.market.instrument_rule(symbol)
        except Exception:
            fallback = self.config.instrument_rules.get(str(symbol).upper())
            if fallback is None:
                raise
            return fallback

    def initialize(self) -> dict[str, Any]:
        init = self.ledger.initialize(self.config, commit_hash=self.commit_hash)
        source = _read_jsonl(self.signal_path)
        self._source_ids_at_start = {str(row.get("signal_id") or "") for row in source if row.get("signal_id")}
        for row in source:
            signal_id = str(row.get("signal_id") or "")
            if not signal_id or self.ledger.signal(signal_id) is not None:
                continue
            try:
                self.ledger.observe_signal(row, commit_hash=self.commit_hash)
                self.ledger.reject_signal(
                    signal_id, "PREEXISTING_SIGNAL_NOT_OBSERVED_LIVE",
                    source_ts=str(row.get("decision_ts") or ""), commit_hash=self.commit_hash,
                )
            except Exception as exc:
                self.ledger.record_event(
                    event_type="ATI_SOURCE_SIGNAL_INVALID", correlation_id=signal_id or "UNKNOWN",
                    reason=str(exc)[:200], source_ts=str(row.get("decision_ts") or ""),
                    commit_hash=self.commit_hash, payload={"preexisting": True},
                )
        reconcile = self.ledger.reconcile()
        if reconcile.get("status") != "PASS":
            raise RuntimeError(f"ATI_PAPER_RECONCILIATION_FAILED:{reconcile.get('blockers')}")
        self.ledger.record_event(
            event_type="ATI_PAPER_RECONCILIATION", correlation_id=ACCOUNT_ID,
            previous_state="STARTING", new_state="PASS", reason="STARTUP_RECONCILIATION",
            source_ts=utc_now(), commit_hash=self.commit_hash, payload=reconcile,
        )
        self._write_status("WAITING_FOR_SIGNAL", reconciliation=reconcile)
        return init

    def _ingest_new_signals(self) -> int:
        seen = 0
        for row in _read_jsonl(self.signal_path):
            signal_id = str(row.get("signal_id") or "")
            if not signal_id or self.ledger.signal(signal_id) is not None:
                continue
            self.ledger.observe_signal(row, observed_at=utc_now(), commit_hash=self.commit_hash)
            self.last_signal_id = signal_id
            seen += 1
        return seen

    def _mark_tick(self, tick: MarketTick) -> None:
        self.last_market_ts = datetime.fromtimestamp(tick.source_ts_ms / 1000.0, tz=timezone.utc).isoformat()
        self.last_market_observed_at = tick.observed_at

    def _service_open_positions(self) -> tuple[int, bool]:
        closed = 0
        market_stale = False
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for position in self.ledger.open_positions():
            by_symbol.setdefault(str(position["symbol"]), []).append(position)
        now_ms = int(time.time() * 1000)
        for symbol, positions in by_symbol.items():
            try:
                after_values: list[int] = []
                for row in positions:
                    if row.get("last_processed_bar_ms") is not None:
                        after_values.append(int(row["last_processed_bar_ms"]))
                        continue
                    entry_ms = int(datetime.fromisoformat(
                        str(row["entry_source_ts"]).replace("Z", "+00:00")
                    ).timestamp() * 1000)
                    first_full = ((entry_ms + 59_999) // 60_000) * 60_000
                    after_values.append(first_full - 60_000)
                after_ms = min(after_values) if after_values else None
                bars = self.market.closed_bars(symbol, after_ms=after_ms, now_ms=now_ms, limit=1000)
                self.ledger.persist_market_bars(bars)
                for bar in bars:
                    for snapshot in list(positions):
                        result = self.broker.process_closed_bar(str(snapshot["position_id"]), bar)
                        if result.get("status") == "ATI_PAPER_POSITION_CLOSED":
                            closed += 1
                            self.last_trade_id = str(result.get("trade_id") or "") or self.last_trade_id
                tick = self.market.ticker(symbol)
                self._mark_tick(tick)
                source_age = time.time() - tick.source_ts_ms / 1000.0
                if source_age > self.config.market_data_stale_after_seconds or source_age < -5:
                    market_stale = True
                    continue
                for position in self.ledger.open_positions(symbol=symbol):
                    self.broker.mark_tick(str(position["position_id"]), tick)
            except AtiPublicMarketError as exc:
                market_stale = True
                self.last_error = str(exc)[:300]
                self.ledger.record_event(
                    event_type="MARKET_DATA_STALE", correlation_id=symbol,
                    reason=self.last_error, source_ts=utc_now(), commit_hash=self.commit_hash,
                    payload={"new_entries_blocked": True, "positions_preserved": True},
                    event_key=f"market_stale_{symbol}_{int(time.time() // 60)}",
                )
        return closed, market_stale

    def _refresh_watch_bars(self) -> bool:
        """Keep the dashboard chart fresh without manufacturing decisions.

        This is a bounded public-candle read at most once per 30 seconds. Bars
        are presentation/marking data only and are never converted to signals.
        """
        now_mono = time.monotonic()
        if now_mono - self._last_watch_refresh_monotonic < 30.0:
            return False
        self._last_watch_refresh_monotonic = now_mono
        stale = False
        now_ms = int(time.time() * 1000)
        for symbol in sorted(self.config.instrument_rules):
            latest = self.ledger.rows("market_bars", limit=1, symbol=symbol)
            after_ms = int(latest[0]["timestamp_ms"]) if latest else None
            try:
                bars = self.market.closed_bars(symbol, after_ms=after_ms, now_ms=now_ms, limit=300)
                self.ledger.persist_market_bars(bars)
                if bars:
                    newest = bars[-1]
                    self.last_market_ts = datetime.fromtimestamp(
                        newest.available_at_ms / 1000.0, tz=timezone.utc,
                    ).isoformat()
                    self.last_market_observed_at = utc_now()
            except (AtiPublicMarketError, OSError) as exc:
                stale = True
                self.last_error = str(exc)[:300]
        return stale

    def _service_pending_signals(self) -> tuple[int, bool]:
        opened = 0
        market_stale = False
        for signal in self.ledger.pending_signals():
            signal_id = str(signal["signal_id"])
            symbol = str(signal["symbol"])
            try:
                tick = self.market.ticker(symbol)
                self._mark_tick(tick)
                result = self.broker.open_from_signal(signal_id, tick, self._instrument_rule(symbol))
                if result.get("status") == "ATI_PAPER_POSITION_OPEN":
                    opened += 1
            except (AtiPublicMarketError, OSError) as exc:
                market_stale = True
                self.last_error = str(exc)[:300]
                self.ledger.record_event(
                    event_type="MARKET_DATA_STALE", correlation_id=signal_id,
                    signal_id=signal_id, reason=self.last_error, source_ts=utc_now(),
                    commit_hash=self.commit_hash,
                    payload={"entry_deferred": True, "signal_status_preserved": True},
                    event_key=f"market_stale_{signal_id}_{int(time.time() // 60)}",
                )
            except ValueError as exc:
                reason = str(exc)[:200]
                if reason == "ATI_PAPER_MARKET_DATA_STALE":
                    market_stale = True
                    self.last_error = reason
                    continue
                self.ledger.reject_signal(
                    signal_id, reason, source_ts=self.last_market_ts,
                    commit_hash=self.commit_hash,
                )
        return opened, market_stale

    def cycle_once(self) -> dict[str, Any]:
        self.cycle += 1
        self.last_error = None
        new_signals = self._ingest_new_signals()
        watch_stale = self._refresh_watch_bars()
        closed, open_stale = self._service_open_positions()
        opened, entry_stale = self._service_pending_signals()
        reconcile = self.ledger.reconcile()
        if reconcile.get("status") != "PASS":
            status = "ERROR"
            self.last_error = f"RECONCILIATION_FAILED:{reconcile.get('blockers')}"
        elif watch_stale or open_stale or entry_stale:
            status = "MARKET_DATA_STALE"
        elif self.ledger.open_positions():
            status = "HEALTHY"
        else:
            status = "WAITING_FOR_SIGNAL"
        return self._write_status(
            status, reconciliation=reconcile, cycle_metrics={
                "new_signals": new_signals, "opened": opened, "closed": closed,
            },
        )

    def _write_status(
        self, status: str, *, reconciliation: dict[str, Any] | None = None,
        cycle_metrics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        valid = {"HEALTHY", "WAITING_FOR_SIGNAL", "MARKET_DATA_STALE", "DEGRADED", "ERROR"}
        if status not in valid:
            status = "ERROR"
        account = self.ledger.account() or {}
        positions = self.ledger.open_positions()
        trades = self.ledger.rows("trades", limit=1)
        signals = self.ledger.rows("signals", limit=1)
        age = None
        if self.last_market_ts:
            try:
                age = max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(self.last_market_ts)).total_seconds())
            except ValueError:
                age = None
        payload = {
            "schema": "ati_paper_executor_status.v1",
            "component": "ATI_PAPER_EXECUTOR",
            "status": status,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "last_heartbeat": utc_now(),
            "cycle": self.cycle,
            "market_data_timestamp": self.last_market_ts,
            "market_data_observed_at": self.last_market_observed_at,
            "market_data_age_seconds": age,
            "data_stale": status == "MARKET_DATA_STALE",
            "account_id": ACCOUNT_ID,
            "initial_balance": account.get("initial_balance"),
            "realized_equity": account.get("realized_equity"),
            "total_equity": account.get("total_equity"),
            "open_positions": len(positions),
            "closed_trades": len(self.ledger.rows("trades", limit=5000)),
            "last_signal": signals[0].get("signal_id") if signals else None,
            "last_trade": trades[0].get("trade_id") if trades else None,
            "reconciliation": reconciliation or self.ledger.reconcile(),
            "ledger_status": "READY" if account else "NO_LEDGER",
            "last_error": self.last_error,
            "commit_hash": self.commit_hash,
            "policy_version": self.config.policy_version,
            "source_policy_version": self.config.source_policy_version,
            "sizing_policy": self.config.sizing_method,
            "configured_position_fraction": self.config.position_fraction,
            "cycle_metrics": cycle_metrics or {},
            **safety_envelope(),
        }
        _atomic_json(self.status_path, payload)
        return payload

    def run(self, *, max_cycles: int = 0) -> dict[str, Any]:
        with SingleInstanceLock():
            STOP_PATH.unlink(missing_ok=True)
            self.initialize()

            def request_stop(*_: Any) -> None:
                self.stop_requested = True

            signal.signal(signal.SIGINT, request_stop)
            signal.signal(signal.SIGTERM, request_stop)
            last: dict[str, Any] = {}
            while not self.stop_requested and not STOP_PATH.exists():
                started = time.monotonic()
                try:
                    last = self.cycle_once()
                except Exception as exc:
                    self.last_error = f"{type(exc).__name__}:{str(exc)[:260]}"
                    last = self._write_status("ERROR")
                if max_cycles > 0 and self.cycle >= max_cycles:
                    break
                elapsed = time.monotonic() - started
                time.sleep(max(0.2, self.config.poll_interval_seconds - elapsed))
            self.ledger.record_event(
                event_type="ATI_PAPER_EXECUTOR_STOPPED", correlation_id=ACCOUNT_ID,
                previous_state=str(last.get("status") or "UNKNOWN"), new_state="STOPPED",
                reason="CONTROLLED_STOP", source_ts=utc_now(), commit_hash=self.commit_hash,
                payload={"cycle": self.cycle, "positions_preserved": True},
            )
            return self._write_status("DEGRADED")


def read_executor_status(path: Path | str | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else DEFAULT_STATUS_PATH
    if not target.is_file() or target.is_symlink():
        return {"component": "ATI_PAPER_EXECUTOR", "status": "DEGRADED",
                "last_error": "STATUS_MISSING", **safety_envelope()}
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"component": "ATI_PAPER_EXECUTOR", "status": "ERROR",
                "last_error": "STATUS_INVALID", **safety_envelope()}
    return {**value, **safety_envelope()} if isinstance(value, dict) else {
        "component": "ATI_PAPER_EXECUTOR", "status": "ERROR",
        "last_error": "STATUS_NOT_OBJECT", **safety_envelope(),
    }
