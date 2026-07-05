"""ResearchOps V10.32 - Bybit Full Microstructure Sample Collector tests.

Research-only, dry-run by default, staging-only, single-exchange bybit_linear.
All HTTP is mocked (payloads mirror the live-probed v5 shapes); no real network
in tests. Verifies: converters, dedup, RATE_LIMITED visibility, staging
containment, same-exchange liquidation sync with side-convention mapping,
V10.24 single-exchange readiness (dense sample genuinely reaches READY; sparse
and liq-less never do), and that Binance readiness is never touched.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

from app import research_lab
from app.labs import bybit_public_microstructure_collector_v10_32 as B32
from app.labs import microstructure_sample_adapter_v10_24 as V24

DAY = 86_400_000
T0 = 1_700_000_000_000


@pytest.fixture(autouse=True)
def _repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "external_data" / "staging").mkdir(parents=True)
    monkeypatch.setattr(B32, "_repo_root", lambda: repo)
    monkeypatch.chdir(repo)
    yield repo


def _payload(kind, n=5, t0=T0, days=0):
    step = (days * DAY // n) if days else 60_000
    if kind == "trades":
        lst = [{"execId": f"e{t0}_{i}", "price": f"{100 + i % 7}", "size": "1",
                "side": "Buy" if i % 2 else "Sell", "time": str(t0 + i * step)}
               for i in range(n)]
    elif kind == "orderbook":
        return {"retCode": 0, "time": t0,
                "result": {"list": [{"bid1Price": "100", "bid1Size": "2",
                                     "ask1Price": "100.1", "ask1Size": "1"}]}}
    elif kind == "oi":
        lst = [{"openInterest": "1000", "timestamp": str(t0 + i * step)} for i in range(n)]
    else:
        lst = [{"symbol": "BTCUSDT", "fundingRate": "0.0001",
                "fundingRateTimestamp": str(t0 + i * step)} for i in range(n)]
    return {"retCode": 0, "result": {"list": lst}}


def _tx(url, headers):
    B32.assert_safe_request(url, headers)
    if "recent-trade" in url:
        return json.dumps(_payload("trades")).encode()
    if "tickers" in url:
        return json.dumps(_payload("orderbook")).encode()
    if "open-interest" in url:
        return json.dumps(_payload("oi")).encode()
    if "funding" in url:
        return json.dumps(_payload("funding")).encode()
    raise AssertionError(url)


def _liq_src(repo, n=4, t0=T0, days=0):
    """Fake V10.30 dataset with position-side rows."""
    d = repo / "external_data" / "staging" / "bybit_liquidations_v10_30" / "dataset"
    d.mkdir(parents=True, exist_ok=True)
    step = (days * DAY // n) if days else 60_000
    from app.labs import bybit_public_liquidations_ws_collector_v10_30 as V30
    with open(d / "liquidations.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=V30.CANON_HEADER)
        w.writeheader()
        for i in range(n):
            ts = t0 + i * step
            pos = "long" if i % 2 else "short"
            w.writerow({"timestamp": ts, "exchange": "bybit_linear", "symbol": "BTCUSDT",
                        "bybit_side_raw": "Buy" if pos == "long" else "Sell",
                        "position_liquidated": pos, "price": "100",
                        "price_type": "bankruptcy_price", "size": "1", "notional": "100",
                        "source": "x", "event_type": "liquidation",
                        "raw_event_id": f"bybit_linear:BTCUSDT:{ts}:{pos}",
                        "received_at": ts})
    return d


def _run(repo, **kw):
    return B32.run_cycle("BTCUSDT", apply=True, transport=_tx,
                         poll_spacing_seconds=0,
                         liq_source_dir=str(_liq_src(repo)), **kw)


# ---- plan / dry-run ---------------------------------------------------------

def test_plan_and_dry_run_no_writes(_repo):
    p = B32.plan()
    assert p["writes_on_plan"] is False and p["can_send_real_orders"] is False
    assert p["final_recommendation"] == "NO LIVE"
    assert p["cross_exchange_liquidations_used_for_ready"] is False
    rep = B32.run_cycle("BTCUSDT", apply=False)
    assert rep["mode"] == "DRY_RUN" and rep["writes"] is False
    assert not (_repo / "external_data" / "staging" / B32.STAGING_MARKER).exists()


# ---- apply: staging-only, all five kinds, manifest+checkpoint ---------------

def test_apply_writes_all_kinds_staging_only(_repo):
    rep = _run(_repo)
    assert rep["mode"] == "APPLY" and B32.STAGING_MARKER in rep["dataset_dir"]
    assert rep["added"] == {"trades": 5, "orderbook": 1, "oi": 5, "funding": 5,
                            "liquidations": 4}
    ds = Path(rep["dataset_dir"])
    for f in ("trades.csv", "orderbook_l2.csv", "open_interest.csv", "funding.csv",
              "liquidations.csv", "manifest.json", "checkpoint.json"):
        assert (ds / f).is_file(), f
    man = json.loads((ds / "manifest.json").read_text(encoding="utf-8"))
    assert man["source_exchange"] == "bybit_linear" and man["single_exchange_sample"] is True
    assert man["final_recommendation"] == "NO LIVE"
    ck = json.loads((ds / "checkpoint.json").read_text(encoding="utf-8"))
    assert ck["rows_by_type"]["trades"] == 5 and ck["exchange"] == "bybit_linear"


def test_dedup_across_cycles(_repo):
    r1 = _run(_repo)
    r2 = _run(_repo)
    assert r1["added"]["trades"] == 5 and r2["added"]["trades"] == 0
    assert r2["added"]["oi"] == 0 and r2["added"]["liquidations"] == 0
    assert r2["cumulative_added"]["trades"] == 5


# ---- converters + side-convention mapping -----------------------------------

def test_liq_sync_maps_position_side_to_order_side(_repo):
    rep = _run(_repo)
    with open(Path(rep["dataset_dir"]) / "liquidations.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 4
    for r in rows:
        assert r["exchange"] == "bybit_linear"
        assert r["side"] in ("buy", "sell")
    # position long -> order sell (Binance canonical convention), short -> buy
    assert B32.v30_liq_to_canonical({"timestamp": 1, "symbol": "X", "price": "1",
                                     "size": "1", "position_liquidated": "long",
                                     "raw_event_id": "a"})["side"] == "sell"
    assert B32.v30_liq_to_canonical({"timestamp": 1, "symbol": "X", "price": "1",
                                     "size": "1", "position_liquidated": "short",
                                     "raw_event_id": "b"})["side"] == "buy"
    assert B32.v30_liq_to_canonical({"position_liquidated": "??"}) is None


def test_trades_converter_taker_side_and_id():
    rows = B32.trades_to_canonical(_payload("trades", n=2), "BTCUSDT")
    assert rows[0]["aggressor_side"] == "sell" and rows[1]["aggressor_side"] == "buy"
    assert rows[0]["trade_id"].startswith("e")


# ---- rate limit + retCode visibility ----------------------------------------

def test_rate_limited_visible_and_non_corrupting(_repo, monkeypatch):
    monkeypatch.setattr(B32.time, "sleep", lambda s: None)

    def tx429(url, headers):
        B32.assert_safe_request(url, headers)
        raise RuntimeError("HTTP Error 429: Too Many Requests")

    rep = B32.run_cycle("BTCUSDT", apply=True, transport=tx429,
                        poll_spacing_seconds=0, liq_source_dir=str(_liq_src(_repo)))
    assert any("RATE_LIMITED" in e for e in rep["errors"])
    assert rep["added"]["trades"] == 0 and rep["added"]["liquidations"] == 4
    # manifest still written coherently (state persisted, nothing corrupted)
    man = json.loads((Path(rep["dataset_dir"]) / "manifest.json").read_text(encoding="utf-8"))
    assert man["cycles"] == 1


def test_bybit_retcode_error_visible(_repo):
    def tx_err(url, headers):
        B32.assert_safe_request(url, headers)
        return json.dumps({"retCode": 10006, "retMsg": "rate limit", "result": {}}).encode()

    rep = B32.run_cycle("BTCUSDT", apply=True, transport=tx_err,
                        poll_spacing_seconds=0, liq_source_dir=str(_liq_src(_repo)))
    assert any("BYBIT_RETCODE:10006" in e for e in rep["errors"])


# ---- containment + allowlist -------------------------------------------------

def test_unsafe_output_dirs_and_symbol(_repo):
    for bad in ("reports/x", f"external_data/staging/{B32.STAGING_MARKER}_evil",
                f"../{B32.STAGING_MARKER}", "external_data/raw/x"):
        rep = B32.run_cycle("BTCUSDT", apply=True, output_dir=bad, transport=_tx)
        assert rep["writes"] is False and any("unsafe_output_dir" in e for e in rep["errors"])
    rep = B32.run_cycle("btc/../x", apply=True, transport=_tx)
    assert any("symbol_not_allowlisted" in e for e in rep["errors"])


def test_request_allowlist_blocks_bad_urls_and_headers():
    B32.assert_safe_request(B32._planned_urls("BTCUSDT")["trades"], {})
    for bad in ("https://evil.com/v5/market/tickers?category=linear",
                "http://api.bybit.com/v5/market/tickers?category=linear",
                "https://api.bybit.com/v5/order/create",
                "https://api.bybit.com/v5/market/tickers?api_key=x"):
        with pytest.raises(ValueError):
            B32.assert_safe_request(bad, {})
    with pytest.raises(ValueError):
        B32.assert_safe_request(B32._planned_urls("BTCUSDT")["oi"], {"X-BAPI-API-KEY": "k"})


def test_root_symlink_blocked(tmp_path, monkeypatch):
    repo = tmp_path / "symrepo"
    (repo / "external_data" / "staging").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (repo / "external_data" / "staging" / B32.STAGING_MARKER).symlink_to(
            outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink unavailable")
    monkeypatch.setattr(B32, "_repo_root", lambda: repo)
    with pytest.raises(ValueError):
        B32.safe_staging_dir()


# ---- readiness: bybit path is separate; never READY without the gates --------

def test_sparse_sample_not_ready_and_no_liq_not_ready(_repo):
    _run(_repo)
    st = B32.status()
    assert st["readiness_verdict"] != V24.C_READY
    assert st["can_research_microstructure"] is False
    assert "NO LIVE" in st["final_recommendation"]


def test_dense_bybit_sample_genuinely_reaches_ready(_repo):
    """Compat proof: a 31-day dense all-bybit dataset passes V10.24.3 -- the
    Bybit path CAN reach research-readiness (data readiness, not an edge)."""
    days = 31

    def tx_dense(url, headers):
        B32.assert_safe_request(url, headers)
        if "recent-trade" in url:
            return json.dumps(_payload("trades", n=1500, days=days)).encode()
        if "tickers" in url:
            return json.dumps(_payload("orderbook")).encode()
        if "open-interest" in url:
            return json.dumps(_payload("oi", n=750, days=days)).encode()
        return json.dumps(_payload("funding", n=93, days=days)).encode()

    # orderbook needs many distinct-ts snapshots: write directly (ticker gives 1/cycle)
    rep = B32.run_cycle("BTCUSDT", apply=True, transport=tx_dense,
                        poll_spacing_seconds=0, orderbook_polls=1,
                        liq_source_dir=str(_liq_src(_repo, n=62, days=days)))
    ds = Path(rep["dataset_dir"])
    seen = B32._load_seen(str(ds), "orderbook")
    ob_rows = [{"timestamp": T0 + i * (days * DAY // 320), "symbol": "BTCUSDT",
                "bid_price_1": "100", "bid_size_1": "2", "ask_price_1": "100.1",
                "ask_size_1": "1", "depth_level": "L1_TICKER"} for i in range(320)]
    B32._append_rows(str(ds), "orderbook", ob_rows, seen)
    st = B32.status()
    assert st["readiness_verdict"] == V24.C_READY, st.get("why_not_ready")
    assert st["can_research_microstructure"] is True
    assert st["final_recommendation"] == "NO LIVE"      # data-ready, never live


def test_bybit_dataset_never_leaks_into_binance_readiness(_repo, monkeypatch):
    from app.labs import free_microstructure_dataset_assembler_v10_29 as A
    monkeypatch.setattr(A, "_repo_root", lambda: _repo)
    _run(_repo)
    src = A.discover_sources()
    for entry in src.values():
        assert B32.STAGING_MARKER not in entry["marker"]
    rep = A.assemble("BTCUSDT", apply=True)
    # nothing from the bybit dataset reached the (empty) Binance sample
    assert all(d["unique_rows"] == 0 for d in rep["per_kind"].values())


# ---- CLI wiring + no dangerous primitives ------------------------------------

def _run_main(argv):
    old = sys.argv
    sys.argv = ["prog"] + argv
    try:
        research_lab.main()
    finally:
        sys.argv = old


def test_cli_allowlisted_and_isolated(monkeypatch, capsys):
    for c in ("bybit-microstructure-plan-v1032", "bybit-microstructure-run-cycle-v1032",
              "bybit-microstructure-status-v1032"):
        assert c in research_lab.PUBLIC_RESEARCH_ONLY_COMMANDS
    monkeypatch.setattr(research_lab, "load_config",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no config")))
    _run_main(["bybit-microstructure-plan-v1032"])
    assert "BYBIT MICROSTRUCTURE PLAN V10.32" in capsys.readouterr().out
    _run_main(["bybit-microstructure-run-cycle-v1032", "--symbols", "BTCUSDT"])
    out = capsys.readouterr().out
    assert "DRY_RUN" in out and "NO LIVE" in out


def test_module_no_dangerous_primitives():
    src = Path(B32.__file__).read_text(encoding="utf-8")
    for tok in ["load_dotenv", "os.environ", "private_get", "private_post",
                "db.execute", "INSERT INTO", "X-MBX-APIKEY", "api_secret",
                "websocket", "order/create", "position/list"]:
        assert tok not in src.replace("X-BAPI-API-KEY", ""), tok
    for name in ["place_order", "create_order", "set_leverage", "set_margin_mode",
                 "open_position"]:
        assert f"{name}(" not in src and f".{name}" not in src, name
