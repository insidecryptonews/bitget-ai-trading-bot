"""ResearchOps V10.29 - Free Microstructure Dataset Assembler tests.

Research-only, offline (NO network at all), dry-run by default, staging-only.
Verifies source merging + dedup + timestamp sort + symbol filtering, hardened
read/write path containment (traversal/symlink/marker), honest gaps, V10.24.3
compatibility (a genuinely dense sample really reaches READY; sparse never
does), the readiness estimates, and CLI isolation. All FS in tmp fake repos.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

from app import research_lab
from app.labs import free_microstructure_dataset_assembler_v10_29 as A
from app.labs import microstructure_sample_adapter_v10_24 as V24
from app.labs import continuous_forward_collection_v10_27 as V27

DAY = 86_400_000
T0 = 1_700_000_000_000


@pytest.fixture(autouse=True)
def _repo(tmp_path, monkeypatch):
    """Fake repo root so tests NEVER read/write the real staging data."""
    repo = tmp_path / "repo"
    (repo / "external_data" / "staging").mkdir(parents=True)
    monkeypatch.setattr(A, "_repo_root", lambda: repo)
    yield repo


# ---- canonical row/file builders -------------------------------------------

def _w(dirp: Path, kind: str, rows: list[dict]) -> None:
    dirp.mkdir(parents=True, exist_ok=True)
    with open(dirp / A._FILES[kind], "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=A._HEADERS[kind])
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in A._HEADERS[kind]})


def t_row(ts, sym="BTCUSDT", price="100", size="1", side="buy"):
    return {"timestamp": ts, "symbol": sym, "price": price, "size": size,
            "aggressor_side": side}


def ob_row(ts, sym="BTCUSDT"):
    return {"timestamp": ts, "symbol": sym, "bid_price_1": "100", "bid_size_1": "2",
            "ask_price_1": "100.1", "ask_size_1": "1", "depth_level": "L1_BOOKTICKER"}


def oi_row(ts, sym="BTCUSDT"):
    return {"timestamp": ts, "symbol": sym, "open_interest": "1000"}


def f_row(ts, sym="BTCUSDT"):
    return {"timestamp": ts, "symbol": sym, "funding_rate": "0.0001"}


def liq_row(ts, sym="BTCUSDT", side="sell", price="100", size="1"):
    return {"timestamp": ts, "exchange": "binance_usdm", "symbol": sym, "side": side,
            "price": price, "size": size, "notional": "100.0",
            "source": "fstream.binance.com/forceOrder", "event_type": "forceOrder",
            "raw_event_id": f"binance_usdm:{sym}:{ts}:{side}:{size}:{price}",
            "received_at": ts}


def _spread(n, days):
    step = max(1, (days * DAY) // n)
    return [T0 + i * step for i in range(n)]


def small_source(repo, sym="BTCUSDT"):
    """Sparse V10.27-style dataset: some data, clearly NOT ready."""
    ds = repo / "external_data" / "staging" / V27.STAGING_MARKER / "dataset"
    _w(ds, "oi", [oi_row(ts, sym) for ts in _spread(30, 2)])
    _w(ds, "funding", [f_row(ts, sym) for ts in _spread(9, 2)])
    _w(ds, "orderbook", [ob_row(ts, sym) for ts in _spread(5, 2)])
    return ds


def dense_source(repo, sym="BTCUSDT", days=31):
    """Fully dense 31-day dataset that genuinely satisfies every V10.24.3 floor."""
    ds = repo / "external_data" / "staging" / V27.STAGING_MARKER / "dataset"
    _w(ds, "trades", [t_row(ts, sym, price=f"{100 + (i % 7) * 0.5}",
                            side=("buy" if i % 2 else "sell"))
                      for i, ts in enumerate(_spread(1500, days))])
    _w(ds, "orderbook", [ob_row(ts, sym) for ts in _spread(320, days)])
    _w(ds, "oi", [oi_row(ts, sym) for ts in _spread(750, days)])
    _w(ds, "funding", [f_row(ts, sym) for ts in _spread(93, days)])
    _w(ds, "liquidations", [liq_row(ts, sym, side=("sell" if i % 2 else "buy"))
                            for i, ts in enumerate(_spread(62, days))])
    return ds


# ---- plan / dry-run ---------------------------------------------------------

def test_plan_no_writes_no_live():
    p = A.plan()
    assert p["writes_on_plan"] is False and p["uses_network"] is False
    assert p["uses_api_keys"] is False and p["can_send_real_orders"] is False
    assert p["final_recommendation"] == "NO LIVE"
    assert set(p["kinds"]) == set(A.KINDS)


def test_dry_run_reads_but_writes_nothing(_repo):
    small_source(_repo)
    rep = A.assemble("BTCUSDT", apply=False)
    assert rep["mode"] == "DRY_RUN" and rep["writes"] is False
    assert rep["per_kind"]["oi"]["unique_rows"] == 30
    assert "missing_trades" in rep["gaps"] and "missing_liquidations" in rep["gaps"]
    out_root = _repo / "external_data" / "staging" / A.STAGING_MARKER
    assert not out_root.exists()          # nothing written on dry-run


def test_symbol_required():
    rep = A.assemble("", apply=False)
    assert any("symbol_required" in e for e in rep["errors"])


# ---- apply: merge + dedup + sort + symbol filter ----------------------------

def test_apply_merges_dedups_sorts_staging_only(_repo):
    ds = small_source(_repo)
    # a V10.25 run with OVERLAPPING oi rows + 5 new (out of order) + an ETH row
    run = _repo / "external_data" / "staging" / "free_microstructure_v10_25" / "r1"
    overlap = [oi_row(ts) for ts in _spread(30, 2)][:10]
    extra = [oi_row(T0 + 9 * DAY - i * 1000) for i in range(5)]     # later + reversed
    _w(run, "oi", extra + overlap + [oi_row(T0 + 50, "ETHUSDT")])
    rep = A.assemble("BTCUSDT", apply=True)
    assert rep["mode"] == "APPLY" and rep["writes"] is True
    d = rep["per_kind"]["oi"]
    assert d["unique_rows"] == 35 and d["duplicates_dropped"] == 10
    assert d["other_symbol_dropped"] == 1
    sample = Path(rep["sample_dir"])
    assert A.STAGING_MARKER in str(sample)
    with open(sample / "open_interest.csv", newline="", encoding="utf-8") as f:
        ts = [int(r["timestamp"]) for r in csv.DictReader(f)]
    assert len(ts) == 35 and ts == sorted(ts)                        # sorted, deduped
    # 0-row kinds must NOT produce files (empty recognized CSV is INVALID)
    assert not (sample / "trades.csv").exists()
    assert not (sample / "liquidations.csv").exists()
    man = json.loads((sample / "manifest.json").read_text(encoding="utf-8"))
    assert man["final_recommendation"] == "NO LIVE" and man["symbol"] == "BTCUSDT"


def test_missing_kinds_reported_as_gaps(_repo):
    small_source(_repo)
    rep = A.assemble("BTCUSDT", apply=True)
    assert "missing_trades" in rep["gaps"] and "missing_liquidations" in rep["gaps"]
    assert rep["readiness_verdict"] != V24.C_READY                   # never invented


def test_header_mismatch_source_is_rejected_not_trusted(_repo):
    ds = _repo / "external_data" / "staging" / V27.STAGING_MARKER / "dataset"
    ds.mkdir(parents=True)
    (ds / "open_interest.csv").write_text("bad,header\n1,2\n", encoding="utf-8")
    rep = A.assemble("BTCUSDT", apply=False)
    assert rep["per_kind"]["oi"]["unique_rows"] == 0
    assert any("header_mismatch" in e for e in rep["errors"])


def test_bad_timestamps_dropped_never_invented(_repo):
    ds = _repo / "external_data" / "staging" / V27.STAGING_MARKER / "dataset"
    _w(ds, "oi", [oi_row(T0), {"timestamp": "not_a_ts", "symbol": "BTCUSDT",
                               "open_interest": "5"}])
    rep = A.assemble("BTCUSDT", apply=False)
    assert rep["per_kind"]["oi"]["unique_rows"] == 1
    assert rep["per_kind"]["oi"]["bad_timestamp_dropped"] == 1


# ---- hardened containment ---------------------------------------------------

def test_unsafe_output_dirs_fail_no_write(_repo):
    small_source(_repo)
    for bad in ("reports/x", f"external_data/staging/{A.STAGING_MARKER}_evil",
                f"../{A.STAGING_MARKER}", "external_data/raw/x",
                f"external_data/staging/{A.STAGING_MARKER}/../db"):
        rep = A.assemble("BTCUSDT", apply=True, output_dir=bad)
        assert rep["writes"] is False, bad
        assert any("unsafe_output_dir" in e for e in rep["errors"]), bad


def test_traversal_and_forbidden_segments_blocked():
    for bad in (f"external_data/staging/{A.STAGING_MARKER}/../x",
                f"external_data/staging/{A.STAGING_MARKER}/db",
                f"external_data/staging/{A.STAGING_MARKER}/backup"):
        with pytest.raises(ValueError):
            A.safe_staging_dir(bad)
    with pytest.raises(ValueError):
        A.safe_read_dir("external_data/staging/unknown_marker_x")


def test_root_symlink_blocked(tmp_path, monkeypatch):
    repo = tmp_path / "symrepo"
    (repo / "external_data" / "staging").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (repo / "external_data" / "staging" / A.STAGING_MARKER).symlink_to(
            outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    monkeypatch.setattr(A, "_repo_root", lambda: repo)
    with pytest.raises(ValueError):
        A.safe_staging_dir()


def test_child_symlink_blocked(tmp_path, monkeypatch):
    repo = tmp_path / "symrepo2"
    root = repo / "external_data" / "staging" / A.STAGING_MARKER
    root.mkdir(parents=True)
    outside = tmp_path / "outside2"
    outside.mkdir()
    try:
        (root / "child").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    monkeypatch.setattr(A, "_repo_root", lambda: repo)
    with pytest.raises(ValueError):
        A.safe_staging_dir(f"external_data/staging/{A.STAGING_MARKER}/child")
    with pytest.raises(ValueError):
        A.safe_read_dir(f"external_data/staging/{A.STAGING_MARKER}/child")


def test_symlinked_source_run_dir_not_listed(_repo, tmp_path):
    root = _repo / "external_data" / "staging" / "free_microstructure_v10_25"
    root.mkdir(parents=True)
    outside = tmp_path / "outside3"
    outside.mkdir()
    try:
        (root / "evil").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    src = A.discover_sources()
    assert src["v10_25_forward_runs"]["dirs"] == []       # symlinked run dir skipped


# ---- V10.24.3 compatibility: sparse never READY, dense genuinely READY ------

def test_sparse_sample_never_ready(_repo):
    small_source(_repo)
    rep = A.assemble("BTCUSDT", apply=True)
    assert rep["readiness_verdict"] in (V24.C_NEEDS_HISTORY, V24.C_PARTIAL, V24.C_INVALID)
    st = A.readiness_status()
    assert st["readiness_verdict"] == rep["readiness_verdict"]
    assert st["can_research_microstructure"] is False


def test_dense_assembled_sample_reaches_ready(_repo):
    dense_source(_repo)
    rep = A.assemble("BTCUSDT", apply=True)
    assert rep["readiness_verdict"] == V24.C_READY, rep.get("why_not_ready")
    assert rep["can_research_microstructure"] is True
    st = A.readiness_status()          # picks the latest assembled run
    assert st["target_selection"] == "latest_assembled_v10_29"
    assert st["readiness_verdict"] == V24.C_READY
    assert st["estimated_days_to_ready"] == 0.0


def test_symbol_mismatch_sources_do_not_poison_sample(_repo):
    dense_source(_repo, sym="ETHUSDT")
    rep = A.assemble("BTCUSDT", apply=True)      # everything is the wrong symbol
    assert rep["writes"] is False                # nothing usable -> nothing written
    assert set(rep["gaps"]) == {f"missing_{k}" for k in A.KINDS}


# ---- readiness estimates + gap report ---------------------------------------

def test_status_verdict_is_pure_passthrough(_repo):
    ds = small_source(_repo)
    st = A.readiness_status(str(ds.relative_to(_repo)).replace("\\", "/"))
    direct = V24.validate_sample(str(ds))["classification"]["verdict"]
    assert st["readiness_verdict"] == direct     # NEVER invented locally


def test_no_sample_at_all(_repo):
    st = A.readiness_status()
    assert st["readiness_verdict"] == V24.C_NO_SAMPLE
    gr = A.gap_report()
    assert any("no data at all" in g for g in gr["gaps"])


def test_estimates_honest_when_rate_unknown(_repo):
    small_source(_repo)
    st = A.readiness_status(f"external_data/staging/{V27.STAGING_MARKER}/dataset")
    d = st["per_kind"]
    assert d["trades"]["rows"] == 0 and d["trades"]["estimated_days_remaining"] is None
    assert "trades" in st["estimate_unknown_for"]
    assert st["estimated_days_to_ready"] is None      # unknowns -> no global promise
    assert d["oi"]["estimate_is_rough"] is True
    assert d["oi"]["estimated_days_remaining"] is not None
    assert d["oi"]["estimated_days_remaining"] > 0


def test_gap_report_names_exact_deficits(_repo):
    small_source(_repo)
    gr = A.gap_report(f"external_data/staging/{V27.STAGING_MARKER}/dataset")
    text = " | ".join(gr["gaps"])
    assert "trades: MISSING" in text and "liquidations: MISSING" in text
    assert "coverage" in text                       # oi/orderbook under 30 days
    assert gr["actions"] and any("continuous-collection-run-cycle-v1027" in a
                                 for a in gr["actions"])
    assert "does NOT mean an edge exists" in gr["honesty"]
    assert gr["final_recommendation"] == "NO LIVE"


# ---- CLI wiring + isolation --------------------------------------------------

def _run_main(argv):
    old = sys.argv
    sys.argv = ["prog"] + argv
    try:
        research_lab.main()
    finally:
        sys.argv = old


def test_cli_allowlisted_and_isolated(_repo, monkeypatch, capsys):
    for c in ("free-microstructure-assembler-plan-v1029",
              "free-microstructure-assemble-sample-v1029",
              "free-microstructure-readiness-status-v1029",
              "free-microstructure-gap-report-v1029"):
        assert c in research_lab.PUBLIC_RESEARCH_ONLY_COMMANDS
    monkeypatch.setattr(research_lab, "load_config",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no config")))
    monkeypatch.setattr(research_lab, "Database",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no db")))
    _run_main(["free-microstructure-assembler-plan-v1029"])
    assert "ASSEMBLER PLAN V10.29" in capsys.readouterr().out
    small_source(_repo)
    _run_main(["free-microstructure-assemble-sample-v1029", "--symbols", "BTCUSDT"])
    out = capsys.readouterr().out
    assert "DRY_RUN" in out and "NO LIVE" in out
    _run_main(["free-microstructure-gap-report-v1029"])
    assert "GAP REPORT V10.29" in capsys.readouterr().out


# ---- outputs gitignored + no dangerous primitives ----------------------------

def test_staging_outputs_gitignored():
    repo = Path(research_lab.__file__).resolve().parents[1]
    gi = (repo / ".gitignore").read_text(encoding="utf-8")
    assert "external_data/staging/" in gi


def test_module_offline_and_no_dangerous_primitives():
    src = Path(A.__file__).read_text(encoding="utf-8")
    for tok in ["urllib", "urlopen", "requests.", "websocket", "socket",
                "load_dotenv", "os.environ", "private_get", "private_post",
                "db.execute", "INSERT INTO", "X-MBX-APIKEY", "listenKey",
                "import torch", "import tensorflow"]:
        assert tok not in src, tok
    for name in ["place_order", "create_order", "set_leverage", "set_margin_mode",
                 "open_position"]:
        assert f"{name}(" not in src and f".{name}" not in src, name
    assert "ExecutionEngine(" not in src and "PaperTrader(" not in src
