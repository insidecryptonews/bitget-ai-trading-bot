"""ResearchOps V10.23(.1) - Intraday Equity->Crypto Lead-Lag tests.

Covers network safety, staging-only writes, STRICT no-lookahead with separated
feature/label namespaces (predictors cannot read future labels), the corrected
random baseline (IS-calibrated, fixed mask), the OOS embargo, V10.23 CLI
defaults (1h/60), CLI isolation from config/.env/DB, the 60d data limit, and the
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


def _chart_bytes(start_ts, closes):
    return json.dumps({"chart": {"result": [{
        "timestamp": [start_ts + i * INTERVAL for i in range(len(closes))],
        "indicators": {"quote": [{"close": closes}]}}], "error": None}}).encode()


def _mock_transport(url, headers):
    L.assert_safe_request(url, headers)
    if "EMPTYSYM" in url:
        return json.dumps({"chart": {"result": [None], "error": None}}).encode()
    return _chart_bytes(1700000000, [100 + i * 0.1 for i in range(50)])


def _series(start, n, step=0.1):
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


# ---- no-lookahead + namespace separation ---------------------------------

def test_alignment_namespace_and_ordering():
    start = 1700000000
    crypto = {s: _series(start, 80) for s in ("BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD")}
    equity = {s: _series(start, 80) for s in ("NVDA", "QQQ", "SPY", "SMH")}
    a = L.align_no_lookahead(crypto, equity, INTERVAL)
    assert a["no_lookahead_status"] == "OK" and a["n_rows"] > 0
    assert a["namespace_separated"] is True
    for r in a["rows"]:
        assert r["feat_max_ts"] <= r["decision_ts"] < r["label_start_ts"]
        assert all(k.startswith("feature_") for k in r["features"])
        assert all(k.startswith("label_") for k in r["labels"])
        # features must NOT contain any future label column
        assert not any(k.startswith("label_") for k in r["features"])


def test_feature_guard_blocks_label_access():
    fg = L.FeatureGuard({"feature_BTC-USD_past_ret_1h": -0.01})
    assert fg.get("feature_BTC-USD_past_ret_1h") == -0.01
    with pytest.raises(ValueError):
        fg.get("label_BTC-USD_future_ret_4h")
    with pytest.raises(ValueError):
        _ = fg["label_BTC-USD_future_ret_1h"]


def test_adversarial_btc_only_cannot_use_future_label():
    # row where the FUTURE label would be a perfect predictor, but it lives in
    # the labels namespace; the BTC-only predictor only sees features -> blocked.
    row = {"features": {"feature_BTC-USD_past_ret_1h": 0.5},  # positive (no signal)
           "labels": {"label_BTC-USD_future_ret_4h": -0.99}}   # crash, but hidden
    guarded = L._feat(row)
    with pytest.raises(ValueError):
        guarded.get("label_BTC-USD_future_ret_4h")
    # BTC-only(past_ret_1h<0) sees only the (positive) past feature -> predicts False
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
        dd = -0.05 if (i % 7 == 0) else 0.01     # ~14% base rate
        nvda = -0.02 if (i % 5 == 0) else 0.01
        rows.append({"decision_ts": i, "feat_max_ts": i, "label_start_ts": i + 1,
                     "features": {"feature_NVDA_ret_1h": nvda, "feature_QQQ_ret_1h": 0.0,
                                  "feature_SMH_ret_1h": 0.0, "feature_SPY_ret_1h": 0.0,
                                  "feature__idx_VIX_ret_1h": 0.0,
                                  "feature_BTC-USD_past_ret_1h": 0.0, "feature_BTC-USD_past_ret_4h": 0.0},
                     "labels": {"label_BTC-USD_future_ret_4h": dd,
                                "label_ETH-USD_future_ret_4h": dd}})
    return rows


def test_random_fixed_mask_deterministic_and_is_calibrated():
    rows = _rows_with_drawdowns()
    e1 = L.evaluate(rows)
    e2 = L.evaluate(rows)
    r1 = e1["predictors"]["random_fixed_mask(IS-calibrated)"]
    r2 = e2["predictors"]["random_fixed_mask(IS-calibrated)"]
    assert r1 == r2                                   # deterministic across calls
    assert "random_score_freq_is" in e1
    # OOS random frequency must be close to the IS-calibrated frequency (no OOS leak)
    assert abs(r1["OOS"]["freq"] - e1["random_score_freq_is"]) < 0.06


def test_prec_recall_mask_is_pure():
    rows = _rows_with_drawdowns(50)
    mask = [bool(i % 3 == 0) for i in range(len(rows))]
    label = lambda r: L._label_drawdown(r, "BTC-USD", 4, -0.02)
    a = L._prec_recall_mask(rows, mask, label)
    b = L._prec_recall_mask(rows, mask, label)
    assert a == b


def test_embargo_split_reported():
    rows = _rows_with_drawdowns()
    e = L.evaluate(rows, label_h=4)
    assert e["split_type"] == "chronological_70_30_with_embargo"
    assert e["embargo_bars"] == 4 and e["purge_embargo_warning"] is True
    assert e["is_n"] + e["oos_n"] <= len(rows)        # embargo removed middle rows
    assert "oos_events" in e


# ---- study + classification + data limit ---------------------------------

def test_run_study_low_sample_and_data_limit():
    start = 1700000000
    crypto = {s: _series(start, 200) for s in ("BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD")}
    equity = {s: _series(start, 200) for s in ("NVDA", "QQQ", "SPY", "SMH")}
    rep = L.run_study(crypto, equity, INTERVAL, days=60, requested_days=365)
    assert rep["no_lookahead"]["status"] == "OK"
    assert rep["requested_days"] == 365 and rep["effective_days"] <= 75
    assert rep["data_source_limited_60d"] is True
    assert rep["classification"]["low_sample_warning"] is True
    assert rep["research_only"] and rep["final_recommendation"] == "NO LIVE"
    assert rep["can_send_real_orders"] is False


def test_classify_no_lookahead_fail_short_circuits():
    c = L.classify({"predictors": {}}, 100, "NO_LOOKAHEAD_FAIL", 60)
    assert c["verdict"] == L.C_NO_LOOKAHEAD_FAIL and c["final_recommendation"] == "NO LIVE"


# ---- CLI defaults + isolation from config/.env/DB -------------------------

def test_v1023_effective_defaults():
    lab = research_lab.ResearchLab.__new__(research_lab.ResearchLab)
    tfs, eff_days, req = lab._v1023_effective("", 30)     # global defaults -> 1h/60
    assert tfs == ["1h"] and eff_days == 60 and req == 30
    tfs2, eff2, req2 = lab._v1023_effective("15m", 90)    # explicit respected
    assert tfs2 == ["15m"] and eff2 == 90 and req2 == 90


def _run_main(argv):
    old = sys.argv
    sys.argv = ["prog"] + argv
    try:
        research_lab.main()
    finally:
        sys.argv = old


def test_v1023_cli_does_not_load_config_or_db(monkeypatch, capsys):
    def boom_cfg(*a, **k):
        raise AssertionError("load_config must NOT be called for V10.23 commands")

    class BoomDB:
        def __init__(self, *a, **k):
            raise AssertionError("Database must NOT be constructed for V10.23 commands")

    monkeypatch.setattr(research_lab, "load_config", boom_cfg)
    monkeypatch.setattr(research_lab, "Database", BoomDB)
    _run_main(["intraday-leadlag-plan-v1023", "--timeframes", "1h", "--days", "60"])
    out = capsys.readouterr().out
    assert "INTRADAY LEADLAG PLAN V10.23" in out and "NO LIVE" in out


def test_non_allowlisted_command_still_uses_config(monkeypatch):
    sentinel = RuntimeError("config path reached for non-allowlisted command")

    def boom_cfg(*a, **k):
        raise sentinel

    monkeypatch.setattr(research_lab, "load_config", boom_cfg)
    with pytest.raises(RuntimeError):
        _run_main(["security-audit"])


def test_report_cli_no_network(tmp_path):
    lab = research_lab.ResearchLab.__new__(research_lab.ResearchLab)
    out = lab.intraday_leadlag_report_v1023_cli(output_dir=str(tmp_path / "nope"))
    assert "NO LIVE" in out and "NO_SCORECARD_YET" in out


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
