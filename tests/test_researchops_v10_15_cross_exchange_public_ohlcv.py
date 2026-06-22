"""ResearchOps V10.15 - Cross-Exchange PUBLIC OHLCV collector tests.

Mocked transport (no real network). Verifies the public-GET safety model
(allowlist, no auth, no private endpoints), fail-closed staging path gate
(blocks raw/db/.env/traversal), bounded pagination (days + max_requests),
flat normalization, missing-symbol reporting, and the NO-LIVE invariants.
"""

from __future__ import annotations

import csv
import os
import shutil
from pathlib import Path

import pytest

from app.labs import cross_exchange_public_ohlcv_v10_15 as X

MODULE_PATH = "app/labs/cross_exchange_public_ohlcv_v10_15.py"
HOUR = X.TF_MS["1h"]


def _binance_tx_factory(dataset_start, dataset_end):
    """Mock Binance transport: returns ascending klines within [startTime,endTime]."""
    calls = {"n": 0}

    def tx(exchange, params):
        calls["n"] += 1
        start = int(params["startTime"]); end = int(params["endTime"])
        limit = int(params["limit"])
        out = []
        t = max(start, dataset_start)
        while t <= min(end, dataset_end) and len(out) < limit:
            out.append([t, "100", "101", "99", "100.5", "10", t + HOUR, "0", 0, "0", "0", "0"])
            t += HOUR
        return out  # binance returns a JSON array

    return tx, calls


# 1. plan makes no network and declares public/no-auth
def test_plan_no_network():
    p = X.cross_exchange_plan(["binance_futures", "bybit_linear"], ["BTCUSDT"], "1h", 365)
    assert p["no_network"] is True and p["public_only"] is True and p["auth"] == "none"
    assert p["planned_fetches"] and p["final_recommendation"] == "NO LIVE"


# 2. dry-run does not touch the network or write
def test_dry_run_no_writes():
    tx, calls = _binance_tx_factory(0, 0)
    rep = X.cross_exchange_fetch(exchanges=["binance_futures"], symbols=["BTCUSDT"],
                                 timeframe="1h", days=365, apply=False, transport=tx)
    assert rep["dry_run"] is True and calls["n"] == 0 and rep["staging_dirs"] == {}
    assert rep["planned_fetches"]


# 3 + 11. apply writes ONLY under safe staging, flat normalized
def test_apply_writes_flat_safe_staging():
    root = "external_data/staging/cross_exchange_public_ohlcv_v10_15/_pytest_tmp"
    now = X._now_ms()
    tx, _ = _binance_tx_factory(now - 5 * 24 * HOUR, now)
    try:
        rep = X.cross_exchange_fetch(exchanges=["binance_futures"], symbols=["BTCUSDT", "ETHUSDT"],
                                     timeframe="1h", days=5, apply=True, transport=tx,
                                     staging_root=root, rate_per_s=0)
        assert rep["errors"] == [] and rep["coverage"]
        d = rep["staging_dirs"]["binance_futures"]
        assert d.startswith("external_data/staging/cross_exchange_public_ohlcv_v10_15")
        f = os.path.join(d, "BTCUSDT_1h_ohlcv.csv")
        assert os.path.isfile(f)
        hdr = open(f, encoding="utf-8").readline().strip()
        assert hdr == "timestamp,open,high,low,close,volume"
    finally:
        shutil.rmtree(root, ignore_errors=True)


# 4/5/6. staging gate blocks raw / db / .env / traversal; allows the marker
def test_staging_gate_blocks_unsafe():
    assert X.safe_staging_dir("external_data/raw/x") is not None
    assert X.safe_staging_dir("external_data/staging/cross_exchange_public_ohlcv_v10_15/db.sqlite") is not None
    assert X.safe_staging_dir("external_data/staging/cross_exchange_public_ohlcv_v10_15/.env") is not None
    assert X.safe_staging_dir("external_data/staging/cross_exchange_public_ohlcv_v10_15/../x") is not None
    assert X.safe_staging_dir("somewhere/else/run1") is not None   # missing marker
    assert X.safe_staging_dir("external_data/staging/cross_exchange_public_ohlcv_v10_15/binance/run1") is None


