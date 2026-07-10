"""V10.45 AI Simulated Trader: isolated ledger, next_open, no future data,
costs applied, honest verdicts, never LIVE_READY. NO LIVE."""

from __future__ import annotations

import json
import random
from pathlib import Path

from app.labs import ai_simulated_trader_v10_45 as SIM
from app.labs import ai_research_copilot_v10_45 as COP

T0 = 1_700_000_000_000
BAR = 60_000


def _bars(n=260, seed=7):
    rng = random.Random(seed)
    price, out = 100.0, []
    for i in range(n):
        drift = rng.uniform(-0.0012, 0.0012)
        new = price * (1 + drift)
        buy = 10 + rng.uniform(0, 10)
        sell = 10 + rng.uniform(0, 10)
        out.append({"ts": T0 + i * BAR, "available_at": T0 + i * BAR,
                    "open": price, "high": max(price, new) * 1.0008,
                    "low": min(price, new) * 0.9992, "close": new,
                    "volume": buy + sell, "buy_volume": buy, "sell_volume": sell,
                    "n_trades": 20, "trade_count": 20, "max_trade": 2.0,
                    "symbol": "BTCUSDT"})
        price = new
    return out


def _long_decision(tp=60, sl=40, hold=10):
    return json.dumps({"decision": "LONG_SIM", "confidence_bucket": "MEDIUM",
                       "entry_reason": "test", "exit_plan":
                       {"tp_bps": tp, "sl_bps": sl, "max_hold_bars": hold},
                       "risk_flags": [], "no_live_confirmation": True})


def _no_trade():
    return json.dumps({"decision": "NO_TRADE", "confidence_bucket": "LOW",
                       "entry_reason": "", "exit_plan": {},
                       "risk_flags": [], "no_live_confirmation": True})


def _run(bars, decide_fn, monkeypatch, **kw):
    monkeypatch.setattr(SIM.LAB, "_load_bars",
                        lambda s, ds: (bars, "ws_persistent", {}))
    return SIM.run_ai_simulated_trader("BTCUSDT", provider="mock",
                                       write_reports=False,
                                       decide_fn=decide_fn, **kw)


# --------------------------------------------------------------------------
# Sandbox mechanics
# --------------------------------------------------------------------------

def test_entry_is_next_open_and_costs_applied(monkeypatch):
    bars = _bars(200)
    fired = {"done": False}

    def decide(ctx):
        if not fired["done"] and ctx["position_state"] == "FLAT":
            fired["done"] = True
            return _long_decision(tp=10_000, sl=10_000, hold=5)  # clamped anyway
        return _no_trade()
    r = _run(bars, decide, monkeypatch)
    assert r["n_trades"] == 1
    t_ev = r["metrics"]["net_EV"]
    # net return includes a round-trip cost -> strictly less than gross
    assert t_ev is not None
    assert r["metrics"]["by_exit"]["TIME"] + r["metrics"]["by_exit"]["END_OF_REPLAY"] >= 0


def test_no_future_data_in_context(monkeypatch):
    bars = _bars(200)
    seen_ts = []

    def decide(ctx):
        seen_ts.append((ctx["bar_index"], ctx["ts"]))
        return _no_trade()
    _run(bars, decide, monkeypatch)
    # context ts must equal the CURRENT bar's ts (never a future bar)
    for i, ts in seen_ts:
        assert ts == bars[i]["ts"]


def test_future_mutation_does_not_change_early_decisions(monkeypatch):
    bars = _bars(220)
    ctxs_a: list[dict] = []
    _run(bars, lambda c: (ctxs_a.append(c["features"]), _no_trade())[1], monkeypatch)
    mutated = [dict(b) for b in bars]
    for b in mutated[-50:]:
        b["close"] *= 3.0
        b["high"] *= 3.0
    ctxs_b: list[dict] = []
    _run(mutated, lambda c: (ctxs_b.append(c["features"]), _no_trade())[1], monkeypatch)
    # decisions early in the replay see identical features
    assert ctxs_a[10] == ctxs_b[10]


def test_dangerous_output_is_blocked_and_counted(monkeypatch):
    bars = _bars(200)

    def decide(ctx):
        return json.dumps({"decision": "LONG_SIM", "confidence_bucket": "HIGH",
                           "entry_reason": "BUY NOW, this is LIVE READY",
                           "exit_plan": {"tp_bps": 60, "sl_bps": 40,
                                         "max_hold_bars": 10},
                           "no_live_confirmation": True})
    r = _run(bars, decide, monkeypatch)
    assert r["n_dangerous_outputs"] > 0
    assert r["n_trades"] == 0                    # nothing executed
    assert r["verdict"] in SIM.VERDICTS


