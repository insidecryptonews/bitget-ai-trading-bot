"""V10.45.4 final research truth closure: separated stop/limit fills with
exactly-computable outcomes, TP favourable gaps, TP1+BE same bar, trailing+TP
same bar, fail-closed dataset verification (manifest/SHA/gaps/raw, no
longest-segment rescue), physically sealed holdout with unforgeable token,
proxies/baselines blocking access, unstable holdout rejection, n_eff-based
lower bounds, two-phase global multiple-testing registry with hard CLOSE,
exposure-matched baseline v2 (full hold distribution, sessions, no duplicate
timestamps), hardlink/containment/atomic writes, CSV-manifest consistency,
structural + URL-encoded sanitization, sensitive-response cache, as_of
resampling and versioned provenance seal. Research only, NO LIVE."""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
from pathlib import Path

import pytest

from app.labs import ai_providers_v10_45_1 as P
from app.labs import edge_discovery_engine_v10_45_1 as ENG
from app.labs import multi_ai_orchestrator_v10_45_1 as ORCH
from app.labs import public_data_backfill_v10_45_1 as BF

T0 = 1_700_000_400_000            # aligned to a 5m boundary
BAR = 60_000
PS = (6.0 + 1.0 / 2 + 2.0) / 10_000.0          # per-side cost fraction
FUND = 1.0 / 10_000.0 / 480                    # funding per 1m bar


def _bars(n=600, seed=3, t0=T0):
    rng = random.Random(seed)
    price, out = 100.0, []
    for i in range(n):
        ch = rng.uniform(-0.002, 0.002)
        new = price * (1 + ch)
        out.append({"ts": t0 + i * BAR, "available_at": t0 + i * BAR + BAR,
                    "open": price, "high": max(price, new) * 1.001,
                    "low": min(price, new) * 0.999, "close": new,
                    "volume": 10.0, "turnover": 1000.0,
                    "symbol": "BTCUSDT", "venue": "bitget"})
        price = new
    return out


def _spec(**kw):
    s = {"strategy_id": "t", "origin": "test", "side": "LONG",
         "regime_filter": "ANY",
         "entry_conditions": [{"feature": "ret_1", "op": ">=", "value": -1.0}],
         "stop_policy": {"type": "fixed", "value": 0.006},
         "take_profit_policy": {"type": "fixed", "value": 0.006},
         "trailing_policy": {"type": "none", "value": 0.0},
         "time_exit": 30, "cooldown": 500}
    s.update(kw)
    return s


def _compile(seen=None, **kw):
    st_, spec = ENG.compile_strategy(_spec(**kw), seen if seen is not None else set())
    assert st_ == "OK", st_
    return spec


def _trade(entry_i, exit_i, ret, reason="TP"):
    return {"entry_i": entry_i, "exit_i": exit_i, "net_return": ret,
            "exit_reason": reason, "bars_held": exit_i - entry_i,
            "censored": False, "tranches": 1}


def _rows_1m(days=3, t_end=T0 + 4320 * BAR, drop=(), bad_ohlc=()):
    """Contiguous 1m raw rows [ts,o,h,l,c,v,t] ending exactly at t_end."""
    t_start = t_end - days * 86_400_000
    rows = []
    k = 0
    ts = t_start
    while ts < t_end:
        if k not in drop:
            if k in bad_ohlc:
                rows.append([ts, 100.0, 99.0, 100.0, 100.0, 1.0, 100.0])
            else:
                rows.append([ts, 100.0, 100.0, 100.0, 100.0, 1.0, 100.0])
        ts += BAR
        k += 1
    return rows, t_start, t_end


# ==========================================================================
# FILLS: limit TP vs stop, exactly computable results
# ==========================================================================

