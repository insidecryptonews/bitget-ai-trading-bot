"""ResearchOps V10.28 - Multi-Symbol Shadow Opportunity Scanner tests.

Research-only, shadow-only, NO keys/auth/DB/orders/network. Bars are injected
(no HTTP). Verifies quality gating + discards, transparent scoring, ranking,
disciplined trade/no-trade decisions (edge floor, stop, RR, regime, duplicates,
max-open, correlation cap), the live loop with periodic autosave + clean
shutdown, hardened output containment, and the absence of dangerous primitives.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import uuid
from pathlib import Path

import pytest

from app import research_lab
from app.labs import multi_symbol_opportunity_scanner_v10_28 as S


# ---- deterministic bar builders (no network) ------------------------------

def ramp(n=260, start=100.0, step=0.004, wiggle=0.004, seed=1):
    """Smooth uptrend (step>0) / downtrend (step<0) with tight bars -> clean gate."""
    import random
    r = random.Random(seed)
    bars, p, ts = [], start, 1_700_000_000_000
    for i in range(n):
        p2 = p * (1 + step + r.uniform(-wiggle, wiggle))
        hi = max(p, p2) * (1 + abs(r.uniform(0, wiggle)))
        lo = min(p, p2) * (1 - abs(r.uniform(0, wiggle)))
        bars.append({"ts": ts + i * 900000, "open": p, "high": hi, "low": lo,
                     "close": p2, "volume": 1000 + r.uniform(0, 300)})
        p = p2
    return bars


def flat(n=260, start=100.0, seed=9):
    """Near-flat noise -> no directional setup, tiny ATR."""
    import random
    r = random.Random(seed)
    bars, ts = [], 1_700_000_000_000
    for i in range(n):
        c = start * (1 + r.uniform(-0.0002, 0.0002))
        bars.append({"ts": ts + i * 900000, "open": start, "high": c * 1.0002,
                     "low": c * 0.9998, "close": c, "volume": 500})
    return bars


def _tmp_out():
    return f"reports/research/v10_28/_pytest_{uuid.uuid4().hex[:8]}"


# ---- plan (no network, no writes) -----------------------------------------

def test_plan_no_network_no_writes_no_live():
    p = S.plan()
    assert p["writes_on_plan"] is False and p["uses_network"] is False
    assert p["uses_api_keys"] is False and p["can_send_real_orders"] is False
    assert p["final_recommendation"] == "NO LIVE" and p["universe_size"] == 19
    assert p["LIVE_TRADING"] is False and p["DRY_RUN"] is True and p["PAPER_TRADING"] is True


def test_plan_custom_universe():
    p = S.plan(universe=["btcusdt", "ethusdt"])
    assert p["universe"] == ["btcusdt", "ethusdt"] and p["universe_size"] == 2


# ---- quality gate ----------------------------------------------------------

def test_quality_gate_passes_clean_series():
    q = S.quality_gate("BTCUSDT", ramp(), S.DEFAULT_CONFIG)
    assert q["passed"] is True and q["reasons"] == []


def test_quality_gate_discards_each_defect():
    cfg = S.DEFAULT_CONFIG
    # insufficient data
    assert "insufficient_data" in ";".join(S.quality_gate("X", ramp(n=50), cfg)["reasons"])
    # no volume
    bars = ramp()
    for b in bars:
        b["volume"] = 0
    assert "no_volume" in S.quality_gate("X", bars, cfg)["reasons"]
    # absurd single-bar move -> bad data
    bars = ramp()
    bars[-1]["close"] = bars[-2]["close"] * 2.0
    assert any("absurd_move" in r for r in S.quality_gate("X", bars, cfg)["reasons"])
    # volatility too low (flat)
    assert "volatility_too_low" in S.quality_gate("X", flat(), cfg)["reasons"]


def test_quality_gate_spread_too_wide():
    bars = ramp()
    for b in bars:
        b["high"] = b["close"] * 1.10
        b["low"] = b["close"] * 0.90     # ~20% bar range -> spread proxy blows the 2% cap
    r = S.quality_gate("X", bars, S.DEFAULT_CONFIG)["reasons"]
    assert any("spread_too_wide" in x for x in r)


# ---- scoring ---------------------------------------------------------------

def test_score_long_and_short_have_stops():
    up = S.score_opportunity("BTCUSDT", ramp(step=0.005, seed=1), S.DEFAULT_CONFIG)
    dn = S.score_opportunity("SOLUSDT", ramp(step=-0.005, seed=2), S.DEFAULT_CONFIG)
    assert up["side"] == "long" and up["stop"] is not None and up["stop"] < up["entry"]
    assert dn["side"] == "short" and dn["stop"] is not None and dn["stop"] > dn["entry"]
    assert up["rr"] == S.DEFAULT_CONFIG["min_rr"] and up["edge_validated"] is False
    assert up["take_profit"] > up["entry"] and dn["take_profit"] < dn["entry"]


def test_flat_market_no_directional_setup():
    s = S.score_opportunity("X", flat(), S.DEFAULT_CONFIG)
    assert s["side"] is None and s["stop"] is None


# ---- scan: ranking + verdict ----------------------------------------------

def test_scan_ranks_and_flags_safety():
    data = {"BTCUSDT": ramp(step=0.006, seed=1), "ETHUSDT": ramp(step=0.005, seed=2),
            "FOOUSDT": ramp(n=40, seed=3)}
    rep = S.scan(data)
    assert rep["analyzed"] and any(d["symbol"] == "FOOUSDT" for d in rep["discarded"])
    scores = [s["edge_score"] for s in rep["opportunity_board"]]
    assert scores == sorted(scores, reverse=True)     # ranked best-first
    assert rep["can_send_real_orders"] is False and rep["edge_validated"] is False
    assert rep["final_recommendation"] == "NO LIVE"


def test_stay_out_when_no_edge():
    rep = S.scan({"AAAUSDT": flat(seed=1), "BBBUSDT": flat(seed=2)})
    assert rep["verdict"] == "STAY_OUT_NO_EDGE" and rep["n_shadow_candidates"] == 0


# ---- decision discipline ---------------------------------------------------

def test_edge_floor_blocks_entry():
    # raise the floor above any achievable heuristic score -> everyone stays out
    rep = S.scan({"BTCUSDT": ramp(step=0.006, seed=1)}, {"min_edge_score": 999})
    assert rep["decisions"] == []
    assert any("edge_below_min" in r for r in rep["stayed_out"][0]["reasons"])


def test_correlation_cap_rejects_second_pick():
    bars = ramp(step=0.006, seed=1)
    # identical series -> correlation 1.0 -> the 2nd clone must be rejected
    rep = S.scan({"BTCUSDT": bars, "ETHUSDT": list(bars)},
                 {"max_correlation": 0.8, "max_open_positions": 5})
    assert len(rep["decisions"]) == 1
    out = [s for s in rep["stayed_out"] if s["symbol"] == "ETHUSDT"][0]
    assert "too_correlated_with_open" in out["reasons"]


def test_max_open_positions_respected():
    data = {f"S{i}USDT": ramp(step=0.006, seed=i) for i in range(4)}
    rep = S.scan(data, {"max_open_positions": 2, "max_correlation": 1.5})  # disable corr gate
    assert len(rep["decisions"]) == 2
    assert any("max_open_positions_reached" in s["reasons"] for s in rep["stayed_out"])


def test_long_blocked_in_risk_off(monkeypatch):
    monkeypatch.setattr(S.REG, "classify_symbol",
                        lambda bars, symbol="", timeframe="": {"verdict": S.REG.R_RISK_OFF})
    rep = S.scan({"BTCUSDT": ramp(step=0.006, seed=1)})
    assert rep["decisions"] == []
    assert any("long_blocked_risk_off" in s["reasons"] for s in rep["stayed_out"])


def test_decisions_are_shadow_only_and_not_actionable():
    rep = S.scan({"BTCUSDT": ramp(step=0.006, seed=1)})
    assert rep["decisions"], "expected at least one observation candidate"
    # V10.29.2 UX: the verdict itself must scream NOT ACTIONABLE
    assert rep["verdict"] == "SHADOW_OBSERVATION_CANDIDATES_NOT_ACTIONABLE"
    assert rep["not_actionable"] is True and rep["no_orders"] is True
    for d in rep["decisions"]:
        assert d["action"] == "SHADOW_OBSERVATION_CANDIDATE_NOT_ACTIONABLE"
        assert d["executed"] is False and d["would_send_real_order"] is False
        assert d["edge_validated"] is False and d["stop"] is not None
        assert d["not_actionable"] is True and d["no_orders"] is True
    # the live board shows the flags NEXT TO the ranking, not only at the foot
    txt = S.render_board(rep, 1, 0.0)
    assert "SHADOW OBSERVATION ONLY - NOT ACTIONABLE - NO EDGE VALIDATED" in txt
    assert "NOT_ACTIONABLE" in txt and "edge_validated=False" in txt
    assert "NOT ACTIONABLE" in txt        # candidates section header
    for banned in ("buy now", "BUY NOW", "signal executable", "EXECUTE",
                   "SHADOW_ENTRY_CANDIDATE", "entry="):
        assert banned not in txt
    # shadow sizing is explicitly shadow-named (V10.29.3), alias kept for compat
    for d in rep["decisions"]:
        assert d["shadow_size_hint_units"] == d["size_hint_units"]
    assert "shadow_size~" in txt and "size~" not in txt.replace("shadow_size~", "")


# ---- journal + autosave ----------------------------------------------------

def test_journal_and_shutdown_files():
    out = _tmp_out()
    try:
        rep = S.scan({"BTCUSDT": ramp(step=0.006, seed=1)})
        jp = S.write_journal(rep, out)
        lp = S.append_scan_log(rep, out, 1)
        S.append_scan_log(rep, out, 2)
        sp = S.write_shutdown({"scans_completed": 2}, out)
        assert Path(jp).exists() and Path(sp).exists()
        lines = [l for l in Path(lp).read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 2
        rec = json.loads(lines[0])
        assert rec["scan_no"] == 1 and rec["final_recommendation"] == "NO LIVE"
        assert json.loads(Path(sp).read_text(encoding="utf-8"))["clean_shutdown"] is True
    finally:
        shutil.rmtree(Path(research_lab.__file__).resolve().parents[1] / out, ignore_errors=True)


def test_safe_output_dir_rejects_bad_paths():
    for bad in ("../evil", "reports/research/v10_28/../../etc",
                "external_data/raw/x", "reports/research/v10_28/db"):
        with pytest.raises(ValueError):
            S.safe_output_dir(bad)


# ---- live loop -------------------------------------------------------------

def test_run_loop_autosaves_every_scan_and_shuts_down_clean():
    out = _tmp_out()
    data = {"BTCUSDT": ramp(step=0.006, seed=1), "ETHUSDT": ramp(step=0.005, seed=2)}
    emitted = []
    try:
        summary = S.run_loop(universe=["BTCUSDT", "ETHUSDT"],
                             bars_provider=lambda s: data.get(s),
                             max_scans=3, interval_seconds=0.0, output_dir=out,
                             sleep_fn=lambda s: None, should_stop=lambda: False,
                             emit=emitted.append)
        assert summary["scans_completed"] == 3 and summary["stop_reason"] == "max_scans"
        assert summary["can_send_real_orders"] is False
        base = Path(research_lab.__file__).resolve().parents[1] / out
        jl = [l for l in (base / "scanner_scans.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(jl) == 3                       # one autosave line per scan
        assert (base / "scanner_state.json").exists() and (base / "scanner_shutdown.json").exists()
        assert any("CLEAN SHUTDOWN COMPLETE" in e for e in emitted)
    finally:
        shutil.rmtree(Path(research_lab.__file__).resolve().parents[1] / out, ignore_errors=True)


def test_run_loop_user_stop_and_fetch_error_isolation():
    out = _tmp_out()
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 1        # stop before the 2nd scan

    def provider(sym):
        if sym == "BADUSDT":
            raise RuntimeError("bad data / latency")
        return ramp(step=0.006, seed=1)

    emitted = []
    try:
        summary = S.run_loop(universe=["BTCUSDT", "BADUSDT"], bars_provider=provider,
                             max_scans=0, interval_seconds=0.0, output_dir=out,
                             sleep_fn=lambda s: None, should_stop=stop, emit=emitted.append)
        assert summary["stop_reason"] == "user_stop"
        assert summary["fetch_errors"] >= 1        # BADUSDT isolated, loop kept going
    finally:
        shutil.rmtree(Path(research_lab.__file__).resolve().parents[1] / out, ignore_errors=True)


# ---- CLI wiring + isolation ------------------------------------------------

def _run_main(argv):
    old = sys.argv
    sys.argv = ["prog"] + argv
    try:
        research_lab.main()
    finally:
        sys.argv = old


def test_cli_allowlisted_and_isolated(monkeypatch, capsys):
    for c in ("opportunity-scanner-plan-v1028", "opportunity-scanner-run-v1028"):
        assert c in research_lab.PUBLIC_RESEARCH_ONLY_COMMANDS
    monkeypatch.setattr(research_lab, "load_config",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no config")))
    monkeypatch.setattr(research_lab, "Database",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no db")))
    _run_main(["opportunity-scanner-plan-v1028"])
    assert "OPPORTUNITY SCANNER PLAN V10.28" in capsys.readouterr().out


def test_run_cli_with_injected_provider_no_network():
    lab = research_lab.ResearchLab.__new__(research_lab.ResearchLab)
    out = _tmp_out()
    data = {"BTCUSDT": ramp(step=0.006, seed=1)}
    emitted = []
    try:
        res = lab.opportunity_scanner_run_v1028_cli(
            universe="BTCUSDT", max_scans=1, interval_seconds=0.0, output_dir=out,
            bars_provider=lambda s: data.get(s), sleep_fn=lambda s: None,
            should_stop=lambda: False, emit=emitted.append)
        assert "final_recommendation=NO LIVE" in res and "clean_shutdown=True" in res
    finally:
        shutil.rmtree(Path(research_lab.__file__).resolve().parents[1] / out, ignore_errors=True)


# ---- no dangerous primitives ----------------------------------------------

def test_module_no_dangerous_primitives():
    src = Path("app/labs/multi_symbol_opportunity_scanner_v10_28.py").read_text(encoding="utf-8")
    for tok in ["load_dotenv", "os.environ", "private_get", "private_post", "db.execute",
                "INSERT INTO", "import torch", "import tensorflow", "X-MBX-APIKEY",
                "listenKey", "requests.post", "urlopen"]:
        assert tok not in src, tok
    for name in ["place_order", "create_order", "set_leverage", "set_margin_mode",
                 "open_position", "execute"]:
        assert f"{name}(" not in src and f".{name}" not in src, name
    assert "ExecutionEngine(" not in src and "PaperTrader(" not in src
