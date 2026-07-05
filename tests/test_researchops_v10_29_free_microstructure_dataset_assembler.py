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


def _write_cont_manifest(repo, last_cycle, rows=100, errors=None, cycles=3, liq=0):
    ds = repo / "external_data" / "staging" / V27.STAGING_MARKER / "dataset"
    ds.mkdir(parents=True, exist_ok=True)
    (ds / "manifest.json").write_text(json.dumps({
        "last_cycle": last_cycle, "cycles": cycles,
        "errors_last_cycle": errors or [],
        "cumulative_added": {"trades": rows, "oi": 10, "liquidations": liq}}),
        encoding="utf-8")


def test_binance_ws_no_frames_suspected_diagnostic(_repo):
    """V10.31: 0 ws liquidations across many error-free cycles while REST kinds
    grow = the Binance derivatives stream is silent from this network. Loud
    diagnosis everywhere, but the verdict NEVER changes and READY stays out."""
    small_source(_repo)
    _write_cont_manifest(_repo, "2026-01-01T00:00:00+00:00", cycles=50, liq=0)
    st = A.readiness_status(f"external_data/staging/{V27.STAGING_MARKER}/dataset")
    assert st["binance_ws_no_frames_suspected"] is True
    assert st["readiness_verdict"] != "MICROSTRUCTURE_RESEARCH_READY"   # diagnosis != READY
    gr = A.gap_report(f"external_data/staging/{V27.STAGING_MARKER}/dataset")
    assert gr["binance_ws_no_frames_suspected"] is True
    assert any("BINANCE_DERIVATIVES_WS_NO_FRAMES_SUSPECTED" in g for g in gr["gaps"])
    assert any("NEEDS_LIQUIDATIONS" in str(st.get("active_gaps"))
               for _ in [0])                                            # gap NOT hidden
    A.write_status_page()
    html = (_repo / "reports" / "research" / "v10_29" / "status.html").read_text(encoding="utf-8")
    assert "BINANCE_DERIVATIVES_WS_NO_FRAMES_SUSPECTED" in html
    assert "can_research_microstructure=false" in html
    assert "Binance native liquidations" in html and "Bybit alternative liquidations" in html


def test_no_frames_diagnostic_clears_when_conditions_absent(_repo):
    small_source(_repo)
    # (a) liquidations present -> no suspicion
    _write_cont_manifest(_repo, "2026-01-01T00:00:00+00:00", cycles=50, liq=7)
    assert A.readiness_status()["binance_ws_no_frames_suspected"] is False
    # (b) few cycles -> too early to suspect
    _write_cont_manifest(_repo, "2026-01-01T00:00:00+00:00", cycles=5, liq=0)
    assert A.readiness_status()["binance_ws_no_frames_suspected"] is False
    # (c) liquidations errored last cycle -> that error is the story, not silence
    _write_cont_manifest(_repo, "2026-01-01T00:00:00+00:00", cycles=50, liq=0,
                         errors=["liquidations:RuntimeError:x"])
    assert A.readiness_status()["binance_ws_no_frames_suspected"] is False


def test_local_loop_scripts_are_safe_and_separated():
    repo = Path(research_lab.__file__).resolve().parents[1]
    # V10.36: renamed to reflect that it collects the FULL microstructure now
    bybit = (repo / "scripts" / "collect_bybit_microstructure_forever.ps1").read_text(encoding="utf-8")
    assert "bybit-liquidations-ws-collect-v1030" in bybit
    assert "bybit-microstructure-run-cycle-v1032" in bybit
    assert "collect_forever.ps1" not in bybit.replace("collect_bybit", "")  # independent loop
    assert "BitgetBotBybitLiqV1030" in bybit                                # own mutex
    assert "NUNCA produce READY" in bybit and "Ctrl+C" in bybit
    for tok in ("place_order", "set_leverage", "private", "api_key", ".env",
                "LIVE_TRADING", "PaperTrader", "Start Menu", "Startup"):
        assert tok not in bybit, tok                                        # no autostart, no danger
    # legacy wrapper: warns and delegates, contains no loop logic of its own
    wrapper = (repo / "scripts" / "collect_bybit_liquidations_forever.ps1").read_text(encoding="utf-8")
    assert "LEGACY WRAPPER" in wrapper
    assert "collect_bybit_microstructure_forever.ps1" in wrapper
    assert "mutex" not in wrapper.lower()          # no duplicate loop machinery
    for tok in ("place_order", "api_key", ".env", "LIVE_TRADING", "Startup"):
        assert tok not in wrapper, tok
    scanner = (repo / "scripts" / "run_scanner.bat").read_text(encoding="utf-8")
    assert ".venv\\Scripts\\python.exe" in scanner                          # venv preferred
    assert '"%PY%" -m app.research_lab opportunity-scanner-run-v1028' in scanner
    for tok in ("place_order", "set_leverage", "private_get", "api_key", "LIVE_TRADING"):
        assert tok not in scanner, tok


