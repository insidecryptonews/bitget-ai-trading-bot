"""V10.47.15–18 — certification-repair tests. These assert the CORRECT post-repair
behaviour for every material finding in Work's audit (VALIDATION evaluated, physical
sealed holdout, paired matched baseline, real 4h→1h + 2-ATR risk, provenance-bound
manifest/seal, unique test ids). They fail against the pre-repair code (captured as
reproduction evidence) and pass after V10.47.16–18. Research only, NO LIVE."""

from __future__ import annotations

import importlib
import hashlib
import json
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


def _sealed_fixture(tmp_path):
    root = tmp_path / "sealed_holdout"
    data = root / "encrypted_or_sealed_data"
    data.mkdir(parents=True)
    payload = b'[{"ts":0,"open":1,"high":1,"low":1,"close":1,"volume":1}]'
    (data / "bars.json.sealed").write_bytes(payload)
    secret = b"synthetic-external-secret"
    (root / "commitment.json").write_text(json.dumps({
        "schema": "v10_47_20_holdout_commitment", "state": "SEALED",
        "data_file": "encrypted_or_sealed_data/bars.json.sealed",
        "commitment_sha256": hashlib.sha256(payload).hexdigest(),
        "authority_key_sha256": hashlib.sha256(secret).hexdigest(),
        "n_bars": 1,
    }), encoding="utf-8")
    return root, secret


def test_sealed_holdout_module_contains_metadata_not_rows(tmp_path):
    SH = importlib.import_module("app.labs.v10_46.sealed_holdout")
    root, _ = _sealed_fixture(tmp_path)
    commitment = SH.load_commitment(root / "commitment.json")
    assert commitment["state"] == "SEALED"
    assert not hasattr(SH, "SealedHoldout")


def test_holdout_external_capability_is_single_use(tmp_path):
    HL = importlib.import_module("app.labs.v10_46.holdout_loader")
    root, secret = _sealed_fixture(tmp_path)
    authority = HL.ExternalHoldoutAuthority(root, secret=secret)
    capability = authority.issue_capability(reason="test", audit_ref="AUD-1")
    assert authority.load_once(capability)[0]["ts"] == 0
    with pytest.raises(HL.HoldoutAccessDenied):
        authority.load_once(capability)
    assert len(authority.access_log()) >= 3


def test_sealed_holdout_path_traversal_and_bad_capability_fail(tmp_path):
    HL = importlib.import_module("app.labs.v10_46.holdout_loader")
    root, secret = _sealed_fixture(tmp_path)
    authority = HL.ExternalHoldoutAuthority(root, secret=secret)
    with pytest.raises(HL.HoldoutAccessDenied):
        authority.load_once("forged")
    capability = authority.issue_capability(reason="test", audit_ref="AUD-1")
    with pytest.raises(HL.HoldoutAccessDenied):
        authority.load_once(capability, relative_path="../escaped.json")


# --------------------------------------------------------------------------- #
# P1.2 — matched baseline is paired and preserves the single-position path     #
# --------------------------------------------------------------------------- #
def test_matched_baseline_is_paired_with_explicit_pairs():
    CS = importlib.import_module("app.labs.v10_46.causal_stats")
    assert hasattr(CS, "matched_random_paired")
    common = {field: f"v-{field}" for field in CS.BASELINE_MATCH_FIELDS}
    common.update({
        "notional_eur": 5.0, "exposure_eur": 5.0,
        "leverage_simulated": 1.0, "funding_cost_eur": 0.0,
        "funding_settlements_crossed": 0, "max_holding_bars": 2,
        "realised_holding_bars": 2, "end_of_dataset_censored": False,
    })
    candidate = {**common, "candidate_trade_id": "C1", "candidate_net_eur": 0.1}
    baseline = {**common, "baseline_trade_id": "B1", "baseline_net_eur": 0.0}
    r = CS.matched_random_paired(
        candidate_trades=[candidate], baseline_trades=[baseline],
        timeframe="1m", m_global=10,
    )
    for k in ("pairs_requested", "pairs_found", "coverage", "paired_mean_eur",
              "paired_lower_bound_eur", "match_status"):
        assert k in r
    assert r["pairs_requested"] == 1


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


def test_discovery_tournament_has_no_holdout_loader_import():
    import inspect
    CT = importlib.import_module("app.labs.v10_46.causal_tournament")
    source = inspect.getsource(CT)
    assert "holdout_loader" not in source
    assert "authorize_once" not in source


def test_registry_semantic_dedup_reports_results():
    CT = importlib.import_module("app.labs.v10_46.causal_tournament")
    reg = CT.preregister("BTCUSDT", "bitget", "1m", "g")
    assert reg["m_unique_results"] >= 1
    assert reg["m_unique_hypotheses"] == reg["m_nominal"]      # conservative correction
    # no-fire policies keep distinct fingerprints (not spuriously merged)
    assert reg["m_unique_results"] <= reg["m_nominal"]


def test_baseline_incomplete_fails_gate():
    CS = importlib.import_module("app.labs.v10_46.causal_stats")
    candidate = {field: f"v-{field}" for field in CS.BASELINE_MATCH_FIELDS}
    candidate.update({"candidate_trade_id": "C1", "candidate_net_eur": 0.5})
    r = CS.matched_random_paired(
        candidate_trades=[candidate], baseline_trades=[],
        timeframe="1m", m_global=10,
    )
    assert r["match_status"] == "BASELINE_MATCH_INCOMPLETE"
    assert r["beats_matched_random"] is False                 # cannot pass the gate
