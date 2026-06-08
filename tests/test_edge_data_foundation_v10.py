"""ResearchOps V10 — Edge Data Foundation tests + cross-module safety scan.

All synthetic. No DB. No network. No real OHLCV.
"""

from __future__ import annotations

import ast
import importlib
import pathlib

import pytest

from app.labs.edge_data_foundation_v10 import (
    ACT_NOT_ACTIONABLE,
    ACT_SHADOW_RESEARCH_ONLY,
    ACT_WATCH_ONLY,
    DATA_BAD,
    DATA_NEED,
    DATA_OK,
    EdgeDiscoveryReadiness,
    assess_foundation,
    cap_actionability,
    load_external_data,
    quality_from_validation,
    validate_external_rows,
)

# All V10 modules — used by the shared safety scan.
V10_MODULES = [
    "app.labs.edge_data_foundation_v10",
    "app.labs.funding_oi_liquidation_research_v10",
    "app.labs.token_unlock_post_listing_research_v10",
    "app.labs.intraday_volatility_breakdown_v10",
    "app.labs.micro_tp_viability_v10",
    "app.labs.event_catalyst_layer_v10",
    "app.labs.edge_discovery_orchestrator_v10",
]


def _ts(i: int = 0) -> str:
    return f"2026-06-01T{i:02d}:00:00+00:00"


# ---------------------------------------------------------------------------
# Missing data => NEED_DATA, never a fabricated result
# ---------------------------------------------------------------------------

def test_missing_external_data_is_need_data():
    r = assess_foundation([], source_label="NO_EXTERNAL_DATA", required_data=["funding_rate"])
    assert isinstance(r, EdgeDiscoveryReadiness)
    assert r.data_quality_status == DATA_NEED
    assert r.data_available is False
    assert r.required_data_missing == ["funding_rate"]
    assert r.final_recommendation == "NO LIVE"


def test_load_external_data_missing_file_returns_empty():
    rows, label = load_external_data("does/not/exist.csv")
    assert rows == []
    assert label in ("MISSING_FILE", "NO_EXTERNAL_DATA")


def test_load_external_data_none_path():
    rows, label = load_external_data(None)
    assert rows == [] and label == "NO_EXTERNAL_DATA"


# ---------------------------------------------------------------------------
# NaN / inf blocked (point 21)
# ---------------------------------------------------------------------------

def test_nan_inf_rejected():
    rows = [
        {"symbol": "BTCUSDT", "timestamp": _ts(), "source": "x", "metric_value": float("nan")},
        {"symbol": "ETHUSDT", "timestamp": _ts(1), "source": "x", "metric_value": float("inf")},
        {"symbol": "SOLUSDT", "timestamp": _ts(2), "source": "x", "metric_value": 1.0},
    ]
    vr = validate_external_rows(rows, value_fields=("metric_value",))
    assert vr.nan_inf_count == 2
    assert len(vr.valid) == 1
    assert vr.valid[0]["symbol"] == "SOLUSDT"


def test_logical_duplicate_detected():
    row = {"symbol": "BTCUSDT", "timestamp": _ts(), "source": "x", "metric_value": 1.0}
    vr = validate_external_rows([dict(row), dict(row)], value_fields=("metric_value",))
    assert vr.duplicate_count == 1
    assert len(vr.valid) == 1


def test_bad_symbol_and_timestamp_rejected():
    rows = [
        {"symbol": "", "timestamp": _ts(), "source": "x"},
        {"symbol": "BTCUSDT", "timestamp": "not-a-date", "source": "x"},
    ]
    vr = validate_external_rows(rows)
    assert len(vr.valid) == 0
    assert vr.bad_symbol_count >= 1
    assert vr.bad_timestamp_count >= 1


