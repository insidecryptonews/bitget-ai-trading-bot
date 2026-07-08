"""ResearchOps V10.43C - Bybit PERSISTENT public-trade WS collector (research only).

The V10.42 collector connects, streams ~60s, closes, sleeps 5s, reconnects. Every
reconnect punches an artificial hole in the tape -> the WS dataset stays TOO_GAPPY.
This module keeps ONE long-lived connection open, flushing continuously, and only
reconnects on a real failure (with controlled backoff). The tape stays contiguous.

Design for reliability + testability:
  * the network is fully injectable: `run_persistent(connect_fn=...)` drives the
    loop with a fake connect/recv, so the whole state machine is unit-tested with
    NO network at all;
  * `connect_and_run_live` is the ONLY function that touches the socket and is
    guarded behind the optional `websocket` dependency (never exercised by tests);
  * writes go to a SEPARATE dataset (bybit_trades_ws_persistent_v10_43c) so the
    V10.42 dataset is never touched; append is idempotent (dedup by trade_id);
  * a writer LOCK (pid + heartbeat, TTL-based) prevents two persistent collectors
    from writing the same dataset;
  * fail-closed everywhere: bad frame -> skip, bad field -> skip, wrong symbol ->
    skip, no connection -> no invented data.

Public market data only. NO keys, NO orders, NO private channels, NO .env, NO LIVE.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import bybit_trades_ws_collector_v10_41 as CORE
from . import bybit_trades_ws_collector_v10_42 as V42
from . import continuous_edge_factory_v10_38 as CE

TOOL_VERSION = "v10.43c"
WS_PERSISTENT_SUBDIR = ("external_data", "staging", "bybit_trades_ws_persistent_v10_43c")
SOURCE_DATASET = "ws_persistent_v10_43c"
WS_PUBLIC_LINEAR = CORE.WS_PUBLIC_LINEAR

DEFAULT_FLUSH_EVERY_N = 400          # flush buffer to disk every N new trades
DEFAULT_FLUSH_EVERY_S = 8           # or at least every N seconds while trades arrive
DEFAULT_STALE_S = 20               # no frames for 20s within a session -> stale/reconnect
DEFAULT_MAX_RUNTIME_S = 21_600     # 6h per live invocation (PS1 loop re-launches)
BACKOFF_S = (1, 2, 5, 10, 20, 30)  # reconnect backoff ladder (capped)
LOCK_TTL_S = 30                    # writer lock considered stale after this many s
HEALTH_STATES = ("HEALTHY", "DEGRADED", "STALE", "DISCONNECTED", "NO_DATA")


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "can_send_real_orders": False,
            "uses_api_keys": False, "subscribes_private_channels": False,
            "sends_orders": False, "not_actionable": True,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def subscribe_message(symbols: list[str]) -> dict:
    return V42.subscribe_message(symbols)


def _dataset_dir(base=None) -> Path:
    if base is not None:
        return Path(base)
    return CE._repo_root().joinpath(*WS_PERSISTENT_SUBDIR)


# ==========================================================================
# Writer lock (pid + heartbeat, TTL-based) -> no two persistent writers
# ==========================================================================

def _lock_path(data_dir) -> Path:
    return Path(data_dir) / "writer.lock"


def read_writer_lock(data_dir) -> dict | None:
    p = _lock_path(data_dir)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def acquire_writer_lock(data_dir, pid: int, now_ms: int,
                        ttl_s: int = LOCK_TTL_S) -> tuple[bool, dict | None]:
    """Acquire the writer lock unless a FRESH lock is held by another pid.
    A lock older than ttl is considered abandoned and can be stolen. Returns
    (acquired, current_holder_or_None)."""
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    cur = read_writer_lock(d)
    if cur is not None:
        fresh = (now_ms - int(cur.get("ts_ms", 0))) <= ttl_s * 1000
        if fresh and int(cur.get("pid", -1)) != int(pid):
            return False, cur
    _write_lock(d, pid, now_ms)
    return True, cur


def _write_lock(data_dir, pid: int, now_ms: int) -> None:
    p = _lock_path(data_dir)
    tmp = str(p) + ".tmp"
    Path(tmp).write_text(json.dumps({"pid": int(pid), "ts_ms": int(now_ms)}),
                         encoding="utf-8")
    os.replace(tmp, p)


def refresh_writer_lock(data_dir, pid: int, now_ms: int) -> None:
    _write_lock(data_dir, pid, now_ms)


def release_writer_lock(data_dir, pid: int) -> None:
    p = _lock_path(data_dir)
    cur = read_writer_lock(p.parent)
    if cur is not None and int(cur.get("pid", -1)) == int(pid):
        try:
            p.unlink()
        except Exception:
            pass


# ==========================================================================
# State + health (pure/testable)
# ==========================================================================

def new_state(symbols: list[str], start_ms: int, base=None) -> dict[str, Any]:
    return {"symbols": list(symbols), "start_ms": int(start_ms),
            "connected": False, "last_msg_ts": None,
            "messages_count": 0, "trades_count": 0,
            "duplicates_skipped": 0, "corrupt_rows_skipped": 0,
            "reconnect_count": 0, "n_flushes": 0,
            "seen": set(), "write_path": str(_dataset_dir(base) / "trades.csv"),
            "current_file_size": 0}


def compute_health(state: dict, now_ms: int, stale_s: int = DEFAULT_STALE_S
                   ) -> dict[str, Any]:
    """Turn the mutable collector state into an auditable health snapshot.
    Pure: no I/O, no clock of its own (now_ms injected)."""
    last = state.get("last_msg_ts")
    age_s = None if last is None else max(0.0, (now_ms - last) / 1000.0)
    connected = bool(state.get("connected"))
    msgs = state.get("messages_count", 0)
    corrupt = state.get("corrupt_rows_skipped", 0)
    recon = state.get("reconnect_count", 0)
    corrupt_ratio = (corrupt / msgs) if msgs else 0.0
    if msgs == 0 and last is None:
        status = "NO_DATA" if not connected else "DISCONNECTED"
    elif not connected:
        status = "DISCONNECTED"
    elif age_s is not None and age_s > stale_s:
        status = "STALE"
    elif corrupt_ratio > 0.05 or recon >= 3:
        status = "DEGRADED"
    else:
        status = "HEALTHY"
    uptime_s = max(0.0, (now_ms - state.get("start_ms", now_ms)) / 1000.0)
    return {"tool_version": TOOL_VERSION, "status": status,
            "connected": connected, "last_msg_ts": last,
            "age_seconds": None if age_s is None else round(age_s, 1),
            "reconnect_count": recon, "messages_count": msgs,
            "trades_count": state.get("trades_count", 0),
            "duplicates_skipped": state.get("duplicates_skipped", 0),
            "corrupt_rows_skipped": corrupt,
            "write_path": str(state.get("write_path", "")).replace("\\", "/"),
            "current_file_size": state.get("current_file_size", 0),
            "current_symbols": state.get("symbols", []),
            "uptime_seconds": round(uptime_s, 1),
            "source_dataset": SOURCE_DATASET, **_safety()}


def write_health(state: dict, now_ms: int, base=None,
                 stale_s: int = DEFAULT_STALE_S) -> dict[str, Any]:
    d = _dataset_dir(base)
    d.mkdir(parents=True, exist_ok=True)
    h = compute_health(state, now_ms, stale_s=stale_s)
    tmp = d / "health.json.tmp"
    tmp.write_text(json.dumps(h, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, d / "health.json")
    return h


def read_health(base=None) -> dict[str, Any]:
    d = _dataset_dir(base)
    p = d / "health.json"
    if not p.is_file():
        trades = d / "trades.csv"
        return {"tool_version": TOOL_VERSION,
                "status": "NO_DATA" if not trades.is_file() else "DISCONNECTED",
                "connected": False, "messages_count": 0, "trades_count": 0,
                "reconnect_count": 0, "duplicates_skipped": 0,
                "corrupt_rows_skipped": 0, "age_seconds": None,
                "current_symbols": [], "source_dataset": SOURCE_DATASET,
                "note": "no health.json yet (collector not started)", **_safety()}
    try:
        h = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"tool_version": TOOL_VERSION, "status": "NO_DATA",
                "read_error": True, **_safety()}
    # freshness of the health file itself (collector down if very old)
    age = (datetime.now(timezone.utc).timestamp() - p.stat().st_mtime)
    h["health_file_age_seconds"] = round(age, 1)
    if age > LOCK_TTL_S * 3 and h.get("status") not in ("NO_DATA",):
        h["status"] = "DISCONNECTED"
        h["note"] = "health.json stale -> collector process not running"
    return h


# ==========================================================================
# Streaming state machine (pure/testable via injected recv_fn)
# ==========================================================================

def _flush(buf: list[dict], state: dict, on_flush, now_ms: int) -> None:
    if not buf:
        return
    buf.sort(key=lambda r: int(r["timestamp"]))
    if on_flush:
        on_flush(list(buf))
    state["n_flushes"] += 1
    buf.clear()


def stream_session(recv_fn: Callable[[], Any], state: dict, on_flush, *,
                   now_ms_fn: Callable[[], int],
                   flush_every_n: int = DEFAULT_FLUSH_EVERY_N,
                   flush_every_s: int = DEFAULT_FLUSH_EVERY_S,
                   stale_s: int = DEFAULT_STALE_S,
                   max_msgs: int | None = None,
                   max_runtime_s: int | None = None,
                   idle_sleep_fn: Callable[[], None] | None = None) -> dict[str, Any]:
    """Consume frames from recv_fn for ONE connection session.

    recv_fn() returns a dict frame, or None to signal 'no data right now', or
    raises StopIteration (clean end) / any Exception (socket error -> reconnect).
    Dedup by trade_id; buffer; flush by count OR elapsed time. Updates `state`
    in place. Returns the reason the session ended."""
    state["connected"] = True
    start = now_ms_fn()
    last_flush = start
    buf: list[dict] = []
    smsgs = 0
    ended = "ok"
    while True:
        if max_msgs is not None and smsgs >= max_msgs:
            ended = "max_msgs"; break
        if max_runtime_s is not None and (now_ms_fn() - start) >= max_runtime_s * 1000:
            ended = "max_runtime"; break
        try:
            frame = recv_fn()
        except StopIteration:
            ended = "stream_end"; break
        except Exception:
            ended = "recv_error"; break
        now = now_ms_fn()
        if frame is None:                                   # idle tick
            if state.get("last_msg_ts") is not None and \
                    (now - state["last_msg_ts"]) > stale_s * 1000:
                ended = "stale"; break
            if buf and (now - last_flush) >= flush_every_s * 1000:
                _flush(buf, state, on_flush, now); last_flush = now
            if idle_sleep_fn:
                idle_sleep_fn()
            continue
        state["messages_count"] += 1
        smsgs += 1
        rows = CORE.parse_public_trade_message(frame) if isinstance(frame, dict) else []
        if not rows:
            topic = str(frame.get("topic", "")) if isinstance(frame, dict) else ""
            if topic.startswith("publicTrade"):            # looked like trades, bad payload
                state["corrupt_rows_skipped"] += 1
            continue                                       # control frame / ack -> ignore
        # keep only the symbols we subscribed to (defensive)
        wanted = {s.upper() for s in state.get("symbols", [])}
        rows = [r for r in rows if str(r.get("symbol", "")).upper() in wanted] if wanted else rows
        uniq, state["seen"] = CORE.dedup_by_trade_id(rows, state["seen"])
        state["duplicates_skipped"] += len(rows) - len(uniq)
        state["trades_count"] += len(uniq)
        buf.extend(uniq)
        state["last_msg_ts"] = now
        if len(buf) >= flush_every_n or (buf and (now - last_flush) >= flush_every_s * 1000):
            _flush(buf, state, on_flush, now); last_flush = now
    _flush(buf, state, on_flush, now_ms_fn())
    state["connected"] = False
    return {"ended": ended, "session_msgs": smsgs}


def run_persistent(connect_fn: Callable[[list[str]], Callable | None],
                   symbols: list[str], *,
                   now_ms_fn: Callable[[], int] | None = None,
                   on_flush: Callable[[list[dict]], None] | None = None,
                   base=None, pid: int | None = None,
                   max_sessions: int | None = None,
                   max_runtime_s: int | None = None,
                   session_max_runtime_s: int | None = None,
                   flush_every_n: int = DEFAULT_FLUSH_EVERY_N,
                   flush_every_s: int = DEFAULT_FLUSH_EVERY_S,
                   stale_s: int = DEFAULT_STALE_S,
                   backoff_sleep_fn: Callable[[int], None] | None = None,
                   idle_sleep_fn: Callable[[], None] | None = None,
                   write_health_file: bool = True,
                   use_lock: bool = True) -> dict[str, Any]:
    """Outer supervisor loop: hold the writer lock, run streaming sessions, and
    reconnect on failure with capped backoff. Fully injectable (connect_fn +
    now_ms_fn + sleep fns) so tests drive it with NO network.

    connect_fn(symbols) -> recv_fn (a callable used by stream_session) or None if
    the connection could not be established (-> counts as a reconnect + backoff)."""
    import time as _time
    now_ms_fn = now_ms_fn or (lambda: int(_time.time() * 1000))
    backoff_sleep_fn = backoff_sleep_fn or (lambda s: _time.sleep(s))
    pid = os.getpid() if pid is None else pid
    start_ms = now_ms_fn()
    state = new_state(symbols, start_ms, base=base)
    if use_lock:
        ok, holder = acquire_writer_lock(_dataset_dir(base), pid, start_ms, ttl_s=LOCK_TTL_S)
        if not ok:
            return {"tool_version": TOOL_VERSION, "status": "ALREADY_RUNNING",
                    "detail": "another persistent writer holds the lock",
                    "holder": holder, **_safety()}

    def flush_and_persist(rows: list[dict]) -> None:
        if on_flush:
            on_flush(rows)
        else:
            try:
                ap = V42.append_rows(rows, _dataset_dir(base))
                p = Path(ap["path"])
                state["current_file_size"] = p.stat().st_size if p.is_file() else 0
            except Exception:
                pass
        if use_lock:
            refresh_writer_lock(_dataset_dir(base), pid, now_ms_fn())
        if write_health_file:
            write_health(state, now_ms_fn(), base=base, stale_s=stale_s)

    sessions = 0
    try:
        while True:
            if max_sessions is not None and sessions >= max_sessions:
                break
            if max_runtime_s is not None and (now_ms_fn() - start_ms) >= max_runtime_s * 1000:
                break
            recv_fn = None
            try:
                recv_fn = connect_fn(symbols)
            except Exception:
                recv_fn = None
            if recv_fn is None:                             # connect failed
                state["reconnect_count"] += 1
                sessions += 1
                if write_health_file:
                    write_health(state, now_ms_fn(), base=base, stale_s=stale_s)
                backoff_sleep_fn(BACKOFF_S[min(state["reconnect_count"] - 1, len(BACKOFF_S) - 1)])
                continue
            res = stream_session(
                recv_fn, state, flush_and_persist, now_ms_fn=now_ms_fn,
                flush_every_n=flush_every_n, flush_every_s=flush_every_s,
                stale_s=stale_s, max_runtime_s=session_max_runtime_s,
                idle_sleep_fn=idle_sleep_fn)
            ws_handle = getattr(recv_fn, "_ws", None)
            if ws_handle is not None:
                try:
                    ws_handle.close()
                except Exception:
                    pass
            sessions += 1
            if write_health_file:
                write_health(state, now_ms_fn(), base=base, stale_s=stale_s)
            if res["ended"] in ("max_runtime", "max_msgs") and \
                    (max_sessions is None or sessions < max_sessions):
                # clean end of a bounded session, not a failure -> continue seamlessly
                continue
            if res["ended"] in ("recv_error", "stale", "stream_end"):
                state["reconnect_count"] += 1
                if max_sessions is None and max_runtime_s is None:
                    break                                   # unbounded caller handled elsewhere
                backoff_sleep_fn(BACKOFF_S[min(state["reconnect_count"] - 1, len(BACKOFF_S) - 1)])
    finally:
        if use_lock:
            release_writer_lock(_dataset_dir(base), pid)
    final = compute_health(state, now_ms_fn(), stale_s=stale_s)
    final["sessions_run"] = sessions
    return final


# ==========================================================================
# LIVE runner (the only network code; guarded, never tested)
# ==========================================================================

def connect_and_run_live(symbols: list[str], base=None,
                         max_runtime_s: int = DEFAULT_MAX_RUNTIME_S,
                         session_max_runtime_s: int = 900) -> dict[str, Any]:
    """Real persistent collection against Bybit v5 public linear ws. Guarded by
    the optional `websocket` dependency; fail-closed if missing. Public only, no
    auth, no orders. NOT exercised by tests."""
    try:
        import websocket  # type: ignore  # websocket-client
    except Exception:
        return {"tool_version": TOOL_VERSION, "status": "DEPENDENCY_MISSING",
                "detail": "websocket-client not importable; live collector unavailable",
                **_safety()}

    def connect_fn(syms):
        try:
            ws = websocket.create_connection(WS_PUBLIC_LINEAR, timeout=20)
            ws.settimeout(5)
            ws.send(json.dumps(subscribe_message(syms)))
        except Exception:
            return None

        def recv():
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:      # type: ignore
                return None                                  # idle tick (no frame yet)
            except Exception:
                raise                                        # socket error -> reconnect
            if not raw:
                return None
            try:
                return json.loads(raw)
            except Exception:
                return {"topic": "publicTrade._parse_error", "data": []}
        recv._ws = ws                                        # keep a handle for close
        return recv

    return run_persistent(
        connect_fn, symbols, base=base, max_runtime_s=max_runtime_s,
        session_max_runtime_s=session_max_runtime_s)


def status() -> dict[str, Any]:
    return {"tool_version": TOOL_VERSION,
            "core_from": "bybit_trades_ws_collector_v10_41",
            "append_from": "bybit_trades_ws_collector_v10_42",
            "persistent_loop_implemented": True, "live_connect_guarded": True,
            "endpoint": WS_PUBLIC_LINEAR, "dataset": "/".join(WS_PERSISTENT_SUBDIR),
            "how_to_run": "scripts/collect_bybit_trades_ws_persistent_forever.ps1",
            "improves_over": "v10.42 (no 60s reconnect holes; one long-lived socket)",
            "safety": "public only, no keys, no orders, research-only", **_safety()}
