"""Versioned provider inventory, configuration, and exact public endpoint policy."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import CONFIG_PATH, PROVIDER_INVENTORY_PATH, safety_envelope

PUBLIC_WS_ENDPOINTS = {
    "bitget": "wss://ws.bitget.com/v2/ws/public",
    "binance": "wss://fstream.binance.com/stream",
    "bybit": "wss://stream.bybit.com/v5/public/linear",
    "okx": "wss://ws.okx.com:8443/ws/v5/public",
    "hyperliquid": "wss://api.hyperliquid.xyz/ws",
}

ALLOWED_WS_HOST_PATHS = {
    ("ws.bitget.com", "/v2/ws/public"),
    ("fstream.binance.com", "/stream"),
    ("stream.bybit.com", "/v5/public/linear"),
    ("ws.okx.com", "/ws/v5/public"),
    ("api.hyperliquid.xyz", "/ws"),
}


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"CROSS_VENUE_JSON_OBJECT_REQUIRED:{path.name}")
    return value


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    config = _read_json(Path(path) if path else CONFIG_PATH)
    if config.get("mode") != "RESEARCH_PAPER_ONLY":
        raise ValueError("CROSS_VENUE_MODE_MUST_BE_RESEARCH_PAPER_ONLY")
    if config.get("can_send_real_orders") is not False or config.get("paper_filter_enabled") is not False:
        raise ValueError("CROSS_VENUE_UNSAFE_CONFIG")
    if config.get("final_recommendation") != "NO LIVE":
        raise ValueError("CROSS_VENUE_FINAL_RECOMMENDATION_MUST_BE_NO_LIVE")
    if int(config.get("paper_max_positions", 0)) != 1:
        raise ValueError("CROSS_VENUE_V1_REQUIRES_ONE_SIMULATED_POSITION")
    active = [str(item).lower() for item in config.get("active_venues") or []]
    eligible = [str(item).lower() for item in config.get("signal_eligible_venues") or []]
    if not active or len(active) != len(set(active)) or config.get("target_venue") not in active:
        raise ValueError("CROSS_VENUE_ACTIVE_VENUE_CONTRACT_INVALID")
    if len(eligible) != len(set(eligible)) or not set(eligible) <= set(active):
        raise ValueError("CROSS_VENUE_SIGNAL_VENUE_CONTRACT_INVALID")
    if int(config.get("minimum_consensus_venues", 0)) < 2 or int(config["minimum_consensus_venues"]) > len(eligible):
        raise ValueError("CROSS_VENUE_CONSENSUS_CONTRACT_INVALID")
    symbols = list(config.get("symbols") or []) + list(config.get("prepared_symbols") or [])
    if not symbols or len(symbols) != len(set(symbols)) or any(
        not str(symbol).isalnum() or not str(symbol).upper().endswith("USDT") for symbol in symbols
    ):
        raise ValueError("CROSS_VENUE_SYMBOL_CONTRACT_INVALID")
    numeric_nonnegative = (
        "round_trip_taker_fee_bps", "adverse_slippage_bps_each_side", "latency_cost_bps",
        "market_impact_bps", "funding_cost_reserve_bps", "basis_risk_reserve_bps",
        "causal_reorder_buffer_ms",
        "paper_notional_usdt", "paper_min_notional_usdt",
        "paper_quantity_step", "minimum_free_disk_bytes", "maximum_stream_bytes_per_venue",
    )
    strictly_positive = {
        "paper_notional_usdt", "paper_min_notional_usdt", "paper_quantity_step",
        "minimum_free_disk_bytes", "maximum_stream_bytes_per_venue",
    }
    for key in numeric_nonnegative:
        try:
            value = float(config[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"CROSS_VENUE_CONFIG_NUMERIC_INVALID:{key}") from exc
        if not math.isfinite(value) or value < 0 or (key in strictly_positive and value <= 0):
            raise ValueError(f"CROSS_VENUE_CONFIG_NUMERIC_INVALID:{key}")
    return config


def load_inventory(path: Path | str | None = None) -> dict[str, Any]:
    inventory = _read_json(Path(path) if path else PROVIDER_INVENTORY_PATH)
    providers = inventory.get("providers")
    if not isinstance(providers, list) or not providers:
        raise ValueError("CROSS_VENUE_PROVIDER_INVENTORY_EMPTY")
    required = {
        "provider_id", "official_domain", "official_docs_url", "verification_status",
        "integration_tier", "last_verified_at", "api_key_required",
    }
    ids: set[str] = set()
    valid_statuses = {
        "VERIFIED_PUBLIC_NO_AUTH", "VERIFIED_PUBLIC_KEY_REQUIRED", "VERIFIED_FREE_TIER",
        "VERIFIED_PAID_ONLY", "REGISTRATION_REQUIRED", "RATE_LIMIT_TOO_LOW",
        "INSUFFICIENT_TIMESTAMPS", "INSUFFICIENT_RESOLUTION", "DEPRECATED", "OFFLINE",
        "TERMS_UNCLEAR", "NOT_RELEVANT", "UNVERIFIED",
    }
    valid_tiers = {"TIER_1_INTEGRATE_NOW", "TIER_2_ADAPTER_READY_DISABLED", "TIER_3_RESEARCH_ONLY", "REJECTED"}
    for provider in providers:
        if not isinstance(provider, dict) or required - set(provider):
            raise ValueError("CROSS_VENUE_PROVIDER_CONTRACT_INVALID")
        provider_id = str(provider["provider_id"])
        if provider_id in ids:
            raise ValueError("CROSS_VENUE_PROVIDER_DUPLICATE")
        if provider.get("verification_status") not in valid_statuses or provider.get("integration_tier") not in valid_tiers:
            raise ValueError("CROSS_VENUE_PROVIDER_CLASSIFICATION_INVALID")
        ids.add(provider_id)
    return inventory


def assert_public_ws_url(venue: str, url: str) -> str:
    venue = str(venue).lower()
    expected = PUBLIC_WS_ENDPOINTS.get(venue)
    parsed = urlparse(str(url))
    pair = ((parsed.hostname or "").lower(), parsed.path)
    if parsed.scheme != "wss" or pair not in ALLOWED_WS_HOST_PATHS or expected is None:
        raise ValueError("CROSS_VENUE_PUBLIC_WS_NOT_ALLOWLISTED")
    expected_parsed = urlparse(expected)
    if pair != ((expected_parsed.hostname or "").lower(), expected_parsed.path):
        raise ValueError("CROSS_VENUE_PUBLIC_WS_VENUE_MISMATCH")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("CROSS_VENUE_PUBLIC_WS_CREDENTIALS_BLOCKED")
    sensitive = {"signature", "apikey", "api_key", "token", "secret", "recvwindow", "timestamp"}
    query_keys = {part.split("=", 1)[0].lower() for part in parsed.query.split("&") if part}
    if query_keys & sensitive:
        raise ValueError("CROSS_VENUE_PUBLIC_WS_SENSITIVE_QUERY_BLOCKED")
    return url


def providers_payload() -> dict[str, Any]:
    inventory = load_inventory()
    return {**inventory, **safety_envelope()}
