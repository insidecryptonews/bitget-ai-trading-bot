"""ResearchOps V10.30 - Bybit Public Liquidations Forward Collector tests.

Research-only, dry-run by default, staging-only, NO keys/private-topics/orders.
All websocket traffic is mocked (fixtures of official v5 All Liquidation
payloads); no real network in tests. Verifies OPTION A isolation: Bybit rows
NEVER reach the Binance sample nor produce MICROSTRUCTURE_RESEARCH_READY.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from app import research_lab
from app.labs import bybit_public_liquidations_ws_collector_v10_30 as B
from app.labs import free_microstructure_dataset_assembler_v10_29 as A


@pytest.fixture(autouse=True)
def _isolated_repo(tmp_path, monkeypatch):
    """Fake repo root: tests never touch the real staging datasets."""
    repo = tmp_path / "repo"
    (repo / "external_data" / "staging").mkdir(parents=True)
    monkeypatch.setattr(B, "_repo_root", lambda: repo)
    monkeypatch.setattr(A, "_repo_root", lambda: repo)
    monkeypatch.chdir(repo)
    yield repo


def _frame(entries):
    """Official v5 All Liquidation frame fixture."""
    return json.dumps({"topic": "allLiquidation.BTCUSDT", "type": "snapshot",
                       "ts": 1783123000000, "data": entries})


def _entry(T=1783123000001, s="BTCUSDT", S="Buy", v="0.5", p="62000.5"):
    return {"T": T, "s": s, "S": S, "v": v, "p": p}


def _collect(events, symbols=("BTCUSDT",), apply=True, max_events=100,
             max_runtime=30):
    return B.collect(list(symbols), apply=apply, max_runtime_seconds=max_runtime,
                     max_events=max_events, event_source=events)


# ---- plan / dry-run ---------------------------------------------------------

def test_plan_no_network_no_writes_no_live():
    p = B.plan()
    assert p["writes_on_plan"] is False and p["uses_network"] is False
    assert p["uses_api_keys"] is False and p["can_send_real_orders"] is False
    assert p["final_recommendation"] == "NO LIVE"
    assert p["cross_exchange_liquidations_used_for_ready"] is False
    assert "long position" in p["side_semantics"] or "long liquidated" in p["side_semantics"]


def test_dry_run_no_writes(_isolated_repo):
    rep = _collect([], apply=False)
    assert rep["mode"] == "DRY_RUN" and rep["writes"] is False
    assert not (_isolated_repo / "external_data" / "staging" / B.STAGING_MARKER).exists()


# ---- parsing + verified side semantics --------------------------------------

def test_parse_official_fixture_and_side_semantics():
    rows, rejects = B.parse_bybit_all_liquidation(
        _frame([_entry(S="Buy"), _entry(T=1783123000002, S="Sell")]))
    assert rejects == [] and len(rows) == 2
    long_liq, short_liq = rows
    # OFFICIAL v5 docs: Buy update => LONG position liquidated (opposite of
    # the Binance ORDER-side convention)
    assert long_liq["bybit_side_raw"] == "Buy" and long_liq["position_liquidated"] == "long"
    assert short_liq["bybit_side_raw"] == "Sell" and short_liq["position_liquidated"] == "short"
    assert long_liq["exchange"] == "bybit_linear"
    assert long_liq["event_type"] == "liquidation"
    assert long_liq["price_type"] == "bankruptcy_price"    # honest price labelling
    assert long_liq["raw_event_id"].startswith("bybit_linear:BTCUSDT:")
    assert B.SIDE_MAPPING_VERIFIED is True


def test_parse_rejects_bad_payloads():
    assert B.parse_bybit_all_liquidation("not json") == ([], ["json_decode_error"])
    assert B.parse_bybit_all_liquidation(json.dumps({"success": True, "op": "subscribe"})) == ([], [])
    rows, rejects = B.parse_bybit_all_liquidation(_frame([_entry(S="Hold")]))
    assert rows == [] and any("unknown_side" in r for r in rejects)
    rows, rejects = B.parse_bybit_all_liquidation(_frame([_entry(p="-5")]))
    assert rows == [] and "non_positive_price_or_size" in rejects
    rows, rejects = B.parse_bybit_all_liquidation(
        json.dumps({"topic": "kline.BTCUSDT", "data": []}))
    assert rows == [] and rejects == ["unexpected_topic"]


# ---- apply: staging-only writes, manifest/checkpoint, dedup, bounds ---------

def test_apply_writes_only_staging_with_manifest_and_checkpoint(_isolated_repo):
    rep = _collect([_frame([_entry(), _entry(T=1783123000002, S="Sell")])])
    assert rep["mode"] == "APPLY" and rep["writes"] is True and rep["added"] == 2
    ds = Path(rep["dataset_dir"])
    assert B.STAGING_MARKER in str(ds)
    assert (ds / "liquidations.csv").is_file()
    man = json.loads((ds / "manifest.json").read_text(encoding="utf-8"))
    assert man["source_exchange"] == "bybit_linear" and man["cycles"] == 1
    assert man["cumulative_rows"] == 2 and man["errors_last_cycle"] == []
    assert man["cross_exchange_liquidations_used_for_ready"] is False
    assert man["final_recommendation"] == "NO LIVE"
    ck = json.loads((ds / "checkpoint.json").read_text(encoding="utf-8"))
    assert ck["rows_total"] == 2 and ck["seen_count"] == 2


def test_dedup_across_cycles(_isolated_repo):
    r1 = _collect([_frame([_entry()])])
    assert r1["added"] == 1
    r2 = _collect([_frame([_entry()]), _frame([_entry(T=1783123999999)])])
    assert r2["added"] == 1                       # duplicate dropped, new kept
    assert r2["cumulative_rows"] == 2


def test_max_events_bounds(_isolated_repo):
    frames = [_frame([_entry(T=1783123000000 + i)]) for i in range(10)]
    rep = _collect(frames, max_events=3)
    assert rep["added"] == 3


def test_max_runtime_bounds(_isolated_repo, monkeypatch):
    clock = {"t": 1000.0}

    def fake_time():
        clock["t"] += 100.0                        # each check jumps 100s
        return clock["t"]

    monkeypatch.setattr(B.time, "time", fake_time)
    frames = [_frame([_entry(T=1783123000000 + i)]) for i in range(50)]
    rep = _collect(frames, max_runtime=150)
    assert rep["added"] < 50                       # stopped early by runtime


# ---- hardened containment / safety gates ------------------------------------

def test_unsafe_output_dirs_fail(_isolated_repo):
    for bad in ("reports/x", f"external_data/staging/{B.STAGING_MARKER}_evil",
                f"../{B.STAGING_MARKER}", "external_data/raw/x",
                f"external_data/staging/{B.STAGING_MARKER}/../db"):
        rep = B.collect(["BTCUSDT"], apply=True, output_dir=bad,
                        event_source=[_frame([_entry()])])
        assert rep["writes"] is False, bad
        assert any("unsafe_output_dir" in e for e in rep["errors"]), bad


def test_root_and_child_symlink_blocked(tmp_path, monkeypatch):
    repo = tmp_path / "symrepo"
    (repo / "external_data" / "staging").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (repo / "external_data" / "staging" / B.STAGING_MARKER).symlink_to(
            outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    monkeypatch.setattr(B, "_repo_root", lambda: repo)
    with pytest.raises(ValueError):
        B.safe_staging_dir()
    with pytest.raises(ValueError):
        B.safe_staging_dir(f"external_data/staging/{B.STAGING_MARKER}/{B.DATASET_SUBDIR}")


def test_ws_url_exact_allowlist_and_auth_headers_blocked():
    B.assert_safe_ws(B.WS_URL, {})
    for bad_url in ("wss://stream.bybit.com/v5/private", "wss://evil.com/v5/public/linear",
                    "ws://stream.bybit.com/v5/public/linear",
                    "wss://stream.bybit.com/v5/public/linear?token=x"):
        with pytest.raises(ValueError):
            B.assert_safe_ws(bad_url, {})
    for bad_hdr in ("Authorization", "X-BAPI-API-KEY", "X-BAPI-SIGN", "Cookie"):
        with pytest.raises(ValueError):
            B.assert_safe_ws(B.WS_URL, {bad_hdr: "x"})


def test_private_topics_blocked():
    B.assert_safe_topics(["allLiquidation.BTCUSDT"])
    for bad in (["order"], ["position.BTCUSDT"], ["execution"], ["wallet"],
                ["liquidation.BTCUSDT;order"], ["allLiquidation.btcusdt"],
                ["kline.1.BTCUSDT"], []):
        with pytest.raises(ValueError):
            B.assert_safe_topics(bad)


def test_ws_dependency_missing_is_loud(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "websocket":
            raise ModuleNotFoundError("No module named 'websocket'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    diag = {}
    with pytest.raises(RuntimeError, match="websocket_client_unavailable"):
        list(B._default_event_source(B.WS_URL, ["allLiquidation.BTCUSDT"], 1, 1, diag))


def test_connected_but_zero_frames_is_surfaced(_isolated_repo, monkeypatch):
    """Incident-2 lesson: a connected-but-silent stream must be LOUD, never a
    quiet success indistinguishable from a calm market."""

    def fake_source(url, topics, max_rt, max_ev, diagnostics):
        diagnostics["connected"] = True     # handshake ok...
        return iter([])                     # ...but zero frames delivered

    monkeypatch.setattr(B, "_default_event_source", fake_source)
    rep = B.collect(["BTCUSDT"], apply=True, max_runtime_seconds=1, max_events=5)
    assert rep["added"] == 0
    assert any("connected_but_zero_frames" in e for e in rep["errors"])


# ---- OPTION A guard: never mixed into the Binance sample / никогда READY ----

def test_option_a_bybit_rows_never_reach_binance_sample(_isolated_repo):
    _collect([_frame([_entry(), _entry(T=1783123000005, S="Sell")])])
    # the V10.29 assembler must NOT discover the v10_30 marker as a source
    src = A.discover_sources()
    for entry in src.values():
        assert B.STAGING_MARKER not in entry["marker"]
        for d in entry["dirs"]:
            assert B.STAGING_MARKER not in d
    rep = A.assemble("BTCUSDT", apply=True)
    assert "missing_liquidations" in rep["gaps"]          # Bybit rows did NOT leak
    assert rep.get("readiness_verdict") != "MICROSTRUCTURE_RESEARCH_READY"


def test_alt_status_informative_but_never_ready(_isolated_repo):
    _collect([_frame([_entry()])])
    alt = B.alt_liquidations_status()
    assert alt["alternative_liquidations_source"] == "bybit_linear"
    assert alt["bybit_liquidations_rows"] == 1
    assert alt["cross_exchange_liquidations_available"] is True
    assert alt["cross_exchange_liquidations_used_for_ready"] is False
    assert "not Binance-native" in alt["warning"]
    st = A.readiness_status()
    assert st.get("bybit_alt", {}).get("bybit_liquidations_rows") == 1
    assert st["readiness_verdict"] != "MICROSTRUCTURE_RESEARCH_READY"
    gr = A.gap_report()
    assert gr["cross_exchange_liquidations_used_for_ready"] is False
    assert any("bybit_linear" in g for g in gr["gaps"])
    assert any("NOT used for READY" in g for g in gr["gaps"])


# ---- CLI wiring + isolation --------------------------------------------------

def _run_main(argv):
    old = sys.argv
    sys.argv = ["prog"] + argv
    try:
        research_lab.main()
    finally:
        sys.argv = old


def test_cli_allowlisted_and_isolated(monkeypatch, capsys):
    for c in ("bybit-liquidations-ws-plan-v1030", "bybit-liquidations-ws-dry-run-v1030",
              "bybit-liquidations-ws-collect-v1030"):
        assert c in research_lab.PUBLIC_RESEARCH_ONLY_COMMANDS
    monkeypatch.setattr(research_lab, "load_config",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no config")))
    monkeypatch.setattr(research_lab, "Database",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no db")))
    _run_main(["bybit-liquidations-ws-plan-v1030"])
    out = capsys.readouterr().out
    assert "BYBIT LIQUIDATIONS WS PLAN V10.30" in out and "NO LIVE" in out
    _run_main(["bybit-liquidations-ws-dry-run-v1030", "--symbols", "BTCUSDT",
               "--max-runtime-seconds", "5", "--max-events", "2"])
    out = capsys.readouterr().out
    assert "DRY_RUN" in out and "NO LIVE" in out      # no network, no writes


# ---- no dangerous primitives -------------------------------------------------

def test_module_no_dangerous_primitives():
    src = Path(B.__file__).read_text(encoding="utf-8")
    for tok in ["load_dotenv", "os.environ", "private_get", "private_post",
                "db.execute", "INSERT INTO", "import torch", "import tensorflow",
                "X-MBX-APIKEY", "listenKey", "api_secret", "recv_window"]:
        assert tok not in src, tok
    for name in ["place_order", "create_order", "set_leverage", "set_margin_mode",
                 "open_position"]:
        assert f"{name}(" not in src and f".{name}" not in src, name
    assert "ExecutionEngine(" not in src and "PaperTrader(" not in src