def test_tp_favorable_gap_long_fills_at_executable_open():
    """LONG TP at +0.6%; next bar OPENS +3% above: a resting limit fills at
    the better executable open, never outside [low, high], no AssertionError.
    The net return is computed exactly."""
    bars = _bars(400, seed=11)
    i = 300
    entry_px = bars[i + 1]["open"]
    gap_open = entry_px * 1.03
    bars[i + 2]["open"] = gap_open
    bars[i + 2]["high"] = gap_open * 1.001
    bars[i + 2]["low"] = gap_open * 0.998
    bars[i + 2]["close"] = gap_open * 0.999
    feats = ENG.build_features(bars)
    spec = _compile(stop_policy={"type": "fixed", "value": 0.02},
                    take_profit_policy={"type": "fixed", "value": 0.006},
                    time_exit=20)
    r = ENG.replay(bars, feats, spec, i_start=i, i_end=i + 20)
    t = r["trades"][0]
    assert t["exit_reason"] == "TP"
    assert t["exit_i"] == i + 2                       # same gap bar
    expected = (gap_open * (1 - PS)) / (entry_px * (1 + PS)) - 1 - FUND * 1
    assert abs(t["net_return"] - expected) < 1e-7
    assert t["net_return"] > 0.02                     # price improvement kept
    assert r["invalid_bar_fills"] == 0


def test_tp_favorable_gap_short_fills_at_executable_open():
    bars = _bars(400, seed=13)
    i = 300
    entry_px = bars[i + 1]["open"]
    gap_open = entry_px * 0.97
    bars[i + 2]["open"] = gap_open
    bars[i + 2]["high"] = gap_open * 1.002
    bars[i + 2]["low"] = gap_open * 0.999
    bars[i + 2]["close"] = gap_open * 1.001
    feats = ENG.build_features(bars)
    spec = _compile(side="SHORT",
                    stop_policy={"type": "fixed", "value": 0.02},
                    take_profit_policy={"type": "fixed", "value": 0.006},
                    time_exit=20)
    r = ENG.replay(bars, feats, spec, i_start=i, i_end=i + 20)
    t = r["trades"][0]
    assert t["exit_reason"] == "TP"
    entry_eff = entry_px * (1 - PS)
    exit_eff = gap_open * (1 + PS)
    expected = entry_eff / exit_eff - 1 - FUND * 1
    assert abs(t["net_return"] - expected) < 1e-7
    assert t["net_return"] > 0.02
    assert r["invalid_bar_fills"] == 0


def test_tp1_and_breakeven_same_bar_conservative():
    """TP1 hit AND the fresh break-even stop touched on the SAME bar: the
    remainder exits at BE in that bar (never assume favourable ordering).
    Exact two-tranche arithmetic."""
    bars = _bars(400, seed=17)
    i = 300
    entry_px = bars[i + 1]["open"]
    b = bars[i + 2]
    b["open"] = entry_px
    b["high"] = entry_px * 1.0056                    # >= TP1 (+0.5%)
    b["low"] = entry_px * 0.9945                     # <= BE, > SL (-1%)
    b["close"] = entry_px * 0.999
    feats = ENG.build_features(bars)
    spec = _compile(stop_policy={"type": "fixed", "value": 0.01},
                    take_profit_policy={
                        "type": "fixed", "value": 0.02,
                        "partial": {"tp1_frac": 0.5, "tp1_value": 0.005,
                                    "move_stop_to_be": True}},
                    time_exit=20)
    r = ENG.replay(bars, feats, spec, i_start=i, i_end=i + 20)
    t = r["trades"][0]
    assert t["exit_reason"] == "BE_STOP"
    assert t["exit_i"] == i + 2                      # SAME bar as TP1
    assert t["tranches"] == 2
    entry_eff = entry_px * (1 + PS)
    leg_tp1 = (entry_px * 1.005 * (1 - PS)) / entry_eff - 1 - FUND * 1
    leg_be = (entry_px * (1 - PS)) / entry_eff - 1 - FUND * 1
    expected = 0.5 * leg_tp1 + 0.5 * leg_be
    assert abs(t["net_return"] - expected) < 1e-7
    assert r["invalid_bar_fills"] == 0


