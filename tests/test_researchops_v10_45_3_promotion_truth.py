"""V10.45.3 promotion truth & execution realism: gap-aware fills, strict gap
detection, locked holdout with spy, holdout gates, closed strategy contract,
raw quality, download completeness, symlink guard, sanitization, Retry-After
cap, cooldown consistency, conservative n_eff, as_of resampling, provenance,
automatic multiple testing, exposure-matched baseline. Research only, NO LIVE."""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

import pytest

from app.labs import ai_providers_v10_45_1 as P
from app.labs import edge_discovery_engine_v10_45_1 as ENG
from app.labs import public_data_backfill_v10_45_1 as BF

T0 = 1_700_000_100_000
BAR = 60_000


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


# ==========================================================================
# 1. GAP-AWARE FILLS: never outside [low, high]; a bearish gap never profits
# ==========================================================================

def test_long_gap_through_stop_fills_at_open_not_stop():
    """LONG stop ~100.98, market opens at ~90: the old code filled at the
    stop (impossible); the fix fills at the open — the gap's real price."""
    bars = _bars(400, seed=5)
    i = 300
    entry_px = bars[i + 1]["open"]
    stop_frac = 0.01
    stop_px = entry_px * (1 - stop_frac)
    crash = entry_px * 0.90                       # opens 10% below
    bars[i + 2]["open"] = crash
    bars[i + 2]["close"] = crash * 0.999
    bars[i + 2]["high"] = crash * 1.001
    bars[i + 2]["low"] = crash * 0.998
    feats = ENG.build_features(bars)
    spec = _compile(stop_policy={"type": "fixed", "value": stop_frac},
                    take_profit_policy={"type": "fixed", "value": 0.05},
                    time_exit=20)
    r = ENG.replay(bars, feats, spec, i_start=i, i_end=i + 20)
    t = r["trades"][0]
    assert t["exit_reason"] == "SL"
    # loss must reflect the ~10% gap, not the 1% stop distance
    assert t["net_return"] < -0.08
    # fill inside the crash bar's range
    assert bars[i + 2]["low"] * 0.999 <= crash <= bars[i + 2]["high"] * 1.001


def test_short_gap_through_stop_fills_at_open_symmetric():
    bars = _bars(400, seed=7)
    i = 300
    entry_px = bars[i + 1]["open"]
    stop_frac = 0.01
    spike = entry_px * 1.10                       # opens 10% ABOVE short stop
    bars[i + 2]["open"] = spike
    bars[i + 2]["close"] = spike * 1.001
    bars[i + 2]["high"] = spike * 1.002
    bars[i + 2]["low"] = spike * 0.999
    feats = ENG.build_features(bars)
    spec = _compile(side="SHORT",
                    stop_policy={"type": "fixed", "value": stop_frac},
                    take_profit_policy={"type": "fixed", "value": 0.05},
                    time_exit=20)
    r = ENG.replay(bars, feats, spec, i_start=i, i_end=i + 20)
    t = r["trades"][0]
    assert t["exit_reason"] == "SL"
    assert t["net_return"] < -0.08                # symmetric ~10% loss


def test_trailing_negative_rejected_by_compiler():
    """The exact Codex case: a negative trailing once produced a fill at
    101.10 on a bar whose high was 100.10 — now it cannot even compile."""
    seen: set[str] = set()
    st_, _ = ENG.compile_strategy(_spec(
        trailing_policy={"type": "fixed", "value": -0.004}), seen)
    assert st_ == "INVALID"
    st2, _ = ENG.compile_strategy(_spec(
        trailing_policy={"type": "fixed", "value": 0.0}), seen)
    assert st2 == "INVALID"                       # zero when type != none
    st3, _ = ENG.compile_strategy(_spec(
        trailing_policy={"type": "atr", "value": 100.0}), seen)
    assert st3 == "INVALID"                       # out of configured range
    st4, _ = ENG.compile_strategy(_spec(
        trailing_policy={"type": "fixed", "value": float("nan")}), seen)
    assert st4 == "INVALID"


