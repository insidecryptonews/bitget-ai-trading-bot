"""ResearchOps V10.36 - Bybit Official Backfill Importer tests.

Research-only, dry-run by default, ONE day per invocation, staging-only.
All HTTP mocked (synthetic gz dumps mirroring the official format); no real
network in tests. Backfill NEVER completes readiness and never touches the
V10.32 forward dataset.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import sys
from pathlib import Path

import pytest

from app import research_lab
from app.labs import bybit_backfill_importer_v10_36 as BF


@pytest.fixture(autouse=True)
def _repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "external_data" / "staging").mkdir(parents=True)
    monkeypatch.setattr(BF, "_repo_root", lambda: repo)
    monkeypatch.chdir(repo)      # relative staging paths resolve inside tmp
    yield repo


def _gz_dump(rows):
    """Official dump format: timestamp(s),symbol,side,size,price,...,trdMatchID"""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["timestamp", "symbol", "side", "size",
                                        "price", "tickDirection", "trdMatchID"])
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return gzip.compress(buf.getvalue().encode())


def _row(ts_s=1585113600.123, side="Buy", price="6400.5", size="0.01", mid="m1"):
    return {"timestamp": ts_s, "symbol": "BTCUSDT", "side": side, "size": size,
            "price": price, "tickDirection": "PlusTick", "trdMatchID": mid}


def _tx(rows):
    def transport(url, headers):
        BF.assert_safe_request(url, headers)
        return _gz_dump(rows)
    return transport


# ---- plan / probe / dry-run --------------------------------------------------

def test_plan_and_dry_run_no_writes(_repo):
    p = BF.plan()
    assert p["writes_on_plan"] is False and p["can_send_real_orders"] is False
    assert p["backfill_completes_readiness"] is False
    rep = BF.import_day("BTCUSDT", "2020-03-25", apply=False)
    assert rep["mode"] == "DRY_RUN"
    assert not (_repo / "external_data" / "staging" / BF.STAGING_MARKER).exists()


def test_probe_lists_days_without_downloading():
    def tx(url, headers):
        BF.assert_safe_request(url, headers)
        assert url.endswith("/")                      # listing only
        return (b'<a href="BTCUSDT2020-03-25.csv.gz">x</a>'
                b'<a href="BTCUSDT2020-03-26.csv.gz">x</a>')
    rep = BF.probe_available_days("BTCUSDT", transport=tx)
    assert rep["available_days"] == 2
    assert rep["first_day"] == "2020-03-25" and rep["last_day"] == "2020-03-26"


# ---- import one day: staging-only, attribution, monotonic, idempotent --------

def test_import_day_writes_staging_with_attribution(_repo):
    rows = [_row(1585113600.1, "Buy", mid="a"), _row(1585113601.2, "Sell", mid="b"),
            _row(1585113602.3, "Buy", mid="c")]
    rep = BF.import_day("BTCUSDT", "2020-03-25", apply=True, transport=_tx(rows))
    assert rep["imported_rows"] == 3 and rep["errors"] == []
    f = Path(rep["file"])
    assert BF.STAGING_MARKER in str(f)
    with open(f, newline="", encoding="utf-8") as fh:
        out = list(csv.DictReader(fh))
    assert all(r["source_exchange"] == "bybit_linear" and r["backfill"] == "true"
               for r in out)
    assert out[0]["timestamp"] == "1585113600100"     # seconds -> ms
    assert out[0]["aggressor_side"] == "buy" and out[1]["aggressor_side"] == "sell"
    man = json.loads(Path(rep["manifest"]).read_text(encoding="utf-8"))
    day = man["days"]["2020-03-25"]
    assert day["url"].startswith("https://public.bybit.com/trading/BTCUSDT/")
    assert len(day["sha256"]) == 64 and day["rows"] == 3
    assert man["backfill_completes_readiness"] is False
    assert man["final_recommendation"] == "NO LIVE"


def test_import_day_idempotent_skip(_repo):
    rows = [_row(mid="a")]
    BF.import_day("BTCUSDT", "2020-03-25", apply=True, transport=_tx(rows))
    rep2 = BF.import_day("BTCUSDT", "2020-03-25", apply=True, transport=_tx(rows))
    assert rep2.get("skipped_existing") is True and rep2["imported_rows"] == 0


def test_import_day_counts_bad_and_non_monotonic(_repo):
    rows = [_row(1585113602.0, mid="a"), _row(1585113601.0, mid="b"),   # backwards
            {"timestamp": "zzz", "symbol": "BTCUSDT", "side": "Buy",
             "size": "1", "price": "1", "trdMatchID": "bad"},
            _row(1585113603.0, side="Hold", mid="c")]                    # bad side
    rep = BF.import_day("BTCUSDT", "2020-03-25", apply=True, transport=_tx(rows))
    assert rep["imported_rows"] == 2 and rep["bad_rows"] == 2
    assert rep["non_monotonic_input_pairs"] == 1        # input disorder visible
    assert rep["output_sorted_ascending"] is True
    with open(rep["file"], newline="", encoding="utf-8") as fh:
        ts = [int(r["timestamp"]) for r in csv.DictReader(fh)]
    assert ts == sorted(ts)                             # output always ascending


def test_rate_limited_download_visible(_repo):
    def tx429(url, headers):
        BF.assert_safe_request(url, headers)
        raise RuntimeError("HTTP Error 429: Too Many Requests")
    rep = BF.import_day("BTCUSDT", "2020-03-25", apply=True, transport=tx429)
    assert any("RATE_LIMITED" in e for e in rep["errors"])
    assert rep["imported_rows"] == 0


def test_unsafe_paths_and_urls_blocked(_repo):
    with pytest.raises(ValueError):
        BF._dump_url("btc/../x", "2020-03-25")
    with pytest.raises(ValueError):
        BF._dump_url("BTCUSDT", "2020-3-25")
    for bad in ("https://evil.com/trading/BTCUSDT/x.csv.gz",
                "http://public.bybit.com/trading/BTCUSDT/x.csv.gz",
                "https://api.bybit.com/v5/order/create"):
        with pytest.raises(ValueError):
            BF.assert_safe_request(bad, {})
    with pytest.raises(ValueError):
        BF.safe_staging_dir("external_data/staging/other_marker")
    rep = BF.import_day("BTCUSDT", "2020-03-25", apply=True,
                        transport=_tx([_row()]), output_dir="reports/x")
    assert any("unsafe_output_dir" in e for e in rep["errors"])


# ---- backfill never touches forward readiness ---------------------------------

def test_backfill_never_reaches_forward_dataset(_repo, monkeypatch):
    from app.labs import bybit_public_microstructure_collector_v10_32 as B32
    monkeypatch.setattr(B32, "_repo_root", lambda: _repo)
    BF.import_day("BTCUSDT", "2020-03-25", apply=True, transport=_tx([_row()]))
    fwd = _repo / "external_data" / "staging" / B32.STAGING_MARKER
    assert not fwd.exists()                            # forward staging untouched
    st = B32.status()
    assert st["readiness_verdict"] == "NO_SAMPLE"      # readiness unaffected


# ---- coverage probes -----------------------------------------------------------

def _cov_tx(ts_lists):
    calls = {"n": 0}
    def tx(url, headers):
        BF.assert_safe_request(url, headers)
        batch = ts_lists[min(calls["n"], len(ts_lists) - 1)]
        calls["n"] += 1
        field = "fundingRateTimestamp" if "funding" in url else "timestamp"
        return json.dumps({"retCode": 0, "result": {
            "list": [{field: str(t), "fundingRate": "0.0001",
                      "openInterest": "1", "symbol": "BTCUSDT"} for t in batch]}}).encode()
    return tx


def test_coverage_ok_partial_no_data_rate_limited():
    start, end = 1_600_000_000_000, 1_600_864_000_000       # ~10 days
    ok = BF.coverage_probe("funding", "BTCUSDT", start, end,
                           transport=_cov_tx([[start + 1000, end - 1000]]))
    assert ok["coverage_verdict"] == "COVERAGE_OK" and ok["rows"] == 2
    partial = BF.coverage_probe("oi", "BTCUSDT", start, end,
                                transport=_cov_tx([[end - 1000, end - 2000]]))
    assert partial["coverage_verdict"] == "PARTIAL_COVERAGE"
    nodata = BF.coverage_probe("oi", "BTCUSDT", start, end, transport=_cov_tx([[]]))
    assert nodata["coverage_verdict"] == "NO_DATA"           # OI-2020-style empty

    def tx429(url, headers):
        BF.assert_safe_request(url, headers)
        raise RuntimeError("HTTP Error 429")
    rl = BF.coverage_probe("funding", "BTCUSDT", start, end, transport=tx429)
    assert rl["coverage_verdict"] == "RATE_LIMITED" and rl["rate_limited"] is True


# ---- CLI + no dangerous primitives ---------------------------------------------

def _run_main(argv):
    old = sys.argv
    sys.argv = ["prog"] + argv
    try:
        research_lab.main()
    finally:
        sys.argv = old


def test_cli_allowlisted_and_isolated(monkeypatch, capsys):
    for c in ("bybit-backfill-plan-v1036", "bybit-backfill-probe-v1036",
              "bybit-backfill-download-day-v1036", "bybit-backfill-import-day-v1036",
              "bybit-backfill-coverage-v1036"):
        assert c in research_lab.PUBLIC_RESEARCH_ONLY_COMMANDS
    monkeypatch.setattr(research_lab, "load_config",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no config")))
    _run_main(["bybit-backfill-plan-v1036"])
    out = capsys.readouterr().out
    assert "BYBIT BACKFILL PLAN V10.36" in out and "NO LIVE" in out
    _run_main(["bybit-backfill-import-day-v1036", "--symbols", "BTCUSDT",
               "--date", "2020-03-25"])
    out = capsys.readouterr().out
    assert "DRY_RUN" in out and "NO LIVE" in out


def test_module_no_dangerous_primitives():
    src = Path(BF.__file__).read_text(encoding="utf-8")
    for tok in ["load_dotenv", "os.environ", "private_get", "private_post",
                "db.execute", "INSERT INTO", "X-MBX-APIKEY", "api_secret",
                "websocket", "order/create"]:
        assert tok not in src, tok
    for name in ["place_order", "create_order", "set_leverage", "set_margin_mode",
                 "open_position"]:
        assert f"{name}(" not in src and f".{name}" not in src, name
