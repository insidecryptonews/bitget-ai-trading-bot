"""ResearchOps V10.7 — adversarial tests for the Bitget PUBLIC data collector.

NO test here touches the real internet: network is injected as a ``transport``
callable and ``_raw_http_get`` is monkeypatched to fail if ever reached on a
blocked path. Invariants: GET-only public allowlist, no auth headers, dry-run
writes nothing, apply writes ONLY under staging, no readiness is ever flipped.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.labs import bitget_public_data_v10_7 as B
from app.labs.provider_sample_validator_v10_6 import build_sample_manifest, validate_sample_dir

MODULE_PATH = "app/labs/bitget_public_data_v10_7.py"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _fake_transport_factory():
    """Returns one candle page then empty (terminates pagination); funding one
    page; OI one snapshot. Records calls. Never hits the network."""
    state = {"candle_pages": 0, "calls": []}

    def transport(path, params, *, timeout=10.0):
        state["calls"].append((path, dict(params)))
        if path == B.EP_CANDLES:
            state["candle_pages"] += 1
            if state["candle_pages"] == 1:
                st = int(params["startTime"])
                bar = 3_600_000
                return {"code": "00000", "data": [
                    [st, "100", "110", "90", "105", "12", "1200"],
                    [st + bar, "105", "108", "104", "106", "8", "850"]]}
            return {"code": "00000", "data": []}
        if path == B.EP_FUNDING:
            return {"code": "00000", "data": [
                {"symbol": params["symbol"], "fundingRate": "0.0001",
                 "fundingTime": str(B._now_ms())}]}
        if path == B.EP_OI:
            return {"code": "00000", "data": {
                "openInterestList": [{"symbol": params["symbol"], "size": "123.4"}],
                "ts": str(B._now_ms())}}
        return {"code": "00000", "data": []}

    return transport, state


def _write_staging_candles(root: Path, symbol: str, tf: str, rows: list[str],
                           fname: str | None = None) -> Path:
    d = root / "candles" / symbol
    d.mkdir(parents=True, exist_ok=True)
    p = d / (fname or f"{tf}.csv")
    header = ",".join(B._CSV_HEADERS["candles"])
    p.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return p


def _candle_row(ts: int, symbol="BTCUSDT", tf="1h", o=100, h=110, l=90, c=105, v=12):
    return f"{ts},{symbol},{B.PRODUCT_TYPE},{tf},{o},{h},{l},{c},{v},1200,{B.DATA_SOURCE},x"


# --------------------------------------------------------------------------
# A. Endpoint registry
# --------------------------------------------------------------------------

def test_registry_is_public_get_only():
    rep = B.endpoint_registry_report()
    assert rep["no_private_auth"] is True
    assert rep["no_env"] is True
    assert rep["public_get_only"] is True
    assert rep["final_recommendation"] == "NO LIVE"
    for ep in rep["endpoints"]:
        assert ep["method"] == "GET"
        assert ep["auth_required"] is False
    assert set(rep["allowed_paths"]) == {B.EP_CANDLES, B.EP_FUNDING, B.EP_OI}


# --------------------------------------------------------------------------
# D. Network allowlist
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://api.bitget.com/api/v2/mix/order/place-order",
    "https://api.bitget.com/api/v2/mix/account/accounts",
    "https://api.bitget.com/api/v2/mix/position/all-position",
    "https://evil.com/api/v2/mix/market/candles",
    "http://api.bitget.com/api/v2/mix/market/candles",
    "https://api.bitget.com/api/v2/spot/trade/place-order",
])
def test_allowlist_blocks_disallowed_urls(url):
    with pytest.raises(B.UnsafeRequestError):
        B.assert_safe_request("GET", url)


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_allowlist_blocks_non_get_methods(method):
    with pytest.raises(B.UnsafeRequestError):
        B.assert_safe_request(method, "https://api.bitget.com/api/v2/mix/market/candles")


@pytest.mark.parametrize("header", ["ACCESS-KEY", "ACCESS-SIGN",
                                    "ACCESS-PASSPHRASE", "ACCESS-TIMESTAMP"])
def test_allowlist_blocks_auth_headers(header):
    with pytest.raises(B.UnsafeRequestError):
        B.assert_safe_request("GET", "https://api.bitget.com/api/v2/mix/market/candles",
                              headers={header: "x"})


def test_good_public_url_allowed():
    assert B.assert_safe_request(
        "GET", "https://api.bitget.com/api/v2/mix/market/candles?symbol=BTCUSDT") is True


def test_default_transport_blocks_private_before_network(monkeypatch):
    # _raw_http_get must NEVER be reached for a blocked path.
    def _boom(*a, **k):
        raise AssertionError("network reached for a blocked path!")
    monkeypatch.setattr(B, "_raw_http_get", _boom)
    with pytest.raises(B.UnsafeRequestError):
        B.default_transport("/api/v2/mix/order/place-order", {"symbol": "BTCUSDT"})


def test_default_transport_sends_no_auth_headers(monkeypatch):
    captured = {}

    def _capture(url, timeout):
        captured["url"] = url
        return {"code": "00000", "data": []}
    monkeypatch.setattr(B, "_raw_http_get", _capture)
    B.default_transport(B.EP_CANDLES, {"symbol": "BTCUSDT", "granularity": "1H"})
    assert captured["url"].startswith("https://api.bitget.com/api/v2/mix/market/candles")
    for forbidden in ("ACCESS-KEY", "ACCESS-SIGN", "access-key"):
        assert forbidden not in captured["url"]


# --------------------------------------------------------------------------
# B. Planner
# --------------------------------------------------------------------------

def test_plan_has_limitations_and_no_live():
    plan = B.build_plan_v107()
    assert plan["free"] is True and plan["api_key_required"] is False
    assert plan["provider_verified"] is False
    assert any("no long historical open interest" in x for x in plan["limitations"])
    assert any("180/365d" in x for x in plan["limitations"])
    assert plan["paper_ready"] is False and plan["live_ready"] is False
    assert plan["final_recommendation"] == "NO LIVE"


# --------------------------------------------------------------------------
# C. Parsers
# --------------------------------------------------------------------------

def test_parse_candles_ok():
    payload = {"code": "00000", "data": [
        [1700000000000, "100", "110", "90", "105", "12", "1200"]]}
    rows = B.parse_candles(payload, symbol="BTCUSDT", timeframe="1h")
    assert len(rows) == 1
    r = rows[0]
    assert r["timestamp_ms"] == 1700000000000 and r["close"] == 105.0
    assert r["symbol"] == "BTCUSDT" and r["source"] == B.DATA_SOURCE


def test_parse_funding_ok():
    payload = {"code": "00000", "data": [
        {"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingTime": "1700000000000"}]}
    rows = B.parse_funding(payload, symbol="BTCUSDT")
    assert len(rows) == 1 and rows[0]["funding_rate"] == 0.0001


def test_parse_oi_snapshot_ok():
    payload = {"code": "00000", "data": {
        "openInterestList": [{"symbol": "BTCUSDT", "size": "12345.6"}],
        "ts": "1700000000000"}}
    rows = B.parse_oi_snapshot(payload, symbol="BTCUSDT")
    assert len(rows) == 1 and rows[0]["open_interest_size"] == 12345.6


def test_parse_candles_tolerant_of_garbage():
    payload = {"data": [["x", "y"], None, [1700000000000, "1", "2", "1", "1.5"]]}
    rows = B.parse_candles(payload, symbol="BTCUSDT", timeframe="1h")
    assert len(rows) == 1  # garbage rows skipped, valid kept


# --------------------------------------------------------------------------
# C/E. Fetcher — dry-run vs apply
# --------------------------------------------------------------------------

def test_dry_run_writes_nothing(tmp_path):
    rep = B.run_fetch_v107(symbols=["BTCUSDT"], timeframes=["1h"], days=5,
                           data_types=["candles", "funding"], apply=False,
                           staging_root=str(tmp_path), sleep_fn=lambda *_: None)
    assert rep["dry_run"] is True
    assert rep["staging_dir"] == ""
    assert list(tmp_path.iterdir()) == []  # nothing written
    assert rep["paper_ready"] is False and rep["live_ready"] is False


def test_apply_writes_only_under_staging(tmp_path):
    transport, state = _fake_transport_factory()
    rep = B.run_fetch_v107(symbols=["BTCUSDT"], timeframes=["1h"], days=2,
                           data_types=["candles", "funding", "oi_snapshot"],
                           apply=True, transport=transport,
                           staging_root=str(tmp_path), sleep_fn=lambda *_: None)
    assert rep["dry_run"] is False
    run_dir = Path(rep["staging_dir"])
    # everything written lives under tmp_path (staging root) — never raw/db/.env
    assert str(run_dir).startswith(str(tmp_path))
    assert (run_dir / "run_report.json").is_file()
    assert (run_dir / "candles" / "BTCUSDT" / "1h.csv").is_file()
    assert (run_dir / "funding" / "BTCUSDT" / "funding.csv").is_file()
    assert sum(rep["rows_written"].values()) > 0
    assert rep["paper_ready"] is False and rep["live_ready"] is False
    assert rep["final_recommendation"] == "NO LIVE"
    # only the allowed public endpoints were called
    assert set(rep["endpoints_called"]) <= {B.EP_CANDLES, B.EP_FUNDING, B.EP_OI}


def test_apply_with_failing_transport_records_errors_not_crash(tmp_path):
    def boom(path, params, *, timeout=10.0):
        raise OSError("network down")
    rep = B.run_fetch_v107(symbols=["BTCUSDT"], timeframes=["1h"], days=2,
                           data_types=["candles"], apply=True, transport=boom,
                           staging_root=str(tmp_path), sleep_fn=lambda *_: None,
                           max_retries=1)
    assert rep["errors"]  # errors captured
    assert rep["final_recommendation"] == "NO LIVE"  # still safe


def test_fetch_rejects_empty_request():
    rep = B.run_fetch_v107(symbols=[], timeframes=[], days=30,
                           data_types=[], apply=False)
    assert any("nothing_to_fetch" in e for e in rep["errors"])


# --------------------------------------------------------------------------
# F. Staging audit
# --------------------------------------------------------------------------

def test_audit_clean_staging_ok(tmp_path):
    rows = [_candle_row(1700000000000 + i * 3_600_000) for i in range(10)]
    _write_staging_candles(tmp_path, "BTCUSDT", "1h", rows)
    rep = B.audit_staging_v107(str(tmp_path))
    assert rep["audit_status"] == "STAGING_OK"
    assert rep["rows_total"] == 10
    assert rep["blockers"] == []
    assert rep["paper_ready"] is False and rep["live_ready"] is False


def test_audit_invalid_ohlcv_blocked(tmp_path):
    rows = [_candle_row(1700000000000, h=80, l=90)]  # high < low
    _write_staging_candles(tmp_path, "BTCUSDT", "1h", rows)
    rep = B.audit_staging_v107(str(tmp_path))
    assert rep["audit_status"] == "STAGING_BLOCKED"
    assert any(b.startswith("candles_invalid_rows") for b in rep["blockers"])


def test_audit_duplicate_timestamps_blocked(tmp_path):
    rows = [_candle_row(1700000000000), _candle_row(1700000000000)]
    _write_staging_candles(tmp_path, "BTCUSDT", "1h", rows)
    rep = B.audit_staging_v107(str(tmp_path))
    assert any(b.startswith("duplicate_timestamps") for b in rep["blockers"])


def test_audit_gaps_detected_as_warning(tmp_path):
    rows = [_candle_row(1700000000000), _candle_row(1700000000000 + 3_600_000),
            _candle_row(1700000000000 + 5 * 3_600_000)]  # gap
    _write_staging_candles(tmp_path, "BTCUSDT", "1h", rows)
    rep = B.audit_staging_v107(str(tmp_path))
    assert any(w.startswith("gap_count") for w in rep["warnings"])
    assert rep["audit_status"] in ("STAGING_HAS_WARNINGS", "STAGING_BLOCKED")


def test_audit_percent_encoded_path_blocked(tmp_path):
    rows = [_candle_row(1700000000000)]
    _write_staging_candles(tmp_path, "BTCUSDT", "1h", rows, fname="1h%2e.csv")
    rep = B.audit_staging_v107(str(tmp_path))
    assert "percent_encoded_path" in rep["blockers"]
    assert rep["audit_status"] == "STAGING_BLOCKED"


def test_audit_refuses_raw_directory(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    rep = B.audit_staging_v107(str(raw))
    assert "refuses_raw_directory" in rep["blockers"]


def test_audit_missing_dir():
    rep = B.audit_staging_v107("does/not/exist")
    assert "staging_dir_not_found" in rep["blockers"]
    assert rep["paper_ready"] is False and rep["live_ready"] is False


# --------------------------------------------------------------------------
# G. Integration with V10.6 — NO readiness bypass
# --------------------------------------------------------------------------

def test_to_sample_then_validate_does_not_bypass_readiness(tmp_path):
    # build a small staging set via apply + mock transport
    transport, _ = _fake_transport_factory()
    rep = B.run_fetch_v107(symbols=["BTCUSDT"], timeframes=["1h"], days=2,
                           data_types=["candles", "funding"], apply=True,
                           transport=transport, staging_root=str(tmp_path),
                           sleep_fn=lambda *_: None)
    run_dir = rep["staging_dir"]
    conv = B.staging_to_sample_v107(run_dir)
    sample_dir = conv["sample_dir"]
    assert any(n.endswith("_ohlcv.csv") for n in conv["written"])

    # the real V10.6 validator reads the converted sample (no bypass)
    v = validate_sample_dir(sample_dir, expected_days=30, provider_id="bitget_official")
    assert v["paper_ready"] is False and v["live_ready"] is False
    # a manifest built from it stays non-promotable (human auth still required)
    man = build_sample_manifest(sample_dir, expected_days=30,
                                provider_id="bitget_official", write=False)
    assert man["gate_promote_allowed"] is False
    assert man["explicit_human_authorization"] is False
    assert man["final_recommendation"] == "NO LIVE"


# --------------------------------------------------------------------------
# H. Collector status
# --------------------------------------------------------------------------

def test_collector_status_no_live():
    st = B.collector_status_v107()
    assert "candles" in st["implemented_endpoints"]
    assert any("liquidations" in x for x in st["still_missing"])
    assert st["no_private_auth"] is True and st["no_env"] is True
    assert st["paper_ready"] is False and st["live_ready"] is False
    assert st["final_recommendation"] == "NO LIVE"


# --------------------------------------------------------------------------
# L. Safety — static source scan
# --------------------------------------------------------------------------

def test_module_has_no_private_or_order_primitives():
    src = Path(MODULE_PATH).read_text(encoding="utf-8")
    forbidden = [
        "place_order", "create_order", "private_get", "private_post",
        "set_leverage", "set_margin_mode", "ExecutionEngine", "PaperTrader",
        "import torch", "from torch", "import jax", "import tensorflow",
        "import timesfm", "load_dotenv",
        # private/trading endpoint fragments must not be hardcoded as reachable
        "/api/v2/mix/order", "/api/v2/mix/account", "/api/v2/mix/position",
    ]
    for token in forbidden:
        assert token not in src, f"{MODULE_PATH} must not contain {token!r}"


def test_module_does_not_read_env_or_write_raw():
    src = Path(MODULE_PATH).read_text(encoding="utf-8")
    # Real env-access patterns must be absent (prose mentions of ".env" in the
    # docstring that say it is NOT used are fine — scan for actual access).
    for token in ("os.environ", "os.getenv", "load_dotenv", "dotenv",
                  'open(".env', "open('.env"):
        assert token not in src, f"{MODULE_PATH} must not contain {token!r}"
    # the only configured write root is the staging dir (raw-dir refusal and
    # staging-only writes are covered behaviourally by the audit/apply tests).
    assert "external_data/staging/bitget_public_v10_7" in src