def test_collector_errors_surface_in_status_gaps_and_page(_repo):
    """A silent per-cycle collector failure (e.g. websocket module missing)
    once cost 40+ cycles of liquidations -- it must be LOUD everywhere now."""
    small_source(_repo)
    err = "liquidations:RuntimeError:websocket_client_unavailable:ModuleNotFoundError"
    _write_cont_manifest(_repo, "2026-01-01T00:00:00+00:00", errors=[err])
    st = A.readiness_status()
    assert st["collector_errors_last_cycle"] == [err]
    gr = A.gap_report()
    assert any("COLLECTOR ERROR" in g for g in gr["gaps"])
    A.write_status_page()
    html = (_repo / "reports" / "research" / "v10_29" / "status.html").read_text(encoding="utf-8")
    assert "COLLECTOR ERROR" in html and "websocket_client_unavailable" in html


def test_bottleneck_reported(_repo):
    small_source(_repo)
    st = A.readiness_status(f"external_data/staging/{V27.STAGING_MARKER}/dataset")
    # trades/liquidations have no observed rate -> they are the bottleneck
    assert st["bottleneck"] in ("trades", "liquidations")
    gr = A.gap_report(f"external_data/staging/{V27.STAGING_MARKER}/dataset")
    assert any("bottleneck" in g for g in gr["gaps"])


def test_status_page_never_contains_old_actionable_labels(_repo):
    small_source(_repo)
    scanner_dir = _repo / "reports" / "research" / "v10_28"
    scanner_dir.mkdir(parents=True)
    (scanner_dir / "scanner_state.json").write_text(json.dumps({
        "written_at": "2026-07-03T00:00:00Z",
        "verdict": "SHADOW_OBSERVATION_CANDIDATES_NOT_ACTIONABLE",
        "n_shadow_candidates": 0, "opportunity_board": []}), encoding="utf-8")
    A.write_status_page()
    html = (_repo / "reports" / "research" / "v10_29" / "status.html").read_text(encoding="utf-8")
    for banned in ("SHADOW_ENTRY_CANDIDATE", "BUY NOW", "EXECUTABLE SIGNAL",
                   "buy now", "signal executable"):
        assert banned not in html, banned
    assert "edge_validated=false" in html and "not_actionable=true" in html


def test_freshness_stale_warning_true_when_collector_is_newer(_repo):
    small_source(_repo)
    _write_cont_manifest(_repo, "2026-01-01T00:00:00+00:00")
    rep = A.assemble("BTCUSDT", apply=True, run_label="latest")
    assert rep["writes"] is True
    # collector moved on AFTER the assemble -> the sample is stale
    _write_cont_manifest(_repo, "2099-01-01T00:00:00+00:00")
    st = A.readiness_status()
    assert st["status_source"] == "latest_assembled_v10_29"
    assert st["assembled_at"] is not None
    assert st["continuous_last_cycle"] == "2099-01-01T00:00:00+00:00"
    assert st["continuous_dataset_rows"] == 110
    assert st["latest_assembled_rows"] > 0
    assert st["stale_assembled_warning"] is True
    gr = A.gap_report()
    assert any("stale" in g for g in gr["gaps"])
    uri = A.write_status_page()
    html = (_repo / "reports" / "research" / "v10_29" / "status.html").read_text(encoding="utf-8")
    assert "WARNING: assembled sample is stale" in html
    assert "continuous_last_cycle=" in html and "assembled_at=" in html
    assert "continuous_dataset_rows=" in html and "latest_assembled_rows=" in html


def test_freshness_no_warning_when_just_assembled(_repo):
    small_source(_repo)
    _write_cont_manifest(_repo, "2026-01-01T00:00:00+00:00")
    A.assemble("BTCUSDT", apply=True, run_label="latest")
    st = A.readiness_status()
    assert st["stale_assembled_warning"] is False
    A.write_status_page()
    html = (_repo / "reports" / "research" / "v10_29" / "status.html").read_text(encoding="utf-8")
    assert "WARNING: assembled sample is stale" not in html
    assert "stale_assembled_warning=false" in html


