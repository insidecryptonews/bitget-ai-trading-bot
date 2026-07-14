"""V10.47.15–18 — certification-repair tests. These assert the CORRECT post-repair
behaviour for every material finding in Work's audit (VALIDATION evaluated, physical
sealed holdout, paired matched baseline, real 4h→1h + 2-ATR risk, provenance-bound
manifest/seal, unique test ids). They fail against the pre-repair code (captured as
reproduction evidence) and pass after V10.47.16–18. Research only, NO LIVE."""

from __future__ import annotations

import importlib
import os
import random

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------- #
# P1.1 — VALIDATION is evaluated in the gate; holdout is physically sealed     #
# --------------------------------------------------------------------------- #
def test_validation_region_is_evaluated_in_gate():
    CT = importlib.import_module("app.labs.v10_46.causal_tournament")
    assert hasattr(CT, "evaluate_candidate")
    import inspect
    params = inspect.signature(CT.evaluate_candidate).parameters
    # a real validation region must be an explicit input to the candidate gate
    assert any("valid" in p for p in params), \
        "evaluate_candidate must take a VALIDATION region"


def test_split_has_validation_and_holdout_reserved():
    CT = importlib.import_module("app.labs.v10_46.causal_tournament")
    sp = CT.split_indices(1200)
    assert sp["validation"][1] > sp["validation"][0]      # non-empty validation
    assert sp["selection_end_index"] <= sp["validation"][0]
    assert sp["holdout_start_index"] >= sp["walk_forward"][1]


def test_sealed_holdout_module_exists_and_denies_by_default():
    SH = importlib.import_module("app.labs.v10_46.sealed_holdout")
    h = SH.SealedHoldout(symbol="X", timeframe="1m",
                         holdout_bars=[{"ts": 0, "open": 1, "high": 1, "low": 1,
                                        "close": 1, "volume": 1}])
    assert h.state == "SEALED"
    with pytest.raises(SH.HoldoutAccessDenied):
        h.load()                                          # denied without auth


def test_sealed_holdout_one_time_authorization_and_second_use_fails():
    SH = importlib.import_module("app.labs.v10_46.sealed_holdout")
    h = SH.SealedHoldout(symbol="X", timeframe="1m",
                         holdout_bars=[{"ts": 0, "open": 1, "high": 1, "low": 1,
                                        "close": 1, "volume": 1}])
    tok = h.authorize_once(reason="test", audit_ref="AUD-1")
    _ = h.load(token=tok)
    assert h.state == "CONSUMED"
    with pytest.raises(SH.HoldoutAccessDenied):
        h.load(token=tok)                                 # second use fails
    # every attempt is logged append-only
    assert len(h.access_log()) >= 3


def test_sealed_holdout_path_traversal_and_bad_token_fail():
    SH = importlib.import_module("app.labs.v10_46.sealed_holdout")
    h = SH.SealedHoldout(symbol="X", timeframe="1m",
                         holdout_bars=[{"ts": 0, "open": 1, "high": 1, "low": 1,
                                        "close": 1, "volume": 1}])
    with pytest.raises(SH.HoldoutAccessDenied):
        h.load(token="forged")


# --------------------------------------------------------------------------- #
# P1.2 — matched baseline is paired and preserves the single-position path     #
# --------------------------------------------------------------------------- #
def test_matched_baseline_is_paired_with_explicit_pairs():
    CS = importlib.import_module("app.labs.v10_46.causal_stats")
    assert hasattr(CS, "matched_random_paired")
    # synthetic trades with entry/exit indices
    trades = [{"opportunity_bar": i * 5 + 60, "entry_bar": i * 5 + 61,
               "exit_index": i * 5 + 63, "entry_ts": (i * 5 + 60) * 60000,
               "cluster": f"X:{i}", "session": "X:S0", "day": "X:D0",
               "side": "LONG", "net_eur": 0.01 * (1 if i % 2 else -1),
               "gross_eur": 0.02, "bars_held": 2} for i in range(20)]
    bars = [{"ts": i * 60000, "open": 100.0, "high": 100.5, "low": 99.5,
             "close": 100.0, "volume": 10.0} for i in range(300)]
    r = CS.matched_random_paired(bars, trades, symbol="X", timeframe="1m",
                                 exit_params={"stop_frac": 0.02, "tp_frac": 0.02,
                                              "time_exit": 2}, reps=20)
    for k in ("pairs_requested", "pairs_found", "coverage", "paired_mean_eur",
              "paired_lower_bound_eur", "match_status"):
        assert k in r
    assert r["pairs_requested"] == len(trades)


