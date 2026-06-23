"""ResearchOps V10.23 - Intraday Equity->Crypto Lead-Lag tests.

Verifies network safety (allowlist, no auth/private), staging-only writes, the
strict no-lookahead alignment, label/feature time-ordering, low-sample handling,
missing-symbol reporting, and the hard NO-LIVE invariants.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from app.labs import intraday_equity_crypto_leadlag_v10_23 as L

MODULE_PATH = "app/labs/intraday_equity_crypto_leadlag_v10_23.py"
INTERVAL = 3600


def _chart_bytes(start_ts, closes):
    return json.dumps({"chart": {"result": [{
        "timestamp": [start_ts + i * INTERVAL for i in range(len(closes))],
        "indicators": {"quote": [{"close": closes}]}}], "error": None}}).encode()


def _mock_transport(url, headers):
    L.assert_safe_request(url, headers)  # enforce allowlist even in tests
    if "EMPTYSYM" in url:
        return json.dumps({"chart": {"result": [None], "error": None}}).encode()
    return _chart_bytes(1700000000, [100 + i * 0.1 for i in range(50)])


# ---- network safety -------------------------------------------------------

def test_allowlist_blocks_bad_host_path_and_auth():
    L.assert_safe_request("https://query1.finance.yahoo.com/v8/finance/chart/NVDA", {})
    with pytest.raises(ValueError):
        L.assert_safe_request("https://evil.example.com/v8/finance/chart/NVDA", {})
    with pytest.raises(ValueError):
        L.assert_safe_request("https://query1.finance.yahoo.com/private/x", {})
    with pytest.raises(ValueError):
        L.assert_safe_request("https://query1.finance.yahoo.com/v8/finance/chart/NVDA",
                              {"Authorization": "Bearer x"})
    with pytest.raises(ValueError):
        L.assert_safe_request("http://query1.finance.yahoo.com/v8/finance/chart/NVDA", {})


def test_plan_does_no_network_and_no_live():
    p = L.intraday_leadlag_plan(["NVDA"], ["BTC-USD"], ["1h"], 60)
    assert p["writes_network_on_plan"] is False
    assert p["research_only"] and p["paper_ready"] is False and p["live_ready"] is False
    assert p["can_send_real_orders"] is False and p["final_recommendation"] == "NO LIVE"


def test_dry_run_writes_nothing():
    rep = L.intraday_leadlag_fetch(["NVDA"], ["BTC-USD"], ["1h"], 60, apply=False)
    assert rep["mode"] == "DRY_RUN" and rep["writes"] is False
    assert "staging_dir" not in rep


def test_apply_writes_only_safe_staging_and_reports_missing(tmp_path):
    rep = L.intraday_leadlag_fetch(["NVDA", "EMPTYSYM"], ["BTC-USD"], ["1h"], 60,
                                   apply=True, transport=_mock_transport)
    try:
        assert rep["mode"] == "APPLY"
        assert L.STAGING_MARKER in rep["staging_dir"]
        assert any(d["symbol"] == "NVDA" for d in rep["downloaded"])
        assert any(f["symbol"] == "EMPTYSYM" for f in rep["failed"])  # missing/empty reported
        assert os.path.isdir(rep["staging_dir"])
    finally:
        shutil.rmtree(rep["staging_dir"], ignore_errors=True)


def test_safe_staging_rejects_forbidden():
    L.safe_staging_dir(f"external_data/staging/{L.STAGING_MARKER}")
    for bad in ("external_data/raw/x", "external_data/staging/x/../../etc",
                f"vault/{L.STAGING_MARKER}", "external_data/staging/other"):
        with pytest.raises(ValueError):
            L.safe_staging_dir(bad)


# ---- no-lookahead alignment ----------------------------------------------

def _series(start, n, step=0.1):
    return {start + i * INTERVAL: 100 + i * step for i in range(n)}


def test_no_lookahead_alignment_ordering():
    start = 1700000000
    crypto = {s: _series(start, 60) for s in ("BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD")}
    equity = {s: _series(start, 60) for s in ("NVDA", "QQQ", "SPY", "SMH")}
    a = L.align_no_lookahead(crypto, equity, INTERVAL)
    assert a["no_lookahead_status"] == "OK" and a["n_rows"] > 0
    for r in a["rows"]:
        assert r["feat_max_ts"] <= r["decision_ts"] < r["label_start_ts"]
    assert a["features_max_ts_le_decision"] is True


def test_equity_bar_not_used_before_close():
    start = 1700000000
    ser = _series(start, 10)
    decision = start + 3 * INTERVAL
    # the bar opening exactly at decision_ts closes at decision+interval -> not usable
    r = L._ret_n_closed(ser, decision, INTERVAL, 1)
    assert r is not None
    last_close_ts = r[1]
    assert last_close_ts <= decision  # used bar already closed by decision_ts


def test_labels_use_future_features_use_past():
    start = 1700000000
    crypto = {s: _series(start, 60) for s in ("BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD")}
    equity = {"NVDA": _series(start, 60), "QQQ": _series(start, 60)}
    a = L.align_no_lookahead(crypto, equity, INTERVAL)
    row = a["rows"][0]
    assert row["label_start_ts"] > row["decision_ts"]   # labels strictly future
    assert row["feat_max_ts"] <= row["decision_ts"]      # features only past/closed


# ---- study + classification ----------------------------------------------

def test_run_study_and_low_sample_warning():
    start = 1700000000
    crypto = {s: _series(start, 200) for s in ("BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD")}
    equity = {s: _series(start, 200) for s in ("NVDA", "QQQ", "SPY", "SMH")}
    rep = L.run_study(crypto, equity, INTERVAL, days=60)
    assert rep["no_lookahead"]["status"] == "OK"
    assert rep["classification"]["low_sample_warning"] is True
    assert rep["research_only"] and rep["final_recommendation"] == "NO LIVE"
    assert rep["paper_ready"] is False and rep["can_send_real_orders"] is False


def test_classify_no_lookahead_fail_short_circuits():
    c = L.classify({"predictors": {}}, 100, "NO_LOOKAHEAD_FAIL", 60)
    assert c["verdict"] == L.C_NO_LOOKAHEAD_FAIL
    assert c["final_recommendation"] == "NO LIVE"


def test_module_no_dangerous_primitives():
    import re
    src = Path(MODULE_PATH).read_text(encoding="utf-8")
    scan = re.sub(r'"never":\s*\[.*?\],', '"never": [],', src, flags=re.DOTALL)
    for tok in ["load_dotenv", "os.environ", "private_get", "private_post",
                "db.execute", "INSERT INTO", "import torch", "import tensorflow"]:
        assert tok not in scan, tok
    for name in ["place_order", "create_order", "set_leverage", "set_margin_mode", "open_position"]:
        assert f"{name}(" not in scan and f".{name}" not in scan, name
    assert "ExecutionEngine(" not in scan and "PaperTrader(" not in scan