def test_fixed_run_label_overwrites_and_wins_latest(_repo):
    small_source(_repo)
    r1 = A.assemble("BTCUSDT", apply=True, run_label="latest")
    r2 = A.assemble("BTCUSDT", apply=True, run_label="latest")
    assert r1["sample_dir"] == r2["sample_dir"]           # same dir, overwritten
    root = _repo / "external_data" / "staging" / A.STAGING_MARKER
    assert [d.name for d in root.iterdir() if d.is_dir()] == ["latest"]
    for bad in ("../x", "a/b", "lat est", ".hidden", "x" * 41):
        rep = A.assemble("BTCUSDT", apply=True, run_label=bad)
        assert rep["writes"] is False and any("unsafe_run_label" in e for e in rep["errors"])


def test_run_id_unique_back_to_back():
    assert A._run_id() != A._run_id()


def test_collector_script_assembles_before_status_page_and_is_safe():
    repo = Path(research_lab.__file__).resolve().parents[1]
    src = (repo / "scripts" / "collect_forever.ps1").read_text(encoding="utf-8")
    i_run = src.index("continuous-collection-run-cycle-v1027")
    i_asm = src.index("free-microstructure-assemble-sample-v1029")
    i_stat = src.index("free-microstructure-readiness-status-v1029")
    i_page = src.index("free-microstructure-status-page-v1029")
    assert i_run < i_asm < i_stat < i_page                 # Codex V10.29.2 flow
    assert "--run-label latest" in src and "--apply" in src
    for tok in ("place_order", "create_order", "set_leverage", "set_margin_mode",
                "private_get", "private_post", "X-MBX-APIKEY", "listenKey",
                ".env", "api_key", "apikey", "LIVE_TRADING", "PaperTrader",
                "ExecutionEngine", "Invoke-WebRequest", "Invoke-RestMethod"):
        assert tok not in src, tok
    assert "Local\\BitgetBotCollectorV1027" in src          # mutex kept
    assert "Ctrl+C" in src                                  # safe-stop help kept


def test_status_page_scanner_ranking_not_actionable(_repo):
    small_source(_repo)
    scanner_dir = _repo / "reports" / "research" / "v10_28"
    scanner_dir.mkdir(parents=True)
    (scanner_dir / "scanner_state.json").write_text(json.dumps({
        "written_at": "2026-07-02T00:00:00Z",
        "verdict": "SHADOW_OBSERVATION_CANDIDATES_NOT_ACTIONABLE",
        "n_shadow_candidates": 1,
        "opportunity_board": [{"symbol": "BTCUSDT", "edge_score": 85,
                               "side": "long", "regime": "RISK_ON"}]}), encoding="utf-8")
    A.write_status_page()
    html = (_repo / "reports" / "research" / "v10_29" / "status.html").read_text(encoding="utf-8")
    assert "NOT_ACTIONABLE" in html
    assert "edge_validated=false" in html and "not_actionable=true" in html
    assert "no_orders=true" in html
    # the flags sit NEXT TO the ranking table, before the board rows
    assert html.index("edge_validated=false") < html.index("BTCUSDT</td>")
    for banned in ("buy now", "signal executable", "http://", "https://", "<script"):
        assert banned not in html


def test_status_page_written_under_reports_and_honest(_repo):
    small_source(_repo)
    uri = A.write_status_page()
    assert uri.startswith("file:///")
    page = _repo / "reports" / "research" / "v10_29" / "status.html"
    assert page.is_file()
    html = page.read_text(encoding="utf-8")
    assert "NO LIVE" in html and "NEEDS_MORE_HISTORY" in html
    assert "MODO SEGURO" in html and "heuristicos" in html    # honest banners
    assert "/api/" not in html                                 # static: no endpoints


def test_status_page_cli_prints_dashboard_link(_repo, monkeypatch, capsys):
    assert "free-microstructure-status-page-v1029" in research_lab.PUBLIC_RESEARCH_ONLY_COMMANDS
    monkeypatch.setattr(research_lab, "load_config",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no config")))
    small_source(_repo)
    _run_main(["free-microstructure-status-page-v1029"])
    out = capsys.readouterr().out
    assert "DASHBOARD: file:///" in out and "NO LIVE" in out


def test_cli_allowlisted_and_isolated(_repo, monkeypatch, capsys):
    for c in ("free-microstructure-assembler-plan-v1029",
              "free-microstructure-assemble-sample-v1029",
              "free-microstructure-readiness-status-v1029",
              "free-microstructure-gap-report-v1029",
              "free-microstructure-status-page-v1029"):
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
