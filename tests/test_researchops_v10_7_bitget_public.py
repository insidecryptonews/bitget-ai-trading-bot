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


def _staging_root(tmp_path) -> str:
    """A temp path that contains the V10.7 staging marker, so staging-only
    write enforcement accepts it (mirrors external_data/staging/bitget_public_v10_7)."""
    p = tmp_path / "external_data" / "staging" / "bitget_public_v10_7"
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


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
    root = _staging_root(tmp_path)
    rep = B.run_fetch_v107(symbols=["BTCUSDT"], timeframes=["1h"], days=2,
                           data_types=["candles", "funding", "oi_snapshot"],
                           apply=True, transport=transport,
                           staging_root=root, sleep_fn=lambda *_: None)
    assert rep["dry_run"] is False
    run_dir = Path(rep["staging_dir"])
    # everything written lives under the staging root — never raw/db/.env
    assert str(run_dir).startswith(root)
    assert "bitget_public_v10_7" in str(run_dir)
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
                           staging_root=_staging_root(tmp_path), sleep_fn=lambda *_: None,
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
                           transport=transport, staging_root=_staging_root(tmp_path),
                           sleep_fn=lambda *_: None)
    run_dir = rep["staging_dir"]
    conv = B.staging_to_sample_v107(run_dir)
    sample_dir = conv["sample_dir"]
    assert conv.get("blocked") is not True
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
# V10.7.1 — staging-ONLY write enforcement (Codex hotfix)
# --------------------------------------------------------------------------

def _make_candle_dir(parent: Path) -> Path:
    d = parent / "candles" / "BTCUSDT"
    d.mkdir(parents=True, exist_ok=True)
    (d / "1h.csv").write_text(
        ",".join(B._CSV_HEADERS["candles"]) + "\n" + _candle_row(1700000000000) + "\n",
        encoding="utf-8")
    return parent


def test_to_sample_refuses_raw_directory(tmp_path):
    raw = _make_candle_dir(tmp_path / "external_data" / "raw")
    conv = B.staging_to_sample_v107(str(raw))
    assert conv["blocked"] is True
    assert any("refuses_raw_directory" in e for e in conv["errors"])
    assert conv["sample_dir"] == ""
    assert not (raw / B.SAMPLE_SUBDIR).exists()  # NOTHING written into raw


def test_to_sample_refuses_outside_staging_root(tmp_path):
    outside = _make_candle_dir(tmp_path / "outside")
    conv = B.staging_to_sample_v107(str(outside))
    assert conv["blocked"] is True
    assert any("staging_dir_outside_allowed_root" in e for e in conv["errors"])
    assert not (outside / B.SAMPLE_SUBDIR).exists()


def test_to_sample_refuses_percent_encoded_path(tmp_path):
    conv = B.staging_to_sample_v107(
        str(tmp_path) + "/external_data/staging/bitget_public_v10_7/run%2e")
    assert conv["blocked"] is True
    assert any("percent_encoded_path_blocked" in e for e in conv["errors"])


def test_to_sample_refuses_traversal(tmp_path):
    conv = B.staging_to_sample_v107(
        str(tmp_path) + "/external_data/staging/bitget_public_v10_7/../raw")
    assert conv["blocked"] is True
    assert conv["sample_dir"] == ""


@pytest.mark.parametrize("bad", [
    "external_data/staging/bitget_public_v10_7/secrets",
    "external_data/staging/bitget_public_v10_7/vault",
    "external_data/staging/bitget_public_v10_7/backups",
    "external_data/staging/bitget_public_v10_7/data.zip",
    "external_data/staging/bitget_public_v10_7/.env",
    "external_data/staging/bitget_public_v10_7/x.db",
])
def test_to_sample_refuses_db_zip_env_backup_vault_paths(tmp_path, bad):
    target = str(tmp_path / bad)
    conv = B.staging_to_sample_v107(target)
    assert conv["blocked"] is True
    assert conv["sample_dir"] == ""


