"""ResearchOps V10.23(.1/.2) - Intraday Equity->Crypto Lead-Lag tests.

Covers network safety, staging-only writes, Yahoo open->close timestamp
normalization, STRICT no-lookahead (close-indexed; features use closed bars only,
labels strictly future) with separated feature/label namespaces, the corrected
random baseline, the OOS embargo, V10.23 CLI defaults that distinguish explicit
flags from the global parser default, CLI isolation from config/.env/DB, and the
hard NO-LIVE invariants.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from app import research_lab
from app.labs import intraday_equity_crypto_leadlag_v10_23 as L

MODULE_PATH = "app/labs/intraday_equity_crypto_leadlag_v10_23.py"
INTERVAL = 3600
B = 1700000000


def _chart_bytes(start_ts, closes):
    return json.dumps({"chart": {"result": [{
        "timestamp": [start_ts + i * INTERVAL for i in range(len(closes))],
        "indicators": {"quote": [{"close": closes}]}}], "error": None}}).encode()


def _mock_transport(url, headers):
    L.assert_safe_request(url, headers)
    if "EMPTYSYM" in url:
        return json.dumps({"chart": {"result": [None], "error": None}}).encode()
    return _chart_bytes(B, [100 + i * 0.1 for i in range(50)])


def _series_close_indexed(start, n, step=0.1):
    """Already close-indexed series (what align_no_lookahead expects)."""
    return {start + i * INTERVAL: 100 + i * step for i in range(n)}


# ---- network safety -------------------------------------------------------

def test_allowlist_blocks_bad_host_path_and_auth():
    L.assert_safe_request("https://query1.finance.yahoo.com/v8/finance/chart/NVDA", {})
    for bad in ("https://evil.example.com/v8/finance/chart/NVDA",
                "https://query1.finance.yahoo.com/private/x",
                "http://query1.finance.yahoo.com/v8/finance/chart/NVDA"):
        with pytest.raises(ValueError):
            L.assert_safe_request(bad, {})
    with pytest.raises(ValueError):
        L.assert_safe_request("https://query1.finance.yahoo.com/v8/finance/chart/NVDA",
                              {"Authorization": "Bearer x"})


def test_plan_no_network_no_live():
    p = L.intraday_leadlag_plan(["NVDA"], ["BTC-USD"], ["1h"], 60)
    assert p["writes_network_on_plan"] is False
    assert p["research_only"] and p["paper_ready"] is False and p["live_ready"] is False
    assert p["can_send_real_orders"] is False and p["final_recommendation"] == "NO LIVE"


def test_dry_run_no_writes():
    rep = L.intraday_leadlag_fetch(["NVDA"], ["BTC-USD"], ["1h"], 60, apply=False)
    assert rep["mode"] == "DRY_RUN" and rep["writes"] is False and "staging_dir" not in rep


def test_apply_writes_only_staging_and_reports_missing():
    rep = L.intraday_leadlag_fetch(["NVDA", "EMPTYSYM"], ["BTC-USD"], ["1h"], 60,
                                   apply=True, transport=_mock_transport)
    try:
        assert L.STAGING_MARKER in rep["staging_dir"]
        assert any(d["symbol"] == "NVDA" for d in rep["downloaded"])
        assert any(f["symbol"] == "EMPTYSYM" for f in rep["failed"])
    finally:
        shutil.rmtree(rep["staging_dir"], ignore_errors=True)


def test_safe_staging_rejects_forbidden():
    L.safe_staging_dir(f"external_data/staging/{L.STAGING_MARKER}")
    for bad in ("external_data/raw/x", "external_data/staging/x/../../etc",
                f"vault/{L.STAGING_MARKER}", "external_data/staging/other"):
        with pytest.raises(ValueError):
            L.safe_staging_dir(bad)


# ---- timestamp normalization (V10.23.2 crux) ------------------------------

def test_open_ts_normalized_to_close_ts():
    raw = {B: 100.0, B + INTERVAL: 101.0, B + 2 * INTERVAL: 102.0}   # open-indexed
    norm = L.normalize_to_close_ts(raw, INTERVAL)
    assert set(norm) == {B + INTERVAL, B + 2 * INTERVAL, B + 3 * INTERVAL}
    assert norm[B + INTERVAL] == 100.0   # the bar OPENED at B is only known at B+interval


def test_crypto_close_not_used_before_its_close_ts():
    raw = {B: 100.0, B + INTERVAL: 101.0, B + 2 * INTERVAL: 102.0}
    norm = L.normalize_to_close_ts(raw, INTERVAL)
    # decision at B+INTERVAL: only ONE closed bar -> cannot form a 1h return
    assert L._ret_n_closed(norm, B + INTERVAL, 1) is None
    # decision at B+2*INTERVAL: last usable close is exactly B+2*INTERVAL (just closed)
    r = L._ret_n_closed(norm, B + 2 * INTERVAL, 1)
    assert r is not None and r[1] == B + 2 * INTERVAL and r[1] <= B + 2 * INTERVAL
    # the future close B+3*INTERVAL must never be selected
    assert r[1] != B + 3 * INTERVAL


def test_alignment_closed_features_future_labels_namespace():
    crypto = {s: _series_close_indexed(B, 80) for s in ("BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD")}
    equity = {s: _series_close_indexed(B, 80) for s in ("NVDA", "QQQ", "SPY", "SMH")}
    a = L.align_no_lookahead(crypto, equity, INTERVAL)
    assert a["no_lookahead_status"] == "OK" and a["n_rows"] > 0
    assert a["namespace_separated"] and a["features_use_closed_bars_only"] and a["labels_strictly_future"]
    for r in a["rows"]:
        assert r["feat_max_ts"] <= r["decision_ts"] < r["label_start_ts"]
        assert r["min_label_close_ts"] > r["decision_ts"]
        assert all(k.startswith("feature_") for k in r["features"])
        assert all(k.startswith("label_") for k in r["labels"])
        assert not any(k.startswith("label_") for k in r["features"])


def test_run_study_reports_timestamp_semantics():
    crypto = {s: {B + i * INTERVAL: 100 + i * 0.1 for i in range(200)}
              for s in ("BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD")}
    equity = {s: {B + i * INTERVAL: 100 + i * 0.1 for i in range(200)}
              for s in ("NVDA", "QQQ", "SPY", "SMH")}   # open-indexed -> run_study normalizes
    rep = L.run_study(crypto, equity, INTERVAL, days=60, requested_days=365)
    assert rep["timestamp_semantics"] == "yahoo_open_ts_normalized_to_close_ts"
    assert rep["raw_ts_semantics"] == "open_time" and rep["analysis_ts_semantics"] == "close_time"
    assert rep["no_lookahead"]["features_use_closed_bars_only"] is True
    assert rep["no_lookahead"]["labels_strictly_future"] is True
    assert rep["requested_days"] == 365 and rep["data_source_limited_60d"] is True
    assert rep["classification"]["low_sample_warning"] is True
    assert rep["final_recommendation"] == "NO LIVE" and rep["can_send_real_orders"] is False


# ---- namespace guard + labels --------------------------------------------

def test_feature_guard_blocks_label_access():
    fg = L.FeatureGuard({"feature_BTC-USD_past_ret_1h": -0.01})
    assert fg.get("feature_BTC-USD_past_ret_1h") == -0.01
    with pytest.raises(ValueError):
        fg.get("label_BTC-USD_future_ret_4h")
    with pytest.raises(ValueError):
        _ = fg["label_BTC-USD_future_ret_1h"]


def test_adversarial_btc_only_cannot_use_future_label():
    row = {"features": {"feature_BTC-USD_past_ret_1h": 0.5},
           "labels": {"label_BTC-USD_future_ret_4h": -0.99}}
    guarded = L._feat(row)
    with pytest.raises(ValueError):
        guarded.get("label_BTC-USD_future_ret_4h")
    assert (guarded.get("feature_BTC-USD_past_ret_1h", 0.0) < 0) is False


def test_label_drawdown_reads_label_namespace():
    row = {"features": {"feature_BTC-USD_past_ret_4h": 0.0},
           "labels": {"label_BTC-USD_future_ret_4h": -0.03}}
    assert L._label_drawdown(row, "BTC-USD", 4, -0.02) is True
    assert L._label_drawdown(row, "BTC-USD", 4, -0.05) is False


# ---- random baseline + embargo -------------------------------------------

def _rows_with_drawdowns(n=300):
    rows = []
    for i in range(n):
        dd = -0.05 if (i % 7 == 0) else 0.01
        nvda = -0.02 if (i % 5 == 0) else 0.01
        rows.append({"decision_ts": i, "feat_max_ts": i, "label_start_ts": i + 1,
                     "min_label_close_ts": i + 1,
                     "features": {"feature_NVDA_ret_1h": nvda, "feature_QQQ_ret_1h": 0.0,
                                  "feature_SMH_ret_1h": 0.0, "feature_SPY_ret_1h": 0.0,
                                  "feature__idx_VIX_ret_1h": 0.0,
                                  "feature_BTC-USD_past_ret_1h": 0.0, "feature_BTC-USD_past_ret_4h": 0.0},
                     "labels": {"label_BTC-USD_future_ret_4h": dd, "label_ETH-USD_future_ret_4h": dd}})
    return rows


def test_random_fixed_mask_deterministic_and_is_calibrated():
    rows = _rows_with_drawdowns()
    e1 = L.evaluate(rows)
    e2 = L.evaluate(rows)
    r1 = e1["predictors"]["random_fixed_mask(IS-calibrated)"]
    assert r1 == e2["predictors"]["random_fixed_mask(IS-calibrated)"]
    assert "random_score_freq_is" in e1
    assert abs(r1["OOS"]["freq"] - e1["random_score_freq_is"]) < 0.06


def test_embargo_split_reported():
    e = L.evaluate(_rows_with_drawdowns(), label_h=4)
    assert e["split_type"] == "chronological_70_30_with_embargo"
    assert e["embargo_bars"] == 4 and e["purge_embargo_warning"] is True
    assert "oos_events" in e


def test_classify_no_lookahead_fail_short_circuits():
    c = L.classify({"predictors": {}}, 100, "NO_LOOKAHEAD_FAIL", 60)
    assert c["verdict"] == L.C_NO_LOOKAHEAD_FAIL and c["final_recommendation"] == "NO LIVE"


def test_classify_rejects_high_lift_but_too_few_flags():
    # score has a big lift but only 6 OOS alerts -> noise, must be REJECTED
    evald = {"predictors": {
        "risk_off_score>=thr": {"OOS": {"precision": 0.1667, "lift": 2.8, "flags": 6}},
        "BTC-only(past_ret_1h<0)": {"OOS": {"precision": 0.05, "lift": 0.8, "flags": 100}},
        "BTC-only(past_ret_4h<0)": {"OOS": {"precision": 0.08, "lift": 1.4, "flags": 95}},
        "random_fixed_mask(IS-calibrated)": {"OOS": {"precision": 0.143, "lift": 2.4, "flags": 7}}}}
    c = L.classify(evald, 600, "OK", 60)
    assert c["verdict"] == L.C_REJECTED and c["beats_random"] is False
    assert c["score_oos_flags"] == 6


def test_classify_weak_when_enough_flags_and_beats_random():
    evald = {"predictors": {
        "risk_off_score>=thr": {"OOS": {"precision": 0.20, "lift": 1.6, "flags": 40}},
        "BTC-only(past_ret_1h<0)": {"OOS": {"precision": 0.10, "lift": 0.9, "flags": 100}},
        "BTC-only(past_ret_4h<0)": {"OOS": {"precision": 0.11, "lift": 1.0, "flags": 95}},
        "random_fixed_mask(IS-calibrated)": {"OOS": {"precision": 0.08, "lift": 0.7, "flags": 40}}}}
    c = L.classify(evald, 600, "OK", 60)
    assert c["verdict"] in (L.C_CANDIDATE, L.C_WEAK) and c["beats_random"] and c["beats_btc_only"]


# ---- CLI defaults: explicit vs global default -----------------------------

def test_v1023_effective_explicit_vs_default():
    eff = research_lab.ResearchLab._v1023_effective
    assert eff("", 30, days_explicit=False, tf_explicit=False) == (["1h"], 60)   # no flags -> 1h/60
    assert eff("", 30, days_explicit=True, tf_explicit=False) == (["1h"], 30)    # explicit --days 30 respected
    assert eff("15m", 90, days_explicit=True, tf_explicit=True) == (["15m"], 90) # explicit tf + days
    assert eff("5m", 30, days_explicit=False, tf_explicit=True) == (["1h"], 60)  # junk tf -> 1h


# ---- allowlist integrity + early dispatch isolation -----------------------

def test_allowlist_is_clean_and_handled():
    cmds = research_lab.PUBLIC_RESEARCH_ONLY_COMMANDS
    assert "intraday-leadlag-build-v1023" not in cmds   # removed: no handler/parser entry
    parser = research_lab.build_argument_parser()
    choices = None
    for act in parser._actions:
        if getattr(act, "dest", None) == "command":
            choices = set(act.choices or [])
            break
    assert choices is not None
    for c in cmds:
        assert c in choices, c   # every allowlisted command is a real parser command


def _run_main(argv):
    old = sys.argv
    sys.argv = ["prog"] + argv
    try:
        research_lab.main()
    finally:
        sys.argv = old


def _boom_cfg(*a, **k):
    raise AssertionError("load_config must NOT be called for V10.23 commands")


class _BoomDB:
    def __init__(self, *a, **k):
        raise AssertionError("Database must NOT be constructed for V10.23 commands")


def test_early_dispatch_plan_no_config_db(monkeypatch, capsys):
    monkeypatch.setattr(research_lab, "load_config", _boom_cfg)
    monkeypatch.setattr(research_lab, "Database", _BoomDB)
    _run_main(["intraday-leadlag-plan-v1023", "--timeframes", "1h", "--days", "60"])
    assert "INTRADAY LEADLAG PLAN V10.23" in capsys.readouterr().out


def test_early_dispatch_fetch_dryrun_no_config_db(monkeypatch, capsys):
    monkeypatch.setattr(research_lab, "load_config", _boom_cfg)
    monkeypatch.setattr(research_lab, "Database", _BoomDB)
    _run_main(["intraday-leadlag-fetch-v1023", "--timeframes", "1h", "--days", "60"])
    out = capsys.readouterr().out
    assert "INTRADAY LEADLAG FETCH V10.23" in out and "DRY_RUN" in out


def test_early_dispatch_study_sample_no_config_db_no_network(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(research_lab, "load_config", _boom_cfg)
    monkeypatch.setattr(research_lab, "Database", _BoomDB)
    # any network call would fail the contract -> blow up if attempted
    monkeypatch.setattr(L, "default_transport",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network")))
    sample = tmp_path / "s"
    os.makedirs(sample)
    for sym in ("BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD", "NVDA"):
        rows = ["ts,close"] + [f"{B + i*INTERVAL},{100 + i*0.1}" for i in range(80)]
        Path(sample, f"{sym}_1h.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    _run_main(["intraday-leadlag-study-v1023", "--sample-dir", str(sample),
               "--equities", "NVDA", "--cryptos", "BTC-USD,ETH-USD,SOL-USD,XRP-USD,DOGE-USD",
               "--timeframe", "1h", "--output-dir", str(tmp_path / "out")])
    out = capsys.readouterr().out
    assert "INTRADAY LEADLAG STUDY V10.23" in out and "NO LIVE" in out
    assert "yahoo_open_ts_normalized_to_close_ts" in out


def test_early_dispatch_report_no_network_no_config_db(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(research_lab, "load_config", _boom_cfg)
    monkeypatch.setattr(research_lab, "Database", _BoomDB)
    monkeypatch.setattr(L, "default_transport",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network")))
    _run_main(["intraday-leadlag-report-v1023", "--output-dir", str(tmp_path / "none")])
    out = capsys.readouterr().out
    assert "INTRADAY LEADLAG REPORT V10.23" in out and "NO LIVE" in out


def test_non_allowlisted_command_still_uses_config(monkeypatch):
    monkeypatch.setattr(research_lab, "load_config",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("config path reached")))
    with pytest.raises(RuntimeError):
        _run_main(["security-audit"])


def test_module_no_dangerous_primitives():
    import re
    src = Path(MODULE_PATH).read_text(encoding="utf-8")
    scan = re.sub(r'"never":\s*\[.*?\],', '"never": [],', src, flags=re.DOTALL)
    for tok in ["load_dotenv", "os.environ", "private_get", "private_post",
                "db.execute", "INSERT INTO", "import torch", "import tensorflow", "import jax"]:
        assert tok not in scan, tok
    for name in ["place_order", "create_order", "set_leverage", "set_margin_mode", "open_position"]:
        assert f"{name}(" not in scan and f".{name}" not in scan, name
    assert "ExecutionEngine(" not in scan and "PaperTrader(" not in scan