def test_trailing_and_tp_same_bar_stop_resolves_first():
    """When one bar touches BOTH the trailed stop and the final TP, the stop
    resolves first (conservative same-bar ambiguity)."""
    bars = _bars(400, seed=19)
    i = 300
    entry_px = bars[i + 1]["open"]
    b1 = bars[i + 2]                                  # rally bar: lifts hwm
    b1["open"] = entry_px
    b1["high"] = entry_px * 1.0045
    b1["low"] = entry_px * 0.9995
    b1["close"] = entry_px * 1.004
    b2 = bars[i + 3]                                  # touches trail AND TP
    b2["open"] = entry_px * 1.002
    b2["high"] = entry_px * 1.008                     # >= TP (+0.6%)
    b2["low"] = entry_px * 0.9994                     # <= trailed stop
    b2["close"] = entry_px * 1.001
    feats = ENG.build_features(bars)
    spec = _compile(stop_policy={"type": "fixed", "value": 0.01},
                    take_profit_policy={"type": "fixed", "value": 0.006},
                    trailing_policy={"type": "fixed", "value": 0.004},
                    time_exit=20)
    r = ENG.replay(bars, feats, spec, i_start=i, i_end=i + 20)
    t = r["trades"][0]
    assert t["exit_reason"] == "TRAIL"                # stop first, not TP
    assert t["exit_i"] == i + 3
    tp_equiv = (entry_px * 1.006 * (1 - PS)) / (entry_px * (1 + PS)) - 1
    assert t["net_return"] < tp_equiv                 # worse than the TP fill


# ==========================================================================
# DATASET FAIL-CLOSED (verify before ANY research step)
# ==========================================================================

def test_verify_dataset_manifest_missing_and_orchestrator_fail_closed(
        tmp_path, monkeypatch):
    monkeypatch.setattr(BF.CE, "_repo_root", lambda: tmp_path)
    v = BF.verify_dataset("bitget", "BTCUSDT")
    assert v["ok"] is False
    assert v["status"] == "INVALID_DATA_MANIFEST_MISSING"
    out = ORCH.run_edge_discovery(symbol="BTCUSDT", use_ai=False,
                                  write_reports=False, log=lambda *a: None)
    assert out["status"] == "INVALID_DATA_MANIFEST_MISSING"
    assert out["can_send_real_orders"] is False
    # fail-closed means NO funnel artifacts at all
    assert "funnel" not in out and "finalists" not in out


def test_verify_dataset_sha_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(BF.CE, "_repo_root", lambda: tmp_path)
    rows, t_start, t_end = _rows_1m()
    BF.save_dataset("bitget", "BTCUSDT", rows, 3,
                    requested_start_ms=t_start, requested_end_ms=t_end)
    assert BF.verify_dataset("bitget", "BTCUSDT")["status"] == "DATASET_VERIFIED"
    csv_path = BF._contained_path("bitget", "BTCUSDT", ".csv")
    with open(csv_path, "a", encoding="utf-8") as f:
        f.write("tampered,after,save\n")
    v = BF.verify_dataset("bitget", "BTCUSDT")
    assert v["ok"] is False
    assert v["status"] == "INVALID_DATA_SHA_MISMATCH"


def test_gappy_dataset_blocks_and_no_longest_segment_rescue(
        tmp_path, monkeypatch):
    """A dataset with ANY gap is INVALID for research. The old
    longest-contiguous-segment rescue path no longer exists anywhere."""
    monkeypatch.setattr(BF.CE, "_repo_root", lambda: tmp_path)
    rows, t_start, t_end = _rows_1m()
    BF.save_dataset("bitget", "BTCUSDT", rows, 3,
                    requested_start_ms=t_start, requested_end_ms=t_end)
    mpath = BF._contained_path("bitget", "BTCUSDT", "_manifest.json")
    man = json.loads(mpath.read_text(encoding="utf-8"))
    man["gap_count"] = 2                              # adversarial manifest
    mpath.write_text(json.dumps(man), encoding="utf-8")
    v = BF.verify_dataset("bitget", "BTCUSDT")
    assert v["status"] == "INVALID_DATA_GAPS"
    assert not hasattr(ENG, "longest_contiguous_segment")
    src = Path(ORCH.__file__).read_text(encoding="utf-8")
    assert "longest_contiguous_segment" not in src