def test_to_sample_accepts_valid_bitget_public_staging_dir(tmp_path):
    run_dir = Path(_staging_root(tmp_path)) / "20260101T000000Z"
    _make_candle_dir(run_dir)
    conv = B.staging_to_sample_v107(str(run_dir))
    assert conv.get("blocked") is not True
    assert any(n.endswith("_ohlcv.csv") for n in conv["written"])
    sample_dir = Path(conv["sample_dir"])
    assert sample_dir.is_dir()
    assert "bitget_public_v10_7" in conv["sample_dir"]
    assert B.SAMPLE_SUBDIR in conv["sample_dir"]
    # the written sample stays strictly inside the run dir (no escape)
    assert str(sample_dir).startswith(str(run_dir))


def test_run_fetch_refuses_raw_staging_root_programmatic(tmp_path):
    transport, _ = _fake_transport_factory()
    raw_root = str(tmp_path / "external_data" / "raw")
    rep = B.run_fetch_v107(symbols=["BTCUSDT"], timeframes=["1h"], days=2,
                           data_types=["candles"], apply=True, transport=transport,
                           staging_root=raw_root, sleep_fn=lambda *_: None)
    assert rep.get("blocked") is True
    assert any("staging_root_rejected:refuses_raw_directory" in e for e in rep["errors"])
    assert rep["rows_written"] == {}
    assert not os.path.isdir(raw_root)  # never even created


def test_run_fetch_refuses_outside_staging_root_programmatic(tmp_path):
    transport, _ = _fake_transport_factory()
    rep = B.run_fetch_v107(symbols=["BTCUSDT"], timeframes=["1h"], days=2,
                           data_types=["candles"], apply=True, transport=transport,
                           staging_root=str(tmp_path / "nope"), sleep_fn=lambda *_: None)
    assert rep.get("blocked") is True
    assert any("staging_root_rejected:staging_dir_outside_allowed_root" in e
               for e in rep["errors"])
    assert rep["rows_written"] == {}


def test_dry_run_still_does_not_write_even_with_raw_root(tmp_path):
    # dry-run must never write, regardless of the (here unsafe) root.
    raw_root = str(tmp_path / "external_data" / "raw")
    rep = B.run_fetch_v107(symbols=["BTCUSDT"], timeframes=["1h"], days=2,
                           data_types=["candles"], apply=False,
                           staging_root=raw_root, sleep_fn=lambda *_: None)
    assert rep["dry_run"] is True
    assert rep["staging_dir"] == ""
    assert not os.path.isdir(raw_root)


def test_zero_rows_adds_warning(tmp_path):
    def empty_transport(path, params, *, timeout=10.0):
        return {"code": "00000", "data": []}
    rep = B.run_fetch_v107(symbols=["BTCUSDT"], timeframes=["1h"], days=2,
                           data_types=["candles", "funding"], apply=True,
                           transport=empty_transport, staging_root=_staging_root(tmp_path),
                           sleep_fn=lambda *_: None)
    assert "no_rows_written" in rep["warnings"]
    assert any(w.startswith("no_rows_written_for_") for w in rep["warnings"])
    assert rep["rows_written"] == {}
    assert rep["final_recommendation"] == "NO LIVE"


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


# --------------------------------------------------------------------------
# V10.7.2 — candle chunking + detailed HTTP errors + expected-data audit
# --------------------------------------------------------------------------
import json as _json  # noqa: E402

_BAR_MS = {"4h": 4 * 3600 * 1000, "6h": 6 * 3600 * 1000, "1h": 3600 * 1000}


