"""ResearchOps V10.21 - Forward-Shadow Regime Overlay tests.

Pure/offline/deterministic. The overlay is READ-ONLY: it classifies the current
regime and a research-only action context, makes NO trades, predicts nothing,
and never flips paper/live. Tests verify the classifier, the path-safe journal,
and the hard safety invariants.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from app.labs import forward_shadow_regime_v10_21 as R

MODULE_PATH = "app/labs/forward_shadow_regime_v10_21.py"


def _bars(closes):
    out = []
    t = 1700000000000
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        hi = max(o, c) * 1.01
        lo = min(o, c) * 0.99
        out.append({"ts": t + i * R.DAY_MS, "open": o, "high": hi, "low": lo, "close": c})
    return out


def test_downtrend_is_risk_off():
    closes = [100 * (0.985 ** i) for i in range(80)]   # steady decline
    r = R.classify_symbol(_bars(closes), symbol="BTCUSDT")
    assert r["regime"] in ("DOWNTREND", "DOWNTREND_RISKOFF")
    assert r["verdict"] in (R.R_LONG_BLOCKED, R.R_RISK_OFF)
    assert r["above_sma20"] is False


def test_uptrend_is_risk_on():
    closes = [100 * (1.015 ** i) for i in range(80)]   # steady rise
    r = R.classify_symbol(_bars(closes), symbol="BTCUSDT")
    assert r["regime"] == "UPTREND" and r["verdict"] == R.R_RISK_ON


def test_flat_is_not_risk_off():
    closes = [100 + (0.05 if i % 2 else -0.05) for i in range(80)]  # tiny chop
    r = R.classify_symbol(_bars(closes), symbol="BTCUSDT")
    assert r["verdict"] in (R.R_RANGE, R.R_NO_EDGE)   # never risk-off/short on flat
    assert r["verdict"] not in (R.R_RISK_OFF, R.R_SHORT_BIAS)


def test_insufficient_data():
    r = R.classify_symbol(_bars([100] * 10), symbol="BTCUSDT")
    assert r["regime"] == "INSUFFICIENT_DATA" and r["verdict"] == R.R_NO_EDGE


def test_basket_risk_off_when_most_down():
    per = []
    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"):
        per.append(R.classify_symbol(_bars([100 * (0.985 ** i) for i in range(80)]), symbol=s))
    b = R.classify_basket(per)
    assert b["basket_verdict"] in (R.R_RISK_OFF, R.R_NO_EDGE)
    assert b["n_downtrend"] >= 3


def test_run_and_journal_path_safe(tmp_path):
    s = tmp_path / "s"
    os.makedirs(s)
    closes = [100 * (0.985 ** i) for i in range(80)]
    lines = ["timestamp,open,high,low,close,volume"] + \
            [f"{1700000000000 + i*R.DAY_MS},{c},{c*1.01},{c*0.99},{c},10" for i, c in enumerate(closes)]
    Path(s, "BTCUSDT_1d_ohlcv.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    rep = R.run_regime(str(s), ["BTCUSDT"], timeframe="1d")
    assert rep["per_symbol"] and rep["makes_no_trades"] is True
    # unsafe output dir is redirected to the canonical journal root
    j = R.write_journal(rep, output_dir="external_data/raw/evil")
    try:
        assert j.startswith("reports/research/v10_21")
        assert os.path.isfile(j)
    finally:
        # cleanup only the snapshot we just made (leave any real journal alone)
        try:
            os.remove(j)
        except OSError:
            pass


def test_safety_flags_everywhere():
    p = R.forward_shadow_regime_plan()
    rep = R.run_regime("does/not/exist", ["BTCUSDT"])
    for out in (p, rep):
        assert out["research_only"] and out["shadow_only"]
        assert out["paper_ready"] is False and out["live_ready"] is False
        assert out["can_send_real_orders"] is False
        assert out["makes_no_trades"] is True and out["edge_validated"] is False
        assert out["final_recommendation"] == "NO LIVE"


def test_counts_and_action_hint():
    per = [R.classify_symbol(_bars([100 * (0.985 ** i) for i in range(80)]), symbol="XRPUSDT"),
           R.classify_symbol(_bars([100 * (1.015 ** i) for i in range(80)]), symbol="BTCUSDT")]
    c = R._counts(per)
    assert set(c) >= {"risk_off_count", "bounce_count", "no_edge_count", "range_count"}
    assert sum(c.values()) == len(per)
    assert "NO orders" in R._action_hint(R.R_RISK_OFF)


def test_journal_line_enriched(tmp_path):
    s = tmp_path / "s"
    os.makedirs(s)
    closes = [100 * (0.985 ** i) for i in range(80)]
    lines = ["timestamp,open,high,low,close,volume"] + \
            [f"{1700000000000 + i*R.DAY_MS},{c},{c*1.01},{c*0.99},{c},10" for i, c in enumerate(closes)]
    Path(s, "XRPUSDT_1d_ohlcv.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    rep = R.run_regime(str(s), ["XRPUSDT"], timeframe="1d")
    assert "counts" in rep and "action_hint" in rep
    j = R.write_journal(rep, output_dir=str(tmp_path / "out"))
    tl = os.path.join(str(tmp_path / "out"), "regime_timeline.jsonl")
    import json
    line = json.loads(Path(tl).read_text(encoding="utf-8").splitlines()[-1])
    assert line["makes_no_trades"] is True
    assert line["can_send_real_orders"] is False
    assert line["final_recommendation"] == "NO LIVE"
    assert "risk_off_count" in line and "no_edge_count" in line
    assert "action_hint" in line


def test_summarize_detects_regime_change():
    rows = [
        {"ts": "t1", "basket": "RISK_ON", "per_symbol": {"BTCUSDT": R.R_RISK_ON, "XRPUSDT": R.R_NO_EDGE}},
        {"ts": "t2", "basket": "RISK_OFF_EARLY_WARNING",
         "per_symbol": {"BTCUSDT": R.R_RISK_ON, "XRPUSDT": R.R_RISK_OFF}},
    ]
    s = R.summarize_timeline(rows, last_n=5)
    assert s["snapshots"] == 2
    events = {(c["symbol"], c["event"]) for c in s["changes"]}
    assert ("XRPUSDT", "ENTERED_RISK_OFF") in events
    assert s["streaks"]["BTCUSDT"]["consecutive_snapshots"] == 2  # unchanged across both
    assert "XRPUSDT" in s["weakest"]


def test_summarize_empty():
    s = R.summarize_timeline([], last_n=5)
    assert s["snapshots"] == 0 and s["latest"] is None and s["changes"] == []


def test_module_no_dangerous_primitives():
    import re
    src = Path(MODULE_PATH).read_text(encoding="utf-8")
    scan = re.sub(r'"never":\s*\[.*?\],', '"never": [],', src, flags=re.DOTALL)
    for tok in ["load_dotenv", "os.environ", "private_get", "private_post",
                "import requests", "import socket", "urllib.request", "db.execute",
                "INSERT INTO", "import torch", "import tensorflow"]:
        assert tok not in scan, tok
    for name in ["place_order", "create_order", "set_leverage", "set_margin_mode", "open_position"]:
        assert f"{name}(" not in scan and f".{name}" not in scan, name
    assert "ExecutionEngine(" not in scan and "PaperTrader(" not in scan
