"""V10.45.6 final certification blockers: no importable raw token issuer,
descriptor-bound signed payloads, full canonical SHA-256 mutation coverage,
composite immutable dataset generations, hardlink TOCTOU at the replace
boundary, fail-closed registry corruption states with locking, exact
structural baseline, event/cluster n_eff hierarchy, unbypassable finiteness
validation, causal STALE_EXIT, repeated-encoding cache hygiene and a ledger
whose official rows all carry the closed registry SHA. Research only,
NO LIVE."""

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

T0 = 1_700_000_400_000
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


def _compile(**kw):
    st_, spec = ENG.compile_strategy(_spec(**kw), set())
    assert st_ == "OK", st_
    return spec


def _trade(entry_i, exit_i, ret, reason="TP", **kw):
    return {"entry_i": entry_i, "exit_i": exit_i, "net_return": ret,
            "exit_reason": reason, "bars_held": exit_i - entry_i,
            "censored": False, "tranches": 1, **kw}


def _rows(n=4320, t0=T0):
    return [[t0 + i * BAR, 100.0, 100.0, 100.0, 100.0, 1.0, 100.0]
            for i in range(n)]


# ==========================================================================
# 1. NO IMPORTABLE RAW ISSUER
# ==========================================================================

def test_no_raw_token_issuer_is_importable():
    """The ONLY holdout entry points are issue_if_all_gates_pass and
    open_with_token; no module attribute can mint or validate a token."""
    forbidden = ("_issue_raw_token", "_redeem_token", "issue_holdout_token",
                 "open_holdout", "_TOKEN_SECRET", "HoldoutAccessToken",
                 "SealedHoldout", "_holdout_service")
    for name in forbidden:
        assert not hasattr(ENG, name), f"{name} must not be importable"
    # nothing else in the module namespace mentions token issuing
    for name in vars(ENG):
        low = name.lower()
        if "token" in low or "redeem" in low:
            assert name in ("issue_if_all_gates_pass", "open_with_token"), \
                f"unexpected token-related symbol: {name}"
    assert callable(ENG.issue_if_all_gates_pass)
    assert callable(ENG.open_with_token)


# ==========================================================================
# 2. CANONICAL HASH MUTATION COVERAGE
# ==========================================================================

def test_canonical_spec_hash_changes_on_every_relevant_mutation():
    base = _compile()
    h0 = ENG.canonical_sha256(base)
    assert len(h0) == 64                              # FULL SHA-256
    mutations = (
        dict(entry_conditions=[{"feature": "rsi_14", "op": "<", "value": 30.0}]),
        dict(stop_policy={"type": "fixed", "value": 0.008}),
        dict(take_profit_policy={"type": "fixed", "value": 0.009}),
        dict(trailing_policy={"type": "fixed", "value": 0.004}),
        dict(time_exit=40),
        dict(cooldown=60),
        dict(side="SHORT"),
        dict(regime_filter="EU"),
        dict(strategy_id="other_name"),
    )
    seen = {h0}
    for mut in mutations:
        h = ENG.canonical_sha256(ENG.compile_strategy(
            _spec(**mut), set())[1])
        assert h not in seen, f"mutation {mut} did not change the hash"
        seen.add(h)
    # timeframe/symbol enter through compile kwargs
    h_tf = ENG.canonical_sha256(ENG.compile_strategy(
        _spec(), set(), timeframe="5m")[1])
    h_sym = ENG.canonical_sha256(ENG.compile_strategy(
        _spec(), set(), symbol="ETHUSDT")[1])
    assert h_tf not in seen and h_sym not in seen and h_tf != h_sym


def test_canonical_metrics_and_cost_hash_mutations():
    rng = random.Random(5)
    trades = [_trade(100 + i * 30, 100 + i * 30 + 10,
                     0.003 + rng.uniform(0, 0.002)) for i in range(20)]
    m1 = ENG.metrics(trades)
    m2 = ENG.metrics(trades[:-1])                     # one fewer trade
    assert ENG.canonical_sha256(m1) != ENG.canonical_sha256(m2)
    c1 = dict(ENG.DEFAULT_COSTS)
    c2 = {**c1, "taker_fee_bps": c1["taker_fee_bps"] + 0.5}
    assert ENG.canonical_sha256(c1) != ENG.canonical_sha256(c2)
    # non-finite values normalize to null instead of breaking the hash
    assert ENG.canonical_sha256({"x": float("nan")}) == \
        ENG.canonical_sha256({"x": None})


# ==========================================================================
# 3. DATASET GENERATIONS: composite identity, immutability
# ==========================================================================