def _windowed_candle_transport():
    """Returns full candle windows; ASSERTS every request interval <= 90 days
    (Bitget's cap) and records the window sizes seen."""
    seen = {"intervals_days": [], "endpoints": set()}

    def transport(path, params, *, timeout=10.0):
        seen["endpoints"].add(path)
        if path == B.EP_CANDLES:
            st, et = int(params["startTime"]), int(params["endTime"])
            interval = (et - st) / 86_400_000
            seen["intervals_days"].append(round(interval, 2))
            assert interval <= 90.0, f"window {interval}d exceeds Bitget 90d cap"
            bar = _BAR_MS["4h"]
            return {"code": "00000", "data": [
                [t, "100", "110", "90", "105", "12", "1200"]
                for t in range(st, et, bar)]}
        if path == B.EP_FUNDING:
            return {"code": "00000", "data": []}
        return {"code": "00000", "data": []}

    return transport, seen


def test_candles_chunking_splits_long_range(tmp_path):
    transport, seen = _windowed_candle_transport()
    rep = B.run_fetch_v107(symbols=["BTCUSDT"], timeframes=["4h"], days=180,
                           data_types=["candles"], apply=True, transport=transport,
                           staging_root=_staging_root(tmp_path), sleep_fn=lambda *_: None)
    # multiple windows, each <= 90 days
    assert len(seen["intervals_days"]) >= 2
    assert all(d <= 90.0 for d in seen["intervals_days"])
    # candle CSV written + rows present
    assert any(k.endswith("candles/BTCUSDT/4h.csv") for k in rep["rows_written"])
    assert sum(rep["rows_written"].values()) > 500  # ~1080 bars over 180d 4h
    assert rep["errors"] == []

    # verify rows are deduped + sorted ascending in the written CSV
    run_dir = Path(rep["staging_dir"])
    import csv as _csv
    with open(run_dir / "candles" / "BTCUSDT" / "4h.csv", encoding="utf-8") as fh:
        ts = [int(r["timestamp_ms"]) for r in _csv.DictReader(fh)]
    assert ts == sorted(ts)
    assert len(ts) == len(set(ts))


def test_candles_chunking_no_infinite_loop(tmp_path):
    # transport returns a full-limit page of IDENTICAL timestamps (no progress).
    def stuck_transport(path, params, *, timeout=10.0):
        if path == B.EP_CANDLES:
            st = int(params["startTime"])
            return {"code": "00000", "data": [
                [st, "100", "110", "90", "105", "1", "1"]
                for _ in range(B.CANDLE_PAGE_LIMIT)]}
        return {"code": "00000", "data": []}
    rep = B.run_fetch_v107(symbols=["BTCUSDT"], timeframes=["4h"], days=180,
                           data_types=["candles"], apply=True, transport=stuck_transport,
                           staging_root=_staging_root(tmp_path), sleep_fn=lambda *_: None)
    # terminates and stays within the hard request guard
    assert rep["requests_made"] <= B.MAX_REQUESTS_PER_RUN


def test_candles_http_error_includes_status_and_msg(tmp_path):
    def err_transport(path, params, *, timeout=10.0):
        raise B.BitgetApiError(
            status=400, code="00001",
            msg="startTime and endTime interval cannot be greater than 90 days")
    rep = B.run_fetch_v107(symbols=["BTCUSDT"], timeframes=["4h"], days=180,
                           data_types=["candles"], apply=True, transport=err_transport,
                           staging_root=_staging_root(tmp_path), sleep_fn=lambda *_: None,
                           max_retries=0)
    assert rep["errors"], "an error must be recorded"
    err = rep["errors"][0]
    for token in ("/api/v2/mix/market/candles", "BTCUSDT", "4h", "400", "00001",
                  "90 days"):
        assert token in err, f"{token!r} missing from {err!r}"
    assert "no_rows_written_for_BTCUSDT_candles" in rep["warnings"]


