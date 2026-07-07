"""ResearchOps V10.42 - Bybit public-trade WS collector RUNNER (research only).

Builds the continuous-collection loop on top of the V10.41 pure core
(parse/dedup/merge/heartbeat). The loop logic is TESTABLE via an injected frame
source (no network). The actual live connect is guarded behind an optional
`websocket` dependency and is NEVER exercised by tests. Public data only: no
keys, no orders, no private channels, no .env.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Iterable

from . import FINAL_RECOMMENDATION_NO_LIVE
from . import bybit_trades_ws_collector_v10_41 as CORE

TOOL_VERSION = "v10.42"
WS_PUBLIC_LINEAR = CORE.WS_PUBLIC_LINEAR
DEFAULT_FLUSH_EVERY = 500        # flush to dataset every N new trades
DEFAULT_STALE_MS = CORE.STALE_MS


def _safety() -> dict[str, Any]:
    return {"research_only": True, "shadow_only": True, "can_send_real_orders": False,
            "uses_api_keys": False, "subscribes_private_channels": False,
            "sends_orders": False, "not_actionable": True,
            "final_recommendation": FINAL_RECOMMENDATION_NO_LIVE}


def subscribe_message(symbols: list[str]) -> dict:
    """Bybit v5 public subscribe frame for publicTrade (no auth)."""
    return {"op": "subscribe", "args": [f"publicTrade.{s}" for s in symbols]}


def collect_from_frames(frames: Iterable[dict], existing_ids: set[str] | None = None,
                        flush_every: int = DEFAULT_FLUSH_EVERY,
                        on_flush: Callable[[list[dict]], None] | None = None,
                        now_ms_fn: Callable[[], int] | None = None) -> dict[str, Any]:
    """Deterministic collection loop over an iterable of ws frames (dicts).
    Parses -> dedups by trade_id -> buffers -> flushes every `flush_every` new
    trades via on_flush. No network. Out-of-order and duplicate trades handled
    by trade_id dedup + timestamp sort on merge."""
    now_ms_fn = now_ms_fn or (lambda: int(time.time() * 1000))
    seen: set[str] = set(existing_ids or set())
    buffer: list[dict] = []
    n_frames = n_parsed = n_new = n_dup = n_malformed = n_flushes = 0
    last_frame_ms = now_ms_fn()
    for frame in frames:
        n_frames += 1
        rows = CORE.parse_public_trade_message(frame) if isinstance(frame, dict) else []
        if not rows and frame:
            n_malformed += 1
        n_parsed += len(rows)
        uniq, seen = CORE.dedup_by_trade_id(rows, seen)
        n_dup += len(rows) - len(uniq)
        n_new += len(uniq)
        buffer.extend(uniq)
        last_frame_ms = now_ms_fn()
        if len(buffer) >= flush_every:
            buffer.sort(key=lambda r: int(r["timestamp"]))
            if on_flush:
                on_flush(buffer)
            n_flushes += 1
            buffer = []
    if buffer:                                    # final flush
        buffer.sort(key=lambda r: int(r["timestamp"]))
        if on_flush:
            on_flush(buffer)
        n_flushes += 1
    hb = CORE.heartbeat(last_frame_ms, now_ms_fn())
    return {"tool_version": TOOL_VERSION, "n_frames": n_frames,
            "n_parsed_trades": n_parsed, "n_new_trades": n_new,
            "n_duplicate_trades": n_dup, "n_malformed_frames": n_malformed,
            "n_flushes": n_flushes, "heartbeat": {"status": hb["status"],
                                                  "stale": hb["stale"]},
            "seen_ids_count": len(seen), **_safety()}


def connect_and_stream(symbols: list[str], on_rows: Callable[[list[dict]], None],
                       max_runtime_seconds: int = 300,
                       stale_ms: int = DEFAULT_STALE_MS) -> dict[str, Any]:
    """LIVE connect to Bybit v5 public linear ws and stream publicTrade frames
    to on_rows. Guarded behind the optional `websocket` dependency; returns a
    fail-closed error dict if it is missing. Public only, no auth, no orders.
    NOT exercised by tests (real network)."""
    try:
        import json as _json
        import websocket  # type: ignore  # websocket-client
    except Exception:
        return {"status": "DEPENDENCY_MISSING",
                "detail": "websocket-client not importable; live loop unavailable",
                **_safety()}
    try:
        ws = websocket.create_connection(WS_PUBLIC_LINEAR, timeout=20)
        ws.send(_json.dumps(subscribe_message(symbols)))
    except Exception as e:                         # fail-closed on connect error
        return {"status": "CONNECT_ERROR", "detail": str(e)[:200], **_safety()}
    started = time.time()
    frames = 0
    seen: set[str] = set()
    last = time.time()
    try:
        while time.time() - started < max_runtime_seconds:
            try:
                raw = ws.recv()
            except Exception:
                break
            if not raw:
                if (time.time() - last) * 1000 > stale_ms:
                    break                          # stale stream
                continue
            try:
                msg = _json.loads(raw)
            except Exception:
                continue
            rows = CORE.parse_public_trade_message(msg)
            uniq, seen = CORE.dedup_by_trade_id(rows, seen)
            if uniq:
                on_rows(uniq)
                last = time.time()
            frames += 1
    finally:
        try:
            ws.close()
        except Exception:
            pass
    return {"status": "STREAM_ENDED", "frames": frames,
            "unique_trades": len(seen), "runtime_s": round(time.time() - started, 1),
            **_safety()}


WS_DATASET_SUBDIR = ("external_data", "staging", "bybit_trades_ws_v10_42")
_CSV_COLS = ["timestamp", "symbol", "price", "size", "aggressor_side",
             "trade_id", "source_exchange"]


def _ws_dataset_dir(base=None):
    from . import continuous_edge_factory_v10_38 as CE
    root = base if base is not None else CE._repo_root()
    return root.joinpath(*WS_DATASET_SUBDIR) if base is None else base


def append_rows(rows: list[dict], data_dir) -> dict[str, Any]:
    """Idempotent append of canonical trade rows to <data_dir>/trades.csv,
    dedup by trade_id, ascending timestamp. SEPARATE from the v1032 dataset."""
    import csv
    import os
    from pathlib import Path
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    f = data_dir / "trades.csv"
    existing: list[dict] = []
    if f.is_file():
        with open(f, "r", newline="", encoding="utf-8") as fh:
            existing = list(csv.DictReader(fh))
    merged, added = CORE.merge_append(existing, rows)
    tmp = data_dir / "trades.csv.tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=_CSV_COLS)
        w.writeheader()
        for r in merged:
            w.writerow({k: r.get(k) for k in _CSV_COLS})
    os.replace(tmp, f)
    return {"rows_added": added, "total_rows": len(merged),
            "path": str(f).replace("\\", "/")}


def ws_collect_sample(symbols: list[str], seconds: int = 60,
                      base_dir=None) -> dict[str, Any]:
    """Bounded LIVE collection sample: connect, stream for `seconds`, append the
    unique trades to the SEPARATE ws dataset. Fail-closed if the ws dependency
    is missing or the connection errors. Public only, no keys, no orders."""
    data_dir = _ws_dataset_dir(base_dir)
    collected: list[dict] = []
    res = connect_and_stream(symbols, collected.extend, max_runtime_seconds=seconds)
    if res.get("status") not in ("STREAM_ENDED",):
        return {"tool_version": TOOL_VERSION, "collect_status": res.get("status"),
                "detail": res.get("detail"), "rows_added": 0, **_safety()}
    ap = append_rows(collected, data_dir)
    return {"tool_version": TOOL_VERSION, "collect_status": res["status"],
            "frames": res.get("frames"), "unique_trades": res.get("unique_trades"),
            "rows_added": ap["rows_added"], "total_rows": ap["total_rows"],
            "dataset": ap["path"], **_safety()}


def status() -> dict[str, Any]:
    return {"tool_version": TOOL_VERSION,
            "core_from": "bybit_trades_ws_collector_v10_41",
            "loop_implemented": True, "live_connect_guarded": True,
            "endpoint": WS_PUBLIC_LINEAR,
            "how_to_run": "scripts/collect_bybit_trades_ws_forever.ps1",
            "safety": "public only, no keys, no orders, research-only", **_safety()}