def test_incomplete_download_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(BF.CE, "_repo_root", lambda: tmp_path)
    rows, t_start, t_end = _rows_1m(drop=set(range(2000, 2010)))
    BF.save_dataset("bitget", "BTCUSDT", rows, 3,
                    requested_start_ms=t_start, requested_end_ms=t_end)
    v = BF.verify_dataset("bitget", "BTCUSDT")
    assert v["ok"] is False
    assert v["status"] == "INVALID_DATA_DOWNLOAD_INCOMPLETE"


def test_raw_quality_fail_blocks_before_resample(tmp_path, monkeypatch):
    """raw_quality_pass=False in the manifest blocks the dataset even when a
    (corrupt or legacy) manifest still claims download_complete=true."""
    monkeypatch.setattr(BF.CE, "_repo_root", lambda: tmp_path)
    rows, t_start, t_end = _rows_1m(bad_ohlc={100})
    m = BF.save_dataset("bitget", "BTCUSDT", rows, 3,
                        requested_start_ms=t_start, requested_end_ms=t_end)
    assert m["raw_quality_pass"] is False             # detected at save time
    mpath = BF._contained_path("bitget", "BTCUSDT", "_manifest.json")
    man = json.loads(mpath.read_text(encoding="utf-8"))
    man["download_complete"] = True                   # adversarial claim
    mpath.write_text(json.dumps(man), encoding="utf-8")
    v = BF.verify_dataset("bitget", "BTCUSDT")
    assert v["status"] == "INVALID_DATA_RAW_QUALITY"


# ==========================================================================
# PHYSICALLY SEALED HOLDOUT + UNFORGEABLE TOKEN
# ==========================================================================

def test_sealed_holdout_blocks_without_or_with_forged_token():
    bars = _bars(600)
    sealed = ENG.SealedHoldout(bars, None, 400, 600)
    assert sealed.descriptor == {"sealed": True, "content": "opaque"}
    with pytest.raises(PermissionError):
        sealed.open(None)
    forged = ENG.HoldoutAccessToken("x", "y", _secret=object())
    with pytest.raises(PermissionError):
        sealed.open(forged)
    # private storage is name-mangled: no public attribute leaks the bars
    public = [a for a in vars(sealed) if not a.startswith("_")]
    assert public == ["descriptor"]


def _good_metrics():
    rng = random.Random(5)                            # aperiodic: no ACF shrink
    trades = [_trade(100 + i * 30, 100 + i * 30 + 10,
                     0.003 + rng.uniform(0.0, 0.002))
              for i in range(30)]
    return ENG.metrics(trades)


def test_execution_proxies_block_holdout_token():
    m = _good_metrics()
    token, reasons = ENG.issue_holdout_token(
        "s1", m, stress_ok=True, data_quality_pass=True,
        baseline_best_lb=0.0001, matched_baseline_ev=0.0001)
    assert token is None
    assert "EXECUTION_PROXIES_BLOCK_HOLDOUT_ACCESS" in reasons


def test_missing_baselines_block_holdout_token():
    m = _good_metrics()
    token, reasons = ENG.issue_holdout_token(
        "s1", m, stress_ok=True, data_quality_pass=True,
        baseline_best_lb=None, matched_baseline_ev=None,
        execution_proxies=())
    assert token is None
    assert "BASELINES_MISSING" in reasons
    assert "MATCHED_BASELINE_MISSING" in reasons


def test_token_granted_without_proxies_opens_sealed_holdout():
    m = _good_metrics()
    token, reasons = ENG.issue_holdout_token(
        "s1", m, stress_ok=True, data_quality_pass=True,
        baseline_best_lb=0.0001, matched_baseline_ev=0.0001,
        execution_proxies=())
    assert reasons == [] and token is not None
    bars = _bars(600)
    sealed = ENG.SealedHoldout(bars, None, 400, 600)
    bars_full, feats_full, _ref, h0, h1 = sealed.open(token)
    assert (h0, h1) == (400, 600)
    assert len(feats_full) == len(bars_full) == 600