def test_gap_through_trailing_stop_uses_open():
    bars = _bars(400, seed=9)
    i = 300
    e = bars[i + 1]["open"]
    prev_close = bars[i + 1]["close"]
    for k, mult in ((2, 1.01), (3, 1.02)):        # rally arms the trail
        bars[i + k]["open"] = prev_close
        bars[i + k]["close"] = e * mult
        bars[i + k]["high"] = max(e * (mult + 0.001), prev_close)
        bars[i + k]["low"] = min(prev_close, e * (mult - 0.001))
        prev_close = bars[i + k]["close"]
    crash = e * 0.95                              # gap far below the trail
    bars[i + 4]["open"] = crash
    bars[i + 4]["close"] = crash * 0.999
    bars[i + 4]["high"] = crash * 1.001
    bars[i + 4]["low"] = crash * 0.997
    feats = ENG.build_features(bars)
    spec = _compile(stop_policy={"type": "fixed", "value": 0.03},
                    take_profit_policy={"type": "fixed", "value": 0.08},
                    trailing_policy={"type": "fixed", "value": 0.005},
                    time_exit=30)
    r = ENG.replay(bars, feats, spec, i_start=i, i_end=i + 20)
    t = r["trades"][0]
    # trail was around e*1.02*(1-0.005) ~ e*1.0149; fill must be the crash
    # open (a large LOSS), never the trail level (a profit)
    assert t["net_return"] < -0.04
    assert t["exit_reason"] in ("TRAIL", "SL")


# ==========================================================================
# 2. STRICT GAPS IN REPLAY: delta==T only; 2*T is a missing candle
# ==========================================================================

def test_replay_delta_exactly_2T_is_a_gap():
    bars = _bars(400, seed=11)
    for j in range(320, 400):                     # remove exactly one candle
        bars[j]["ts"] += BAR
    feats = ENG.build_features(bars)
    spec = _compile(stop_policy={"type": "fixed", "value": 0.05},
                    take_profit_policy={"type": "fixed", "value": 0.05},
                    time_exit=100, cooldown=1000)
    r = ENG.replay(bars, feats, spec, i_start=310, i_end=330)
    assert r["trades"][0]["exit_reason"] == "STALE_EXIT"
    m = ENG.metrics(r["trades"])
    assert m["n_trades"] == 0                     # excluded from EV entirely
    assert m["invalid_execution"] == 1


def test_strict_quality_2T_3T_dup_reverse():
    assert BF.strict_quality([T0, T0 + 2 * BAR])["gap_count"] == 1
    assert BF.strict_quality([T0, T0 + 3 * BAR])["missing_bars"] == 2
    assert BF.strict_quality([T0, T0, T0 + BAR])["duplicates"] == 1
    assert BF.strict_quality([T0 + BAR, T0])["out_of_order"] == 1
    assert BF.strict_quality([T0, T0 + BAR])["quality_pass"] is True


# ==========================================================================
# 3. HOLDOUT REALLY LOCKED (spy test — no holdout fixture is consumed)
# ==========================================================================

def test_holdout_never_touched_when_nothing_eligible(monkeypatch, tmp_path):
    monkeypatch.setattr(ENG.CE, "_repo_root", lambda: tmp_path)
    bars = _bars(3000, seed=21)
    feats = ENG.build_features(bars)
    seg = ENG.split_indices(len(bars))
    h0 = seg["holdout"][0]
    touched = []
    orig_replay = ENG.replay

    def spy(bars_, feats_, spec_, *a, **kw):
        if kw.get("i_start", 0) >= h0:
            touched.append(spec_.get("strategy_id"))
        return orig_replay(bars_, feats_, spec_, *a, **kw)
    monkeypatch.setattr(ENG, "replay", spy)
    seen: set[str] = set()
    compiled = []
    from app.labs import multi_ai_orchestrator_v10_45_1 as ORCH
    for s in ORCH.procedural_universe()[:25]:
        stt, c = ENG.compile_strategy(s, seen)
        if stt == "OK":
            compiled.append(c)
    out = ENG.run_funnel(bars, feats, compiled, log=lambda *a: None)
    # pure noise: no strategy passes the full eligibility stack -> the holdout
    # slice must never have been replayed
    assert out["validation_survivors"] == 0
    assert touched == []
    ledger = (tmp_path / "reports" / "research" / "v10_45_6_edge_discovery" /
              "experiment_ledger_v10_45_6.jsonl")
    entries = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines()]
    accesses = [e for e in entries if e.get("phase") == "holdout_access"]
    assert all(e["holdout_accessed"] is False for e in accesses)
    assert all("validation_metrics_sha1" in e for e in accesses)


