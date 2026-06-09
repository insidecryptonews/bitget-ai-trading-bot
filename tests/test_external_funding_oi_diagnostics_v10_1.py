"""ResearchOps V10.1 — funding/OI/liquidation diagnostics tests.

All synthetic. No DB, no network. Verifies: NEED_DATA without data, bucket
mechanics, conservative gates (WATCH/GREEN), no-lookahead bucket
definition, and the hard invariants (never paper/live ready).
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import random

from app.labs.external_funding_oi_diagnostics_v10_1 import (
    STATUS_GREEN,
    STATUS_NEED_DATA,
    STATUS_REJECT,
    STATUS_WATCH,
    aggregate_liquidations,
    build_features,
    run_funding_oi_diagnostics,
)
from app.labs.external_event_study_v10_1 import build_market_series

BASE = 1_780_000_000_000
STEP = 3600000


def _gen(sym, n=1600, seed=0):
    random.seed(seed)
    rows = []
    price = 100.0
    drop = 0
    spikes = set(range(180, n, 12))
    for i in range(n):
        fr = 0.00005
        if i in spikes:
            fr = 0.02
            drop = 24
        if drop > 0:
            ret = -0.003 + random.gauss(0, 0.0015)
            drop -= 1
        else:
            ret = 0.0008 + random.gauss(0, 0.002)
        o = price
        c = o * (1 + ret)
        rows.append({"symbol": sym, "timestamp_ms": BASE + i * STEP, "price_close": c,
                     "price_high": max(o, c) * 1.001, "price_low": min(o, c) * 0.999,
                     "funding_rate": fr, "oi_usd_close": 1.8e9 + i * 1e6})
        price = c
    return rows


def _market():
    return _gen("BTCUSDT", seed=1) + _gen("ETHUSDT", seed=2)


def test_no_data_is_need_data():
    r = run_funding_oi_diagnostics([], [], hours=2160)
    assert r.status == STATUS_NEED_DATA
    assert r.final_recommendation == "NO LIVE"
    assert r.paper_ready is False and r.live_ready is False


def test_buckets_evaluated_and_invariants():
    r = run_funding_oi_diagnostics(_market(), [], hours=100000,
                                   bootstrap_n=200, baseline_n=120, per_symbol=False)
    assert r.status == "OK"
    assert r.buckets_evaluated > 0
    assert r.symbols == ["BTCUSDT", "ETHUSDT"]
    assert r.paper_ready is False and r.live_ready is False
    assert r.final_recommendation == "NO LIVE"
    # every bucket carries a verdict from the allowed set
    allowed = {STATUS_REJECT, STATUS_WATCH, STATUS_GREEN, "NEED_MORE_DATA"}
    assert all(b["status"] in allowed for b in r.buckets)


def test_conservative_gate_assigns_status():
    # In this synthetic the post-funding-spike short bucket has a real edge,
    # so it must reach WATCH or GREEN (not stay NEED_MORE / REJECT).
    r = run_funding_oi_diagnostics(_market(), [], hours=100000,
                                   bootstrap_n=300, baseline_n=200, per_symbol=False)
    by = {b["name"]: b for b in r.buckets if b["symbol_scope"] == "ALL"}
    flush = by.get("crowded_longs_flush_z15__SHORT")
    assert flush is not None
    assert flush["matched_events"] >= 50
    assert flush["status"] in (STATUS_WATCH, STATUS_GREEN)


def test_low_count_bucket_is_need_more_data():
    # A bucket with few events must be NEED_MORE_DATA, never promoted.
    rows = _gen("BTCUSDT", n=300, seed=9)  # short series => few spikes
    r = run_funding_oi_diagnostics(rows, [], hours=100000,
                                   bootstrap_n=100, baseline_n=80, per_symbol=False)
    for b in r.buckets:
        if b["matched_events"] < 50:
            assert b["status"] == "NEED_MORE_DATA"


def test_features_no_lookahead():
    # Feature at bar i must not change when FUTURE bars are mutated.
    rows = _gen("BTCUSDT", n=600, seed=3)
    mbs_a = build_market_series(rows)
    feats_a = build_features(mbs_a, {})
    rows_b = [dict(r) for r in rows]
    for r in rows_b:
        if r["timestamp_ms"] >= BASE + 300 * STEP:  # mutate 2nd half
            r["funding_rate"] = 9.9
            r["oi_usd_close"] = 1.0
            r["price_close"] = 1.0
    mbs_b = build_market_series(rows_b)
    feats_b = build_features(mbs_b, {})
    # feature at bar 200 (first half) must be identical
    fa = feats_a["BTCUSDT"][200]
    fb = feats_b["BTCUSDT"][200]
    assert fa["funding_z"] == fb["funding_z"]
    assert fa["oi_z"] == fb["oi_z"]
    assert fa["price_24h"] == fb["price_24h"]


def test_aggregate_liquidations():
    liq = [
        {"symbol": "BTCUSDT", "timestamp_ms": BASE, "side": "LONG", "notional_usd": 100.0},
        {"symbol": "BTCUSDT", "timestamp_ms": BASE, "side": "SHORT", "notional_usd": 40.0},
        {"symbol": "BTCUSDT", "timestamp_ms": BASE, "side": "LONG", "notional_usd": 25.0},
    ]
    agg = aggregate_liquidations(liq)
    assert agg["BTCUSDT"][BASE]["long"] == 125.0
    assert agg["BTCUSDT"][BASE]["short"] == 40.0


def test_safety_scan_module():
    mod = "app.labs.external_funding_oi_diagnostics_v10_1"
    src = pathlib.Path(importlib.import_module(mod).__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    forbidden = {"place_order", "set_leverage", "set_margin_mode",
                 "private_get", "private_post", "execute", "open_position"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            assert name not in forbidden, f"calls {name}"
    for tok in ("import requests", "import ccxt", "os.environ[", "load_dotenv",
                "LIVE_TRADING = True", "can_send_real_orders = True",
                "import paper_trader", "import execution_engine"):
        assert tok not in src, f"contains {tok}"