# apply with an unsafe staging_root is blocked before any write
def test_apply_blocks_unsafe_root():
    tx, calls = _binance_tx_factory(0, 0)
    rep = X.cross_exchange_fetch(exchanges=["binance_futures"], symbols=["BTCUSDT"],
                                 timeframe="1h", days=5, apply=True, transport=tx,
                                 staging_root="external_data/raw/evil", rate_per_s=0)
    assert rep.get("blocked") is True
    assert any("staging_dir_rejected" in e for e in rep["errors"])
    assert calls["n"] == 0


# 7. auth headers are rejected
def test_no_auth_headers():
    url = "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1h"
    assert X.assert_safe_request("GET", url, headers={}) is True
    with pytest.raises(X.UnsafeRequestError):
        X.assert_safe_request("GET", url, headers={"X-MBX-APIKEY": "k"})
    with pytest.raises(X.UnsafeRequestError):
        X.assert_safe_request("GET", url, headers={"Authorization": "Bearer x"})


# 8. private / non-allowlisted endpoints are rejected
def test_no_private_endpoint():
    with pytest.raises(X.UnsafeRequestError):
        X.assert_safe_request("GET", "https://fapi.binance.com/fapi/v1/order?x=1")
    with pytest.raises(X.UnsafeRequestError):
        X.assert_safe_request("GET", "https://api.bybit.com/v5/order/create")
    with pytest.raises(X.UnsafeRequestError):
        X.assert_safe_request("POST", "https://fapi.binance.com/fapi/v1/klines")
    with pytest.raises(X.UnsafeRequestError):
        X.assert_safe_request("GET", "http://fapi.binance.com/fapi/v1/klines")  # not https


# 9. pagination respects --days (fewer days -> fewer bars, bounded start)
def test_pagination_respects_days():
    now = X._now_ms()
    tx, _ = _binance_tx_factory(now - 400 * 24 * HOUR, now)
    rep = {"errors": []}
    rows5, _ = X.fetch_series(tx, "binance_futures", "BTCUSDT", "1h", days=5,
                              request_budget=50, rate_per_s=0, rep=rep)
    rows30, _ = X.fetch_series(tx, "binance_futures", "BTCUSDT", "1h", days=30,
                               request_budget=50, rate_per_s=0, rep=rep)
    assert len(rows30) > len(rows5) > 0
    assert rows5[0]["ts"] >= now - 6 * 24 * HOUR   # did not page before the window


# 10. pagination respects max_requests budget
def test_pagination_respects_budget():
    now = X._now_ms()
    tx, _ = _binance_tx_factory(now - 400 * 24 * HOUR, now)
    rep = {"errors": []}
    rows, used = X.fetch_series(tx, "binance_futures", "BTCUSDT", "1h", days=365,
                                request_budget=3, rate_per_s=0, rep=rep)
    assert used == 3 and len(rows) > 0


# 12. missing symbol is reported, not crashed
def test_missing_symbol_reported():
    def empty_tx(exchange, params):
        return []   # nothing available
    rep = X.cross_exchange_fetch(exchanges=["binance_futures"], symbols=["NOPEUSDT"],
                                 timeframe="1h", days=5, apply=True, transport=empty_tx,
                                 staging_root="external_data/staging/cross_exchange_public_ohlcv_v10_15/_pytest_missing",
                                 rate_per_s=0)
    try:
        assert "binance_futures:NOPEUSDT" in rep["missing_symbols"]
    finally:
        shutil.rmtree("external_data/staging/cross_exchange_public_ohlcv_v10_15/_pytest_missing", ignore_errors=True)


# 13/14/15/16. NO-LIVE invariants on every output
def test_no_live_invariants():
    for out in (X.cross_exchange_plan(["binance_futures"], ["BTCUSDT"]),
                X.cross_exchange_fetch(exchanges=["binance_futures"], symbols=["BTCUSDT"], apply=False)):
        assert out["research_only"] is True and out["shadow_only"] is True
        assert out["paper_ready"] is False and out["live_ready"] is False
        assert out["can_send_real_orders"] is False
        assert out["paper_candidate_future"] is False
        assert out["final_recommendation"] == "NO LIVE"


# source scan: no private/auth/key/db primitives
def test_module_no_dangerous_primitives():
    src = Path(MODULE_PATH).read_text(encoding="utf-8")
    for tok in ["place_order", "create_order", "load_dotenv", "os.environ",
                "import ccxt", "api_key", "apiKey", "ACCESS-KEY", "db.execute",
                "INSERT INTO", "set_leverage"]:
        assert tok not in src, tok
    # parsers exist for both venues
    assert "parse_binance" in src and "parse_bybit" in src
