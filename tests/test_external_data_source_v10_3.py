"""ResearchOps V10.3 — data source strategy + provider audit tests.

All synthetic. No DB, no network, no real data, no secrets.
"""

from __future__ import annotations

import ast
import importlib
import pathlib

from app.labs.external_data_provider_registry_v10_3 import (
    CLASS_INITIAL,
    CLASS_INTERMEDIATE,
    CLASS_NO_DATA,
    OI_POLICY_ALLOW,
    OI_POLICY_BLOCK,
    READY_INITIAL,
    READY_NEED_LONG_HISTORY,
    ST_CURRENT,
    ST_PROXY_ONLY,
    recommended_next_providers,
    registry_rows,
    run_data_source_audit,
    run_provider_readiness,
)

BASE = 1_780_000_000_000
STEP = 3600000


def _clean(n):
    return [{"symbol": "ETHUSDT", "timestamp_ms": BASE + i * STEP, "price_close": 100.0 + i,
             "price_high": 101.0, "price_low": 99.0, "funding_rate": 0.0001, "oi_usd_close": 9e8}
            for i in range(n)]


def _raw(n, missing_slice=None):
    rows = []
    for i in range(n):
        oi = 9e8
        if missing_slice and missing_slice[0] <= i < missing_slice[1]:
            oi = ""
        rows.append({"symbol": "ETHUSDT", "timestamp": BASE + i * STEP, "price_close": 100.0,
                     "funding_rate": 0.0001, "oi_usd_close": oi, "source": "coinalyze"})
    return rows


# --- registry ---

def test_registry_has_core_providers():
    rows = registry_rows()
    ids = {r["provider_id"] for r in rows}
    assert {"coinalyze", "tardis_dev", "coinglass", "coinapi", "kaiko",
            "ccdata_cryptocompare", "bitget_official", "binance_okx_proxy"} <= ids


def test_coinalyze_is_current_and_not_180d():
    row = next(r for r in registry_rows() if r["provider_id"] == "coinalyze")
    assert row["status"] == ST_CURRENT
    assert row["suitable_for_180d"] is False  # intraday cap ~84d


def test_binance_okx_is_proxy_only():
    row = next(r for r in registry_rows() if r["provider_id"] == "binance_okx_proxy")
    assert row["status"] == ST_PROXY_ONLY
    assert row["bitget_perp_support"] is False


def test_recommended_candidates_are_180d_capable():
    cands = recommended_next_providers()
    assert cands == ["tardis_dev", "coinglass"]


def test_needs_manual_verification_present():
    pr = run_provider_readiness()
    assert "coinapi" in pr.needs_manual_verification
    assert pr.recommended_next_provider == "tardis_dev"
    assert pr.live_ready is False
    assert pr.final_recommendation == "NO LIVE"


# --- audit: strict gating ---

def test_audit_short_history_need_long_history():
    r = run_data_source_audit(_clean(60 * 24), _raw(60 * 24), hours=8760)
    assert r.backtester_readiness == READY_NEED_LONG_HISTORY
    assert r.data_classification == CLASS_INTERMEDIATE
    assert r.paper_ready is False
    assert r.live_ready is False


def test_audit_84d_is_intermediate_research_only():
    r = run_data_source_audit(_clean(84 * 24), _raw(84 * 24), hours=8760)
    assert r.data_classification == CLASS_INTERMEDIATE
    assert r.current_clean_days < 180


def test_audit_missing_oi_clustered_blocks_oi_buckets():
    raw = _raw(2000, missing_slice=(100, 600))  # 500 consecutive missing => clustered
    r = run_data_source_audit(_clean(80 * 24), raw, hours=8760)
    assert r.oi_bucket_policy == OI_POLICY_BLOCK
    assert "oi_pure_buckets_promotion" in r.blocked_actions
    assert r.current_missing_oi_ratio > 0.10


def test_audit_missing_oi_raw_absent_blocks_oi_buckets_even_with_180d_clean():
    r = run_data_source_audit(_clean(200 * 24), [], hours=8760)
    assert r.missing_oi_status == "NEED_MORE_DATA"
    assert r.oi_bucket_policy == OI_POLICY_BLOCK
    assert "missing_oi_audit_unavailable" in r.data_blockers
    assert "oi_pure_buckets_promotion" in r.blocked_actions
    assert r.paper_ready is False
    assert r.live_ready is False
    assert r.final_recommendation == "NO LIVE"


def test_audit_no_clean_data_blocks_oi_buckets_and_live():
    r = run_data_source_audit([], [], hours=8760)
    assert r.backtester_readiness == READY_NEED_LONG_HISTORY
    assert r.data_classification == CLASS_NO_DATA
    assert r.oi_bucket_policy == OI_POLICY_BLOCK
    assert "missing_oi_audit_unavailable" in r.data_blockers
    assert r.paper_ready is False
    assert r.live_ready is False
    assert r.final_recommendation == "NO LIVE"


def test_audit_high_missing_oi_ratio_blocks_oi_buckets():
    raw = _raw(2000)
    for i in range(0, len(raw), 5):
        raw[i]["oi_usd_close"] = ""
    r = run_data_source_audit(_clean(200 * 24), raw, hours=8760)
    assert r.current_missing_oi_ratio > 0.10
    assert r.oi_bucket_policy == OI_POLICY_BLOCK
    assert "oi_pure_buckets_promotion" in r.blocked_actions


def test_audit_180d_ready_but_never_live():
    r = run_data_source_audit(_clean(200 * 24), _raw(200 * 24), hours=8760)
    assert r.data_classification == CLASS_INITIAL
    assert r.backtester_readiness == READY_INITIAL
    assert r.oi_bucket_policy == OI_POLICY_ALLOW
    assert r.live_ready is False
    assert r.paper_ready is False
    assert r.final_recommendation == "NO LIVE"


def test_audit_no_data():
    r = run_data_source_audit([], [], hours=8760)
    assert r.data_classification == CLASS_NO_DATA
    assert r.backtester_readiness == READY_NEED_LONG_HISTORY
    assert r.oi_bucket_policy == OI_POLICY_BLOCK


def test_audit_always_blocks_paper_and_live():
    for n in (0, 60 * 24, 200 * 24, 400 * 24):
        r = run_data_source_audit(_clean(n), [], hours=8760)
        assert r.live_ready is False
        assert r.paper_ready is False
        assert r.can_send_real_orders is False
        assert "live_trading" in r.blocked_actions
        assert "enable_paper_policy_filter" in r.blocked_actions
        assert "paper_trading" in r.blocked_actions


# --- doc ---

def test_data_source_strategy_doc_exists():
    p = pathlib.Path(__file__).resolve().parents[1] / "docs" / "research_v10_3_data_source_strategy.md"
    assert p.exists()
    t = p.read_text(encoding="utf-8")
    assert "NEEDS_MANUAL_VERIFICATION" in t
    assert "NO LIVE" in t
    for prov in ("Coinalyze", "Tardis.dev", "CoinGlass", "Kaiko", "CoinAPI",
                 "Bitget official", "Binance/OKX"):
        assert prov in t


# --- safety ---

def test_safety_scan_module():
    mod = "app.labs.external_data_provider_registry_v10_3"
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
