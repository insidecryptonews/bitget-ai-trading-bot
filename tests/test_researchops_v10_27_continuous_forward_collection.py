"""ResearchOps V10.27 - Continuous Forward Collection Runner tests.

Research-only, dry-run by default, staging-only, NO keys/auth/DB/orders. All
network (websocket + REST) is mocked. Verifies persistent append + dedup across
restarts, the cumulative manifest, hardened staging, and the V10.24.3 status.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from app import research_lab
from app.labs import continuous_forward_collection_v10_27 as C


@pytest.fixture(autouse=True)
def _isolated_repo(tmp_path, monkeypatch):
    """NEVER touch the real repo dataset: point _repo_root at a fake repo AND
    chdir there so relative staging paths resolve inside tmp. Without this a
    full-suite run destroys the real armed collector dataset and flakes when
    the background collector is running."""
    repo = tmp_path / "repo"
    (repo / "external_data" / "staging").mkdir(parents=True)
    monkeypatch.setattr(C, "_repo_root", lambda: repo)
    monkeypatch.chdir(repo)
    yield repo


def _liq(symbol="BTCUSDT", side="SELL", price="50000", qty="1", T=1700000000000):
    return {"e": "forceOrder", "E": T,
            "o": {"s": symbol, "S": side, "p": price, "ap": price, "q": qty, "z": qty,
                  "X": "FILLED", "T": T}}


def _rest_transport(url, headers):
    # enforce the V10.25 exact allowlist even for the mock
    from app.labs import free_public_microstructure_collector_v10_25 as V25
    V25.assert_safe_request(url, headers)
    if "aggTrades" in url:
        # 3 trades; two share the same ms with different price/size (both legit)
        return json.dumps([
            {"p": "100.0", "q": "1", "T": 1700000000000, "m": True},
            {"p": "100.5", "q": "2", "T": 1700000000000, "m": False},
            {"p": "101.0", "q": "1", "T": 1700000060000, "m": False},
        ]).encode()
    if "bookTicker" in url:
        return json.dumps({"symbol": "BTCUSDT", "bidPrice": "100", "bidQty": "2",
                           "askPrice": "101", "askQty": "1", "time": 1700000000000}).encode()
    if "openInterestHist" in url:
        return json.dumps([{"symbol": "BTCUSDT", "sumOpenInterest": "1000",
                            "timestamp": 1700000000000 + i * 3600000} for i in range(5)]).encode()
    if "fundingRate" in url:
        return json.dumps([{"symbol": "BTCUSDT", "fundingRate": "0.0001",
                            "fundingTime": 1700000000000 + i * 28800000} for i in range(5)]).encode()
    raise AssertionError(f"unexpected url {url}")


def _run(symbols=("BTCUSDT",), kinds=("liquidations", "orderbook", "oi", "funding"),
         liq=None, T0=1700000000000):
    liq = liq if liq is not None else [_liq(T=T0 + i * 60000) for i in range(4)]
    return C.run_cycle("binance_usdm", list(symbols), list(kinds), apply=True,
                       max_runtime_seconds=30, max_events=1000,
                       liq_event_source=liq, rest_transport=_rest_transport,
                       poll_spacing_seconds=0)


def test_orderbook_multi_poll_grows_density():
    calls = {"n": 0}

    def tx(url, headers):
        from app.labs import free_public_microstructure_collector_v10_25 as V25
        V25.assert_safe_request(url, headers)
        assert "bookTicker" in url
        calls["n"] += 1
        return json.dumps({"symbol": "BTCUSDT", "bidPrice": "100", "bidQty": "2",
                           "askPrice": "101", "askQty": "1",
                           "time": 1700000000000 + calls["n"] * 500}).encode()

    rep = C.run_cycle("binance_usdm", ["BTCUSDT"], ["orderbook"], apply=True,
                      max_runtime_seconds=30, max_events=10, liq_event_source=[],
                      rest_transport=tx, orderbook_polls=3, poll_spacing_seconds=0)
    assert calls["n"] == 3 and rep["added"]["orderbook"] == 3   # 3 distinct snapshots


def _cleanup(dataset_dir):
    # dataset_dir = .../continuous_forward_v10_27/dataset ; remove the whole marker root
    marker_root = os.path.dirname(dataset_dir)
    shutil.rmtree(marker_root, ignore_errors=True)


# ---- plan / dry-run -------------------------------------------------------

def test_plan_no_network_no_writes_no_live():
    p = C.plan()
    assert p["writes_on_plan"] is False and p["uses_api_keys"] is False
    assert p["can_send_real_orders"] is False and p["final_recommendation"] == "NO LIVE"
    assert set(p["kinds"]) == set(C.KINDS)


def test_dry_run_no_writes():
    rep = C.run_cycle("binance_usdm", ["BTCUSDT"], list(C.KINDS), apply=False)
    assert rep["mode"] == "DRY_RUN" and rep["writes"] is False and "dataset_dir" not in rep


# ---- apply: accumulate + dedup across cycles ------------------------------

def test_cycle_appends_and_dedups_across_restarts():
    r1 = _run()
    try:
        assert r1["mode"] == "APPLY" and r1["writes"] is True
        assert C.STAGING_MARKER in r1["dataset_dir"]
        assert r1["added"]["liquidations"] == 4
        assert r1["added"]["orderbook"] == 1 and r1["added"]["oi"] == 5 and r1["added"]["funding"] == 5
        # second cycle: same liquidations (dup) + 2 NEW ones -> only 2 added
        liq2 = [_liq(T=1700000000000 + i * 60000) for i in range(4)] + \
               [_liq(T=1700000000000 + (10 + i) * 60000) for i in range(2)]
        r2 = _run(liq=liq2)
        assert r2["added"]["liquidations"] == 2          # dedup persisted across "restart"
        assert r2["added"]["oi"] == 0 and r2["added"]["funding"] == 0   # same REST history -> deduped
        # cumulative manifest
        man = json.loads(Path(r2["manifest"]).read_text(encoding="utf-8"))
        assert man["cycles"] == 2
        assert man["cumulative_added"]["liquidations"] == 6
        assert man["final_recommendation"] == "NO LIVE"
        # liquidations.csv has 6 data rows
        liq_csv = Path(os.path.join(r1["dataset_dir"], "liquidations.csv")).read_text(encoding="utf-8")
        assert len([l for l in liq_csv.splitlines() if l.strip()]) == 1 + 6   # header + 6
    finally:
        _cleanup(r1["dataset_dir"])


def test_trades_kind_accumulates_and_dedups():
    # V10.27.2: without trades the V10.24.3 floor (trades>=1000) could NEVER pass
    assert "trades" in C.KINDS
    r1 = _run(kinds=("trades",), liq=[])
    assert r1["added"]["trades"] == 3        # same-ms distinct trades both kept
    r2 = _run(kinds=("trades",), liq=[])
    assert r2["added"]["trades"] == 0        # dedup persisted across "restart"
    csv_path = Path(r1["dataset_dir"]) / "trades.csv"
    lines = [l for l in csv_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1 + 3               # header + 3 rows, no duplicates


def test_checkpoint_written_and_updated_in_staging(_isolated_repo):
    # V10.31: explicit atomic checkpoint next to the manifest
    r1 = _run()
    ck_path = Path(r1["dataset_dir"]) / "checkpoint.json"
    assert r1["checkpoint"].endswith("checkpoint.json") and ck_path.is_file()
    ck = json.loads(ck_path.read_text(encoding="utf-8"))
    assert ck["cycles"] == 1 and ck["exchange"] == "binance_usdm"
    assert ck["symbols"] == ["BTCUSDT"] and ck["errors_last_cycle"] == []
    assert ck["rows_by_type"]["liquidations"] == 4
    assert ck["final_recommendation"] == "NO LIVE" and ck["can_send_real_orders"] is False
    r2 = _run(liq=[])
    ck2 = json.loads(ck_path.read_text(encoding="utf-8"))
    assert ck2["cycles"] == 2 and ck2["last_cycle"] == r2["cycle_time"]
    # containment: checkpoint lives INSIDE the hardened staging dataset dir
    assert C.STAGING_MARKER in str(ck_path)
    assert not (ck_path.parent / "checkpoint.json.tmp").exists()   # atomic replace


def test_status_runs_v1024_and_not_ready_when_sparse():
    r1 = _run()
    try:
        rep = C.status()
        assert rep["dataset_dir"] == r1["dataset_dir"]
        assert rep["cumulative_added"]["liquidations"] == 4
        # sparse forward data -> never instantly READY
        assert rep["readiness_verdict"] != "MICROSTRUCTURE_RESEARCH_READY"
        assert rep["can_research_microstructure"] is False
        assert rep["final_recommendation"] == "NO LIVE"
    finally:
        _cleanup(r1["dataset_dir"])


# ---- hardened staging containment -----------------------------------------

def test_staging_rejects_forbidden_and_substring():
    C.safe_staging_dir(f"external_data/staging/{C.STAGING_MARKER}")
    for bad in (f"reports/{C.STAGING_MARKER}", f"tmp/{C.STAGING_MARKER}",
                f"../{C.STAGING_MARKER}", f"external_data/staging/{C.STAGING_MARKER}_evil",
                "external_data/raw/x"):
        with pytest.raises(ValueError):
            C.safe_staging_dir(bad)


def test_apply_unsafe_output_dir_no_network_no_write():
    boom = [_liq()]

    class _Boom:
        def __iter__(self_):
            raise AssertionError("liq source iterated despite blocked staging")

    rep = C.run_cycle("binance_usdm", ["BTCUSDT"], ["liquidations"], apply=True,
                      output_dir=f"reports/{C.STAGING_MARKER}", liq_event_source=_Boom(),
                      rest_transport=_rest_transport)
    assert rep["mode"] == "APPLY" and rep["writes"] is False
    assert any("unsafe_output_dir" in e for e in rep["errors"])
    assert "dataset_dir" not in rep


def test_root_symlink_blocked(tmp_path, monkeypatch):
    repo = tmp_path / "symrepo"   # distinct from the autouse _isolated_repo root
    (repo / "external_data" / "staging").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (repo / "external_data" / "staging" / C.STAGING_MARKER).symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    monkeypatch.setattr(C, "_repo_root", lambda: repo)
    with pytest.raises(ValueError):
        C.safe_staging_dir(f"external_data/staging/{C.STAGING_MARKER}")
    with pytest.raises(ValueError):
        C.safe_staging_dir(f"external_data/staging/{C.STAGING_MARKER}/{C.DATASET_SUBDIR}")


# ---- CLI isolation + security ---------------------------------------------

def _run_main(argv):
    old = sys.argv
    sys.argv = ["prog"] + argv
    try:
        research_lab.main()
    finally:
        sys.argv = old


def test_cli_allowlisted_and_isolated(monkeypatch, capsys):
    for c in ("continuous-collection-plan-v1027", "continuous-collection-run-cycle-v1027",
              "continuous-collection-status-v1027"):
        assert c in research_lab.PUBLIC_RESEARCH_ONLY_COMMANDS
    monkeypatch.setattr(research_lab, "load_config",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no config")))
    monkeypatch.setattr(research_lab, "Database",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no db")))
    _run_main(["continuous-collection-plan-v1027"])
    assert "CONTINUOUS COLLECTION PLAN V10.27" in capsys.readouterr().out
    _run_main(["continuous-collection-run-cycle-v1027", "--symbols", "BTCUSDT",
               "--max-runtime-seconds", "5", "--max-events", "2"])
    out = capsys.readouterr().out
    assert "DRY_RUN" in out and "NO LIVE" in out    # no --apply -> no network/writes


def test_module_no_dangerous_primitives():
    import re
    src = Path(C.__file__).read_text(encoding="utf-8")   # absolute: survives chdir
    scan = re.sub(r'"never":\s*\[.*?\],', '"never": [],', src, flags=re.DOTALL)
    for tok in ["load_dotenv", "os.environ", "private_get", "private_post", "db.execute",
                "INSERT INTO", "import torch", "import tensorflow", "X-MBX-APIKEY", "listenKey"]:
        assert tok not in scan, tok
    for name in ["place_order", "create_order", "set_leverage", "set_margin_mode", "open_position"]:
        assert f"{name}(" not in scan and f".{name}" not in scan, name
    assert "ExecutionEngine(" not in scan and "PaperTrader(" not in scan