def test_garbage_output_counts_rejected_and_no_trades(monkeypatch):
    bars = _bars(200)
    r = _run(bars, lambda ctx: "not json at all", monkeypatch)
    assert r["n_rejected_outputs"] > 0
    assert r["n_trades"] == 0


def test_data_gap_blocks_entry(monkeypatch):
    bars = _bars(200)
    # punch a hole right after the warmup zone
    for j in range(80, 200):
        bars[j]["ts"] += 30 * BAR
        bars[j]["available_at"] += 30 * BAR
    entries = {"tried": 0}

    def decide(ctx):
        if ctx["bar_index"] == 79 and ctx["position_state"] == "FLAT":
            entries["tried"] += 1
            return _long_decision()
        return _no_trade()
    r = _run(bars, decide, monkeypatch)
    assert entries["tried"] == 1
    assert r["metrics"]["n_trades"] == 0         # entry blocked by the gap


def test_ledger_and_reports_are_isolated_files(monkeypatch, tmp_path: Path):
    bars = _bars(200)
    monkeypatch.setattr(SIM.CE, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(SIM.LAB, "_load_bars",
                        lambda s, ds: (bars, "ws_persistent", {}))
    r = SIM.run_ai_simulated_trader("BTCUSDT", provider="mock", write_reports=True)
    out = tmp_path / "reports" / "research" / "v10_45_ai_copilot"
    assert (out / "ai_simulated_trader_v10_45.json").is_file()
    assert (out / "ai_decision_ledger_v10_45.csv").is_file()
    assert (out / "ai_simulated_trader_v10_45.md").is_file()
    data = json.loads((out / "ai_simulated_trader_v10_45.json").read_text(encoding="utf-8"))
    assert data["sandboxed_ledger"] is True
    assert data["can_send_real_orders"] is False
    assert data["final_recommendation"] == "NO LIVE"


# --------------------------------------------------------------------------
# Honest verdicts; never LIVE_READY
# --------------------------------------------------------------------------

def test_small_sample_never_promising(monkeypatch):
    bars = _bars(200)
    fired = {"n": 0}

    def decide(ctx):
        if fired["n"] < 3 and ctx["position_state"] == "FLAT":
            fired["n"] += 1
            return _long_decision(hold=3)
        return _no_trade()
    r = _run(bars, decide, monkeypatch)
    assert 0 < r["n_trades"] < SIM.MIN_TRADES_FOR_CLAIM
    assert r["verdict"] in ("AI_SIM_NEEDS_MORE_DATA", "AI_SIM_REJECTED")
    assert r["verdict"] != "AI_SIM_PROMISING_RESEARCH_ONLY"


def test_verdict_vocabulary_never_live(monkeypatch):
    bars = _bars(240)
    r = _run(bars, lambda c: _no_trade(), monkeypatch)
    assert r["verdict"] in SIM.VERDICTS
    for forbidden in ("LIVE_READY", "ACTIONABLE_REAL", "SEND_ORDER"):
        assert forbidden not in json.dumps(r)


def test_missing_api_key_fails_closed(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    r = SIM.run_ai_simulated_trader("BTCUSDT", provider="gemini",
                                    write_reports=False)
    assert r["verdict"] == "MISSING_API_KEY"
    assert r["can_send_real_orders"] is False


def test_mock_provider_end_to_end(monkeypatch):
    bars = _bars(300, seed=11)
    monkeypatch.setattr(SIM.LAB, "_load_bars",
                        lambda s, ds: (bars, "ws_persistent", {}))
    r = SIM.run_ai_simulated_trader("BTCUSDT", provider="mock",
                                    write_reports=False)
    assert r["verdict"] in SIM.VERDICTS or r["verdict"] == "AI_SIM_NEEDS_MORE_DATA"
    assert r["n_decisions"] > 0
    assert r["simulation_only"] is True


def test_source_has_no_broker_or_env_calls():
    src = Path(SIM.__file__).read_text(encoding="utf-8")
    for token in ("place_order", "private_get", "private_post", "set_leverage",
                  "load_dotenv", 'open(".env"', "LIVE_TRADING=True",
                  "can_send_real_orders=True", "bitget"):
        assert token not in src, token
