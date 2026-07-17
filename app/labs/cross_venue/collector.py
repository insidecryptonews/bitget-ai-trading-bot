"""Persistent public-feed collector supervisor for one venue."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from . import PROVIDER_INVENTORY_PATH, safety_envelope
from .adapters import PublicVenueAdapter, make_adapter
from .models import receive_clock
from .providers import load_config, load_inventory
from .storage import StreamStore, atomic_json

BACKOFF_SECONDS = (1, 2, 5, 10, 20, 30)


def _verification_payload(
    adapter: PublicVenueAdapter, first_event: dict[str, Any], *, connector_injected: bool = False,
) -> dict[str, Any]:
    inventory_bytes = PROVIDER_INVENTORY_PATH.read_bytes()
    inventory = load_inventory()
    provider = next(item for item in inventory["providers"] if item["provider_id"] == adapter.venue)
    return {
        "schema": "cross_venue_provider_verification.v1", "provider": adapter.venue,
        "verified_at": first_event.get("local_receive_wall_ts"),
        "official_docs_url": provider["official_docs_url"],
        "official_public_ws": adapter.connection_url(), "http_status": None,
        "websocket_connected": True, "first_event_schema": first_event.get("raw_schema"),
        "first_event_type": first_event.get("event_type"),
        "first_exchange_event_ts": first_event.get("exchange_event_ts"),
        "first_local_receive_wall_ms": first_event.get("local_receive_wall_ms"),
        "inventory_sha256": hashlib.sha256(inventory_bytes).hexdigest(),
        "rate_limit": provider.get("websocket_limits"), "errors": [],
        "result": "TEST_CONNECTOR_ONLY" if connector_injected else "VERIFIED_PUBLIC_NO_AUTH",
        **safety_envelope(),
    }


def collect_session(
    adapter: PublicVenueAdapter,
    store: StreamStore,
    *,
    connector: Callable[..., Any] | None = None,
    max_messages: int | None = None,
    max_seconds: float | None = None,
    now_fn: Callable[[], float] = time.monotonic,
    stop_requested: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    start = now_fn(); last_health_write = start
    connector_injected = connector is not None
    normalized = 0; raw = 0; first: dict[str, Any] | None = None
    adapter.connect(connector)
    adapter.subscribe()
    while max_messages is None or adapter.messages < max_messages:
        if stop_requested():
            break
        if max_seconds is not None and now_fn() - start >= max_seconds:
            break
        frame = adapter.receive()
        if frame is None or not isinstance(frame, dict):
            continue
        clock = receive_clock()
        store.append_raw(frame, clock[1], adapter.connection_id); raw += 1
        rows = adapter.normalize(frame, clock=clock)
        normalized += store.append_events(rows)
        if rows and first is None:
            first = rows[0]
            verification = store.venue_root / "verification.json"
            if not verification.exists():
                atomic_json(verification, _verification_payload(
                    adapter, first, connector_injected=connector_injected,
                ))
        now = now_fn()
        if now - last_health_write >= 1.0:
            store.write_health(adapter.health()); last_health_write = now
    store.write_health(adapter.health())
    duration = max(0.0, now_fn() - start)
    session_verification = (
        _verification_payload(adapter, first, connector_injected=connector_injected) if first is not None else {
            "schema": "cross_venue_provider_verification.v1", "provider": adapter.venue,
            "result": "NEED_DATA_NO_NORMALIZED_EVENT", "errors": [adapter.last_error] if adapter.last_error else [],
            **safety_envelope(),
        }
    )
    session_verification.update({
        "command": f"python -m app.labs.cross_venue.cli collect --venue {adapter.venue}",
        "dns_tls_websocket_upgrade": (
            "MOCK_OR_INJECTED_CONNECTOR" if connector_injected
            else "PASS" if adapter.connected else "SESSION_CLOSED_OR_NOT_CONNECTED"
        ),
        "session_duration_seconds": duration, "raw_frames": raw,
        "normalized_events": normalized,
        "observed_normalized_events_per_second": normalized / duration if duration > 0 else None,
        "rate_limit_headers": "NOT_AVAILABLE_ON_PUBLIC_WEBSOCKET",
        "exchange_clock_used_for_leadership": False,
    })
    atomic_json(store.venue_root / "session_verification.json", session_verification)
    return {"raw_frames": raw, "normalized_events": normalized, "first_event": first,
            "health": adapter.health(), **safety_envelope()}


def run_collector(
    venue: str,
    *,
    symbols: list[str] | None = None,
    root: Path | str | None = None,
    max_sessions: int | None = None,
    max_messages: int | None = None,
    max_seconds: float | None = None,
    connector: Callable[..., Any] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    stop_file: Path | str | None = None,
) -> dict[str, Any]:
    config = load_config()
    venue = str(venue).lower()
    if venue not in config["active_venues"]:
        return {"venue": venue, "status": "DISABLED_BY_CONFIG", **safety_envelope()}
    selected_symbols = symbols or list(config["symbols"])
    allowed_symbols = {
        str(item).upper() for item in
        list(config.get("symbols") or []) + list(config.get("prepared_symbols") or [])
    }
    if not selected_symbols or any(str(item).upper() not in allowed_symbols for item in selected_symbols):
        raise ValueError("CROSS_VENUE_SYMBOL_OUTSIDE_ALLOWLIST")
    adapter = make_adapter(venue, selected_symbols)
    store = StreamStore(venue, root)
    stop_path = Path(stop_file) if stop_file is not None else None
    stop_requested = lambda: bool(stop_path and stop_path.is_file())
    store.open()
    sessions = 0; errors: list[str] = []; total_events = 0
    try:
        while max_sessions is None or sessions < max_sessions:
            if stop_requested():
                break
            sessions += 1
            try:
                result = collect_session(
                    adapter, store, connector=connector, max_messages=max_messages,
                    max_seconds=max_seconds, stop_requested=stop_requested,
                )
                total_events += int(result["normalized_events"])
                if max_sessions is not None:
                    break
                adapter.reconnect()
            except KeyboardInterrupt:
                break
            except Exception as exc:
                if adapter.connected:
                    adapter.gaps += 1
                adapter.last_error = f"{type(exc).__name__}:{str(exc)[:240]}"
                errors.append(adapter.last_error)
                store.write_health(adapter.health())
                adapter.reconnect()
                if max_sessions is not None and sessions >= max_sessions:
                    break
                if stop_requested():
                    break
                sleep_fn(BACKOFF_SECONDS[min(len(errors) - 1, len(BACKOFF_SECONDS) - 1)])
        health = adapter.health()
        store.write_health(health)
        return {"venue": venue, "sessions": sessions, "normalized_events": total_events,
                "errors": errors[-20:], "health": health, **safety_envelope()}
    finally:
        adapter.close()
        try:
            store.write_health(adapter.health())
        finally:
            store.close()


def collector_main(venue: str, symbols: list[str] | None = None) -> int:
    payload = run_collector(venue, symbols=symbols)
    print(json.dumps(payload, indent=2, default=str))
    return 0 if payload.get("health", {}).get("status") not in {"ERROR"} else 2