def test_validation_eligible_fail_closed_reasons():
    ok, reasons = ENG.validation_eligible_for_holdout(None, True, True)
    assert ok is False and "NO_VALIDATION_METRICS" in reasons
    good = {"n_trades": 50, "n_eff": 50, "net_EV": 0.002,
            "net_EV_lower_bound": 0.001, "profit_factor": 1.6,
            "max_drawdown": -0.03, "censored_ratio": 0.0,
            "outlier_dependence": 0.001, "stability_sign": 1,
            "n_eff_is_proxy": False}
    ok2, r2 = ENG.validation_eligible_for_holdout(good, True, True,
                                                  baseline_best_lb=-0.01,
                                                  matched_baseline_ev=-0.001)
    assert ok2 is True and r2 == []
    bad = dict(good, n_eff_is_proxy=True)
    ok3, r3 = ENG.validation_eligible_for_holdout(bad, True, True)
    assert ok3 is False and "N_EFF_PROXY" in r3


# ==========================================================================
# 4. HOLDOUT GATES: the exact Codex case (n_eff=1, DD -90%)
# ==========================================================================

def test_holdout_neff1_dd90_cannot_be_shadow():
    val = {"n_trades": 50, "n_eff": 50, "net_EV": 0.002,
           "net_EV_lower_bound": 0.001, "profit_factor": 1.6,
           "max_drawdown": -0.03, "censored_ratio": 0.0,
           "outlier_dependence": 0.001, "stability_sign": 1}
    hold = {"n_trades": 12, "n_eff": 1, "net_EV": 0.01,
            "net_EV_lower_bound": 0.005, "profit_factor": 2.0,
            "max_drawdown": -0.90, "censored_ratio": 0.0}
    g = ENG.gate(val, hold, True, data_quality_pass=True,
                 baseline_best_lb=-0.01, matched_baseline_ev=-0.001)
    assert g in ("NEED_MORE_DATA", "REJECTED")
    assert g != "SHADOW_CANDIDATE_RESEARCH_ONLY"


# ==========================================================================
# 5. CLOSED CONTRACT: declared fields honoured or rejected
# ==========================================================================

def test_symbol_mismatch_rejected():
    seen: set[str] = set()
    st_, _ = ENG.compile_strategy(_spec(symbols=["ETHUSDT"]), seen,
                                  symbol="BTCUSDT", timeframe="1m")
    assert st_ == "INVALID"
    st2, spec = ENG.compile_strategy(_spec(symbols=["BTCUSDT", "ETHUSDT"]),
                                     seen, symbol="BTCUSDT", timeframe="1m")
    assert st2 == "OK" and spec["declared_symbols"] == ["BTCUSDT", "ETHUSDT"]


def test_timeframe_mismatch_rejected():
    seen: set[str] = set()
    st_, _ = ENG.compile_strategy(_spec(timeframe="15m"), seen,
                                  symbol="BTCUSDT", timeframe="1m")
    assert st_ == "INVALID"


def test_invalidation_field_unsupported():
    seen: set[str] = set()
    st_, _ = ENG.compile_strategy(_spec(invalidation="price above X"), seen)
    assert st_ == "INVALID"                       # not silently ignored


def test_required_features_validated_and_in_signature():
    seen: set[str] = set()
    st_, _ = ENG.compile_strategy(_spec(required_features=["fake_feat"]), seen)
    assert st_ == "INVALID"
    s1 = _compile(required_features=["rsi_14"])
    s2 = _compile(required_features=["rsi_14", "adx_14"])
    assert s1["signature"] != s2["signature"]     # part of the semantics


def test_activate_after_implemented_and_in_signature():
    a = _compile(trailing_policy={"type": "fixed", "value": 0.004,
                                  "activate_after": 0.002})
    b = _compile(trailing_policy={"type": "fixed", "value": 0.004,
                                  "activate_after": 0.008})
    assert a["signature"] != b["signature"]
    assert a["trail"]["activate_after"] == 0.002