def test_funnel_with_no_survivor_reports_zero_holdout_accesses(
        tmp_path, monkeypatch):
    """Even with nothing screened there must be explicit evidence of ZERO
    holdout accesses."""
    monkeypatch.setattr(ENG.CE, "_repo_root", lambda: tmp_path)
    bars = _bars(6000, seed=23)
    feats = ENG.build_features(bars)
    spec = _compile(strategy_id="hopeless",
                    entry_conditions=[{"feature": "ret_1", "op": ">",
                                       "value": 0.5}])   # never fires
    out = ENG.run_funnel(bars, feats, [spec], log=lambda *a: None)
    assert out["holdout_accesses"] == 0
    assert out["validation_survivors"] == 0


def _planted_bars(n=8000, reverse_from=None):
    rng = random.Random(42)
    price, bars = 100.0, []
    for i in range(n):
        if (i % 79) < 3:
            drift = -0.004 if (reverse_from is not None
                               and i >= reverse_from) else 0.004
        else:
            drift = rng.uniform(-0.0012, 0.0012)
        new = price * (1 + drift)
        bars.append({"ts": T0 + i * BAR, "available_at": T0 + i * BAR + BAR,
                     "open": price, "high": max(price, new) * 1.0008,
                     "low": min(price, new) * 0.9992, "close": new,
                     "volume": 10.0, "turnover": 100.0,
                     "symbol": "X", "venue": "bitget"})
        price = new
    return bars


def _planted_spec(sid="planted_cycle_long"):
    _, spec = ENG.compile_strategy(_spec(
        strategy_id=sid,
        entry_conditions=[{"feature": "ret_1", "op": ">", "value": 0.003}],
        stop_policy={"type": "fixed", "value": 0.008},
        take_profit_policy={"type": "fixed", "value": 0.008},
        time_exit=4, cooldown=3), set())
    return spec


def test_unstable_holdout_is_never_promoted(tmp_path, monkeypatch):
    """Edge planted in discovery+validation but REVERSED inside the holdout:
    with a legitimately issued token (proxies waived through the real
    factory), the holdout replay fails its gates and the candidate is NOT
    promoted. Access itself is recorded."""
    monkeypatch.setattr(ENG.CE, "_repo_root", lambda: tmp_path)
    bars = _planted_bars(reverse_from=6200)           # holdout starts at 6440
    feats = ENG.build_features(bars)
    real_issue = ENG.issue_holdout_token

    def waive_proxies(sid, m, stress_ok, dqp, blb, mbe, execution_proxies=None):
        return real_issue(sid, m, stress_ok, dqp, blb, mbe,
                          execution_proxies=())

    monkeypatch.setattr(ENG, "issue_holdout_token", waive_proxies)
    out = ENG.run_funnel(bars, feats, [_planted_spec()], log=lambda *a: None)
    assert out["validation_survivors"] >= 1           # DV edge is real
    assert out["holdout_accesses"] == 1               # token was granted
    fin = out["finalists"][0]
    assert fin["state"] not in ("SHADOW_CANDIDATE_RESEARCH_ONLY",
                                "PAPER_CANDIDATE_RESEARCH_ONLY")


# ==========================================================================
# n_eff AND LOWER BOUNDS
# ==========================================================================

