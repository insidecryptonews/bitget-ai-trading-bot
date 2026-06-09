"""ResearchOps V10.2 — missing-OI audit + long-history validation + alt plan tests.

All synthetic. No DB, no network, no real data.
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import random

from app.labs.external_missing_oi_audit_v10_2 import (
    STATUS_CLUSTERED,
    STATUS_DATA_OK,
    STATUS_HIGH,
    STATUS_NEED_MORE,
    run_missing_oi_audit,
)
from app.labs.external_long_history_validation_v10_2 import (
    HISTORY_INTERMEDIATE,
    HISTORY_STRONGER,
    HISTORY_TOO_SHORT,
    STATUS_NEED_DATA,
    SUGG_BACKTEST,
    SUGG_EXTEND,
    SUGG_FIX_OI,
    SUGG_REJECT_PIVOT,
    _history_status,
    build_consolidated_decision,
    run_long_history_validation,
)
from app.labs.external_symbol_expansion_plan_v10_2 import (
    STATUS_BLOCKED,
    STATUS_GATED_OPEN,
    build_alt_expansion_plan,
)

BASE = 1_780_000_000_000
STEP = 3600000

V102_MODULES = [
    "app.labs.external_missing_oi_audit_v10_2",
    "app.labs.external_long_history_validation_v10_2",
    "app.labs.external_symbol_expansion_plan_v10_2",
]


def _mrow(sym, i, oi=1.8e9):
    return {"symbol": sym, "timestamp": BASE + i * STEP, "price_close": 100.0,
            "funding_rate": 0.0001, "oi_usd_close": ("" if oi is None else oi), "source": "coinalyze"}


# --- Missing-OI audit ---

def test_missing_oi_audit_global_and_per_symbol():
    rows = [_mrow("BTCUSDT", i, oi=(None if i % 20 == 0 else 1.8e9)) for i in range(1000)]
    rows += [_mrow("ETHUSDT", i, oi=(None if i % 3 == 0 else 9e8)) for i in range(1000)]
    r = run_missing_oi_audit(rows)
    assert r.status == STATUS_HIGH
    assert r.per_symbol["ETHUSDT"]["ratio"] > r.per_symbol["BTCUSDT"]["ratio"]
    assert r.eth_worse_than_btc is True
    assert "BLOCK_OI_BUCKETS" in r.recommendations
    assert r.final_recommendation == "NO LIVE"


def test_missing_oi_audit_clustered():
    rows = [_mrow("ETHUSDT", i, oi=(None if 100 <= i < 300 else 9e8)) for i in range(1000)]
    r = run_missing_oi_audit(rows)
    assert r.status == STATUS_CLUSTERED
    assert r.max_consecutive_missing >= 3
    assert r.clustered is True


def test_missing_oi_audit_clean():
    rows = [_mrow("BTCUSDT", i) for i in range(500)]
    assert run_missing_oi_audit(rows).status == STATUS_DATA_OK


def test_missing_oi_audit_no_data():
    assert run_missing_oi_audit([]).status == STATUS_NEED_MORE


# --- history_status thresholds ---

def test_history_status_thresholds():
    assert _history_status(30) == HISTORY_TOO_SHORT
    assert _history_status(179) == HISTORY_TOO_SHORT
    assert _history_status(200) == HISTORY_INTERMEDIATE
    assert _history_status(365) == HISTORY_STRONGER
    assert _history_status(400) == HISTORY_STRONGER


# --- consolidated decision tree ---

def _green(bucket_id="crowded_longs_flush_z15__SHORT", scope="ETHUSDT", status="STABILITY_GREEN"):
    return {"bucket_id": bucket_id, "symbol_scope": scope, "stability_status": status}


def test_decision_extend_when_too_short():
    d = build_consolidated_decision(history_status=HISTORY_TOO_SHORT,
                                    stability_buckets=[_green()], missing_status=STATUS_DATA_OK)
    assert d["suggested_next_code_prompt_type"] == SUGG_EXTEND
    assert d["final_recommendation"] == "NO LIVE"
    assert d["max_label"] == "SHADOW_RESEARCH_ONLY_FUTURE"


def test_decision_backtest_when_green_nonoi_low_missing():
    d = build_consolidated_decision(history_status=HISTORY_INTERMEDIATE,
                                    stability_buckets=[_green()], missing_status=STATUS_DATA_OK)
    assert d["suggested_next_code_prompt_type"] == SUGG_BACKTEST
    assert d["eth_specific_candidate"] is True


def test_decision_fix_missing_oi():
    oi = [_green(bucket_id="oi_z_ge_15__SHORT", status="MISSING_OI_RISK")]
    d = build_consolidated_decision(history_status=HISTORY_INTERMEDIATE,
                                    stability_buckets=oi, missing_status=STATUS_HIGH)
    assert d["suggested_next_code_prompt_type"] == SUGG_FIX_OI


def test_decision_reject_pivot_on_oos():
    oos = [{"bucket_id": "funding_pos__SHORT", "symbol_scope": "ETHUSDT", "stability_status": "OOS_FAIL"}]
    d = build_consolidated_decision(history_status=HISTORY_INTERMEDIATE,
                                    stability_buckets=oos, missing_status=STATUS_DATA_OK)
    assert d["suggested_next_code_prompt_type"] == SUGG_REJECT_PIVOT


def test_decision_dashboard_phase_always_present():
    d = build_consolidated_decision(history_status=HISTORY_TOO_SHORT, stability_buckets=[], missing_status=STATUS_DATA_OK)
    assert d["dashboard_next_phase"] == "TRADER_READONLY_AFTER_LONG_HISTORY_VALIDATION"


# --- alt expansion plan gating ---

def test_alt_expansion_blocked_until_validation():
    p = build_alt_expansion_plan(btc_eth_validated=False)
    assert p.alt_expansion_status == STATUS_BLOCKED
    assert p.max_alt_symbols_next_phase == 3
    assert len(p.candidate_alt_symbols) == 5
    assert "missing_oi_ratio_below_10pct" in p.inclusion_criteria


def test_alt_expansion_gated_open_after_validation():
    p = build_alt_expansion_plan(btc_eth_validated=True)
    assert p.alt_expansion_status == STATUS_GATED_OPEN


# --- long-history validation orchestrator ---

def test_long_history_no_data_need_data():
    r = run_long_history_validation([], [], [], hours=8760)
    assert r.status == STATUS_NEED_DATA
    assert r.final_recommendation == "NO LIVE"
    assert r.paper_ready is False and r.live_ready is False
    assert r.next_research_decision["suggested_next_code_prompt_type"] == SUGG_EXTEND


def test_long_history_short_window_end_to_end():
    # ~45 days of 1h bars for one symbol => TOO_SHORT.
    random.seed(1)
    rows = []
    price = 100.0
    for i in range(45 * 24):
        price *= (1 + random.gauss(0, 0.002))
        rows.append({"symbol": "ETHUSDT", "timestamp_ms": BASE + i * STEP, "price_close": price,
                     "price_high": price * 1.001, "price_low": price * 0.999,
                     "funding_rate": 0.0001, "oi_usd_close": 9e8})
    r = run_long_history_validation(rows, [], [], hours=8760, bootstrap_n=30, baseline_n=20)
    assert r.status == "OK"
    assert r.history_status == HISTORY_TOO_SHORT
    assert r.days_covered < 180
    assert r.paper_ready is False and r.live_ready is False
    assert r.next_research_decision["suggested_next_code_prompt_type"] == SUGG_EXTEND


# --- dashboard contract exists + read-only requirements ---

def test_dashboard_contract_exists_and_is_read_only():
    p = pathlib.Path(__file__).resolve().parents[1] / "docs" / "dashboard_trader_readonly_contract_v10_2.md"
    assert p.exists(), "dashboard contract doc missing"
    txt = p.read_text(encoding="utf-8").lower()
    assert "read-only" in txt
    assert "no order" in txt
    assert "no \"go live\"" in txt or "no live" in txt
    assert "can_send_real_orders" in txt
    assert "no leverage" in txt


def test_runbook_exists_and_warns_no_live():
    p = pathlib.Path(__file__).resolve().parents[1] / "docs" / "research_v10_2_long_history_runbook.md"
    assert p.exists()
    txt = p.read_text(encoding="utf-8")
    assert "NO LIVE" in txt
    assert "Do NOT" in txt or "do NOT" in txt


# --- safety scan ---

def test_v102_modules_safety_scan():
    forbidden = {"place_order", "set_leverage", "set_margin_mode",
                 "private_get", "private_post", "execute", "open_position"}
    for mod in V102_MODULES:
        src = pathlib.Path(importlib.import_module(mod).__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                assert name not in forbidden, f"{mod} calls {name}"
        for tok in ("import requests", "import ccxt", "os.environ[", "load_dotenv",
                    "LIVE_TRADING = True", "can_send_real_orders = True",
                    "import paper_trader", "import execution_engine", "COINALYZE_API_KEY"):
            assert tok not in src, f"{mod} contains {tok}"
