"""ResearchOps V10.1 — funding/OI stability+OOS validator tests.

All synthetic. No DB, no network. Verdict logic is tested directly
(deterministic); an end-to-end synthetic run checks structure + invariants.
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import random

from app.labs.external_funding_oi_stability_v10_1 import (
    STABILITY_TABLE_COLUMNS,
    STATUS_COST_FAIL,
    STATUS_GREEN,
    STATUS_MISSING_OI,
    STATUS_NEED_DATA,
    STATUS_NEED_MORE,
    STATUS_OOS_FAIL,
    STATUS_REGIME_UNSTABLE,
    STATUS_WATCH,
    StabilityResult,
    _stability_verdict,
    build_next_research_decision,
    run_funding_oi_stability,
    stability_table_rows,
)

BASE = 1_780_000_000_000
STEP = 3600000


def _m(**kw):
    base = dict(total_matched=200, first_half_matched=80, second_half_matched=80,
                total_net_24h=1.2, first_half_net_24h=1.0, second_half_net_24h=1.1,
                first_half_edge_24h=0.9, second_half_edge_24h=0.8, total_ci_low=0.5,
                cost_x1_net_24h=1.2, cost_x2_net_24h=0.6, one_event_dominance=0.03,
                total_blocker="NONE", missing_oi_risk=False, regime_unstable=False,
                horizon_risk=False)
    base.update(kw)
    return base


# 1. stable two splits -> STABILITY_GREEN
def test_stable_two_splits_green():
    assert _stability_verdict(_m(), oi_based=False) == (STATUS_GREEN, "NONE")


# 2. positive total, bad second half -> OOS_FAIL
def test_second_half_negative_oos_fail():
    s, _ = _stability_verdict(_m(second_half_net_24h=-0.4), oi_based=False)
    assert s == STATUS_OOS_FAIL


def test_edge_sign_flip_oos_fail():
    s, _ = _stability_verdict(_m(second_half_edge_24h=-0.2), oi_based=False)
    assert s == STATUS_OOS_FAIL


# 3. cost x1 positive but cost x2 <=0 -> COST_FAIL
def test_cost_x2_fail():
    s, _ = _stability_verdict(_m(cost_x2_net_24h=-0.05), oi_based=False)
    assert s == STATUS_COST_FAIL


# 4. few events per split -> NEED_MORE_DATA
def test_few_events_need_more():
    assert _stability_verdict(_m(second_half_matched=10), oi_based=False)[0] == STATUS_NEED_MORE
    assert _stability_verdict(_m(total_matched=40), oi_based=False)[0] == STATUS_NEED_MORE


# 5. OI bucket with missing>10% never green
def test_oi_missing_risk_blocks_green():
    s, b = _stability_verdict(_m(missing_oi_risk=True), oi_based=True)
    assert s == STATUS_MISSING_OI
    # a non-OI bucket with the same flag is unaffected (green)
    assert _stability_verdict(_m(missing_oi_risk=True), oi_based=False)[0] == STATUS_GREEN


# 6. horizon risk (only 24h) -> WATCH, not green
def test_horizon_risk_caps_at_watch():
    s, b = _stability_verdict(_m(horizon_risk=True), oi_based=False)
    assert s == STATUS_WATCH and "horizon_risk" in b


def test_regime_unstable():
    s, _ = _stability_verdict(_m(regime_unstable=True), oi_based=False)
    assert s == STATUS_REGIME_UNSTABLE


# --- next_research_decision (7,8,9) ---

def _res(status, *, bid="x", scope="ETHUSDT", net=1.0, oi=False, hr=False):
    r = StabilityResult(bucket_id=bid, symbol_scope=scope, total_net_ev_24h=net)
    r.stability_status = status
    r.oi_based = oi
    r.horizon_risk = hr
    return r


# 7. GREEN without critical blockers -> EXTEND_HISTORY
def test_decision_extend_history_on_green():
    d = build_next_research_decision([_res(STATUS_GREEN, bid="crowded", net=1.4)])
    assert d["suggested_next_code_prompt_type"] == "EXTEND_HISTORY"
    assert d["any_stability_green"] is True
    assert d["eth_specific_candidate"] is True


# 8. best is OI bucket blocked by missing OI -> FIX_MISSING_OI
def test_decision_fix_missing_oi():
    d = build_next_research_decision([
        _res(STATUS_MISSING_OI, bid="oi_z_ge_15__SHORT", scope="ETHUSDT", net=1.4, oi=True),
        _res("REJECT", bid="other"),
    ])
    assert d["suggested_next_code_prompt_type"] == "FIX_MISSING_OI"
    assert d["missing_oi_risk_on_best"] is True


# 9. all fail -> PIVOT
def test_decision_pivot_when_all_fail():
    d = build_next_research_decision([_res("REJECT"), _res(STATUS_OOS_FAIL), _res(STATUS_COST_FAIL)])
    assert d["suggested_next_code_prompt_type"] == "PIVOT_TO_UNLOCKS"
    assert d["any_stability_green"] is False


def test_decision_never_recommends_live_or_paper():
    for results in ([_res(STATUS_GREEN)], [_res(STATUS_WATCH)], [_res("REJECT")]):
        d = build_next_research_decision(results)
        assert d["max_label"] == "SHADOW_RESEARCH_ONLY_FUTURE"
        assert d["final_recommendation"] == "NO LIVE"
        assert "LIVE" not in d["suggested_next_code_prompt_type"]


# --- end-to-end synthetic + invariants (10, 11, 12, 13) ---

def _gen(sym, n=1600, seed=0):
    random.seed(seed)
    rows = []
    price = 100.0
    drop = 0
    spikes = set(range(200, n, 12))
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


def test_no_data_need_data():
    r = run_funding_oi_stability([], [], hours=2160)
    assert r.status == STATUS_NEED_DATA
    assert r.final_recommendation == "NO LIVE"
    assert r.paper_ready is False and r.live_ready is False


def test_end_to_end_structure_and_invariants():
    market = _gen("ETHUSDT", seed=1) + _gen("BTCUSDT", seed=2)
    r = run_funding_oi_stability(market, [], hours=100000, missing_oi_ratio=0.1522,
                                 bootstrap_n=150, baseline_n=100)
    assert r.status == "OK"
    assert r.buckets, "expected buckets"
    allowed = {STATUS_GREEN, STATUS_WATCH, STATUS_OOS_FAIL, STATUS_COST_FAIL,
               STATUS_REGIME_UNSTABLE, STATUS_MISSING_OI, STATUS_NEED_MORE, "REJECT"}
    for b in r.buckets:
        assert b["stability_status"] in allowed
    # OI-based buckets must carry missing_oi_risk=true at 15.22% missing
    oi_buckets = [b for b in r.buckets if b["bucket_id"] in
                  ("oi_z_ge_15__SHORT", "oi_pct_ge_90__SHORT", "oi_up_24h_price_down__SHORT")
                  and b["symbol_scope"] == "ETHUSDT"]
    assert oi_buckets and all(b["missing_oi_risk"] for b in oi_buckets)
    # none of those OI buckets can be STABILITY_GREEN with missing-OI risk
    assert all(b["stability_status"] != STATUS_GREEN for b in oi_buckets)
    assert r.paper_ready is False and r.live_ready is False
    assert r.next_research_decision.get("final_recommendation") == "NO LIVE"


def test_table_columns_complete():
    market = _gen("ETHUSDT", seed=3) + _gen("BTCUSDT", seed=4)
    r = run_funding_oi_stability(market, [], hours=100000, bootstrap_n=120, baseline_n=80)
    rows = stability_table_rows(r)
    assert rows
    for row in rows:
        assert set(row.keys()) == set(STABILITY_TABLE_COLUMNS)
        assert row["final_recommendation"] == "NO LIVE"


def test_no_lookahead_features_reused():
    # The stability tool reuses build_features (no-lookahead, tested in the
    # diagnostics suite). Confirm the dependency is the audited one.
    from app.labs.external_funding_oi_stability_v10_1 import build_features as sf
    from app.labs.external_funding_oi_diagnostics_v10_1 import build_features as df
    assert sf is df


def test_safety_scan_module():
    mod = "app.labs.external_funding_oi_stability_v10_1"
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
                "import paper_trader", "import execution_engine", "COINALYZE_API_KEY"):
        assert tok not in src, f"contains {tok}"
