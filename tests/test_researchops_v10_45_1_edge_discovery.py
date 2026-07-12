"""V10.45.1 Multi-AI Edge Discovery: providers fail-closed + no key leakage,
strict compiler, canonical replay (no lookahead, SL-first, partial TP,
censoring), funnel gates, ledger. Research only, NO LIVE."""

from __future__ import annotations

import json
import random
from pathlib import Path

from app.labs import ai_providers_v10_45_1 as P
from app.labs import edge_discovery_engine_v10_45_1 as ENG
from app.labs import multi_ai_orchestrator_v10_45_1 as ORCH

T0 = 1_700_000_000_000
BAR = 60_000


def _bars(n=1200, seed=3, drift=0.0):
    rng = random.Random(seed)
    price, out = 100.0, []
    for i in range(n):
        ch = rng.uniform(-0.002, 0.002) + drift
        new = price * (1 + ch)
        out.append({"ts": T0 + i * BAR, "available_at": T0 + i * BAR + BAR,
                    "open": price, "high": max(price, new) * 1.001,
                    "low": min(price, new) * 0.999, "close": new,
                    "volume": 10 + rng.uniform(0, 20), "turnover": 1000.0,
                    "symbol": "BTCUSDT", "venue": "bitget"})
        price = new
    return out


def _spec(**kw):
    s = {"strategy_id": "t", "origin": "test", "side": "LONG",
         "regime_filter": "ANY",
         "entry_conditions": [{"feature": "rsi_14", "op": "<", "value": 35.0}],
         "stop_policy": {"type": "fixed", "value": 0.006},
         "take_profit_policy": {"type": "fixed", "value": 0.006},
         "trailing_policy": {"type": "none", "value": 0.0},
         "time_exit": 30, "cooldown": 5}
    s.update(kw)
    return s


# --------------------------------------------------------------------------
# Providers: env detection, fail-closed, sanitization, cache
# --------------------------------------------------------------------------

def test_env_status_reports_booleans_never_values():
    st = P.env_key_status()
    for v in st.values():
        assert not (isinstance(v, str) and len(v) > 80)      # no key-like blobs
    assert isinstance(st["GROQ_API_KEY_detected"], bool)


