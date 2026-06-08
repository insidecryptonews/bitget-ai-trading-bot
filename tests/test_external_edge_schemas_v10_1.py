"""ResearchOps V10.1 — schemas/validator tests + cross-module safety scan.

All synthetic. No DB, no network, no files.
"""

from __future__ import annotations

import ast
import importlib
import pathlib

import pytest

from app.labs.external_edge_schemas_v10_1 import (
    DS_LISTING,
    DS_PERP_LIQ,
    DS_PERP_MARKET,
    DS_TOKEN_UNLOCK,
    logical_key,
    normalize_timestamp_to_ms,
    validate_row,
)

V101_MODULES = [
    "app.labs.external_edge_schemas_v10_1",
    "app.labs.external_edge_ingest_v10_1",
    "app.labs.external_event_study_v10_1",
]


def _good_market():
    return {"symbol": "BTCUSDT", "exchange": "bitget", "timestamp": "2026-06-01T00:00:00Z",
            "price_open": 69000, "price_high": 69500, "price_low": 68800, "price_close": 69200,
            "volume_usd": 1e9, "funding_rate": 0.0001, "oi_usd_close": 1.8e9, "source": "manual"}


# 1. ISO -> UNIX ms UTC
def test_iso_to_unix_ms():
    assert normalize_timestamp_to_ms("2026-06-01T00:00:00Z") == 1780272000000


# 2. UNIX ms stays ms
def test_unix_ms_stays():
    ms = 1780272000000
    assert normalize_timestamp_to_ms(ms) == ms
    # seconds upscaled to ms
    assert normalize_timestamp_to_ms(1780272000) == ms


# 3. NaN/inf rejected
def test_nan_inf_rejected():
    bad = _good_market()
    bad["funding_rate"] = float("nan")
    r = validate_row(bad, DS_PERP_MARKET)
    assert not r["valid"]
    assert any("nan_or_inf" in e for e in r["errors"])
    bad2 = _good_market()
    bad2["oi_usd_close"] = float("inf")
    assert not validate_row(bad2, DS_PERP_MARKET)["valid"]


# 4. empty symbol rejected
def test_empty_symbol_rejected():
    bad = _good_market()
    bad["symbol"] = ""
    r = validate_row(bad, DS_PERP_MARKET)
    assert not r["valid"]
    assert ("bad_symbol" in r["errors"]) or ("missing_symbol" in r["errors"])


# 5. empty source rejected
def test_empty_source_rejected():
    bad = _good_market()
    bad["source"] = ""
    r = validate_row(bad, DS_PERP_MARKET)
    assert not r["valid"]
    assert ("empty_source" in r["errors"]) or ("missing_source" in r["errors"])


# 6. missing required fields rejected
def test_missing_required_rejected():
    bad = _good_market()
    del bad["price_close"]
    r = validate_row(bad, DS_PERP_MARKET)
    assert not r["valid"]
    assert "missing_price_close" in r["errors"]


# 7. logical_key deterministic
def test_logical_key_deterministic():
    g = _good_market()
    assert logical_key(g, DS_PERP_MARKET) == logical_key(dict(g), DS_PERP_MARKET)
    # different timestamp => different key
    g2 = _good_market()
    g2["timestamp"] = "2026-06-01T01:00:00Z"
    assert logical_key(g, DS_PERP_MARKET) != logical_key(g2, DS_PERP_MARKET)


def test_liquidation_side_validation():
    base = {"symbol": "BTCUSDT", "exchange": "bitget", "timestamp": 1780272000000,
            "side": "LONG", "notional_usd": 1e6, "price": 69000, "source": "coinglass"}
    assert validate_row(base, DS_PERP_LIQ)["valid"]
    bad = dict(base)
    bad["side"] = "xyz"
    assert "bad_side" in validate_row(bad, DS_PERP_LIQ)["errors"]


def test_unlock_and_listing_required_fields():
    unlock = {"event_id": "u1", "token_symbol": "ABC", "event_time": "2026-06-20T00:00:00Z",
              "event_type": "unlock", "source": "tokenomist"}
    assert validate_row(unlock, DS_TOKEN_UNLOCK)["valid"]
    listing = {"event_id": "l1", "symbol_perp_bitget": "ABCUSDT", "token_symbol_spot": "ABC",
               "listing_exchange": "bitget", "listing_time": "2026-06-01T00:00:00Z", "source": "manual"}
    assert validate_row(listing, DS_LISTING)["valid"]


def test_bad_timestamp_rejected():
    bad = _good_market()
    bad["timestamp"] = "not-a-date"
    r = validate_row(bad, DS_PERP_MARKET)
    assert not r["valid"] and "bad_timestamp" in r["errors"]


# ---------------------------------------------------------------------------
# Safety scan across ALL V10.1 modules (points 31-40)
# ---------------------------------------------------------------------------

FORBIDDEN_CALLS = {"place_order", "set_leverage", "set_margin_mode",
                   "private_get", "private_post", "execute", "open_position"}
FORBIDDEN_TRUE = {"LIVE_TRADING", "ENABLE_PAPER_POLICY_FILTER",
                  "can_send_real_orders", "allow_real_writes"}


@pytest.mark.parametrize("mod", V101_MODULES)
def test_v101_no_forbidden_calls(mod):
    path = pathlib.Path(importlib.import_module(mod).__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            assert name not in FORBIDDEN_CALLS, f"{mod} calls {name}"


@pytest.mark.parametrize("mod", V101_MODULES)
def test_v101_no_forbidden_true_assign(mod):
    path = pathlib.Path(importlib.import_module(mod).__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                name = getattr(tgt, "id", None) or getattr(tgt, "attr", None)
                if (name in FORBIDDEN_TRUE and isinstance(node.value, ast.Constant)
                        and node.value.value is True):
                    raise AssertionError(f"{mod} {name}=True")


@pytest.mark.parametrize("mod", V101_MODULES)
def test_v101_no_network_db_or_runtime(mod):
    src = pathlib.Path(importlib.import_module(mod).__file__).read_text(encoding="utf-8")
    for tok in ("import requests", "import urllib", "import ccxt", "import websocket",
                "import aiohttp", "http.client", "os.environ[",
                "import paper_trader", "import edge_guard", "import signal_engine",
                "import strategy_engine", "import execution_engine",
                "apply=True"):
        assert tok not in src, f"{mod} contains {tok}"