def test_generation_reuse_and_conflict(tmp_path, monkeypatch):
    monkeypatch.setattr(BF.CE, "_repo_root", lambda: tmp_path)
    rows = _rows()
    m1 = BF.save_dataset("bitget", "BTCUSDT", rows, 3,
                         requested_start_ms=T0,
                         requested_end_ms=T0 + 4320 * BAR)
    # identical republish -> SAME generation reused, still verifies
    m2 = BF.save_dataset("bitget", "BTCUSDT", rows, 3,
                         requested_start_ms=T0,
                         requested_end_ms=T0 + 4320 * BAR)
    assert m2["generation_id"] == m1["generation_id"]
    # same CSV + DIFFERENT contract -> DIFFERENT generation, old one intact
    m3 = BF.save_dataset("bitget", "BTCUSDT", rows, 4,
                         requested_start_ms=T0,
                         requested_end_ms=T0 + 4320 * BAR)
    assert m3["generation_id"] != m1["generation_id"]
    d = BF._dataset_dir("bitget", "BTCUSDT")
    assert (d / f"gen_{m1['generation_id']}" / "data.csv").is_file()
    assert BF.verify_dataset("bitget", "BTCUSDT")["status"] == \
        "DATASET_VERIFIED"
    # a pre-planted CONFLICTING dir under the id save_dataset would compute
    # is refused, never overwritten
    rows2 = _rows()
    rows2[7][4] = 101.0
    rows2[7][2] = 101.0
    import io as _io, csv as _csv
    buf = _io.StringIO()
    w = _csv.writer(buf, lineterminator="\n")
    w.writerow(BF.CSV_HEADER)
    for r in rows2:
        w.writerow(r)
    csv_sha2 = hashlib.sha256(buf.getvalue().encode()).hexdigest()
    probe = dict(json.loads((d / f"gen_{m1['generation_id']}" /
                             "manifest.json").read_text(encoding="utf-8")))
    probe["sha256"] = csv_sha2
    gid2 = BF.compute_generation_id(csv_sha2, BF.manifest_contract_sha(probe),
                                    probe["source"], "BTCUSDT", "1m")
    # V10.46: only a COMPLETE generation blocks; a planted COMPLETE dir whose
    # content differs from what save_dataset(rows2) would produce must raise
    # GENERATION_CONFLICT and never be overwritten. (An INCOMPLETE planted dir
    # would instead be recovered/cleaned — the idempotency contract.)
    evil = d / f"gen_{gid2}"
    evil.mkdir()
    planted_csv = b"planted"
    (evil / "data.csv").write_bytes(planted_csv)
    (evil / "manifest.json").write_bytes(b"{}")
    (evil / BF.GEN_COMPLETE_MARKER).write_text(json.dumps({
        "state": "COMPLETE", "generation_id": gid2,
        "csv_sha256": hashlib.sha256(planted_csv).hexdigest(),
        "manifest_sha256": hashlib.sha256(b"{}").hexdigest(),
        "contract_sha256": "DIFFERENT_CONTRACT"}), encoding="utf-8")
    with pytest.raises(IOError, match="GENERATION_CONFLICT"):
        BF.save_dataset("bitget", "BTCUSDT", rows2, 3,
                        requested_start_ms=T0,
                        requested_end_ms=T0 + 4320 * BAR)
    assert (evil / "data.csv").read_bytes() == planted_csv


def test_crash_before_current_keeps_previous_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(BF.CE, "_repo_root", lambda: tmp_path)
    rows = _rows()
    m1 = BF.save_dataset("bitget", "BTCUSDT", rows, 3,
                         requested_start_ms=T0,
                         requested_end_ms=T0 + 4320 * BAR)
    orig = BF.safe_atomic_write

    def crash_on_marker(path, data):
        if str(path).endswith(BF.CURRENT_MARKER):
            raise IOError("crash before CURRENT")
        return orig(path, data)

    monkeypatch.setattr(BF, "safe_atomic_write", crash_on_marker)
    rows2 = _rows()
    rows2[3][4] = 100.5
    rows2[3][2] = 100.5
    with pytest.raises(IOError):
        BF.save_dataset("bitget", "BTCUSDT", rows2, 3,
                        requested_start_ms=T0,
                        requested_end_ms=T0 + 4320 * BAR)
    monkeypatch.setattr(BF, "safe_atomic_write", orig)
    # the PREVIOUS generation is fully current and verifies
    v = BF.verify_dataset("bitget", "BTCUSDT")
    assert v["status"] == "DATASET_VERIFIED"
    assert v["generation_id"] == m1["generation_id"]


