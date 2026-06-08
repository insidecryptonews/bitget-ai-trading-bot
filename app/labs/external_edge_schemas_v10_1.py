"""ResearchOps V10.1 — External Edge Data schemas + validators.

Defines the schema + row validators for the four external datasets that
feed the edge-discovery research families. Pure, dependency-free, NO I/O,
NO network, NO DB. Used by ``external_edge_ingest_v10_1`` and
``external_event_study_v10_1``.

Datasets:

- ``perp_market_state``  — funding / OI / price snapshots.
- ``perp_liquidations``  — liquidation prints.
- ``token_unlock_events``— token unlock calendar + tokenomics.
- ``listing_events``     — listing / post-listing events.

HARD CONTRACT — research only. This module never opens orders, never
calls private endpoints, never touches the DB, never mutates safety
flags, never fabricates data. Invalid rows are *classified*, never
silently coerced.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Iterable

FINAL_RECOMMENDATION_NO_LIVE = "NO LIVE"

# Dataset identifiers.
DS_PERP_MARKET = "perp_market_state"
DS_PERP_LIQ = "perp_liquidations"
DS_TOKEN_UNLOCK = "token_unlock_events"
DS_LISTING = "listing_events"

ALL_DATASETS = (DS_PERP_MARKET, DS_PERP_LIQ, DS_TOKEN_UNLOCK, DS_LISTING)

# Per-dataset required fields (validation rejects rows missing any).
_REQUIRED: dict[str, tuple[str, ...]] = {
    DS_PERP_MARKET: (
        "symbol", "exchange", "timestamp", "price_open", "price_high",
        "price_low", "price_close", "volume_usd", "funding_rate",
        "oi_usd_close", "source",
    ),
    DS_PERP_LIQ: (
        "symbol", "exchange", "timestamp", "side", "notional_usd", "price",
        "source",
    ),
    DS_TOKEN_UNLOCK: (
        "event_id", "token_symbol", "event_time", "event_type", "source",
    ),
    DS_LISTING: (
        "event_id", "symbol_perp_bitget", "token_symbol_spot",
        "listing_exchange", "listing_time", "source",
    ),
}

# Numeric fields per dataset that must be finite when present.
_NUMERIC_FIELDS: dict[str, tuple[str, ...]] = {
    DS_PERP_MARKET: (
        "price_open", "price_high", "price_low", "price_close", "volume_usd",
        "funding_rate", "funding_rate_predicted", "oi_usd_close", "oi_usd_open",
        "oi_usd_high", "oi_usd_low", "oi_contracts_open", "oi_contracts_high",
        "oi_contracts_low", "oi_contracts_close", "long_short_ratio",
        "basis_pct", "premium_index", "data_latency_ms",
    ),
    DS_PERP_LIQ: ("notional_usd", "price", "qty_contracts", "data_latency_ms"),
    DS_TOKEN_UNLOCK: (
        "unlock_tokens", "unlock_pct_max_supply", "unlock_pct_circulating",
        "unlock_value_usd", "circulating_supply", "max_supply",
        "circulating_mcap_usd", "fdv_usd", "fdv_to_mcap", "reliability_score",
    ),
    DS_LISTING: ("pairs_listed", "reliability_score"),
}

# Which field carries the event/observation time per dataset.
_TIME_FIELD: dict[str, str] = {
    DS_PERP_MARKET: "timestamp",
    DS_PERP_LIQ: "timestamp",
    DS_TOKEN_UNLOCK: "event_time",
    DS_LISTING: "listing_time",
}

# Symbol field per dataset (for logical key / grouping).
_SYMBOL_FIELD: dict[str, str] = {
    DS_PERP_MARKET: "symbol",
    DS_PERP_LIQ: "symbol",
    DS_TOKEN_UNLOCK: "token_symbol",
    DS_LISTING: "symbol_perp_bitget",
}

VALID_SIDES = frozenset({"LONG", "SHORT", "BUY", "SELL", "long", "short", "buy", "sell"})


# --------------------------------------------------------------------------
# Primitive validators / normalizers
# --------------------------------------------------------------------------


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return False
        try:
            return math.isfinite(float(s))
        except ValueError:
            return False
    return False


def normalize_timestamp_to_ms(value: Any) -> int | None:
    """Normalize a timestamp to UNIX milliseconds UTC.

    Accepts: int/float (s, ms or us — auto-detected by magnitude), and ISO
    strings (with or without ``Z`` / offset; naive treated as UTC).
    Returns None when unparseable. Never raises.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)) or value <= 0:
            return None
        v = float(value)
        # Heuristic by magnitude: seconds (~1e9), ms (~1e12), us (~1e15).
        if v >= 1e17:        # nanoseconds
            return int(v / 1e6)
        if v >= 1e14:        # microseconds
            return int(v / 1e3)
        if v >= 1e11:        # milliseconds
            return int(v)
        return int(v * 1000)  # seconds
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # Pure numeric string?
        try:
            return normalize_timestamp_to_ms(float(s)) if _is_intish(s) else _iso_to_ms(s)
        except ValueError:
            return _iso_to_ms(s)
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    return None


def _is_intish(s: str) -> bool:
    s = s.strip()
    if s.startswith(("+", "-")):
        s = s[1:]
    return s.replace(".", "", 1).isdigit()


