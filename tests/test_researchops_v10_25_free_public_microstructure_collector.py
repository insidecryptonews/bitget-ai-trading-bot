"""ResearchOps V10.25 - Free Public Microstructure Collector tests.

Public-GET-only, dry-run by default, staging-only writes, NO keys/auth/DB/orders.
Network is always mocked here. Verifies converters produce V10.24.3-compatible
canonical CSVs and the NO-LIVE invariants.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from app import research_lab
from app.labs import free_public_microstructure_collector_v10_25 as F
from app.labs import microstructure_sample_adapter_v10_24 as M

MODULE_PATH = "app/labs/free_public_microstructure_collector_v10_25.py"


def _mock_transport(url, headers):
    F.assert_safe_request(url, headers)            # enforce allowlist even in tests
    assert not any(k.lower() in F._AUTH_HEADER_KEYS for k in headers)
    if "aggTrades" in url:
        return json.dumps([{"p": "62538.4", "q": "0.003", "T": 1782271461921 + i * 1000, "m": i % 2 == 0}
                           for i in range(20)]).encode()
    if "bookTicker" in url:
        return json.dumps({"symbol": "BTCUSDT", "bidPrice": "62538.4", "bidQty": "0.5",
                           "askPrice": "62538.5", "askQty": "0.9", "time": 1782271462508}).encode()
    if "openInterestHist" in url:
        return json.dumps([{"symbol": "BTCUSDT", "sumOpenInterest": "98059.1",
                            "timestamp": 1782270900000 + i * 300000} for i in range(10)]).encode()
    if "fundingRate" in url:
        return json.dumps([{"symbol": "BTCUSDT", "fundingRate": "-0.00001",
                            "fundingTime": 1782230400002 + i * 28800000} for i in range(10)]).encode()
    raise AssertionError(f"unexpected url {url}")


# ---- network safety -------------------------------------------------------

def test_allowlist_blocks_bad_host_path_and_auth():
    F.assert_safe_request("https://fapi.binance.com/fapi/v1/aggTrades?symbol=BTCUSDT", {})
    F.assert_safe_request("https://data.binance.vision/data/futures/um/daily/x.zip", {})
    for bad in ("https://evil.example.com/fapi/v1/x", "http://fapi.binance.com/fapi/v1/x",
                "https://fapi.binance.com/sapi/v1/account"):
        with pytest.raises(ValueError):
            F.assert_safe_request(bad, {})
    with pytest.raises(ValueError):
        F.assert_safe_request("https://fapi.binance.com/fapi/v1/x", {"X-MBX-APIKEY": "k"})


def test_plan_no_network_no_writes_no_live():
    p = F.free_microstructure_plan()
    assert p["writes_on_plan"] is False and p["uses_api_keys"] is False and p["uses_db"] is False
    assert p["paper_ready"] is False and p["live_ready"] is False
    assert p["can_send_real_orders"] is False and p["final_recommendation"] == "NO LIVE"
    verdicts = {s["source"]: s["verdict"] for s in p["sources"]}
    assert any(v == F.USABLE_FREE for v in verdicts.values())
    assert any(v == F.FORWARD_ONLY for v in verdicts.values())   # liquidations gap
    assert any(v == F.PAID_ONLY for v in verdicts.values())


def test_dry_run_no_network_no_writes(monkeypatch):
    monkeypatch.setattr(F, "default_transport",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network in dry-run")))
    rep = F.forward_collect("BTCUSDT", ["trades", "orderbook", "oi", "funding"], apply=False)
    assert rep["mode"] == "DRY_RUN" and rep["writes"] is False
    assert "staging_dir" not in rep
    assert set(rep["planned_urls"]) == {"trades", "orderbook", "oi", "funding"}


# ---- converters -> V10.24.3 canonical -------------------------------------

def test_aggtrades_aggressor_mapping():
    canon = F.aggtrades_to_canonical([{"p": "1", "q": "2", "T": 100, "m": True},
                                      {"p": "1", "q": "2", "T": 200, "m": False}], "BTCUSDT")
    assert canon[0]["aggressor_side"] == "sell"   # buyer is maker -> seller aggresses
    assert canon[1]["aggressor_side"] == "buy"
    assert canon[0]["symbol"] == "BTCUSDT"


def test_converters_produce_v1024_detectable_types():
    tr = F.aggtrades_to_canonical([{"p": "1", "q": "2", "T": 100, "m": True}], "BTCUSDT")
    ob = F.bookticker_to_canonical([{"time": 1, "bidPrice": "1", "bidQty": "2",
                                     "askPrice": "3", "askQty": "4"}], "BTCUSDT")
    oi = F.oi_to_canonical([{"timestamp": 1, "sumOpenInterest": "100"}], "BTCUSDT")
    fu = F.funding_to_canonical([{"fundingTime": 1, "fundingRate": "0.0001"}], "BTCUSDT")
    assert M.detect_type("trades.csv", list(tr[0])) == "trades"
    assert M.detect_type("orderbook_l2.csv", list(ob[0])) == "orderbook"
    assert M.detect_type("open_interest.csv", list(oi[0])) == "oi"
    assert M.detect_type("funding.csv", list(fu[0])) == "funding"


def test_apply_writes_only_staging_and_is_v1024_validatable():
    rep = F.forward_collect("BTCUSDT", ["trades", "orderbook", "oi", "funding"],
                            apply=True, transport=_mock_transport, rate_limit_seconds=0.0)
    try:
        assert rep["mode"] == "APPLY"
        assert F.STAGING_MARKER in rep["staging_dir"]
        assert {w["kind"] for w in rep["written"]} == {"trades", "orderbook", "oi", "funding"}
        assert not rep["errors"]
        # the produced staging dir is readable by the V10.24.3 validator
        vr = M.validate_sample(rep["staging_dir"])
        assert vr["by_type"]["trades"]["has_aggressor_side"] is True
        assert vr["by_type"]["orderbook"]["l1_imbalance_median"] is not None
        assert vr["final_recommendation"] == "NO LIVE"
    finally:
        shutil.rmtree(rep["staging_dir"], ignore_errors=True)
        try:
            os.rmdir(os.path.dirname(rep["staging_dir"]))
        except OSError:
            pass


def test_safe_staging_rejects_forbidden():
    F.safe_staging_dir(f"external_data/staging/{F.STAGING_MARKER}")
    for bad in ("external_data/raw/x", f"vault/{F.STAGING_MARKER}",
                "external_data/staging/other", "external_data/staging/x/../y"):
        with pytest.raises(ValueError):
            F.safe_staging_dir(bad)


def test_rate_limit_default_conservative():
    import inspect
    sig = inspect.signature(F.forward_collect)
    assert sig.parameters["rate_limit_seconds"].default >= 0.5


# ---- CLI isolation (no config/.env/DB) ------------------------------------

def _run_main(argv):
    old = sys.argv
    sys.argv = ["prog"] + argv
    try:
        research_lab.main()
    finally:
        sys.argv = old


def test_cli_allowlisted_and_isolated(monkeypatch, capsys):
    for c in ("free-microstructure-sources-plan-v1025",
              "free-microstructure-collector-dry-run-v1025",
              "free-microstructure-forward-collect-v1025"):
        assert c in research_lab.PUBLIC_RESEARCH_ONLY_COMMANDS
    monkeypatch.setattr(research_lab, "load_config",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no config")))
    monkeypatch.setattr(research_lab, "Database",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no db")))
    _run_main(["free-microstructure-sources-plan-v1025"])
    assert "FREE MICROSTRUCTURE SOURCES PLAN V10.25" in capsys.readouterr().out
    _run_main(["free-microstructure-collector-dry-run-v1025", "--symbols", "BTCUSDT"])
    out = capsys.readouterr().out
    assert "DRY_RUN" in out and "NO LIVE" in out


# ---- V10.25.1 endpoint allowlist + staging containment hardening ----------

def test_exact_endpoint_allowlist_blocks_private_and_methods():
    ok = ["https://fapi.binance.com/fapi/v1/aggTrades",
          "https://fapi.binance.com/fapi/v1/ticker/bookTicker",
          "https://fapi.binance.com/fapi/v1/fundingRate",
          "https://fapi.binance.com/futures/data/openInterestHist",
          "https://data.binance.vision/data/futures/um/daily/x.zip"]
    for u in ok:
        F.assert_safe_request(u, {})            # exact public GET endpoints pass
    blocked = ["https://fapi.binance.com/fapi/v1/account",
               "https://fapi.binance.com/fapi/v1/order",
               "https://fapi.binance.com/fapi/v1/leverage",
               "https://fapi.binance.com/fapi/v1/marginType",
               "https://fapi.binance.com/fapi/v1/positionRisk",
               "https://fapi.binance.com/fapi/v1/userDataStream",
               "https://fapi.binance.com/fapi/v1/klines",        # not in exact allowlist
               "https://api.bybit.com/v5/market/kline",          # bybit removed from runtime
               "https://evil.example.com/fapi/v1/aggTrades"]
    for u in blocked:
        with pytest.raises(ValueError):
            F.assert_safe_request(u, {})


def test_method_must_be_get():
    with pytest.raises(ValueError):
        F.assert_safe_request("https://fapi.binance.com/fapi/v1/aggTrades", {}, method="POST")
    with pytest.raises(ValueError):
        F.assert_safe_request("https://fapi.binance.com/fapi/v1/aggTrades", {}, method="DELETE")


def test_auth_or_apikey_headers_blocked():
    for h in ({"Authorization": "Bearer x"}, {"X-MBX-APIKEY": "k"}, {"signature": "s"}):
        with pytest.raises(ValueError):
            F.assert_safe_request("https://fapi.binance.com/fapi/v1/aggTrades", h)


def test_staging_must_resolve_inside_exact_root():
    F.safe_staging_dir(f"external_data/staging/{F.STAGING_MARKER}")
    for bad in (f"reports/{F.STAGING_MARKER}",
                f"tmp/{F.STAGING_MARKER}",
                f"../{F.STAGING_MARKER}",
                f"external_data/staging/{F.STAGING_MARKER}/../../{F.STAGING_MARKER}",
                "external_data/staging/free_microstructure_v10_25_evil"):
        with pytest.raises(ValueError):
            F.safe_staging_dir(bad)


def test_apply_with_unsafe_output_dir_no_network_no_write(monkeypatch):
    monkeypatch.setattr(F, "default_transport",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network on unsafe dir")))
    rep = F.forward_collect("BTCUSDT", ["funding"], apply=True,
                            output_dir=f"reports/{F.STAGING_MARKER}", transport=None)
    assert rep["mode"] == "APPLY" and rep["writes"] is False
    assert any("unsafe_output_dir" in e for e in rep["errors"])
    assert "staging_dir" not in rep


def test_staging_symlink_escape_blocked(tmp_path):
    # a staging path whose component is a symlink pointing outside the root is rejected
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / F.STAGING_MARKER
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ValueError):
        F.safe_staging_dir(str(link))


def test_funding_symbol_mismatch_fail_closed():
    with pytest.raises(ValueError):
        F.funding_to_canonical([{"symbol": "ETHUSDT", "fundingRate": "0.0001", "fundingTime": 1}],
                               "BTCUSDT")
    # no symbol in row but explicit param -> forced to the requested symbol
    out = F.funding_to_canonical([{"fundingRate": "0.0001", "fundingTime": 1}], "BTCUSDT")
    assert out and out[0]["symbol"] == "BTCUSDT"


def test_orderbook_canonical_marks_l1():
    ob = F.bookticker_to_canonical([{"time": 1, "bidPrice": "1", "bidQty": "2",
                                     "askPrice": "3", "askQty": "4"}], "BTCUSDT")
    assert ob[0]["depth_level"] == "L1_BOOKTICKER"
    assert "limitations" in F.free_microstructure_plan()


# ---- V10.25.2 root-symlink + sensitive query hardening --------------------

def _fake_repo_with_symlinked_marker(tmp_path):
    repo = tmp_path / "repo"
    (repo / "external_data" / "staging").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = repo / "external_data" / "staging" / F.STAGING_MARKER
    marker.symlink_to(outside, target_is_directory=True)
    return repo


def test_staging_root_symlink_to_outside_blocked(tmp_path, monkeypatch):
    try:
        repo = _fake_repo_with_symlinked_marker(tmp_path)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    monkeypatch.setattr(F, "_repo_root", lambda: repo)
    with pytest.raises(ValueError):
        F.safe_staging_dir(f"external_data/staging/{F.STAGING_MARKER}")
    with pytest.raises(ValueError):           # child under a symlinked root also blocked
        F.safe_staging_dir(f"external_data/staging/{F.STAGING_MARKER}/run1")


def test_apply_with_symlinked_staging_root_no_network(tmp_path, monkeypatch):
    try:
        repo = _fake_repo_with_symlinked_marker(tmp_path)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    monkeypatch.setattr(F, "_repo_root", lambda: repo)
    monkeypatch.setattr(F, "default_transport",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network on symlinked root")))
    rep = F.forward_collect("BTCUSDT", ["funding"], apply=True)   # default -> symlinked root
    assert rep["mode"] == "APPLY" and rep["writes"] is False
    assert any("unsafe_output_dir" in e for e in rep["errors"])
    assert "staging_dir" not in rep


def test_sensitive_query_params_blocked():
    base = "https://fapi.binance.com/fapi/v1/aggTrades"
    F.assert_safe_request(base + "?symbol=BTCUSDT&limit=1000", {})   # clean public params pass
    for q in ("signature=abc", "apiKey=abc", "api_key=abc", "X-MBX-APIKEY=k",
              "timestamp=123&recvWindow=5000", "secret=x", "token=y", "access_key=z"):
        with pytest.raises(ValueError):
            F.assert_safe_request(base + "?" + q, {})
    # private endpoint + POST + auth header still blocked
    with pytest.raises(ValueError):
        F.assert_safe_request("https://fapi.binance.com/fapi/v1/order?symbol=BTCUSDT", {})
    with pytest.raises(ValueError):
        F.assert_safe_request(base, {}, method="POST")
    with pytest.raises(ValueError):
        F.assert_safe_request(base, {"X-MBX-APIKEY": "k"})


def test_depth_level_l1_compatible_with_v1024(tmp_path):
    import csv as _csv
    ob = F.bookticker_to_canonical(
        [{"time": 1700000000000 + i * 1000, "bidPrice": "100", "bidQty": "2",
          "askPrice": "101", "askQty": "1"} for i in range(20)], "BTCUSDT")
    assert ob[0]["depth_level"] == "L1_BOOKTICKER"
    p = tmp_path / "orderbook_l2.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=list(F._CANON["orderbook"][1]))
        w.writeheader()
        for r in ob:
            w.writerow({k: r.get(k, "") for k in F._CANON["orderbook"][1]})
    assert "depth_level" in p.read_text(encoding="utf-8").splitlines()[0]
    ob_m = M.validate_sample(str(tmp_path))["by_type"]["orderbook"]
    assert ob_m["valid"] is True and ob_m["l1_imbalance_median"] is not None  # L1 column ok in V10.24.3
    plan = F.free_microstructure_plan()
    assert any("L1" in lim and "L2" in lim for lim in plan["limitations"])    # honest: L1 not L2


def test_module_no_dangerous_primitives():
    import re
    src = Path(MODULE_PATH).read_text(encoding="utf-8")
    scan = re.sub(r'"never":\s*\[.*?\],', '"never": [],', src, flags=re.DOTALL)
    for tok in ["load_dotenv", "os.environ", "private_get", "private_post",
                "db.execute", "INSERT INTO", "import torch", "import tensorflow",
                "X-MBX-APIKEY", "set_leverage", "set_margin_mode"]:
        assert tok not in scan, tok
    for name in ["place_order", "create_order", "open_position"]:
        assert f"{name}(" not in scan and f".{name}" not in scan, name
    assert "ExecutionEngine(" not in scan and "PaperTrader(" not in scan
