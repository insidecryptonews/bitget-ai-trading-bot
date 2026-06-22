"""ResearchOps V10.9 — coverage-aware readiness + multi-window validation tests.

Pure/offline. No real network (history-limits uses a mock transport). Verifies
honest coverage reporting (undercoverage never reads as clean), per-timeframe
undercoverage that a global ratio would hide, requested-days status on samples,
public-GET-only history probing, multi-window candidate degradation, and the
hard invariant that NOTHING is ever approved for paper/live.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.labs import coverage_readiness_v10_9 as C
from app.labs import bitget_public_data_v10_7 as pub

MODULE_PATH = "app/labs/coverage_readiness_v10_9.py"
DAY = 86_400_000
TF_MS = {"4h": 240 * 60_000, "6h": 360 * 60_000, "1h": 60 * 60_000}


def _now_ms():
    return C._now_ms()


def _candle_rows(tf, days, end_ts):
    bar = TF_MS[tf]
    start = end_ts - days * DAY
    out = []
    t = start
    p = 100.0
    while t <= end_ts:
        out.append((t, p, p * 1.01, p * 0.99, p * 1.002))
        p *= 1.002
        t += bar
    return out


def _write_staging_candles(staging_dir, symbol, tf, days, end_ts=None):
    end_ts = end_ts or _now_ms()
    d = Path(staging_dir) / "candles" / symbol
    d.mkdir(parents=True, exist_ok=True)
    rows = _candle_rows(tf, days, end_ts)
    hdr = "timestamp_ms,symbol,product_type,timeframe,open,high,low,close,volume_base,volume_quote,source,fetched_at"
    lines = [hdr] + [f"{ts},{symbol},usdt-futures,{tf},{o},{h},{l},{c},10,1000,bitget_public,x"
                     for (ts, o, h, l, c) in rows]
    (d / f"{tf}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_run_report(staging_dir, requested_days, symbols, timeframes,
                      data_types=("candles",)):
    rr = {"requested_days": requested_days, "symbols": list(symbols),
          "timeframes": list(timeframes), "data_types": list(data_types),
          "errors": [], "warnings": []}
    Path(staging_dir, "run_report.json").write_text(json.dumps(rr), encoding="utf-8")


def _write_sample_ohlcv(sample_dir, symbol, tf, days, end_ts=None):
    end_ts = end_ts or _now_ms()
    Path(sample_dir).mkdir(parents=True, exist_ok=True)
    rows = _candle_rows(tf, days, end_ts)
    lines = ["timestamp,open,high,low,close,volume"] + \
            [f"{ts},{o},{h},{l},{c},10" for (ts, o, h, l, c) in rows]
    Path(sample_dir, f"{symbol}_{tf}_ohlcv.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    Path(sample_dir, f"{symbol}_funding.csv").write_text(
        "timestamp,funding_rate\n" + "\n".join(f"{ts},0.0001" for (ts, *_) in rows) + "\n",
        encoding="utf-8")


# --------------------------------------------------------------------------
# A. Coverage audit
# --------------------------------------------------------------------------

def test_audit_warns_when_requested_days_undercovered(tmp_path):
    end = _now_ms()
    _write_staging_candles(tmp_path, "BTCUSDT", "4h", 200, end)
    _write_staging_candles(tmp_path, "BTCUSDT", "6h", 285, end)
    _write_run_report(tmp_path, 365, ["BTCUSDT"], ["4h", "6h"])
    r = C.coverage_audit(str(tmp_path), expected_days=365)
    assert r["requested_days"] == 365
    assert r["requested_days_undercovered"] is True
    assert r["audit_status"] != "STAGING_OK"
    assert "requested_days_undercovered" in r["warnings"]
    assert r["coverage_status"] in ("PARTIAL_COVERAGE", "INSUFFICIENT_COVERAGE")
    assert r["final_recommendation"] == "NO LIVE"


def test_audit_ok_only_when_coverage_full(tmp_path):
    end = _now_ms()
    _write_staging_candles(tmp_path, "BTCUSDT", "4h", 180, end)
    _write_staging_candles(tmp_path, "BTCUSDT", "6h", 180, end)
    _write_run_report(tmp_path, 180, ["BTCUSDT"], ["4h", "6h"])
    r = C.coverage_audit(str(tmp_path), expected_days=180)
    assert r["coverage_status"] == "FULL_COVERAGE"
    assert r["audit_status"] == "STAGING_OK"
    assert r["requested_days_undercovered"] is False


def test_audit_computes_per_timeframe_coverage(tmp_path):
    end = _now_ms()
    _write_staging_candles(tmp_path, "BTCUSDT", "4h", 150, end)
    _write_staging_candles(tmp_path, "BTCUSDT", "6h", 150, end)
    _write_run_report(tmp_path, 150, ["BTCUSDT"], ["4h", "6h"])
    r = C.coverage_audit(str(tmp_path), expected_days=150)
    assert set(r["timeframe_coverage_summary"].keys()) == {"4h", "6h"}
    assert "min_ratio" in r["timeframe_coverage_summary"]["4h"]


def test_audit_catches_4h_undercoverage_hidden_by_6h_global(tmp_path):
    # 6H spans the full window (drives global), 4H only ~2/3 -> still flagged.
    end = _now_ms()
    _write_staging_candles(tmp_path, "BTCUSDT", "4h", 200, end)
    _write_staging_candles(tmp_path, "BTCUSDT", "6h", 300, end)
    _write_run_report(tmp_path, 300, ["BTCUSDT"], ["4h", "6h"])
    r = C.coverage_audit(str(tmp_path), expected_days=300)
    assert "4h" in r["undercovered_timeframes"]
    assert any(w.startswith("timeframe_coverage_below_expected:4h") for w in r["warnings"])
    assert r["audit_status"] != "STAGING_OK"   # per-tf gap is not hidden


def test_audit_missing_run_report_warns(tmp_path):
    _write_staging_candles(tmp_path, "BTCUSDT", "6h", 180, _now_ms())
    r = C.coverage_audit(str(tmp_path), expected_days=180)
    assert "run_report_missing_expected_data_unverifiable" in r["warnings"]


def test_audit_refuses_raw_dir(tmp_path):
    raw = tmp_path / "external_data" / "raw"
    (raw / "candles" / "BTCUSDT").mkdir(parents=True)
    r = C.coverage_audit(str(raw), expected_days=180)
    assert r["audit_status"] == "STAGING_BLOCKED"
    assert "refuses_raw_directory" in r["blockers"]


# --------------------------------------------------------------------------
# B. Sample coverage
# --------------------------------------------------------------------------

def test_sample_coverage_reports_partial_requested_days(tmp_path):
    _write_sample_ohlcv(tmp_path, "BTCUSDT", "4h", 180)
    _write_sample_ohlcv(tmp_path, "BTCUSDT", "6h", 180)
    r = C.sample_coverage(str(tmp_path), expected_days=365)
    assert r["requested_days_status"] in ("PARTIAL_REQUESTED_DAYS", "FAILS_REQUESTED_DAYS")
    assert "requested_days_undercovered" in r["human_warnings"]
    assert r["provider_verified"] is False
    assert r["final_recommendation"] == "NO LIVE"


def test_sample_not_ready_when_oi_liquidations_missing(tmp_path):
    _write_sample_ohlcv(tmp_path, "BTCUSDT", "4h", 200)
    r = C.sample_coverage(str(tmp_path), expected_days=180)
    assert r["sample_ready"] is False
    assert any("ohlcv" not in t for t in r["required_types_missing"]) or r["required_types_missing"]
    assert "open_interest" in r["required_types_missing"]
    assert "liquidations" in r["required_types_missing"]


# --------------------------------------------------------------------------
# E. Data readiness sample
# --------------------------------------------------------------------------

def test_data_readiness_sample_reports_clean_days_but_not_verified(tmp_path):
    _write_sample_ohlcv(tmp_path, "BTCUSDT", "4h", 180)
    r = C.data_readiness_sample(str(tmp_path), expected_days=365)
    assert r["clean_days"] > 0
    assert r["provider_verified"] is False
    assert r["manifest_promotable"] is False
    assert r["backtester_readiness"] != "READY_FOR_REPLAY_RESEARCH"
    assert r["paper_ready"] is False and r["live_ready"] is False


# --------------------------------------------------------------------------
# C. History limits probe (public GET only)
# --------------------------------------------------------------------------

def test_history_limits_dry_run_writes_no_files(tmp_path):
    r = C.history_limits_probe(symbols=["BTCUSDT"], timeframes=["4h", "6h"],
                               apply=False, output_dir=str(tmp_path))
    assert r["dry_run"] is True
    assert r["written_path"] == ""
    assert list(tmp_path.iterdir()) == []
    assert r["public_get_only"] is True


def test_history_limits_apply_writes_only_report(tmp_path):
    calls = {"paths": set()}

    def fake_transport(path, params, *, timeout=10.0):
        calls["paths"].add(path)
        # one page then empty -> bounded walk terminates
        if params.get("endTime", 0) > _now_ms() - DAY:
            st = int(params["endTime"]) - 200 * TF_MS["4h"]
            bar = TF_MS["4h"]
            return {"code": "00000", "data": [[t, "1", "2", "1", "1.5", "1", "1"]
                                              for t in range(st, int(params["endTime"]), bar)]}
        return {"code": "00000", "data": []}

    r = C.history_limits_probe(symbols=["BTCUSDT"], timeframes=["4h"], apply=True,
                               transport=fake_transport, output_dir=str(tmp_path),
                               requested_days=[365])
    assert r["dry_run"] is False
    assert calls["paths"] <= {pub.EP_CANDLES}        # ONLY the public candles path
    assert r["written_path"].endswith("history_limits.json")
    assert os.path.isfile(r["written_path"])
    assert r["probes"] and r["probes"][0]["rows_returned"] > 0


def test_history_limits_only_uses_public_allowlist():
    r = C.history_limits_probe(symbols=["BTCUSDT"], timeframes=["4h"], apply=False)
    assert set(r["allowed_paths"]) <= set(pub.ALLOWED_PATHS)
    assert r["no_private_auth"] is True and r["no_env"] is True


# --------------------------------------------------------------------------
# D. Multi-window validation
# --------------------------------------------------------------------------

def _multiwindow_fixture(sample_dir, symbols, tf="6h", days=200):
    for s in symbols:
        _write_sample_ohlcv(sample_dir, s, tf, days)


def test_multi_window_rejects_single_window_candidate(tmp_path):
    sample = tmp_path / "s"
    _multiwindow_fixture(sample, ["BTCUSDT", "ETHUSDT", "SOLUSDT"], "6h", 200)
    rep = C.multi_window_validation(
        sample_dir=str(sample), windows=[60, 120], symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        timeframes=["6h"], sides=["LONG", "SHORT"],
        entry_families=["breakout_momentum", "volatility_expansion"],
        exit_policies=["atr_trailing", "fixed_tp_sl_time"], min_trades=10,
        walk_forward_mode="rolling", gap_policy="adverse_open", max_grid_combos=100)
    # a candidate that only survives ONE window can never be RESEARCH_CANDIDATE_ONLY
    for a in rep["multi_window_candidates"]:
        if a["windows_passed"] < 2:
            assert a["final_tier"] != C.lab.CAND_RESEARCH_ONLY
    assert rep["final_recommendation"] == "NO LIVE"
    assert rep["edge_validated"] is False


def test_multi_window_report_says_hypotheses_not_signals(tmp_path):
    sample = tmp_path / "s"
    _multiwindow_fixture(sample, ["BTCUSDT", "ETHUSDT"], "6h", 160)
    rep = C.multi_window_validation(
        sample_dir=str(sample), windows=[60, 120], symbols=["BTCUSDT", "ETHUSDT"],
        timeframes=["6h"], sides=["LONG", "SHORT"],
        entry_families=["breakout_momentum"], exit_policies=["atr_trailing"],
        min_trades=10, max_grid_combos=50)
    run_dir = C.write_multi_window_reports(rep, output_dir=str(tmp_path / "out"))
    md = Path(run_dir, "report.md").read_text(encoding="utf-8").lower()
    assert "not signals" in md and "no live" in md
    assert os.path.isfile(os.path.join(run_dir, "multi_window_summary.json"))


def test_multi_window_intermediate_never_research_candidate_only(tmp_path):
    sample = tmp_path / "s"
    _multiwindow_fixture(sample, ["BTCUSDT", "ETHUSDT", "SOLUSDT"], "6h", 200)
    rep = C.multi_window_validation(
        sample_dir=str(sample), windows=[60, 120, 180], symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        timeframes=["6h"], sides=["LONG", "SHORT"],
        entry_families=["breakout_momentum", "volatility_expansion"],
        exit_policies=["atr_trailing", "fixed_tp_sl_time", "time_death_exit"],
        min_trades=10, max_grid_combos=120,
        data_classification=C.lab.CLS_INTERMEDIATE)
    tiers = {a["final_tier"] for a in rep["multi_window_candidates"]}
    assert C.lab.CAND_RESEARCH_ONLY not in tiers   # INTERMEDIATE caps at WEAK
    assert tiers <= {C.lab.CAND_REJECTED, C.lab.CAND_WEAK}


# --------------------------------------------------------------------------
# F. Provider gap plan
# --------------------------------------------------------------------------

def test_provider_gap_plan_no_paid_download_before_sample():
    r = C.provider_gap_plan()
    assert r["no_paid_download_before_sample_validation"] is True
    assert r["missing_oi_historical"] is True and r["missing_liquidations"] is True
    assert r["final_recommendation"] == "NO LIVE"


def test_provider_gap_plan_writes_docs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    md_path, json_path = C.write_provider_gap_plan(C.provider_gap_plan())
    md = Path(md_path).read_text(encoding="utf-8").lower()
    assert "no paid download before" in md
    assert "no live" in md
    assert os.path.isfile(json_path)


# --------------------------------------------------------------------------
# Safety — static source scan + no-approval invariants
# --------------------------------------------------------------------------

def test_module_has_no_dangerous_primitives():
    src = Path(MODULE_PATH).read_text(encoding="utf-8")
    for token in ["place_order", "create_order", "private_get", "private_post",
                  "set_leverage", "set_margin_mode", "ExecutionEngine",
                  "PaperTrader", "import torch", "from torch", "import jax",
                  "import tensorflow", "import timesfm", "load_dotenv",
                  "os.environ", "import httpx", "import requests"]:
        assert token not in src, f"{MODULE_PATH} must not contain {token!r}"


def test_no_approved_for_paper_or_live_anywhere(tmp_path):
    sample = tmp_path / "s"
    _multiwindow_fixture(sample, ["BTCUSDT", "ETHUSDT"], "6h", 160)
    rep = C.multi_window_validation(
        sample_dir=str(sample), windows=[60, 120], symbols=["BTCUSDT", "ETHUSDT"],
        timeframes=["6h"], sides=["LONG", "SHORT"],
        entry_families=["breakout_momentum"], exit_policies=["atr_trailing"],
        min_trades=10, max_grid_combos=50)
    blob = json.dumps(rep, default=str)
    assert "APPROVED_FOR_PAPER" not in blob and "APPROVED_FOR_LIVE" not in blob
    for r in (C.provider_gap_plan(), C.coverage_audit(str(sample), 90)):
        b = json.dumps(r, default=str)
        assert "APPROVED_FOR_PAPER" not in b and "APPROVED_FOR_LIVE" not in b


def test_outputs_never_flip_paper_live():
    plans = [C.provider_gap_plan()]
    for p in plans:
        assert p["paper_ready"] is False and p["live_ready"] is False
        assert p["can_send_real_orders"] is False