# ==========================================================================
# 4. HARDLINK TOCTOU AT THE REPLACE BOUNDARY
# ==========================================================================

def test_hardlink_created_after_first_check_is_caught(tmp_path, monkeypatch):
    monkeypatch.setattr(BF.CE, "_repo_root", lambda: tmp_path)
    d = tmp_path / "data"
    d.mkdir()
    target = d / "t.bin"
    BF.safe_atomic_write(target, b"original")
    alias = d / "alias.bin"

    def racer(path):
        try:
            os.link(target, alias)                    # attacker wins the race
        except OSError:
            pytest.skip("filesystem without hardlink support")

    monkeypatch.setattr(BF, "_between_write_and_replace", racer)
    with pytest.raises(ValueError, match="hardlink"):
        BF.safe_atomic_write(target, b"new-secret-content")
    # the alias NEVER received the new content and the dest is untouched
    assert alias.read_bytes() == b"original"
    assert target.read_bytes() == b"original"
    assert list(d.glob("*.part")) == []


# ==========================================================================
# 5. REGISTRY CORRUPTION: fail-closed everywhere + locking
# ==========================================================================

def _open_close_registry(sprint="sp_c"):
    members = ENG.enumerate_trial_members(
        [{"strategy_id": "s", "signature": "g", "side": "LONG"}], "X", "1m")
    ENG.registry_open(sprint, members)
    return ENG.registry_close(sprint, len(members), ["r"])


def test_registry_schema_and_sequence_corruption(tmp_path, monkeypatch):
    monkeypatch.setattr(ENG.CE, "_repo_root", lambda: tmp_path)
    _open_close_registry()
    p = ENG._out() / ENG.REGISTRY_FILE
    # valid JSON, invalid schema
    good = p.read_bytes()
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "alien", "at": "x"}) + "\n")
    with pytest.raises(ENG.RegistryError) as ei:
        ENG._registry_records()
    assert ei.value.status == "REGISTRY_SCHEMA_INVALID"
    # duplicated raw close -> sequence invalid
    p.write_bytes(good)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "sprint_close", "at": "x",
                             "sprint_id": "sp_c", "m_global": 1}) + "\n")
    with pytest.raises(ENG.RegistryError) as ei:
        ENG.registry_is_closed("sp_c")
    assert ei.value.status == "REGISTRY_SEQUENCE_INVALID"
    # truncated close: cut the file mid-line
    p.write_bytes(good[:-7])
    with pytest.raises(ENG.RegistryError) as ei:
        ENG._registry_records()
    assert ei.value.status == "REGISTRY_TRUNCATED"
    # append after corruption is impossible
    with pytest.raises(ENG.RegistryError):
        ENG.registry_append({"kind": "note", "sprint_id": "other"})
    # phase A cannot run over a corrupt registry either
    bars = _bars(6000, seed=23)
    feats = ENG.build_features(bars)
    seg = ENG.split_indices(len(bars))
    ENG.set_run_context(run_id="r", sprint_id="sp_c", symbol="X",
                        timeframe="1m", registry_sha_at_close="zz")
    with pytest.raises(ENG.RegistryError):
        ENG.run_funnel_phase_a(bars[:seg["validation"][1]],
                               feats[:seg["validation"][1]],
                               [_compile()], seg, log=lambda *a: None)