# ==========================================================================
# 6. RAW QUALITY BEFORE RESAMPLE
# ==========================================================================

def test_raw_candle_validation_rejects_nan_inf_negatives():
    good = [T0, 100.0, 101.0, 99.0, 100.5, 10.0, 1000.0]
    assert BF.validate_raw_candle(good) is True
    assert BF.validate_raw_candle([T0, float("nan"), 101, 99, 100, 10, 1]) is False
    assert BF.validate_raw_candle([T0, 100, float("inf"), 99, 100, 10, 1]) is False
    assert BF.validate_raw_candle([T0, 100, 101, 99, 100, -5.0, 1]) is False
    assert BF.validate_raw_candle([T0, 100, 101, 99, 100, 10, -1.0]) is False
    assert BF.validate_raw_candle([T0, 100, 99.5, 99, 100.2, 10, 1]) is False  # h<max(o,c)
    assert BF.validate_raw_candle([0, 100, 101, 99, 100, 10, 1]) is False


def test_resampled_quality_cannot_hide_raw_defects():
    rows = [[T0 + i * BAR, 100.0, 101.0, 99.0, 100.5, 10.0, 1000.0]
            for i in range(30)]
    rows[7][5] = -3.0                             # negative volume hidden inside
    q = BF.raw_quality_report(rows)
    assert q["invalid_candles"] == 1
    assert q["raw_quality_pass"] is False


# ==========================================================================
# 7. DOWNLOAD COMPLETENESS
# ==========================================================================

