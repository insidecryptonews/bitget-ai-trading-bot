"""Read-only Cross-Venue dashboard API."""

from __future__ import annotations

import math
import time
from typing import Any, Callable

from . import ENGINE_SNAPSHOT_PATH, ENGINE_STATUS_PATH, safety_envelope
from .ledger import CrossVenueLedger
from .providers import providers_payload
from .service import collector_health
from .storage import read_json, storage_status


def _base() -> dict[str, Any]:
    return {"schema": "cross_venue_api.v1", **safety_envelope()}


def snapshot() -> dict[str, Any]:
    value = read_json(ENGINE_SNAPSHOT_PATH, {}) or {}
    if value:
        return {**value, **safety_envelope()}
    return {**_base(), "status": "CONNECTING", "venues": [], "prices": [], "orderflow": [],
            "signals": [], "positions": [], "trades": [], "equity": [], "events": [],
            "leadlag": {"leaderboard": [], "pending_outcomes": 0},
            "leverage": {"scenarios": [], "base_trade_count": 0},
            "health": health_payload(), "note": "WAITING_FOR_FIRST_PUBLIC_EVENTS"}


def status_payload() -> dict[str, Any]:
    data = snapshot()
    evaluations = ((data.get("leadlag") or {}).get("evaluation_counts") or {})
    providers = data.get("providers") or {}
    return {**_base(), "status": (data.get("health") or {}).get("status", "CONNECTING"),
            "generated_at": data.get("generated_at"), "reconciliation": data.get("reconciliation"),
            "counts": {
                "active_venues": providers.get("active_venue_count", len(providers.get("active") or [])),
                "active_streams": providers.get("active_stream_count", len(data.get("venues") or [])),
                "raw_evaluations": evaluations.get("raw_evaluations", 0),
                "unique_market_episodes": evaluations.get("unique_market_episodes", 0),
                "candidate_signals": evaluations.get("candidate_signals", 0),
                "accepted_simulated_signals": evaluations.get("accepted_simulated_signals", 0),
                "positions": len(data.get("positions") or []),
                "trades": len(data.get("trades") or []),
            }}


def health_payload() -> dict[str, Any]:
    value = read_json(ENGINE_STATUS_PATH, {}) or {}
    if value:
        age = max(0.0, time.time() - ENGINE_STATUS_PATH.stat().st_mtime) if ENGINE_STATUS_PATH.is_file() else None
        status = str(value.get("status") or "CONNECTING")
        if age is not None and age > 10 and status not in {"ERROR"}:
            status = "STALE"
        return {**value, "status": status, "engine_status_age_seconds": age, **safety_envelope()}
    collectors = {venue: collector_health(venue) for venue in ("bitget", "binance", "bybit", "okx", "hyperliquid")}
    return {**_base(), "status": "CONNECTING", "collectors": collectors,
            "components": {name: {"status": "CONNECTING", **safety_envelope()} for name in
                           ("CROSS_VENUE_NORMALIZER", "CROSS_VENUE_LEADLAG", "CROSS_VENUE_PAPER", "CROSS_VENUE_LEVERAGE_LAB")}}


def account_payload() -> dict[str, Any]:
    account = CrossVenueLedger().account()
    return {**_base(), "status": "OK" if account else "NO_LEDGER", "account": account}


def rows_payload(table: str, key: str, limit: int = 500) -> dict[str, Any]:
    return {**_base(), "status": "OK", key: CrossVenueLedger().rows(table, limit)}


def performance_payload() -> dict[str, Any]:
    trades = CrossVenueLedger().rows("trades", 5000)
    values = [float(row["net_pnl"]) for row in trades if math.isfinite(float(row["net_pnl"]))]
    wins, losses = [v for v in values if v > 0], [v for v in values if v < 0]
    return {**_base(), "status": "NEED_MORE_DATA" if len(values) < 200 else "RESEARCH_ONLY",
            "trades": len(values), "net_pnl": sum(values),
            "net_ev": sum(values) / len(values) if values else None,
            "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
            "win_rate": len(wins) / len(values) if values else None, "promotion_allowed": False}


def _section(name: str) -> dict[str, Any]:
    data = snapshot(); value = data.get(name)
    return {**_base(), "status": "OK" if value is not None and value != [] else "NEED_DATA", name: value}


def prices_payload() -> dict[str, Any]:
    data = snapshot()
    prices = data.get("prices") or []
    return {**_base(), "status": "OK" if prices else "NEED_DATA", "prices": prices,
            "normalized_price_series": data.get("normalized_price_series") or {}}


READERS: dict[str, Callable[[], dict[str, Any]]] = {
    "/api/cross-venue/status": status_payload,
    "/api/cross-venue/providers": providers_payload,
    "/api/cross-venue/venues": lambda: _section("venues"),
    "/api/cross-venue/prices": prices_payload,
    "/api/cross-venue/orderflow": lambda: _section("orderflow"),
    "/api/cross-venue/leadlag": lambda: _section("leadlag"),
    "/api/cross-venue/signals": lambda: _section("signals"),
    "/api/cross-venue/episodes": lambda: {
        **_base(),
        "status": "OK" if ((snapshot().get("leadlag") or {}).get("recent_episodes") or []) else "NEED_DATA",
        "episodes": (snapshot().get("leadlag") or {}).get("recent_episodes") or [],
        "evaluation_counts": (snapshot().get("leadlag") or {}).get("evaluation_counts") or {},
    },
    "/api/cross-venue/account": account_payload,
    "/api/cross-venue/positions": lambda: {**_base(), "status": "OK", "positions": CrossVenueLedger().open_positions()},
    "/api/cross-venue/trades": lambda: rows_payload("trades", "trades"),
    "/api/cross-venue/equity": lambda: rows_payload("equity", "equity", 2000),
    "/api/cross-venue/events": lambda: rows_payload("events", "events", 500),
    "/api/cross-venue/leverage": lambda: _section("leverage"),
    "/api/cross-venue/health": health_payload,
    "/api/cross-venue/performance": performance_payload,
    "/api/cross-venue/storage": storage_status,
}


def api_payload(path: str, query: dict[str, list[str]] | None = None) -> tuple[dict[str, Any], int]:
    del query
    reader = READERS.get(path)
    if reader is None:
        return {**_base(), "error": "NOT_FOUND"}, 404
    try:
        return reader(), 200
    except Exception as exc:
        return {**_base(), "status": "ERROR", "error": f"{type(exc).__name__}:{str(exc)[:240]}"}, 500


def dashboard_snapshot() -> dict[str, Any]:
    return snapshot()


def health_components() -> dict[str, dict[str, Any]]:
    payload = health_payload(); components = dict(payload.get("collectors") or {})
    named = {str(row.get("component") or key): row for key, row in components.items()}
    named.update(payload.get("components") or {})
    return named