def test_registry_lock_blocks_concurrent_writer(tmp_path, monkeypatch):
    monkeypatch.setattr(ENG.CE, "_repo_root", lambda: tmp_path)
    lock_path = ENG._out() / (ENG.REGISTRY_FILE + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        lk = ENG._RegistryLock(timeout_s=0.3)
        with pytest.raises(TimeoutError):
            lk.__enter__()
    finally:
        os.close(fd)
        os.unlink(lock_path)
    # after release, the lock works again
    ENG.registry_append({"kind": "note", "sprint_id": "sp_free"})


# ==========================================================================
# 6. N_EFF: cluster_id level of the hierarchy
# ==========================================================================

def test_cluster_id_hierarchy_and_unique_clusters():
    rng = random.Random(7)
    same = [_trade(100 + i * 40, 100 + i * 40 + 10,
                   0.002 + 0.001 * rng.random(), cluster_id="C1")
            for i in range(20)]
    m = ENG.metrics(same)
    assert m["cluster_source"] == "cluster_id"
    assert m["n_cluster_id"] == 1 and m["n_eff_final"] == 1
    assert m["degenerate_returns"] is True            # single cluster blocks
    uniq = [_trade(100 + i * 40, 100 + i * 40 + 10,
                   0.002 + 0.001 * rng.random(), cluster_id=f"C{i}")
            for i in range(20)]
    m2 = ENG.metrics(uniq)
    assert m2["n_cluster_id"] == 20
    assert m2["degenerate_returns"] is False
    assert m2["n_eff_final"] > 1
    # event_id outranks cluster_id when both exist
    both = [_trade(100 + i * 40, 100 + i * 40 + 10,
                   0.002 + 0.001 * rng.random(), event_id=f"e{i}",
                   cluster_id="C1") for i in range(20)]
    m3 = ENG.metrics(both)
    assert m3["cluster_source"] == "event_id"
    assert m3["n_eff_final"] == 1                     # min() stays conservative


# ==========================================================================
# 7. NON-FINITE: no public bypass, structured states
# ==========================================================================

def test_forged_receipt_cannot_bypass_finiteness():
    bars = _bars(400)
    feats = ENG.build_features(bars)
    bars[310]["close"] = float("nan")

    class Fake:                                        # forged receipt
        _bars_id = id(bars)
        _feats_id = id(feats)
        n = len(bars)

    r = ENG.replay(bars, feats, _compile(), i_start=300, i_end=320,
                   verified=Fake())
    assert r["ok"] is False
    assert r["status"] == "INVALID_NON_FINITE_INPUT"
    # a REAL receipt only comes from the full scan, which refuses NaN
    with pytest.raises(ValueError, match="INVALID_NON_FINITE_INPUT"):
        ENG.verify_finite_dataset(bars, feats)


def test_build_features_and_resample_structured_errors():
    with pytest.raises(ValueError, match="INVALID_BAR_INPUT"):
        ENG.build_features([{"ts": T0}])              # missing fields
    with pytest.raises(ValueError, match="INVALID_BAR_INPUT"):
        ENG.build_features([None])                    # not even a dict
    bad = _bars(30)
    bad[7]["high"] = float("inf")
    with pytest.raises(ValueError, match="INVALID_BAR_INPUT"):
        ENG.resample_bars(bad, 5)
    with pytest.raises(ValueError, match="INVALID_BAR_INPUT"):
        ENG.build_features(bad)


# ==========================================================================
# 8. STALE_EXIT: causal contract
# ==========================================================================

def _gap_bars(seed, side_up=False):
    bars = _bars(400, seed=seed)
    i = 300
    # remove one bar AFTER i+2 so a gap opens mid-trade
    gap_at = i + 3
    for j in range(gap_at, len(bars)):
        bars[j]["ts"] += BAR                          # creates delta 2*BAR
        bars[j]["available_at"] += BAR
    return bars, i, gap_at


def test_stale_exit_uses_next_open_and_next_index_long():
    bars, i, gap_at = _gap_bars(41)
    feats = ENG.build_features(bars)
    spec = _compile(stop_policy={"type": "fixed", "value": 0.05},
                    take_profit_policy={"type": "fixed", "value": 0.05},
                    time_exit=50)
    r = ENG.replay(bars, feats, spec, i_start=i, i_end=i + 30)
    stale = [t for t in r["trades"] if t["exit_reason"] == "STALE_EXIT"]
    assert stale, "gap must force a stale exit"
    t = stale[0]
    # CAUSAL: the exit index is the bar at which the gap became KNOWN and
    # the price is THAT bar's open — never future time with a past price
    assert t["exit_i"] == gap_at
    entry_px = bars[i + 1]["open"]
    ps = (6.0 + 0.5 + 2.0) / 10_000.0
    expected = (bars[gap_at]["open"] * (1 - ps)) / (entry_px * (1 + ps)) - 1
    fund = 1.0 / 10_000.0 / 480 * t["bars_held"]
    assert abs(t["net_return"] - (expected - fund)) < 1e-7
    # and STALE_EXIT stays OUT of the metrics
    m = ENG.metrics(r["trades"])
    assert m["invalid_execution"] >= 1


def test_stale_exit_short_and_invalid_when_no_causal_price():
    bars, i, gap_at = _gap_bars(43)
    feats = ENG.build_features(bars)
    spec = _compile(side="SHORT",
                    stop_policy={"type": "fixed", "value": 0.05},
                    take_profit_policy={"type": "fixed", "value": 0.05},
                    time_exit=50)
    r = ENG.replay(bars, feats, spec, i_start=i, i_end=i + 30)
    stale = [t for t in r["trades"] if t["exit_reason"] == "STALE_EXIT"]
    assert stale and stale[0]["exit_i"] == gap_at
    # no causal executable price -> STALE_EXIT_INVALID, excluded from metrics
    bars2, i2, gap2 = _gap_bars(47)
    bars2[gap2]["open"] = float("nan")
    feats2 = ENG.build_features(_bars(400, seed=47))  # clean features
    r2 = ENG.replay(bars2, feats2, spec, i_start=i2, i_end=i2 + 30)
    inv = [t for t in r2["trades"]
           if t["exit_reason"] in ("STALE_EXIT_INVALID",)]
    if r2.get("ok") is False:
        # the pre-scan may refuse the NaN outright: equally fail-closed
        assert r2["status"] == "INVALID_NON_FINITE_INPUT"
    else:
        assert inv, "non-finite next open must yield STALE_EXIT_INVALID"
        assert ENG.metrics(r2["trades"])["invalid_execution"] >= 1


# ==========================================================================
# 9. LEDGER: 100% official rows carry the closed registry SHA
# ==========================================================================

def test_official_ledger_rows_all_carry_registry_sha(tmp_path, monkeypatch):
    monkeypatch.setattr(BF.CE, "_repo_root", lambda: tmp_path)
    rng = random.Random(7)
    rows, price = [], 100.0
    n = 9000
    for i in range(n):
        ch = rng.uniform(-0.002, 0.002)
        new = price * (1 + ch)
        rows.append([T0 + i * BAR, round(price, 6),
                     round(max(price, new) * 1.0008, 6),
                     round(min(price, new) * 0.9992, 6), round(new, 6),
                     10.0, 1000.0])
        price = new
    BF.save_dataset("bitget", "BTCUSDT", rows, 7, requested_start_ms=T0,
                    requested_end_ms=T0 + n * BAR)
    out = ORCH.run_edge_discovery(symbol="BTCUSDT", use_ai=False,
                                  write_reports=False, log=lambda *a: None)
    assert out.get("status", "SUMMARY") == "SUMMARY" or "funnel" in out
    led_p = (tmp_path / "reports" / "research" / "v10_45_6_edge_discovery" /
             "experiment_ledger_v10_45_6.jsonl")
    led = [json.loads(l) for l in led_p.read_text(encoding="utf-8")
           .splitlines()]
    assert led, "official ledger must exist"
    missing = [e for e in led if not e.get("registry_sha_at_close")]
    assert missing == [], f"{len(missing)}/{len(led)} rows without registry SHA"
    for e in led:
        assert e.get("run_id") and e.get("sprint_id")
    kinds = {e.get("phase") for e in led}
    assert "compile" in kinds                          # buffered, then flushed


# ==========================================================================
# 10. AI PROVENANCE + DASHBOARD GLOBAL (light)
# ==========================================================================

def test_ai_call_rows_carry_full_provenance_fields():
    # register the deterministic mock under a REAL provider slot so the
    # role-assignment loop uses it (no network involved)
    providers = {"ollama": P.MockProvider()}
    gen = ORCH.generate_hypotheses(providers, "test note",
                                   log=lambda *a: None)
    assert gen["calls"], "mock provider must produce calls"
    for c in gen["calls"]:
        for k in ("role", "provider", "model", "prompt_sha",
                  "raw_output_sha256", "response_sha256", "cache_key"):
            assert k in c, k
        if c["ok"]:
            assert c["response_sha256"] and len(c["response_sha256"]) == 64


def test_dashboard_panel_reads_global_sprint(tmp_path, monkeypatch):
    from app.labs import research_dashboard_v10_43c as D
    monkeypatch.setattr(D.CE, "_repo_root", lambda: tmp_path)
    rd = tmp_path / "reports" / "research" / "v10_45_6_edge_discovery"
    rd.mkdir(parents=True)
    (rd / "sprint_summary_v10_45_6.json").write_text(json.dumps({
        "sprint_id": "sprint_test", "m_global": 4041,
        "registry_state": "CLOSED",
        "verdict": "No se encontró edge validado en las familias probadas",
        "runs": [{"timeframe": "1m", "funnel": {"universe": 1},
                  "holdout_accesses": 0}]}), encoding="utf-8")
    (rd / "commit_seal_v10_45_6.json").write_text(json.dumps({
        "repo_commit_head": "a" * 40, "git_tree_oid": "b" * 40,
        "dirty_tracked_files": False, "match": True}), encoding="utf-8")
    (rd / "CURRENT_OUTPUT_MANIFEST.json").write_text(json.dumps({
        "output_manifest_id": "sprint_test",
        "output_manifest_sha256": "c" * 64}), encoding="utf-8")
    html = D._panel_edge_discovery({})
    assert "sprint_test" in html and "4041" in html
    assert ("a" * 12) in html and ("b" * 12) in html
    assert "MATCH" in html
    assert "holdout_accesses=0" in html