def test_quality_from_validation_levels():
    import datetime as _dt
    assert quality_from_validation(validate_external_rows([])) == DATA_NEED
    now = _dt.datetime(2026, 6, 1, 5, tzinfo=_dt.timezone.utc)
    clean = [{"symbol": "BTCUSDT", "timestamp": _ts(i), "source": "x", "metric_value": float(i),
              "source_reliability": 0.9} for i in range(3)]
    # Evaluate freshness relative to a reference time so rows are FRESH.
    vr = validate_external_rows(clean, value_fields=("metric_value",), now=now)
    assert quality_from_validation(vr) == DATA_OK


# ---------------------------------------------------------------------------
# Actionability clamp (point 22) — low reliability => NOT_ACTIONABLE
# ---------------------------------------------------------------------------

def test_cap_actionability_low_reliability():
    assert cap_actionability(ACT_SHADOW_RESEARCH_ONLY, source_reliability=0.1, data_quality_status=DATA_OK) == ACT_NOT_ACTIONABLE


def test_cap_actionability_bad_data():
    assert cap_actionability(ACT_SHADOW_RESEARCH_ONLY, source_reliability=0.99, data_quality_status=DATA_BAD) == ACT_NOT_ACTIONABLE


def test_cap_actionability_embargo_caps_to_watch():
    assert cap_actionability(ACT_SHADOW_RESEARCH_ONLY, source_reliability=0.9, data_quality_status=DATA_OK, embargo=True) == ACT_WATCH_ONLY


def test_cap_actionability_never_exceeds_shadow():
    # Even "operative-sounding" proposals are clamped to the SHADOW ceiling.
    assert cap_actionability("LIVE", source_reliability=0.99, data_quality_status=DATA_OK) == ACT_NOT_ACTIONABLE


# ---------------------------------------------------------------------------
# Shared safety scan across ALL V10 modules (points 1-8)
# ---------------------------------------------------------------------------

FORBIDDEN_CALLS = {
    "place_order", "set_leverage", "set_margin_mode",
    "private_get", "private_post", "execute", "open_position",
}
FORBIDDEN_TRUE_ASSIGNS = {
    "LIVE_TRADING", "ENABLE_PAPER_POLICY_FILTER",
    "can_send_real_orders", "allow_real_writes",
}


@pytest.mark.parametrize("mod", V10_MODULES)
def test_v10_modules_no_forbidden_calls(mod):
    path = pathlib.Path(importlib.import_module(mod).__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            assert name not in FORBIDDEN_CALLS, f"{mod} calls {name}"


@pytest.mark.parametrize("mod", V10_MODULES)
def test_v10_modules_no_forbidden_true_assigns(mod):
    path = pathlib.Path(importlib.import_module(mod).__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                name = getattr(tgt, "id", None) or getattr(tgt, "attr", None)
                if (name in FORBIDDEN_TRUE_ASSIGNS and isinstance(node.value, ast.Constant)
                        and node.value.value is True):
                    raise AssertionError(f"{mod} {name}=True")


@pytest.mark.parametrize("mod", V10_MODULES)
def test_v10_modules_no_runtime_imports(mod):
    src = pathlib.Path(importlib.import_module(mod).__file__).read_text(encoding="utf-8")
    for forbidden in ("import paper_trader", "import edge_guard", "import signal_engine",
                      "import strategy_engine", "import candidate_ranking",
                      "import execution_engine"):
        assert forbidden not in src, f"{mod} has {forbidden}"


@pytest.mark.parametrize("mod", V10_MODULES)
def test_v10_modules_no_network_or_secrets(mod):
    src = pathlib.Path(importlib.import_module(mod).__file__).read_text(encoding="utf-8")
    # Import-prefixed tokens avoid matching benign identifiers such as the
    # ``need_websocket`` boolean flag in the micro-TP report.
    for forbidden in ("import requests", "import urllib", "import http.client",
                      "import ccxt", "import websocket", "import aiohttp",
                      "os.environ["):
        assert forbidden not in src, f"{mod} touches {forbidden}"