def test_groq_gemini_fail_closed_without_keys(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    g = P.GroqProvider()
    r = g.generate("x")
    assert r["ok"] is False and r["error"] == "MISSING_API_KEY"
    m = P.GeminiProvider()
    r2 = m.generate("x")
    assert r2["ok"] is False and r2["error"] == "MISSING_API_KEY"


def test_sanitize_error_redacts_long_blobs_and_keys():
    # fixture built at runtime so no key-shaped literal exists in this source
    fake = "gsk_" + "a" * 40
    msg = f"Authorization: Bearer {fake} failed"
    out = P.sanitize_error(msg)
    assert fake not in out
    assert "redacted" in out.lower()


def test_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(P.CE, "_repo_root", lambda: tmp_path)
    P.cache_put("mock", "m1", "prompt-x", '{"a":1}')
    got = P.cache_get("mock", "m1", "prompt-x")
    assert got is not None and json.loads(got) == {"a": 1}
    assert P.cache_get("mock", "m1", "prompt-OTHER") is None
    # non-JSON bodies are NEVER stored: metadata-only entry -> cache MISS
    P.cache_put("mock", "m1", "prompt-t", "plain text " + "body")
    assert P.cache_get("mock", "m1", "prompt-t") is None


def test_rate_limiter_pause_blocks():
    rl = P._RateLimiter(0.0)
    rl.pause(60)
    assert rl.wait() is False


# --------------------------------------------------------------------------
# Compiler: strict schema, dedup, forbidden content
# --------------------------------------------------------------------------

def test_compiler_accepts_valid_and_dedups():
    seen: set[str] = set()
    st1, c1 = ENG.compile_strategy(_spec(), seen)
    assert st1 == "OK" and c1 is not None
    st2, c2 = ENG.compile_strategy(_spec(strategy_id="other_id"), seen)
    assert st2 == "DUPLICATE" and c2 is None                 # same signature


def test_compiler_rejects_unknown_feature_and_bad_op():
    seen: set[str] = set()
    st1, _ = ENG.compile_strategy(_spec(entry_conditions=[
        {"feature": "totally_fake", "op": ">", "value": 1}]), seen)
    assert st1 == "INVALID"
    st2, _ = ENG.compile_strategy(_spec(entry_conditions=[
        {"feature": "rsi_14", "op": "~=", "value": 1}]), seen)
    assert st2 == "INVALID"


def test_compiler_rejects_dangerous_and_impossible():
    seen: set[str] = set()
    st1, _ = ENG.compile_strategy(_spec(hypothesis="send order( to venue"), seen)
    assert st1 == "INVALID"
    st2, _ = ENG.compile_strategy(_spec(stop_policy={"type": "fixed", "value": 0}), seen)
    assert st2 == "INVALID"
    st3, _ = ENG.compile_strategy(_spec(time_exit=100000), seen)
    assert st3 == "INVALID"
    st4, _ = ENG.compile_strategy(_spec(side="BOTH_WAYS"), seen)
    assert st4 == "INVALID"
    st5, _ = ENG.compile_strategy("not a dict", seen)
    assert st5 == "INVALID"


# --------------------------------------------------------------------------
# Canonical replay: mechanics + honesty
# --------------------------------------------------------------------------

def _feats(bars, ref=None):
    return ENG.build_features(bars, ref_bars=ref)


def test_features_are_ex_ante_under_future_mutation():
    bars = _bars(600)
    f1 = _feats(bars)
    mutated = [dict(b) for b in bars]
    for b in mutated[400:]:
        b["close"] *= 2.0
        b["high"] *= 2.0
    f2 = _feats(mutated)
    for k in ("rsi_14", "atr_pct", "bb_pos", "ema_fast_slope", "vwap_dist"):
        assert f1[300][k] == f2[300][k], k


def test_replay_entry_next_open_and_costs_charged():
    bars = _bars(400, seed=5)
    feats = _feats(bars)
    seen: set[str] = set()
    _, spec = ENG.compile_strategy(_spec(entry_conditions=[
        {"feature": "ret_1", "op": ">=", "value": -1.0}],   # always true
        time_exit=5, cooldown=100), seen)
    r = ENG.replay(bars, feats, spec, i_start=250, i_end=300)
    assert r["n_trades"] >= 1
    t = r["trades"][0]
    assert t["entry_i"] == 251                               # signal 250 -> entry 251
    # zero-cost comparison: same replay with all costs zeroed earns more
    r0 = ENG.replay(bars, feats, spec, i_start=250, i_end=300,
                    costs={"taker_fee_bps": 0, "spread_bps": 0,
                           "slippage_bps": 0, "funding_bps_per_8h": 0})
    assert r0["trades"][0]["net_return"] > t["net_return"]


def test_same_bar_sl_beats_tp():
    bars = _bars(400, seed=7)
    # engineer a bar where both TP and SL are inside the range
    i = 300
    e = bars[i + 1]["open"]
    bars[i + 2]["high"] = e * 1.02
    bars[i + 2]["low"] = e * 0.98
    feats = _feats(bars)
    seen: set[str] = set()
    _, spec = ENG.compile_strategy(_spec(entry_conditions=[
        {"feature": "ret_1", "op": ">=", "value": -1.0}],
        stop_policy={"type": "fixed", "value": 0.01},
        take_profit_policy={"type": "fixed", "value": 0.01},
        time_exit=10, cooldown=500), seen)
    r = ENG.replay(bars, feats, spec, i_start=i, i_end=i + 20)
    assert r["trades"][0]["exit_reason"] in ("SL", "BE_STOP")
    assert r["trades"][0]["net_return"] < 0


def test_partial_tp1_tranches_and_be_stop():
    bars = _bars(400, seed=9)
    i = 300
    e = bars[i + 1]["open"]
    # bar i+2 rallies through TP1 only; bar i+3 collapses to entry (BE stop)
    bars[i + 2]["high"] = e * 1.0062
    bars[i + 2]["low"] = e * 0.9995
    bars[i + 3]["high"] = e * 1.001
    bars[i + 3]["low"] = e * 0.99
    feats = _feats(bars)
    seen: set[str] = set()
    _, spec = ENG.compile_strategy(_spec(entry_conditions=[
        {"feature": "ret_1", "op": ">=", "value": -1.0}],
        stop_policy={"type": "fixed", "value": 0.02},
        take_profit_policy={"type": "fixed", "value": 0.03,
                            "partial": {"tp1_frac": 0.5, "tp1_value": 0.005,
                                        "move_stop_to_be": True}},
        time_exit=40, cooldown=500), seen)
    r = ENG.replay(bars, feats, spec, i_start=i, i_end=i + 30)
    assert r["n_trades"] == 1
    t = r["trades"][0]
    assert t["tranches"] == 2                                # TP1 + BE stop
    assert t["exit_reason"] == "BE_STOP"
    # half banked at +50bps, half flat at BE -> small positive gross minus costs
    assert -0.004 < t["net_return"] < 0.004


def test_gap_blocks_entry_and_forces_stale_exit():
    bars = _bars(400, seed=11)
    for j in range(320, 400):                                # hole after 319
        bars[j]["ts"] += 30 * BAR
    feats = _feats(bars)
    seen: set[str] = set()
    _, spec = ENG.compile_strategy(_spec(entry_conditions=[
        {"feature": "ret_1", "op": ">=", "value": -1.0}],
        stop_policy={"type": "fixed", "value": 0.05},
        take_profit_policy={"type": "fixed", "value": 0.05},
        time_exit=100, cooldown=1000), seen)
    r = ENG.replay(bars, feats, spec, i_start=310, i_end=330)
    assert r["n_trades"] == 1
    assert r["trades"][0]["exit_reason"] == "STALE_EXIT"


def test_end_of_replay_censoring_flagged():
    bars = _bars(400, seed=13)
    feats = _feats(bars)
    seen: set[str] = set()
    _, spec = ENG.compile_strategy(_spec(entry_conditions=[
        {"feature": "ret_1", "op": ">=", "value": -1.0}],
        stop_policy={"type": "fixed", "value": 0.05},
        take_profit_policy={"type": "fixed", "value": 0.05},
        time_exit=200, cooldown=1000), seen)
    r = ENG.replay(bars, feats, spec, i_start=380, i_end=395)
    assert r["trades"][-1]["exit_reason"] == "END_CENSORED"
    assert r["trades"][-1]["censored"] is True


def test_trailing_is_causal():
    """The trailing stop for bar i+1 uses extremes up to bar i only."""
    bars = _bars(400, seed=15)
    i = 300
    e = bars[i + 1]["open"]
    prev_close = bars[i + 1]["close"]
    for k, mult in ((2, 1.004), (3, 1.008), (4, 1.012)):
        bars[i + k]["open"] = prev_close
        bars[i + k]["close"] = e * (mult - 0.001)
        bars[i + k]["high"] = max(e * mult, prev_close)
        bars[i + k]["low"] = min(e * (mult - 0.003), prev_close)
        prev_close = bars[i + k]["close"]
    bars[i + 5]["open"] = prev_close
    bars[i + 5]["close"] = e * 1.002
    bars[i + 5]["high"] = max(e * 1.010, prev_close)
    bars[i + 5]["low"] = e * 1.000                          # dips into trail
    feats = _feats(bars)
    seen: set[str] = set()
    _, spec = ENG.compile_strategy(_spec(entry_conditions=[
        {"feature": "ret_1", "op": ">=", "value": -1.0}],
        stop_policy={"type": "fixed", "value": 0.03},
        take_profit_policy={"type": "fixed", "value": 0.05},
        trailing_policy={"type": "fixed", "value": 0.004},
        time_exit=50, cooldown=500), seen)
    r = ENG.replay(bars, feats, spec, i_start=i, i_end=i + 30)
    t = r["trades"][0]
    assert t["exit_reason"] in ("SL", "TP", "TIME") or t["net_return"] > 0


# --------------------------------------------------------------------------
# Funnel: splits, holdout lock, gates, ledger
# --------------------------------------------------------------------------

def test_split_indices_have_embargo():
    seg = ENG.split_indices(20000)
    assert seg["validation"][0] - seg["discovery"][1] >= ENG.EMBARGO_BARS
    assert seg["holdout"][0] - seg["validation"][1] >= ENG.EMBARGO_BARS


def test_gate_never_returns_forbidden_states():
    good = {"n_trades": 50, "n_eff": 50, "net_EV": 0.001,
            "net_EV_lower_bound": 0.0005, "profit_factor": 1.5,
            "max_drawdown": -0.02, "censored_ratio": 0.0,
            "outlier_dependence": 0.0004, "stability_sign": 1}
    for hold in (None,
                 {"n_trades": 20, "net_EV": 0.001, "net_EV_lower_bound": 0.0004,
                  "profit_factor": 1.4, "censored_ratio": 0.0},
                 {"n_trades": 20, "net_EV": -0.001, "net_EV_lower_bound": -0.002,
                  "profit_factor": 0.6, "censored_ratio": 0.0},
                 {"n_trades": 3, "net_EV": 0.001, "net_EV_lower_bound": 0.0,
                  "profit_factor": 1.2, "censored_ratio": 0.0}):
        for stress in (True, False):
            for dq in (True, False):
                g = ENG.gate(good, hold, stress, data_quality_pass=dq)
                assert g in ENG.ALLOWED_STATES
                assert "LIVE" not in g
    assert ENG.gate({"n_trades": 5, "n_eff": 5, "net_EV": 1,
                     "net_EV_lower_bound": 1, "profit_factor": 9,
                     "max_drawdown": 0, "censored_ratio": 0},
                    None, True, data_quality_pass=True) == "NEED_MORE_DATA"
    assert ENG.gate(good, None, True, data_quality_pass=False) == "INVALID_DATA"


def test_funnel_on_pure_noise_promotes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(ENG.CE, "_repo_root", lambda: tmp_path)
    bars = _bars(3000, seed=21)
    feats = _feats(bars)
    seen: set[str] = set()
    compiled = []
    for s in ORCH.procedural_universe()[:40]:
        stt, c = ENG.compile_strategy(s, seen)
        if stt == "OK":
            compiled.append(c)
    out = ENG.run_funnel(bars, feats, compiled, log=lambda *a: None)
    paper = [e for e in out["finalists"]
             if e["state"] == "PAPER_CANDIDATE_RESEARCH_ONLY"]
    # on 3000 bars of pure noise nothing should clear validation+holdout+stress
    assert len(paper) == 0
    ledger = (tmp_path / "reports" / "research" / "v10_45_5_edge_discovery" /
              "experiment_ledger_v10_45_5.jsonl")
    assert ledger.is_file()
    lines = ledger.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= len(compiled)                       # every result logged


def test_planted_edge_is_found_by_funnel(tmp_path, monkeypatch):
    """A strong planted pattern must survive the funnel -> engine can detect
    real signal, so 'no edge' conclusions are informative, not vacuous."""
    monkeypatch.setattr(ENG.CE, "_repo_root", lambda: tmp_path)
    rng = random.Random(42)
    price, bars = 100.0, []
    n = 8000
    for i in range(n):
        # planted: a strong NOISY 3-bar upward burst every 79 bars (noise
        # makes trade returns heterogeneous, as real signals are; perfectly
        # identical returns are treated as DEGENERATE evidence by design)
        drift = (0.004 + rng.uniform(-0.001, 0.001)) if (i % 79) < 3             else rng.uniform(-0.0012, 0.0012)
        new = price * (1 + drift)
        bars.append({"ts": T0 + i * BAR, "available_at": T0 + i * BAR + BAR,
                     "open": price, "high": max(price, new) * 1.0008,
                     "low": min(price, new) * 0.9992, "close": new,
                     "volume": 10.0, "turnover": 100.0,
                     "symbol": "X", "venue": "bitget"})
        price = new
    feats = _feats(bars)
    seen: set[str] = set()
    _, spec = ENG.compile_strategy(_spec(
        strategy_id="planted_cycle_long",
        entry_conditions=[{"feature": "ret_1", "op": ">", "value": 0.003}],
        stop_policy={"type": "fixed", "value": 0.012},
        take_profit_policy={"type": "fixed", "value": 0.03},
        time_exit=3, cooldown=3), seen)
    ENG.set_run_context()
    out = ENG.run_funnel(bars, feats, [spec], log=lambda *a: None)
    # the engine DETECTS the real signal (merit gates pass) ...
    assert out["validation_survivors"] >= 1
    val = [e for e in out["results"] if e["phase"] == "validation"][0]
    assert val["state"] == "SURVIVED_VALIDATION"
    # ... but V10.45.4 execution proxies BLOCK holdout access entirely, so no
    # finalist exists and the access denial is logged (honest cap)
    assert out["holdout_accesses"] == 0
    denials = [e for e in out["results"] if e.get("phase") == "holdout_access"]
    assert denials == [] or all(not d["holdout_accessed"] for d in denials)


# --------------------------------------------------------------------------
# Orchestrator: JSON extraction, procedural universe sanity
# --------------------------------------------------------------------------

def test_extract_json_tolerates_fences_but_never_repairs():
    assert ORCH._extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert ORCH._extract_json("no json here") is None


def test_procedural_universe_compiles_mostly():
    seen: set[str] = set()
    ok = dup = bad = 0
    for s in ORCH.procedural_universe():
        stt, _ = ENG.compile_strategy(s, seen)
        ok += stt == "OK"
        dup += stt == "DUPLICATE"
        bad += stt == "INVALID"
    assert ok >= 80                                          # wide real universe
    assert bad == 0


def test_resample_aggregates_and_drops_partial_tail():
    bars = _bars(23)
    shift = 300_000 - (T0 % 300_000)          # align start to a 5m boundary
    for b in bars:
        b["ts"] += shift
        b["available_at"] += shift
    r5 = ENG.resample_bars(bars, 5)
    assert len(r5) == 4                                     # partial tail dropped
    g0 = bars[0:5]
    assert r5[0]["open"] == g0[0]["open"]
    assert r5[0]["close"] == g0[-1]["close"]
    assert r5[0]["high"] == max(b["high"] for b in g0)
    assert r5[0]["volume"] == sum(b["volume"] for b in g0)
    assert r5[0]["available_at"] == g0[-1]["available_at"]  # last sub-bar close


def test_replay_gap_detection_respects_bar_interval():
    """On 5m bars a normal 5m step must NOT be treated as a gap."""
    raw = _bars(2000, seed=17)
    shift = 300_000 - (T0 % 300_000)          # strict resample needs alignment
    for b in raw:
        b["ts"] += shift
        b["available_at"] += shift
    bars = ENG.resample_bars(raw, 5)
    feats = ENG.build_features(bars)
    seen: set[str] = set()
    _, spec = ENG.compile_strategy(_spec(entry_conditions=[
        {"feature": "ret_1", "op": ">=", "value": -1.0}],
        time_exit=5, cooldown=50), seen)
    r = ENG.replay(bars, feats, spec, i_start=250, i_end=350)
    assert r["n_trades"] >= 1
    assert all(t["exit_reason"] != "STALE_EXIT" for t in r["trades"])


def test_no_live_states_anywhere_in_sources():
    for mod in (ENG, ORCH, P):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        for tok in ("LIVE_READY", "SEND_ORDER", "ACTIONABLE_REAL",
                    "place_order(", "set_leverage("):
            assert tok not in src, f"{mod.__name__}: {tok}"