def _full_staging(tmp_path, *, with_candles=True, errors=None):
    run_dir = Path(_staging_root(tmp_path)) / "RUN1"
    sets = [("funding", "funding.csv", "funding",
             "1700000000000,BTCUSDT,usdt-futures,0.0001,bitget_public,x"),
            ("oi_snapshot", "oi_snapshot.csv", "oi_snapshot",
             "1700000000000,BTCUSDT,usdt-futures,123,bitget_public,x")]
    if with_candles:
        sets.insert(0, ("candles", "4h.csv", "candles",
                        "1700000000000,BTCUSDT,usdt-futures,4h,100,110,90,105,12,1200,bitget_public,x"))
    for dtype, fn, hkey, row in sets:
        d = run_dir / dtype / "BTCUSDT"
        d.mkdir(parents=True, exist_ok=True)
        (d / fn).write_text(",".join(B._CSV_HEADERS[hkey]) + "\n" + row + "\n",
                            encoding="utf-8")
    rr = {"data_types": ["candles", "funding", "oi_snapshot"],
          "symbols": ["BTCUSDT"], "timeframes": ["4h"],
          "errors": errors or [], "warnings": []}
    (run_dir / "run_report.json").write_text(_json.dumps(rr), encoding="utf-8")
    return run_dir


def test_audit_blocks_when_requested_candles_missing(tmp_path):
    run_dir = _full_staging(tmp_path, with_candles=False)
    rep = B.audit_staging_v107(str(run_dir))
    assert rep["audit_status"] != "STAGING_OK"
    assert rep["audit_status"] == "STAGING_BLOCKED"
    assert "expected_data_type_missing:candles" in rep["blockers"]
    assert rep["paper_ready"] is False and rep["live_ready"] is False


def test_audit_warns_when_run_report_errors_present(tmp_path):
    run_dir = _full_staging(tmp_path, with_candles=True,
                            errors=["request_failed:/api/v2/mix/market/candles:X:4h:400:00001:x"])
    rep = B.audit_staging_v107(str(run_dir))
    assert rep["audit_status"] != "STAGING_OK"  # never clean with run errors
    assert "run_report_errors_present" in rep["warnings"]


def test_audit_ok_when_expected_data_present(tmp_path):
    run_dir = _full_staging(tmp_path, with_candles=True, errors=[])
    rep = B.audit_staging_v107(str(run_dir))
    assert rep["audit_status"] == "STAGING_OK"
    assert rep["blockers"] == []
    assert rep["expected_data"]["run_report_found"] is True
    assert rep["final_recommendation"] == "NO LIVE"


def test_deep_4h_6h_plan_does_not_overpromise(tmp_path):
    plan = B.build_plan_v107()
    # the 90-day-per-request limit must be stated honestly
    assert "per_request_limit_note" in plan
    assert "90" in plan["per_request_limit_note"]
    # 6H is not asserted as a ready 180d source without verification
    note = plan["per_request_limit_note"].lower()
    assert "verify" in note or "not asserted" in note
    assert plan["provider_verified"] is False
    assert plan["final_recommendation"] == "NO LIVE"


def test_one_h_four_h_60d_still_works_in_mocks(tmp_path):
    # regression: the prior happy path (1H/4H, shorter range) still fetches.
    transport, seen = _windowed_candle_transport()
    rep = B.run_fetch_v107(symbols=["BTCUSDT"], timeframes=["1h", "4h"], days=60,
                           data_types=["candles", "funding"], apply=True,
                           transport=transport, staging_root=_staging_root(tmp_path),
                           sleep_fn=lambda *_: None)
    assert rep["errors"] == []
    assert any("candles/BTCUSDT/4h.csv" in k for k in rep["rows_written"])
    assert all(d <= 90.0 for d in seen["intervals_days"])


def test_sample_validate_surfaces_missing_ohlcv_human_warning(tmp_path):
    # funding-only sample (no OHLCV) must flag a human warning, stay not-ready.
    (tmp_path / "BTCUSDT_funding.csv").write_text(
        "timestamp,funding_rate\n1700000000000,0.0001\n", encoding="utf-8")
    v = validate_sample_dir(str(tmp_path), expected_days=30, provider_id="bitget_official")
    assert "ohlcv" in v["quality"]["required_types_missing"]
    assert any("ohlcv" in w for w in v.get("human_warnings", []))
    assert v["sample_ready"] is False
    assert v["paper_ready"] is False and v["live_ready"] is False