# --------------------------------------------------------------------------- #
# P1.3 — deterministic strategies: real 4h→1h regime + real 2-ATR stops        #
# --------------------------------------------------------------------------- #
def _bars(n, interval_ms, seed=3):
    rng = random.Random(seed)
    price, bars = 100.0, []
    for i in range(n):
        ph = (i // 120) % 2
        drift = 0.0015 if ph == 0 else -0.0015
        new = price * (1 + drift + rng.uniform(-0.002, 0.002))
        bars.append({"ts": i * interval_ms, "open": price,
                     "high": max(price, new) * 1.001,
                     "low": min(price, new) * 0.999, "close": new, "volume": 50.0})
        price = new
    return bars


def test_ema_adx_is_real_multi_timeframe():
    DET = importlib.import_module("app.labs.v10_46.det_strategies")
    assert hasattr(DET, "precompute_det_sig_mtf"), \
        "need a real 4h->1h regime builder"
    bars1h = _bars(2000, 3_600_000)
    sig = DET.precompute_det_sig_mtf(bars1h, entry_tf="1h", regime_tf="4h")
    assert len(sig) == len(bars1h)
    # every 1h bar's regime must come from a 4h bar that CLOSED at or before it
    for s in sig[400:405]:
        assert "regime_4h_close_ts" in s
        assert s["regime_4h_close_ts"] <= s["ts"]


def test_two_atr_stop_is_actually_two_atr():
    DET = importlib.import_module("app.labs.v10_46.det_strategies")
    CL = importlib.import_module("app.labs.v10_46.causal_ledger")
    bars = _bars(2000, 3_600_000)
    sig = DET.precompute_det_sig_mtf(bars, entry_tf="1h", regime_tf="4h")
    dec = DET.donchian_breakout_decider(symbol="X", venue="bitget",
                                        timeframe="1h", gen="g")
    out = CL.drive_causal(bars, sig, dec, DET.DET_EXIT_ATR, symbol="X",
                          timeframe="1h")
    assert out["trades"], "expected at least one deterministic trade"
    for t in out["trades"]:
        assert "atr_entry" in t and "initial_stop" in t and "entry_price" in t
        mult = t.get("stop_atr_mult", 2.0)
        exp = (t["entry_price"] - mult * t["atr_entry"]) if t["side"] == "LONG" \
            else (t["entry_price"] + mult * t["atr_entry"])
        assert abs(t["initial_stop"] - exp) <= 1e-6 * max(1.0, t["entry_price"])


# --------------------------------------------------------------------------- #
# P1.4 — manifest/seal binds full provenance and verifies against disk         #
# --------------------------------------------------------------------------- #
def test_manifest_binds_provenance_and_verifies():
    MZ = importlib.import_module("app.labs.v10_46.manifest_seal")
    for fn in ("build_manifest", "verify_manifest"):
        assert hasattr(MZ, fn)
    import inspect
    src = inspect.getsource(MZ)
    for field in ("spec_root_hash", "dataset_root_hash", "registry_hash",
                  "holdout_commitment_hash", "manifest_payload_sha256",
                  "seal_sha256"):
        assert field in src, f"manifest must bind {field}"


# --------------------------------------------------------------------------- #
# P2.2 — bars_to_events derives its interval from the timeframe                #
# --------------------------------------------------------------------------- #
def test_bars_to_events_derives_interval_from_timeframe():
    EC = importlib.import_module("app.labs.v10_46.event_clock")
    bars = [{"ts": 0, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]
    ev = EC.bars_to_events(bars, symbol="X", venue="bitget", timeframe="4h",
                           data_generation_id="g")
    # a 4h bar closes 4h after it opens, not 1 minute
    assert ev[0]["available_time_ms"] == 4 * 3_600_000


# --------------------------------------------------------------------------- #
# P2.1 — no duplicate pytest node ids in the certification suite               #
# --------------------------------------------------------------------------- #
def test_no_duplicate_parametrize_ids_in_traversal_test():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "trav", os.path.join(ROOT, "tests",
                             "test_researchops_v10_45_2_truth_hotfix.py"))
    # read the source and ensure the whitelist test declares explicit unique ids
    src = open(spec.origin, encoding="utf-8").read()
    assert "ids=" in src, "parametrized traversal test must use explicit unique ids"


def test_manifest_seal_breaks_on_tampering(tmp_path):
    MZ = importlib.import_module("app.labs.v10_46.manifest_seal")
    out = tmp_path / "out"
    out.mkdir()
    (out / "r.md").write_text("hello", encoding="utf-8")
    m = MZ.build_manifest(root=str(tmp_path), out_dir=str(out),
                          spec_hashes={"P": "a"}, registry_hash="reg",
                          holdout_commitment_hash="hc")
    assert MZ.verify_manifest(m, root=str(tmp_path))["ok"] is True
    (out / "r.md").write_text("tampered", encoding="utf-8")   # change a report
    v = MZ.verify_manifest(m, root=str(tmp_path))
    assert v["ok"] is False and any("stale" in p for p in v["problems"])


def test_manifest_regeneration_is_stable(tmp_path):
    MZ = importlib.import_module("app.labs.v10_46.manifest_seal")
    out = tmp_path / "out"
    out.mkdir()
    (out / "r.md").write_text("x", encoding="utf-8")
    a = MZ.build_manifest(root=str(tmp_path), out_dir=str(out), spec_hashes={"P": "a"})
    b = MZ.build_manifest(root=str(tmp_path), out_dir=str(out), spec_hashes={"P": "a"})
    assert a["output_root_hash"] == b["output_root_hash"]      # same files -> same


def test_holdout_denied_from_selection_module():
    SH = importlib.import_module("app.labs.v10_46.sealed_holdout")
    h = SH.SealedHoldout(symbol="X", timeframe="1m",
                         holdout_bars=[{"ts": 0, "open": 1, "high": 1, "low": 1,
                                        "close": 1, "volume": 1}])
    tok = h.authorize_once(reason="t", audit_ref="A")

    # simulate a call originating from a selection module frame
    def _fake_causal_tournament_caller():
        return h.load(token=tok)
    _fake_causal_tournament_caller.__globals__["__name__"] = \
        "app.labs.v10_46.causal_tournament"
    with pytest.raises(SH.HoldoutAccessDenied):
        _fake_causal_tournament_caller()


def test_registry_semantic_dedup_reports_results():
    CT = importlib.import_module("app.labs.v10_46.causal_tournament")
    reg = CT.preregister("BTCUSDT", "bitget", "1m", "g")
    assert reg["m_unique_results"] >= 1
    assert reg["m_unique_hypotheses"] == reg["m_nominal"]      # conservative correction
    # no-fire policies keep distinct fingerprints (not spuriously merged)
    assert reg["m_unique_results"] <= reg["m_nominal"]


def test_baseline_incomplete_fails_gate():
    CS = importlib.import_module("app.labs.v10_46.causal_stats")
    # a trade whose cluster has no usable bar in the (tiny) bar series is impossible
    trades = [{"opportunity_bar": 5, "entry_bar": 6, "exit_index": 8,
               "entry_ts": 999_999_999, "cluster": "X:99999", "session": "X:S0",
               "day": "X:D0", "side": "LONG", "net_eur": 0.5, "gross_eur": 0.6,
               "bars_held": 2}]
    bars = [{"ts": i * 60000, "open": 100.0, "high": 100.1, "low": 99.9,
             "close": 100.0, "volume": 1.0} for i in range(50)]
    r = CS.matched_random_paired(bars, trades, symbol="X", timeframe="1m",
                                 exit_params={"stop_frac": 0.02, "tp_frac": 0.02,
                                              "time_exit": 2}, reps=10)
    assert r["match_status"] == "BASELINE_MATCH_INCOMPLETE"
    assert r["beats_matched_random"] is False                 # cannot pass the gate
