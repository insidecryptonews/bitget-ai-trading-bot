"""ResearchOps V10.24 - Microstructure Sample Adapter tests.

Offline-only. Verifies schema detection, per-type validation, the readiness
classification, path-safety (no .env/db/raw/backups/vault/traversal),
staging-only normalization, CLI isolation, and the NO-LIVE invariants.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from app import research_lab
from app.labs import microstructure_sample_adapter_v10_24 as M

MODULE_PATH = "app/labs/microstructure_sample_adapter_v10_24.py"
B = 1_700_000_000_000  # ms
DAY = M.DAY_MS


def _write(path: Path, header, rows):
    lines = [",".join(header)] + [",".join(str(c) for c in r) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _trades(side=True, n=40, span_days=35):
    rows = []
    for i in range(n):
        ts = B + int(i * span_days * DAY / n)
        base = [ts, "BTCUSDT", 50000 + i, 0.5]
        rows.append(base + (["buy" if i % 2 else "sell"] if side else []))
    header = ["timestamp", "symbol", "price", "size"] + (["aggressor_side"] if side else [])
    return header, rows


# ---- plan -----------------------------------------------------------------

def test_plan_no_network_no_writes_no_live():
    p = M.microstructure_plan()
    assert p["uses_network"] is False and p["uses_db"] is False and p["writes_on_plan"] is False
    assert p["reads_only_local_files"] is True
    assert p["paper_ready"] is False and p["live_ready"] is False
    assert p["can_send_real_orders"] is False and p["final_recommendation"] == "NO LIVE"


# ---- empty / detection ----------------------------------------------------

def test_validate_empty_is_no_sample(tmp_path):
    rep = M.validate_sample(str(tmp_path))
    assert rep["classification"]["verdict"] == M.C_NO_SAMPLE
    assert rep["final_recommendation"] == "NO LIVE" and rep["uses_network"] is False


def test_trades_with_aggressor_detected(tmp_path):
    h, r = _trades(side=True)
    _write(tmp_path / "trades.csv", h, r)
    rep = M.validate_sample(str(tmp_path))
    tr = rep["by_type"].get("trades", {})
    assert tr.get("valid") is True and tr.get("has_aggressor_side") is True
    assert tr["buy_sell_imbalance"] is not None


def test_trades_without_aggressor_needs_aggressor(tmp_path):
    h, r = _trades(side=False, span_days=35)   # enough history so the gap is aggressor, not history
    _write(tmp_path / "trades.csv", h, r)
    rep = M.validate_sample(str(tmp_path))
    assert rep["by_type"]["trades"]["has_aggressor_side"] is False
    assert rep["classification"]["verdict"] == M.C_NEEDS_AGGRESSOR
    assert M.C_NEEDS_AGGRESSOR in rep["classification"]["gaps"]


def test_orderbook_crossed_is_invalid(tmp_path):
    h = ["timestamp", "symbol", "bid_price_1", "bid_size_1", "ask_price_1", "ask_size_1"]
    rows = [[B + i * 1000, "BTCUSDT", 50010, 1, 50000, 1] for i in range(20)]  # bid>=ask
    _write(tmp_path / "orderbook_l2.csv", h, rows)
    rep = M.validate_sample(str(tmp_path))
    ob = rep["by_type"].get("orderbook", {})
    assert ob.get("valid") is False and ob.get("crossed_book_rows", 0) > 0


def test_orderbook_valid_spread_computed(tmp_path):
    h = ["timestamp", "symbol", "bid_price_1", "bid_size_1", "ask_price_1", "ask_size_1"]
    rows = [[B + i * 1000, "BTCUSDT", 49995, 1, 50005, 1] for i in range(20)]
    _write(tmp_path / "orderbook_l2.csv", h, rows)
    rep = M.validate_sample(str(tmp_path))
    ob = rep["by_type"]["orderbook"]
    assert ob["valid"] is True and ob["spread_median"] is not None and ob["depth_levels"] >= 1


def test_liquidations_schema_detected(tmp_path):
    h = ["timestamp", "symbol", "side", "price", "size", "notional"]
    rows = [[B + i * DAY, "BTCUSDT", "sell" if i % 2 else "buy", 50000, 2, 100000] for i in range(10)]
    _write(tmp_path / "liquidations.csv", h, rows)
    rep = M.validate_sample(str(tmp_path))
    liq = rep["by_type"].get("liquidations", {})
    assert liq.get("valid") is True and liq.get("side_valid") is True and liq.get("notional_calculable") is True


def test_oi_and_funding_schema_detected(tmp_path):
    _write(tmp_path / "open_interest.csv", ["timestamp", "symbol", "open_interest"],
           [[B + i * DAY, "BTCUSDT", 1000 + i] for i in range(10)])
    _write(tmp_path / "funding.csv", ["timestamp", "symbol", "funding_rate"],
           [[B + i * DAY, "BTCUSDT", 0.0001 * (i + 1)] for i in range(10)])
    rep = M.validate_sample(str(tmp_path))
    assert rep["by_type"]["oi"]["valid"] is True
    assert rep["by_type"]["funding"]["valid"] is True


def test_full_sample_research_ready(tmp_path):
    h, r = _trades(side=True, span_days=40)
    _write(tmp_path / "trades.csv", h, r)
    _write(tmp_path / "orderbook_l2.csv",
           ["timestamp", "symbol", "bid_price_1", "ask_price_1"],
           [[B + int(i * 40 * DAY / 40), "BTCUSDT", 49995, 50005] for i in range(40)])
    _write(tmp_path / "open_interest.csv", ["timestamp", "symbol", "open_interest"],
           [[B + i * DAY, "BTCUSDT", 1000 + i] for i in range(40)])
    rep = M.validate_sample(str(tmp_path))
    cls = rep["classification"]
    assert cls["verdict"] == M.C_READY
    assert cls["future_research_ready_if_sample_passes"] is True
    assert cls["future_labs_ready"]["aggressive_flow_imbalance"] is True


# ---- path safety ----------------------------------------------------------

def test_unsafe_sample_dir_blocked():
    for bad in ("external_data/raw/x", "secrets/.env", "a/../b", "x/db/y",
                "backups/z", "vault/z", "x/credentials/y"):
        with pytest.raises(ValueError):
            M.assert_safe_sample_dir(bad)


def test_validate_unsafe_dir_returns_invalid():
    rep = M.validate_sample("external_data/raw/evil")
    assert rep["classification"]["verdict"] == M.C_INVALID
    assert any("unsafe" in e for e in rep["errors"])


def test_normalization_writes_only_staging_marker(tmp_path):
    h, r = _trades(side=True, span_days=35)
    _write(tmp_path / "trades.csv", h, r)
    rep = M.validate_sample(str(tmp_path), apply_normalization=True)
    nrm = rep["normalization"]
    try:
        assert nrm["applied"] is True
        assert M.STAGING_MARKER in nrm["out_dir"]
        assert nrm["wrote_only_staging_marker"] is True
        assert any(p.endswith("trades_normalized.csv") for p in nrm["files"])
    finally:
        # clean only the run dir we just created under the staging marker
        run_root = os.path.dirname(os.path.dirname(nrm["out_dir"]))
        shutil.rmtree(nrm["out_dir"], ignore_errors=True)
        # remove the now-empty <run_id> dir
        try:
            os.rmdir(os.path.dirname(nrm["out_dir"]))
        except OSError:
            pass


def test_safe_normalized_dir_requires_marker():
    M.safe_normalized_dir("run1", f"external_data/staging/{M.STAGING_MARKER}")
    for bad in ("external_data/staging/other", "external_data/raw", "vault"):
        with pytest.raises(ValueError):
            M.safe_normalized_dir("run1", bad)


# ---- CLI isolation (no config/.env/DB) ------------------------------------

def _run_main(argv):
    import sys
    old = sys.argv
    sys.argv = ["prog"] + argv
    try:
        research_lab.main()
    finally:
        sys.argv = old


def test_cli_commands_allowlisted_and_isolated(monkeypatch, capsys):
    cmds = research_lab.PUBLIC_RESEARCH_ONLY_COMMANDS
    for c in ("microstructure-sample-plan-v1024", "microstructure-sample-validate-v1024",
              "microstructure-sample-report-v1024"):
        assert c in cmds
    monkeypatch.setattr(research_lab, "load_config",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no config")))
    monkeypatch.setattr(research_lab, "Database",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no db")))
    _run_main(["microstructure-sample-plan-v1024"])
    assert "MICROSTRUCTURE SAMPLE PLAN V10.24" in capsys.readouterr().out
    _run_main(["microstructure-sample-report-v1024", "--output-dir", "reports/research/v10_24"])
    assert "MICROSTRUCTURE SAMPLE REPORT V10.24" in capsys.readouterr().out


def test_module_no_dangerous_primitives():
    import re
    src = Path(MODULE_PATH).read_text(encoding="utf-8")
    scan = re.sub(r'"never":\s*\[.*?\],', '"never": [],', src, flags=re.DOTALL)
    for tok in ["load_dotenv", "os.environ", "private_get", "private_post",
                "import requests", "import socket", "urllib.request", "db.execute",
                "INSERT INTO", "import torch", "import tensorflow", "import jax"]:
        assert tok not in scan, tok
    for name in ["place_order", "create_order", "set_leverage", "set_margin_mode", "open_position"]:
        assert f"{name}(" not in scan and f".{name}" not in scan, name
    assert "ExecutionEngine(" not in scan and "PaperTrader(" not in scan