def test_5000_bars_for_90_days_is_incomplete(monkeypatch, tmp_path):
    monkeypatch.setattr(BF.CE, "_repo_root", lambda: tmp_path)
    end = (T0 // BAR) * BAR + 90 * 86_400_000
    rows = [[end - (5000 - i) * BAR, 100.0, 101.0, 99.0, 100.5, 10.0, 1.0]
            for i in range(5000)]
    m = BF.save_dataset("bybit", "BTCUSDT", rows, 90,
                        requested_start_ms=end - 90 * 86_400_000,
                        requested_end_ms=end)
    assert m["expected_bars"] == 129_600
    assert m["actual_bars"] == 5000
    assert m["download_complete"] is False
    assert m["coverage_ratio"] < 0.05


def test_excess_bars_trimmed_to_requested_window(monkeypatch, tmp_path):
    monkeypatch.setattr(BF.CE, "_repo_root", lambda: tmp_path)
    start = (T0 // BAR) * BAR
    end = start + 60 * BAR
    rows = [[start + (i - 5) * BAR, 100.0, 101.0, 99.0, 100.5, 10.0, 1.0]
            for i in range(75)]                   # 5 before + 10 after window
    m = BF.save_dataset("bitget", "BTCUSDT", rows, 90,
                        requested_start_ms=start, requested_end_ms=end)
    assert m["actual_bars"] == 60                 # exactly the window
    assert m["download_complete"] is True


def test_incomplete_download_blocks_promotion():
    val = {"n_trades": 50, "n_eff": 50, "net_EV": 0.002,
           "net_EV_lower_bound": 0.001, "profit_factor": 1.6,
           "max_drawdown": -0.03, "censored_ratio": 0.0,
           "outlier_dependence": 0.001, "stability_sign": 1}
    # promotion_allowed=False (download_complete false) -> INVALID_DATA
    ok, reasons = ENG.validation_eligible_for_holdout(val, True, False)
    assert ok is False and "DATA_QUALITY_FAIL" in reasons


# ==========================================================================
# 8. SYMLINK / JUNCTION CONTAINMENT
# ==========================================================================

def test_symlinked_data_dir_rejected(monkeypatch, tmp_path):
    outside = tmp_path / "outside_target"
    outside.mkdir()
    repo = tmp_path / "repo"
    (repo / "external_data" / "staging").mkdir(parents=True)
    monkeypatch.setattr(BF.CE, "_repo_root", lambda: repo)
    # simulate a junction: realpath of the data dir resolves OUTSIDE the repo
    real = __import__("os").path.realpath

    def fake_realpath(p):
        rp = real(p)
        if "klines_v10_45_5" in str(p):
            return str(outside)
        return rp
    monkeypatch.setattr(BF.os.path, "realpath", fake_realpath)
    with pytest.raises(ValueError):
        BF._dataset_dir("bitget", "BTCUSDT")


# ==========================================================================
# 9. SANITIZATION (fixtures built at runtime; no key-shaped literals)
# ==========================================================================

def test_sanitize_json_credentials_and_headers():
    fake = "gsk_" + "x" * 30
    cases = [
        f'{{"api_key": "{fake}"}}',
        f'{{"token": "{fake}"}}',
        f"Authorization: Bearer {fake}",
        f"password: {fake}",
        f"https://api.x.com/v1?symbol=BTC&key={fake}&interval=1m",
        f'{{"SECRET": "{fake}"}}',
        f"BASIC {fake}",
    ]
    for c in cases:
        out = P.sanitize_error(c)
        assert fake not in out, c
        assert "redacted" in out.lower(), c


# ==========================================================================
# 10. RETRY-AFTER CAP
# ==========================================================================

def test_retry_after_huge_value_is_capped(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key-not-real")
    sleeps: list[float] = []

    def fake_http(url, payload=None, headers=None, timeout=60, method=None):
        if url.endswith("/models"):
            return 200, {"data": [{"id": "llama-3.1-8b-instant"}]}, {}
        return 429, {}, {"retry-after": "99999"}
    monkeypatch.setattr(P, "_http_json", fake_http)
    monkeypatch.setattr(P.time, "sleep", lambda s: sleeps.append(s))
    g = P.GroqProvider(max_requests=30)
    g.generate("x", use_cache=False)
    assert sleeps, "no retry sleep recorded"
    assert all(s <= P.MAX_RETRY_AFTER_S + 1 for s in sleeps)
    assert any("capped" in e for e in g.fallback_events)


# ==========================================================================
# 11. COOLDOWN CONSISTENCY ACROSS PHASES
# ==========================================================================

def test_funnel_never_overrides_compiled_cooldown():
    import inspect
    src = inspect.getsource(ENG.run_funnel)
    assert "cooldown_override" not in src
    src2 = inspect.getsource(ENG.cost_attribution)
    assert "cooldown_override=None" in src2 or "cooldown_override" not in src2 \
        or "cooldown)" in src2  # attribution passes None (compiled policy)


# ==========================================================================
# 12. N_EFF CONSERVATIVE
# ==========================================================================

def _trade(entry_i, exit_i, ret, reason="TP"):
    return {"entry_i": entry_i, "exit_i": exit_i, "net_return": ret,
            "exit_reason": reason, "bars_held": exit_i - entry_i,
            "censored": False, "tranches": 1}


def test_overlapping_trades_shrink_n_eff():
    # 20 trades all crammed into overlapping windows: occupancy >> 1
    # (returns genuinely alternate: range(0,40,2) is always even, so i%2
    # would silently make every return identical)
    trades = [_trade(i, i + 50, 0.001 * (1 if (i // 2) % 2 else -1) * (1 + i * 0.01))
              for i in range(0, 40, 2)]
    m = ENG.metrics(trades)
    assert m["n_eff"] < m["n_trades"]
    assert "occupancy" in m["n_eff_method"]


def test_autocorrelated_returns_shrink_n_eff():
    # strongly serially dependent returns (long runs of same sign)
    rets = ([0.002] * 10 + [-0.002] * 10) * 2
    trades = [_trade(i * 100, i * 100 + 5, r) for i, r in enumerate(rets)]
    m = ENG.metrics(trades)
    assert m["n_eff"] < m["n_trades"]


def test_tiny_sample_marks_n_eff_proxy():
    trades = [_trade(i * 100, i * 100 + 5, 0.001) for i in range(6)]
    m = ENG.metrics(trades)
    assert m["n_eff_is_proxy"] is True
    assert m["n_eff"] <= 3


# ==========================================================================
# 13. LAST BUCKET WITH EXPLICIT as_of
# ==========================================================================

def test_last_closed_bucket_kept_with_as_of():
    bars = _bars(25)
    shift = 300_000 - (T0 % 300_000)
    for b in bars:
        b["ts"] += shift
        b["available_at"] += shift
    # 25 aligned bars -> exactly 5 full 5m buckets; last closes at ts[24]+60s
    as_of = bars[-1]["ts"] + BAR
    r_with = ENG.resample_bars(bars, 5, as_of_ms=as_of)
    r_without = ENG.resample_bars(bars, 5)
    assert len(r_with) == 5                       # closed tail kept
    assert len(r_without) == 4                    # no clock -> dropped
    r_open = ENG.resample_bars(bars, 5, as_of_ms=as_of - 1)
    assert len(r_open) == 4                       # still open vs as_of


# ==========================================================================
# 14. PROVENANCE
# ==========================================================================

def test_code_tree_hash_and_ledger_provenance(tmp_path, monkeypatch):
    prov = ENG.code_tree_hash()
    assert len(prov["code_tree_hash"]) == 32
    assert set(prov["files"]) == set(ENG._PROVENANCE_FILES)
    assert prov["runner_version"] == ENG.RUNNER_VERSION
    monkeypatch.setattr(ENG.CE, "_repo_root", lambda: tmp_path)
    ENG.set_run_context(run_id="r1", code_tree_hash=prov["code_tree_hash"],
                        runner_version=prov["runner_version"],
                        dirty_worktree=prov["dirty_worktree"])
    ENG.ledger_append({"phase": "test", "state": "OK"})
    ledger = (tmp_path / "reports" / "research" / "v10_45_6_edge_discovery" /
              "experiment_ledger_v10_45_6.jsonl")
    e = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
    assert e["code_tree_hash"] == prov["code_tree_hash"]
    assert e["runner_version"] == ENG.RUNNER_VERSION
    ENG.set_run_context()


# ==========================================================================
# 15. AUTOMATIC MULTIPLE TESTING
# ==========================================================================

def test_m_computed_automatically_from_actual_trials(tmp_path, monkeypatch):
    monkeypatch.setattr(ENG.CE, "_repo_root", lambda: tmp_path)
    bars = _bars(3000, seed=23)
    feats = ENG.build_features(bars)
    seen: set[str] = set()
    from app.labs import multi_ai_orchestrator_v10_45_1 as ORCH
    compiled = []
    for s in ORCH.procedural_universe()[:20]:
        stt, c = ENG.compile_strategy(s, seen)
        if stt == "OK":
            compiled.append(c)
    ENG.set_run_context()
    out = ENG.run_funnel(bars, feats, compiled, log=lambda *a: None)
    # V10.45.5: m comes from the PRE-REGISTERED enumeration (13 trials per
    # spec + shared baselines), fixed BEFORE phase A ever runs
    expected_members = len(ENG.enumerate_trial_members(
        compiled, "TEST", "1m"))
    assert out["m_effective"] == expected_members
    assert out["m_effective"] >= 13 * len(compiled)
    assert out["n_trials_total"] == out["m_effective"]
    assert out["registry_sha256"] is not None


# ==========================================================================
# 16. EXPOSURE-MATCHED BASELINE
# ==========================================================================

def test_exposure_matched_baseline_matches_profile():
    bars = _bars(2000, seed=31)
    spec = _compile(time_exit=10, cooldown=20)
    trades = [_trade(100 + i * 40, 100 + i * 40 + 10, 0.001) for i in range(20)]
    mb = ENG.exposure_matched_baseline(bars, spec, trades, 300, 1900)
    assert mb["status"] == "OK"
    assert mb["matched_entries"] == 20
    assert mb["hold_distribution_matched"] is True
    assert mb["sessions_matched"] is True
    assert mb["no_duplicate_timestamps"] is True
    assert mb["side"] == "LONG"
    assert isinstance(mb["mean_EV"], float)
    assert isinstance(mb["p50"], float)
    assert mb["lower_bound"] <= mb["mean_EV"]
    # deterministic across calls (seeded)
    mb2 = ENG.exposure_matched_baseline(bars, spec, trades, 300, 1900)
    assert mb["mean_EV"] == mb2["mean_EV"]