def test_lower_bound_uses_n_eff_not_n_raw():
    trades = [_trade(100 + i * 2, 100 + i * 2 + 10,
                     0.001 * (1 if (i // 2) % 2 else -1) * (1 + i * 0.01))
              for i in range(40)]
    m = ENG.metrics(trades)
    assert m["n_eff"] < m["n_raw"] == 40
    sens = m["lb_sensitivity_n_vs_neff"]
    assert m["net_EV_lower_bound"] == sens["lb_with_n_eff"]
    assert sens["lb_with_n_eff"] < sens["lb_with_n_raw"]
    assert m["overlap_factor"] > 1.0                  # overlap was penalised


def test_identical_returns_cannot_claim_zero_uncertainty():
    trades = [_trade(100 + i * 30, 100 + i * 30 + 10, 0.002)
              for i in range(12)]
    m = ENG.metrics(trades)
    assert m["profit_factor"] == 999.0                # degenerate PF...
    assert m["sd_floor_applied"] is True              # ...gets a variance floor
    assert m["net_EV_lower_bound"] < m["net_EV"]      # never lb == mean
    expected = 0.002 - (1.65 + math.sqrt(math.log(2))) * 0.001 / math.sqrt(
        m["n_eff"])
    assert abs(m["net_EV_lower_bound"] - expected) < 1e-6


# ==========================================================================
# TWO-PHASE GLOBAL MULTIPLE-TESTING REGISTRY
# ==========================================================================

def test_two_phase_registry_same_m_global_across_members(tmp_path, monkeypatch):
    monkeypatch.setattr(ENG.CE, "_repo_root", lambda: tmp_path)
    bars = _planted_bars()
    feats = ENG.build_features(bars)
    seg = ENG.split_indices(len(bars))
    v1 = seg["validation"][1]
    h0, h1 = seg["holdout"]
    sealed = ENG.SealedHoldout(bars, None, h0, h1)
    quiet = lambda *a: None
    stA1 = ENG.run_funnel_phase_a(bars[:v1], feats[:v1],
                                  [_planted_spec("m1")], seg, log=quiet)
    stA2 = ENG.run_funnel_phase_a(bars[:v1], feats[:v1],
                                  [_planted_spec("m2")], seg, log=quiet)
    m_global = stA1["m_partial"] + stA2["m_partial"]
    ENG.registry_append({"kind": "sprint_member", "sprint_id": "sp_test",
                         "run_id": "r1", "m_partial": stA1["m_partial"],
                         "m_global": m_global})
    ENG.registry_append({"kind": "sprint_member", "sprint_id": "sp_test",
                         "run_id": "r2", "m_partial": stA2["m_partial"],
                         "m_global": m_global})
    ENG.registry_close("sp_test", m_global, ["r1", "r2"])
    assert ENG.registry_sha() is not None
    fB1 = ENG.run_funnel_phase_b(stA1, sealed, m_global, log=quiet)
    fB2 = ENG.run_funnel_phase_b(stA2, sealed, m_global, log=quiet)
    for f in (fB1, fB2):
        assert f["m_effective"] == m_global           # SAME total everywhere
        vals = [e for e in f["results"] if e.get("phase") == "validation"]
        assert vals, "planted edge must reach validation"
        for e in vals:
            assert e["n_tests_applied"] == m_global
            assert e["metrics"]["n_tests_applied"] == m_global
    reg = (tmp_path / "reports" / "research" / "v10_45_4_edge_discovery" /
           ENG.REGISTRY_FILE).read_text(encoding="utf-8")
    kinds = [json.loads(x)["kind"] for x in reg.splitlines() if x.strip()]
    assert kinds.count("sprint_member") == 2
    assert kinds.count("sprint_close") == 1


def test_registry_closed_rejects_new_trials(tmp_path, monkeypatch):
    monkeypatch.setattr(ENG.CE, "_repo_root", lambda: tmp_path)
    ENG.registry_append({"kind": "sprint_member", "sprint_id": "sp_x",
                         "run_id": "r1", "m_partial": 10, "m_global": 10})
    ENG.registry_close("sp_x", 10, ["r1"])
    with pytest.raises(ValueError, match="REGISTRY_CLOSED"):
        ENG.registry_append({"kind": "sprint_member", "sprint_id": "sp_x",
                             "run_id": "r_late", "m_partial": 5,
                             "m_global": 15})
    # other sprints stay open
    ENG.registry_append({"kind": "sprint_member", "sprint_id": "sp_y",
                         "run_id": "r9", "m_partial": 3, "m_global": 3})


# ==========================================================================
# EXPOSURE-MATCHED BASELINE v2
# ==========================================================================

def test_baseline_matches_full_hold_distribution_not_median():
    bars = _bars(3000, seed=29)
    spec = _compile(time_exit=25, cooldown=20)
    holds = [5, 10, 20, 40] * 5
    trades = [_trade(300 + i * 60, 300 + i * 60 + holds[i], 0.001)
              for i in range(20)]
    mb = ENG.exposure_matched_baseline(bars, spec, trades, 250, 2900)
    assert mb["status"] == "OK"
    assert mb["hold_distribution_matched"] is True
    assert mb["sessions_matched"] is True
    assert mb["matched_entries"] == 20
    assert mb["mean_placed_per_seed"] > 15            # window is large enough
    for k in ("p25", "p50", "p75", "lower_bound", "sd_across_seeds"):
        assert k in mb
    mb2 = ENG.exposure_matched_baseline(bars, spec, trades, 250, 2900)
    assert mb == mb2                                  # seeded, deterministic


def test_baseline_never_duplicates_entry_timestamps():
    """20 identical-profile trades but only a handful of legal windows: the
    used-set forbids duplicated entry timestamps, so far fewer than 20 get
    placed per seed."""
    bars = _bars(2000, seed=31)
    spec = _compile(time_exit=10, cooldown=20)
    trades = [_trade(300 + (i % 3), 300 + (i % 3) + 10, 0.001)
              for i in range(20)]
    mb = ENG.exposure_matched_baseline(bars, spec, trades, 300, 316)
    assert mb["status"] == "OK"
    assert mb["no_duplicate_timestamps"] is True
    assert mb["mean_placed_per_seed"] <= 5 < mb["matched_entries"]


# ==========================================================================
# PATH SAFETY + ATOMIC WRITES + CSV/MANIFEST CONSISTENCY
# ==========================================================================

def test_safe_atomic_write_verified_and_no_tmp_residue(tmp_path, monkeypatch):
    monkeypatch.setattr(BF.CE, "_repo_root", lambda: tmp_path)
    d = tmp_path / "external_data" / "staging" / "klines_v10_45_4"
    d.mkdir(parents=True)
    p = d / "x.csv"
    data = b"ts,open\n1,2\n"
    sha = BF.safe_atomic_write(p, data)
    assert p.read_bytes() == data
    assert sha == hashlib.sha256(data).hexdigest()
    assert list(d.glob("*.tmp")) == []


def test_safe_atomic_write_rejects_hardlinked_destination(tmp_path, monkeypatch):
    monkeypatch.setattr(BF.CE, "_repo_root", lambda: tmp_path)
    d = tmp_path / "data"
    d.mkdir()
    target = d / "t.json"
    target.write_bytes(b"{}")
    try:
        os.link(target, d / "alias.json")
    except OSError:
        pytest.skip("filesystem without hardlink support")
    with pytest.raises(ValueError, match="hardlink"):
        BF.safe_atomic_write(target, b"new")
    assert target.read_bytes() == b"{}"               # untouched


def test_safe_atomic_write_rejects_target_outside_root(
        tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("repo_root")
    outside = tmp_path_factory.mktemp("outside_root")
    monkeypatch.setattr(BF.CE, "_repo_root", lambda: root)
    with pytest.raises(ValueError, match="escapes"):
        BF.safe_atomic_write(outside / "evil.csv", b"data")
    assert not (outside / "evil.csv").exists()


def test_failure_between_csv_and_manifest_leaves_no_valid_pair(
        tmp_path, monkeypatch):
    monkeypatch.setattr(BF.CE, "_repo_root", lambda: tmp_path)
    rows, t_start, t_end = _rows_1m()
    orig = BF.safe_atomic_write

    def crash_on_manifest(path, data):
        if str(path).endswith("_manifest.json"):
            raise IOError("simulated crash between CSV and manifest")
        return orig(path, data)

    monkeypatch.setattr(BF, "safe_atomic_write", crash_on_manifest)
    with pytest.raises(IOError):
        BF.save_dataset("bitget", "BTCUSDT", rows, 3,
                        requested_start_ms=t_start, requested_end_ms=t_end)
    monkeypatch.setattr(BF, "safe_atomic_write", orig)
    v = BF.verify_dataset("bitget", "BTCUSDT")
    assert v["ok"] is False                           # orphan CSV != valid pair
    assert v["status"] == "INVALID_DATA_MANIFEST_MISSING"


# ==========================================================================
# SANITIZATION (structural, mixed-case, URL-encoded) + SAFE CACHE
# ==========================================================================

def test_sanitize_obj_redacts_short_secrets_and_nested_structures():
    secret_short = "ab" + "c"                         # runtime-built, 3 chars
    obj = {"API-Key": secret_short,
           "Authorization": "Basic " + "dXNlcjpwYXNz",
           "nested": [{"access_token": secret_short, "keep": "fine"},
                      {"Client-Secret": secret_short}],
           "note": "public text"}
    clean = P.sanitize_obj(obj)
    dumped = json.dumps(clean)
    assert secret_short not in dumped.replace("public", "")
    assert clean["API-Key"] == "<redacted>"
    assert clean["Authorization"] == "<redacted>"
    assert clean["nested"][0]["access_token"] == "<redacted>"
    assert clean["nested"][1]["Client-Secret"] == "<redacted>"
    assert clean["note"] == "public text"
    assert clean["nested"][0]["keep"] == "fine"


def test_bearer_basic_mixed_case_redacted():
    tok = "abc.def." + "ghi"
    s1 = P.sanitize_error("header AuThOrIzAtIoN: BeArEr " + tok)
    s2 = P.sanitize_error("using BaSiC " + "dXNlcjpwYXNz" + " for auth")
    assert tok not in s1
    assert "dXNlcjpwYXNz" not in s2


def test_url_encoded_credentials_redacted():
    v1 = "sk" + "_live_" + "abc123"
    s = P.sanitize_error(f"GET /x?api_key%3D{v1}%26user%3Dme&token%3dZZ" + "Z9")
    assert v1 not in s
    assert "ZZZ9" not in s


def test_cache_never_stores_sensitive_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(P.CE, "_repo_root", lambda: tmp_path)
    secret = "sk-" + "live-" + "SECRETVALUE"
    resp = json.dumps({"ok": True, "api_key": secret,
                       "text": "strategies here"})
    P.cache_put("mock", "m1", "prompt-1", resp)
    files = list(tmp_path.rglob("mock_*.json"))
    assert len(files) == 1
    raw = files[0].read_text(encoding="utf-8")
    assert secret not in raw
    assert "<redacted>" in raw
    got = P.cache_get("mock", "m1", "prompt-1")
    assert secret not in got
    assert "strategies here" in got
    # clean JSON stays byte-identical (provenance-exact cache)
    P.cache_put("mock", "m1", "prompt-2", '{"a":1}')
    assert P.cache_get("mock", "m1", "prompt-2") == '{"a":1}'


# ==========================================================================
# RESAMPLING WITH EXPLICIT as_of
# ==========================================================================

def test_last_closed_5m_bucket_kept_only_with_as_of():
    bars = _bars(1440, t0=T0)                         # exactly one day, aligned
    day_end = T0 + 1440 * BAR
    with_asof = ENG.resample_bars(bars, 5, as_of_ms=day_end)
    without = ENG.resample_bars(bars, 5)
    assert len(with_asof) == 288                      # 1440/5, final bucket kept
    assert len(without) == 287                        # unprovable tail dropped
    assert with_asof[-1]["ts"] == day_end - 5 * BAR
    # an OPEN last bucket (as_of before its close) is dropped
    early = ENG.resample_bars(bars, 5, as_of_ms=day_end - BAR)
    assert len(early) == 287
    # 90 completed days at 1m/5m/15m (the tournament contract)
    assert 90 * 1440 == 129_600
    assert 129_600 // 5 == 25_920 and 129_600 // 15 == 8_640


# ==========================================================================
# VERSIONED PROVENANCE SEAL
# ==========================================================================

def test_write_commit_seal_is_fail_closed_and_versioned(tmp_path, monkeypatch):
    monkeypatch.setattr(ENG.CE, "_repo_root", lambda: tmp_path)
    seal = ENG.write_commit_seal(expected_commit="deadbeef" * 5)
    p = (tmp_path / "reports" / "research" / "v10_45_4_edge_discovery" /
         "commit_seal_v10_45_4.json")
    assert p.is_file()
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["match"] is False                  # no git here -> no claim
    for k in ("code_tree_hash", "files", "runner_version", "registry_file",
              "registry_sha256", "dirty_worktree", "tool_version"):
        assert k in on_disk
    assert on_disk["tool_version"] == "v10.45.4"
    assert on_disk["can_send_real_orders"] is False
    assert seal["final_recommendation"].startswith("NO LIVE")
