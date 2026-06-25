"""ResearchOps V10.26 - Free Public Liquidations WS Collector tests.

Public websocket only, dry-run by default, staging-only, NO keys/auth/DB/orders.
The websocket is always mocked here (event_source injection) -- no real network.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from app import research_lab
from app.labs import free_public_liquidations_ws_collector_v10_26 as L
from app.labs import microstructure_sample_adapter_v10_24 as M

MODULE_PATH = "app/labs/free_public_liquidations_ws_collector_v10_26.py"


def _ev(symbol="BTCUSDT", side="SELL", price="50000", qty="0.5", T=1700000000000):
    return {"e": "forceOrder", "E": T,
            "o": {"s": symbol, "S": side, "p": price, "ap": price, "q": qty,
                  "z": qty, "X": "FILLED", "T": T}}


class _BoomIterable:
    def __iter__(self):
        raise AssertionError("event source iterated despite a blocked staging dir")


# ---- plan / dry-run -------------------------------------------------------

def test_plan_no_network_no_writes_no_live():
    p = L.liquidations_ws_plan()
    assert p["writes_on_plan"] is False and p["uses_api_keys"] is False
    assert p["subscribes_private_channels"] is False
    assert p["paper_ready"] is False and p["live_ready"] is False
    assert p["can_send_real_orders"] is False and p["final_recommendation"] == "NO LIVE"
    assert "binance_usdm" in p["implemented_exchanges"]


def test_dry_run_no_writes(monkeypatch):
    monkeypatch.setattr(L, "_default_event_source",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no ws in dry-run")))
    rep = L.collect("binance_usdm", ["BTCUSDT"], apply=False)
    assert rep["mode"] == "DRY_RUN" and rep["writes"] is False and "staging_dir" not in rep


def test_collect_without_apply_no_writes():
    rep = L.collect("binance_usdm", ["BTCUSDT"], apply=False, max_events=2, max_runtime_seconds=1)
    assert rep["writes"] is False and "output_file" not in rep


# ---- apply (mocked source) ------------------------------------------------

def _cleanup(rep):
    shutil.rmtree(rep["staging_dir"], ignore_errors=True)
    try:
        os.rmdir(os.path.dirname(rep["staging_dir"]))
    except OSError:
        pass


def test_apply_writes_only_staging_and_manifest():
    events = [_ev(T=1700000000000 + i * 60000) for i in range(6)]
    rep = L.collect("binance_usdm", ["BTCUSDT"], apply=True, max_events=50,
                    max_runtime_seconds=30, event_source=events)
    try:
        assert rep["mode"] == "APPLY" and rep["writes"] is True
        assert L.STAGING_MARKER in rep["staging_dir"]
        assert rep["event_count"] == 6
        assert os.path.isfile(rep["output_file"]) and os.path.isfile(rep["manifest"])
        man = json.loads(Path(rep["manifest"]).read_text(encoding="utf-8"))
        assert man["final_recommendation"] == "NO LIVE" and man["event_count"] == 6
        assert man["research_only"] is True and man["can_send_real_orders"] is False
    finally:
        _cleanup(rep)


def test_output_csv_v1024_detected_as_liquidations_not_ready():
    events = [_ev(side="SELL" if i % 2 else "BUY", price="50000", qty="1",
                  T=1700000000000 + i * 3600000) for i in range(15)]
    rep = L.collect("binance_usdm", ["BTCUSDT"], apply=True, max_events=50,
                    max_runtime_seconds=30, event_source=events)
    try:
        vr = M.validate_sample(rep["staging_dir"])
        assert "liquidations" in vr["by_type"]
        assert vr["by_type"]["liquidations"]["valid"] is True
        assert vr["classification"]["verdict"] != M.C_READY   # sparse + only liquidations
        assert vr["final_recommendation"] == "NO LIVE"
    finally:
        _cleanup(rep)


# ---- staging containment (hardened) ---------------------------------------

def _fake_repo_symlink_marker(tmp_path):
    repo = tmp_path / "repo"
    (repo / "external_data" / "staging").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / "external_data" / "staging" / L.STAGING_MARKER).symlink_to(outside, target_is_directory=True)
    return repo


def test_staging_root_symlink_and_child_blocked(tmp_path, monkeypatch):
    try:
        repo = _fake_repo_symlink_marker(tmp_path)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    monkeypatch.setattr(L, "_repo_root", lambda: repo)
    with pytest.raises(ValueError):
        L.safe_staging_dir(f"external_data/staging/{L.STAGING_MARKER}")
    with pytest.raises(ValueError):
        L.safe_staging_dir(f"external_data/staging/{L.STAGING_MARKER}/run1")


def test_apply_unsafe_output_dir_no_network_no_write():
    rep = L.collect("binance_usdm", ["BTCUSDT"], apply=True,
                    output_dir=f"reports/{L.STAGING_MARKER}", event_source=_BoomIterable())
    assert rep["mode"] == "APPLY" and rep["writes"] is False
    assert any("unsafe_output_dir" in e for e in rep["errors"])
    assert "staging_dir" not in rep


def test_staging_rejects_traversal_and_substring():
    L.safe_staging_dir(f"external_data/staging/{L.STAGING_MARKER}")
    for bad in (f"reports/{L.STAGING_MARKER}", f"tmp/{L.STAGING_MARKER}",
                f"../{L.STAGING_MARKER}", f"external_data/staging/{L.STAGING_MARKER}_evil",
                "external_data/raw/x"):
        with pytest.raises(ValueError):
            L.safe_staging_dir(bad)


# ---- websocket request safety ---------------------------------------------

def test_ws_allowlist_blocks_host_path_auth_query_private():
    L.assert_safe_ws("wss://fstream.binance.com/ws/!forceOrder@arr", {})
    L.assert_safe_ws("wss://fstream.binance.com/ws/btcusdt@forceOrder", {})
    for bad in ("wss://evil.example.com/ws/!forceOrder@arr",
                "ws://fstream.binance.com/ws/!forceOrder@arr",            # not wss
                "wss://fstream.binance.com/ws/btcusdt@trade",             # not a liquidation stream
                "wss://fstream.binance.com/ws/btcusdt@listenKey",         # private/user stream
                "wss://fstream.binance.com/ws/!forceOrder@arr?apiKey=x",  # sensitive query
                "wss://fstream.binance.com/ws/!forceOrder@arr?signature=x"):
        with pytest.raises(ValueError):
            L.assert_safe_ws(bad, {})
    with pytest.raises(ValueError):
        L.assert_safe_ws("wss://fstream.binance.com/ws/!forceOrder@arr", {"X-MBX-APIKEY": "k"})
    with pytest.raises(ValueError):
        L.assert_safe_ws("wss://fstream.binance.com/ws/!forceOrder@arr", {"Authorization": "Bearer x"})


# ---- parsing / side / dedup -----------------------------------------------

def test_binance_force_order_to_canonical():
    row, why = L.parse_binance_force_order(_ev(side="SELL", price="50000", qty="0.5"), received_at=123)
    assert why is None and row["exchange"] == "binance_usdm" and row["symbol"] == "BTCUSDT"
    assert row["side"] == "sell" and float(row["notional"]) == 25000.0
    assert row["received_at"] == 123 and row["event_type"] == "forceOrder"
    assert set(L.CANON_HEADER) <= set(row)


def test_side_mapping_and_uncertain_rejected():
    assert L.parse_binance_force_order(_ev(side="BUY"))[0]["side"] == "buy"
    assert L.parse_binance_force_order(_ev(side="SELL"))[0]["side"] == "sell"
    row, why = L.parse_binance_force_order(_ev(side="WEIRD"))
    assert row is None and why == "side_mapping_uncertain"


def test_invalid_payload_no_crash_rejected():
    rep = L.collect("binance_usdm", ["BTCUSDT"], apply=True, max_events=50, max_runtime_seconds=30,
                    event_source=[{"e": "depthUpdate"}, "not-json", _ev(price="0")])
    try:
        assert rep["event_count"] == 0
        assert "not_a_force_order_event" in rep["rejected"]
        assert "json_decode_error" in rep["rejected"]
        assert "non_positive_price_or_size" in rep["rejected"]
    finally:
        _cleanup(rep)


def test_dedup_identical_events():
    ev = _ev(T=1700000000000)
    rep = L.collect("binance_usdm", ["BTCUSDT"], apply=True, max_events=50, max_runtime_seconds=30,
                    event_source=[ev, dict(ev), dict(ev)])
    try:
        assert rep["event_count"] == 1 and rep["duplicates"] == 2
    finally:
        _cleanup(rep)


# ---- CLI isolation + security ---------------------------------------------

def _run_main(argv):
    old = sys.argv
    sys.argv = ["prog"] + argv
    try:
        research_lab.main()
    finally:
        sys.argv = old


def test_cli_allowlisted_and_isolated(monkeypatch, capsys):
    for c in ("free-liquidations-ws-plan-v1026", "free-liquidations-ws-dry-run-v1026",
              "free-liquidations-ws-collect-v1026"):
        assert c in research_lab.PUBLIC_RESEARCH_ONLY_COMMANDS
    monkeypatch.setattr(research_lab, "load_config",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no config")))
    monkeypatch.setattr(research_lab, "Database",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no db")))
    _run_main(["free-liquidations-ws-plan-v1026"])
    assert "FREE LIQUIDATIONS WS PLAN V10.26" in capsys.readouterr().out
    _run_main(["free-liquidations-ws-collect-v1026", "--symbols", "BTCUSDT",
               "--max-runtime-seconds", "5", "--max-events", "2"])
    out = capsys.readouterr().out
    assert "DRY_RUN" in out and "NO LIVE" in out   # no --apply -> no websocket/writes


def test_module_no_dangerous_primitives():
    import re
    src = Path(MODULE_PATH).read_text(encoding="utf-8")
    scan = re.sub(r'"never":\s*\[.*?\],', '"never": [],', src, flags=re.DOTALL)
    for tok in ["load_dotenv", "os.environ", "private_get", "private_post", "db.execute",
                "INSERT INTO", "import torch", "import tensorflow", "listenKey", "X-MBX-APIKEY"]:
        assert tok not in scan, tok
    for name in ["place_order", "create_order", "set_leverage", "set_margin_mode", "open_position"]:
        assert f"{name}(" not in scan and f".{name}" not in scan, name
    assert "ExecutionEngine(" not in scan and "PaperTrader(" not in scan