def _iso_to_ms(s: str) -> int | None:
    raw = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def validate_symbol(value: Any) -> bool:
    return isinstance(value, str) and len(value.strip()) >= 2


def validate_source(value: Any) -> bool:
    return isinstance(value, str) and len(value.strip()) >= 1


def reject_nan_inf(row: dict[str, Any], dataset_type: str | None = None) -> list[str]:
    """Return the list of numeric fields holding NaN/inf (or non-numeric
    garbage where a number is expected). Empty list => clean."""
    bad: list[str] = []
    fields = _NUMERIC_FIELDS.get(dataset_type or "", ())
    check = fields if fields else tuple(row.keys())
    for f in check:
        if f in row and row[f] is not None and str(row.get(f)).strip() != "":
            if not _is_finite_number(row[f]):
                bad.append(f)
    return bad


def schema_required_fields(dataset_type: str) -> tuple[str, ...]:
    return _REQUIRED.get(dataset_type, ())


def time_field(dataset_type: str) -> str:
    return _TIME_FIELD.get(dataset_type, "timestamp")


def symbol_field(dataset_type: str) -> str:
    return _SYMBOL_FIELD.get(dataset_type, "symbol")


def logical_key(row: dict[str, Any], dataset_type: str) -> str:
    """Deterministic dedup key. For market/liq it is symbol+exchange+ts
    (+side/notional for liq). For events it is the event_id (falling back
    to symbol+time). Always a stable string."""
    sym = str(row.get(symbol_field(dataset_type)) or "").strip().upper()
    ts = normalize_timestamp_to_ms(row.get(time_field(dataset_type)))
    ts_s = str(ts) if ts is not None else str(row.get(time_field(dataset_type)) or "")
    if dataset_type == DS_PERP_MARKET:
        ex = str(row.get("exchange") or "").strip().lower()
        return f"{DS_PERP_MARKET}|{sym}|{ex}|{ts_s}"
    if dataset_type == DS_PERP_LIQ:
        ex = str(row.get("exchange") or "").strip().lower()
        side = str(row.get("side") or "").strip().lower()
        notional = str(row.get("notional_usd") or "")
        price = str(row.get("price") or "")
        return f"{DS_PERP_LIQ}|{sym}|{ex}|{ts_s}|{side}|{notional}|{price}"
    if dataset_type == DS_TOKEN_UNLOCK:
        eid = str(row.get("event_id") or "").strip()
        return f"{DS_TOKEN_UNLOCK}|{eid or (sym + '|' + ts_s)}"
    if dataset_type == DS_LISTING:
        eid = str(row.get("event_id") or "").strip()
        return f"{DS_LISTING}|{eid or (sym + '|' + ts_s)}"
    return f"{dataset_type}|{sym}|{ts_s}"


def validate_row(row: dict[str, Any], dataset_type: str) -> dict[str, Any]:
    """Validate a single row. Returns a dict with:

    - ``valid``: bool
    - ``errors``: list[str]
    - ``timestamp_ms``: int | None (normalized)
    - ``logical_key``: str

    Never raises, never coerces the original row in place.
    """
    if dataset_type not in ALL_DATASETS:
        return {"valid": False, "errors": [f"unknown_dataset:{dataset_type}"],
                "timestamp_ms": None, "logical_key": ""}
    errors: list[str] = []

    # Required fields present + non-empty.
    for f in schema_required_fields(dataset_type):
        v = row.get(f)
        if v is None or (isinstance(v, str) and not v.strip()):
            errors.append(f"missing_{f}")

    # Symbol.
    sym = row.get(symbol_field(dataset_type))
    if not validate_symbol(sym):
        errors.append("bad_symbol")

    # Source.
    if not validate_source(row.get("source")):
        if "missing_source" not in errors:
            errors.append("empty_source")

    # Timestamp.
    ts_ms = normalize_timestamp_to_ms(row.get(time_field(dataset_type)))
    if ts_ms is None:
        errors.append("bad_timestamp")

    # NaN / inf.
    bad_numeric = reject_nan_inf(row, dataset_type)
    for f in bad_numeric:
        errors.append(f"nan_or_inf:{f}")

    # Side (liquidations only).
    if dataset_type == DS_PERP_LIQ:
        side = row.get("side")
        if not (isinstance(side, str) and side.strip() in VALID_SIDES):
            errors.append("bad_side")

    return {
        "valid": not errors,
        "errors": errors,
        "timestamp_ms": ts_ms,
        "logical_key": logical_key(row, dataset_type),
    }


def detect_dataset_type(row: dict[str, Any]) -> str | None:
    """Best-effort dataset inference from a row's fields."""
    keys = set(row.keys())
    if {"funding_rate", "oi_usd_close"} & keys:
        return DS_PERP_MARKET
    if "notional_usd" in keys and "side" in keys:
        return DS_PERP_LIQ
    if {"unlock_pct_circulating", "unlock_tokens", "fdv_to_mcap"} & keys or row.get("event_type"):
        return DS_TOKEN_UNLOCK
    if {"symbol_perp_bitget", "listing_exchange", "listing_time"} & keys:
        return DS_LISTING
    return None
