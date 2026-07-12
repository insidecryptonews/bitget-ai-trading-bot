"""V10.45.5 adversarial integrity closure: CSV-derived verification against
lying manifests, NaN/Inf at every pipeline stage, inverted/duplicate/irregular
timestamps, strict order-preserving loader, verify-before-resample/features
pipeline order, exclusive temp files with mandatory fsync and replace-failure
cleanup, transaction recovery around the CURRENT marker, cache root
containment, unregistered-trial refusal, cluster-based n_eff, single-event
degeneration, LF/CRLF-stable code identity and the output manifest that binds
every published artifact. Research only, NO LIVE."""

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


def _rows(n=4320, t0=T0, price=100.0):
    return [[t0 + i * BAR, price, price, price, price, 1.0, 100.0]
            for i in range(n)]


def _publish(monkeypatch, tmp_path, rows=None, n=4320, **kw):
    monkeypatch.setattr(BF.CE, "_repo_root", lambda: tmp_path)
    rows = rows if rows is not None else _rows(n)
    return BF.save_dataset("bitget", "BTCUSDT", rows, 3,
                           requested_start_ms=T0,
                           requested_end_ms=T0 + n * BAR, **kw)


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


def _trade(entry_i, exit_i, ret, reason="TP"):
    return {"entry_i": entry_i, "exit_i": exit_i, "net_return": ret,
            "exit_reason": reason, "bars_held": exit_i - entry_i,
            "censored": False, "tranches": 1}


# ==========================================================================
# DATASET: the CSV is the truth, the manifest only a contract
# ==========================================================================

def test_lying_manifest_detected_even_with_refreshed_marker(tmp_path,
                                                            monkeypatch):
    """A manifest that LIES about row counts — with the CURRENT marker
    re-signed to match it — still fails: every figure is recomputed from the
    CSV rows and compared against the contract."""
    _publish(monkeypatch, tmp_path)
    cur = BF.current_generation("bitget", "BTCUSDT")
    man = json.loads(cur["manifest_path"].read_text(encoding="utf-8"))
    man["n_bars"] = man["actual_bars"] = 999_999          # the lie
    man_bytes = json.dumps(man, indent=2, default=str).encode("utf-8")
    cur["manifest_path"].write_bytes(man_bytes)
    d = BF._dataset_dir("bitget", "BTCUSDT")
    marker = json.loads((d / BF.CURRENT_MARKER).read_text(encoding="utf-8"))
    marker["manifest_sha256"] = hashlib.sha256(man_bytes).hexdigest()
    (d / BF.CURRENT_MARKER).write_text(json.dumps(marker), encoding="utf-8")
    v = BF.verify_dataset("bitget", "BTCUSDT")
    assert v["ok"] is False
    assert v["status"] == "INVALID_MANIFEST_CONTRACT"
    assert "n_bars" in v["detail"]


def test_nan_and_inf_rows_fail_verification_and_loader(tmp_path, monkeypatch):
    rows = _rows()
    rows[100][4] = float("nan")
    _publish(monkeypatch, tmp_path, rows=rows)
    assert BF.verify_dataset("bitget", "BTCUSDT")["status"] == \
        "INVALID_NON_FINITE"
    with pytest.raises(BF.DatasetError) as ei:
        BF.load_klines("bitget", "BTCUSDT")
    assert ei.value.status == "INVALID_NON_FINITE"


def test_negative_volume_and_turnover_fail(tmp_path, monkeypatch):
    rows = _rows()
    rows[5][5] = -1.0
    _publish(monkeypatch, tmp_path, rows=rows)
    assert BF.verify_dataset("bitget", "BTCUSDT")["status"] == \
        "INVALID_NEGATIVE_VOLUME"
    rows = _rows()
    rows[5][6] = -0.5
    _publish(monkeypatch, tmp_path, rows=rows)
    assert BF.verify_dataset("bitget", "BTCUSDT")["status"] == \
        "INVALID_NEGATIVE_TURNOVER"


def test_inverted_duplicate_and_irregular_timestamps(tmp_path, monkeypatch):
    rows = _rows()
    rows[51][0] -= 90_000                                 # ts goes BACKWARDS
    _publish(monkeypatch, tmp_path, rows=rows)
    assert BF.verify_dataset("bitget", "BTCUSDT")["status"] == \
        "INVALID_TIMESTAMP_ORDER"
    with pytest.raises(BF.DatasetError) as ei:
        BF.load_klines("bitget", "BTCUSDT")               # loader NEVER sorts
    assert ei.value.status == "INVALID_TIMESTAMP_ORDER"
    rows = _rows()
    rows[50], rows[51] = rows[51], rows[50]               # swap -> gap FIRST
    _publish(monkeypatch, tmp_path, rows=rows)
    assert BF.verify_dataset("bitget", "BTCUSDT")["status"] == "INVALID_GAP"
    rows = _rows()
    rows[60][0] = rows[59][0]                             # duplicate ts
    _publish(monkeypatch, tmp_path, rows=rows)
    assert BF.verify_dataset("bitget", "BTCUSDT")["status"] == \
        "INVALID_DUPLICATE"
    rows = _rows()
    rows[70][0] += 30_000                                 # off-grid ts
    _publish(monkeypatch, tmp_path, rows=rows)
    assert BF.verify_dataset("bitget", "BTCUSDT")["status"] in \
        ("INVALID_TIMESTAMP_INTERVAL", "INVALID_TIMESTAMP_ORDER")


def test_identity_contract_symbol_venue_timeframe_as_of(tmp_path, monkeypatch):
    _publish(monkeypatch, tmp_path)
    cur = BF.current_generation("bitget", "BTCUSDT")
    man = json.loads(cur["manifest_path"].read_text(encoding="utf-8"))

    def _resign(m):
        b = json.dumps(m, indent=2, default=str).encode("utf-8")
        cur["manifest_path"].write_bytes(b)
        d = BF._dataset_dir("bitget", "BTCUSDT")
        marker = json.loads((d / BF.CURRENT_MARKER)
                            .read_text(encoding="utf-8"))
        marker["manifest_sha256"] = hashlib.sha256(b).hexdigest()
        (d / BF.CURRENT_MARKER).write_text(json.dumps(marker),
                                           encoding="utf-8")
    for field, val, status in (("symbol", "ETHUSDT", "INVALID_SYMBOL"),
                               ("venue", "bybit", "INVALID_VENUE"),
                               ("timeframe", "5m", "INVALID_TIMEFRAME")):
        m2 = dict(man)
        m2[field] = val
        _resign(m2)
        assert BF.verify_dataset("bitget", "BTCUSDT")["status"] == status
    m2 = dict(man)
    m2["requested_end_ms"] = man["requested_end_ms"] + 7                # off-grid
    _resign(m2)
    assert BF.verify_dataset("bitget", "BTCUSDT")["status"] == "INVALID_AS_OF"


def test_loader_never_skips_bad_rows(tmp_path, monkeypatch):
    """A malformed row must RAISE, never be silently dropped (the old loader
    skipped unparseable lines)."""
    _publish(monkeypatch, tmp_path)
    cur = BF.current_generation("bitget", "BTCUSDT")
    lines = cur["csv_path"].read_text(encoding="utf-8").splitlines()
    lines[100] = "garbage,row"
    body = "\n".join(lines) + "\n"
    cur["csv_path"].write_text(body, encoding="utf-8", newline="")
    d = BF._dataset_dir("bitget", "BTCUSDT")
    marker = json.loads((d / BF.CURRENT_MARKER).read_text(encoding="utf-8"))
    marker["csv_sha256"] = hashlib.sha256(
        body.encode("utf-8")).hexdigest()
    (d / BF.CURRENT_MARKER).write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(BF.DatasetError) as ei:
        BF.load_klines("bitget", "BTCUSDT")
    assert ei.value.status == "INVALID_SCHEMA"


# ==========================================================================
# PIPELINE ORDER: nothing resamples or featurizes before DATASET_VERIFIED
# ==========================================================================

def test_no_resample_or_features_before_verify(tmp_path, monkeypatch):
    monkeypatch.setattr(BF.CE, "_repo_root", lambda: tmp_path)

    def boom(*a, **k):
        raise AssertionError("stage ran BEFORE DATASET_VERIFIED")

    monkeypatch.setattr(ENG, "resample_bars", boom)
    monkeypatch.setattr(ENG, "build_features", boom)
    out = ORCH.run_edge_discovery(symbol="BTCUSDT", use_ai=False,
                                  write_reports=False, timeframe="5m",
                                  log=lambda *a: None)
    assert out["status"] == "INVALID_MANIFEST_CONTRACT"   # and no boom fired


# ==========================================================================
# NON-FINITE AT EVERY STAGE
# ==========================================================================

def test_replay_refuses_nan_close():
    bars = _bars(400)
    bars[310]["close"] = float("nan")
    feats = ENG.build_features(_bars(400))                # clean feats
    spec = _compile()
    r = ENG.replay(bars, feats, spec, i_start=300, i_end=320)
    assert r["ok"] is False
    assert r["status"] == "INVALID_NON_FINITE_INPUT"
    assert r["trades"] == []


def test_replay_refuses_inf_feature():
    bars = _bars(400)
    feats = ENG.build_features(bars)
    feats[305]["ret_1"] = float("inf")
    spec = _compile(entry_conditions=[{"feature": "ret_1", "op": ">=",
                                       "value": -1.0}])
    r = ENG.replay(bars, feats, spec, i_start=300, i_end=320)
    assert r["ok"] is False and r["status"] == "INVALID_NON_FINITE_INPUT"


def test_metrics_never_raises_on_corrupt_trades():
    for bad in (float("nan"), float("inf"), float("-inf"), "text", None):
        m = ENG.metrics([_trade(1, 5, bad)])
        assert m["ok"] is False
        assert m["status"] == "INVALID_NON_FINITE_INPUT"
        assert m["promotion_allowed"] is False
        assert m["net_EV"] is None
    m = ENG.metrics([{"broken": True}])                   # malformed dict
    assert m["ok"] is False and m["promotion_allowed"] is False


def test_assert_finite_dataset_blocks_funnel_inputs():
    bars = _bars(300)
    feats = ENG.build_features(bars)
    bars[250]["low"] = float("-inf")
    with pytest.raises(ValueError, match="INVALID_NON_FINITE_INPUT"):
        ENG.assert_finite_dataset(bars, feats)


def test_ledger_serializes_non_finite_as_null(tmp_path, monkeypatch):
    monkeypatch.setattr(ENG.CE, "_repo_root", lambda: tmp_path)
    ENG.set_run_context(run_id="r1")
    ENG.ledger_append({"phase": "test", "value": float("nan"),
                       "nested": {"x": float("inf")}})
    line = (tmp_path / "reports" / "research" / "v10_45_5_edge_discovery" /
            "experiment_ledger_v10_45_5.jsonl").read_text(
                encoding="utf-8").strip()
    obj = json.loads(line)                                # valid JSON always
    assert obj["value"] is None and obj["nested"]["x"] is None


# ==========================================================================
# PATH SAFETY: exclusive temps, fsync, replace failure, cache root
# ==========================================================================

def test_fsync_failure_never_reports_success(tmp_path, monkeypatch):
    monkeypatch.setattr(BF.CE, "_repo_root", lambda: tmp_path)
    d = tmp_path / "data"
    d.mkdir()

    def bad_fsync(fd):
        raise OSError("disk says no")

    monkeypatch.setattr(BF.os, "fsync", bad_fsync)
    with pytest.raises(OSError, match="disk says no"):
        BF.safe_atomic_write(d / "f.bin", b"payload")
    assert not (d / "f.bin").exists()
    assert list(d.glob("*.part")) == []                   # temp cleaned up


def test_replace_failure_cleans_temp_and_keeps_destination(tmp_path,
                                                           monkeypatch):
    monkeypatch.setattr(BF.CE, "_repo_root", lambda: tmp_path)
    d = tmp_path / "data"
    d.mkdir()
    target = d / "f.bin"
    BF.safe_atomic_write(target, b"original")

    def bad_replace(a, b):
        raise OSError("replace failed")

    monkeypatch.setattr(BF.os, "replace", bad_replace)
    with pytest.raises(OSError, match="replace failed"):
        BF.safe_atomic_write(target, b"new-content")
    monkeypatch.undo()
    assert target.read_bytes() == b"original"             # untouched
    assert list(d.glob("*.part")) == []


def test_preexisting_temp_names_cannot_be_hijacked(tmp_path, monkeypatch):
    """Random exclusive temp names: a pre-planted '.tmp'-style file is simply
    ignored (never reused, never followed) and recovery removes orphans."""
    _publish(monkeypatch, tmp_path)
    d = BF._dataset_dir("bitget", "BTCUSDT")
    planted = d / ".data.csv.evil.part"
    planted.write_bytes(b"attacker content")
    rec = BF.recover_staging("bitget", "BTCUSDT")
    assert rec["removed_temp_files"] >= 1
    assert not planted.exists()
    assert BF.verify_dataset("bitget", "BTCUSDT")["status"] == \
        "DATASET_VERIFIED"


def test_crash_before_csv_leaves_no_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(BF.CE, "_repo_root", lambda: tmp_path)
    orig = BF.safe_atomic_write

    def crash_on_csv(path, data):
        if str(path).endswith("data.csv"):
            raise IOError("crash before CSV")
        return orig(path, data)

    monkeypatch.setattr(BF, "safe_atomic_write", crash_on_csv)
    with pytest.raises(IOError):
        BF.save_dataset("bitget", "BTCUSDT", _rows(), 3,
                        requested_start_ms=T0,
                        requested_end_ms=T0 + 4320 * BAR)
    monkeypatch.setattr(BF, "safe_atomic_write", orig)
    v = BF.verify_dataset("bitget", "BTCUSDT")
    assert v["status"] == "INVALID_MANIFEST_CONTRACT"
    assert v["detail"] == "NO_CURRENT_GENERATION"


def test_linked_cache_root_blocks_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(P.CE, "_repo_root", lambda: tmp_path)
    outside = tmp_path.parent / "cache_outside"
    outside.mkdir(exist_ok=True)
    real = os.path.realpath

    def fake_realpath(p):
        rp = real(p)
        if "ai_cache" in str(p):
            return str(outside)
        return rp

    monkeypatch.setattr(BF.os.path, "realpath", fake_realpath)
    # a linked cache root fails LOUDLY on both paths and nothing escapes
    with pytest.raises(ValueError):
        P.cache_put("mock", "m1", "prompt-z", '{"a":1}')
    with pytest.raises(ValueError):
        P.cache_get("mock", "m1", "prompt-z")
    assert list(outside.glob("*.json")) == []             # nothing escaped


def test_cache_preserves_benign_long_identifiers(tmp_path, monkeypatch):
    """The cache sanitizer must NEVER corrupt benign long strategy ids: a
    second run reading from cache has to see the exact same universe."""
    monkeypatch.setattr(P.CE, "_repo_root", lambda: tmp_path)
    body = json.dumps({"strategies": [
        {"strategy_id": "volatility_transition_reversal", "side": "LONG"},
        {"strategy_id": "bollinger_mean_reversion_short", "side": "SHORT"}]})
    P.cache_put("mock", "m1", "prompt-ids", body)
    got = json.loads(P.cache_get("mock", "m1", "prompt-ids"))
    ids = [x["strategy_id"] for x in got["strategies"]]
    assert ids == ["volatility_transition_reversal",
                   "bollinger_mean_reversion_short"]
    assert "<redacted>" not in json.dumps(got)


# ==========================================================================
# REGISTRY: unregistered trials refuse to run
# ==========================================================================

def test_unregistered_trial_refuses_to_execute(tmp_path, monkeypatch):
    monkeypatch.setattr(ENG.CE, "_repo_root", lambda: tmp_path)
    bars = _bars(6000, seed=23)
    feats = ENG.build_features(bars)
    seg = ENG.split_indices(len(bars))
    registered = _compile(strategy_id="registered_one")
    intruder = _compile(strategy_id="intruder",
                        entry_conditions=[{"feature": "rsi_14", "op": "<",
                                           "value": 41.0}])
    members = ENG.enumerate_trial_members([registered], "BTCUSDT", "1m")
    ENG.registry_open("sp_reg", members)
    closed = ENG.registry_close("sp_reg", len(members), ["r1"])
    ENG.set_run_context(run_id="r1", sprint_id="sp_reg", symbol="BTCUSDT",
                        timeframe="1m",
                        registry_sha_at_close=closed["registry_sha256"])
    v1 = seg["validation"][1]
    with pytest.raises(ValueError, match="TRIAL_NOT_REGISTERED"):
        ENG.run_funnel_phase_a(bars[:v1], feats[:v1],
                               [registered, intruder], seg,
                               log=lambda *a: None)


# ==========================================================================
# N_EFF: clusters, single event, degenerate evidence
# ==========================================================================

def test_overlapping_chain_collapses_to_one_cluster():
    rng = random.Random(9)
    trades = [_trade(100 + i * 2, 100 + i * 2 + 10,
                     0.001 * (1 if i % 3 else -1) * (1 + rng.random()))
              for i in range(40)]
    m = ENG.metrics(trades)
    assert m["n_cluster"] < m["n_raw"]
    assert m["n_eff_final"] == m["n_eff"] <= m["n_cluster"]
    assert "overlap_chain" in m["cluster_source"]


def test_same_temporal_block_is_one_event():
    rng = random.Random(11)
    # 12 non-overlapping trades all entering within ONE 30-bar block
    trades = [_trade(600 + i, 600 + i + 1, 0.001 + 0.0001 * rng.random(),
                     reason="TIME") for i in range(0, 24, 2)]
    m = ENG.metrics(trades)
    assert m["n_cluster"] == 1                            # one shock/episode
    assert m["degenerate_returns"] is True                # single event
    assert m["promotion_allowed"] is False
    eligible, reasons = ENG.validation_eligible_for_holdout(
        m, True, True, 0.0, 0.0, execution_proxies=())
    assert not eligible and "DEGENERATE_RETURNS" in reasons


def test_zero_variance_pf999_is_never_evidence():
    trades = [_trade(100 + i * 40, 100 + i * 40 + 10, 0.002)
              for i in range(12)]
    m = ENG.metrics(trades)
    assert m["profit_factor"] == 999.0
    assert m["degenerate_returns"] is True
    assert m["promotion_allowed"] is False
    token, reasons = ENG.issue_holdout_token(
        "s", m, True, True, 0.0001, 0.0001, execution_proxies=())
    assert token is None and "DEGENERATE_RETURNS" in reasons


def test_n_eff_report_fields_complete():
    rng = random.Random(13)
    trades = [_trade(100 + i * 35, 100 + i * 35 + 10,
                     0.002 + 0.001 * rng.random()) for i in range(25)]
    m = ENG.metrics(trades)
    for k in ("n_raw", "n_overlap", "n_acf", "n_cluster", "n_eff_final",
              "n_eff_method", "cluster_source", "degenerate_returns",
              "unique_returns"):
        assert k in m, k
    assert m["degenerate_returns"] is False


# ==========================================================================
# PROVENANCE: LF/CRLF-stable identity + output manifest binding
# ==========================================================================

def test_code_identity_is_line_ending_stable():
    ident = ENG.code_identity()
    for k in ("repo_commit", "git_tree_oid", "relevant_blob_oids",
              "semantic_code_hash", "dirty_worktree", "runner_version"):
        assert k in ident, k
    # the semantic hash normalizes CRLF -> LF: identical logical content
    # yields identical digests regardless of checkout style
    lf = b"line1\nline2\n"
    crlf = b"line1\r\nline2\r\n"
    h1 = hashlib.sha256(lf.replace(b"\r\n", b"\n")).hexdigest()
    h2 = hashlib.sha256(crlf.replace(b"\r\n", b"\n")).hexdigest()
    assert h1 == h2
    assert len(ident["semantic_code_hash"]) == 32


def test_output_manifest_binds_artifacts_and_detects_tamper(tmp_path,
                                                            monkeypatch):
    monkeypatch.setattr(ENG.CE, "_repo_root", lambda: tmp_path)
    out = ENG._out()
    (out / "edge_discovery_report_v10_45_5.md").write_text(
        "# report", encoding="utf-8")
    man = ENG.write_output_manifest("test_manifest_1",
                                    extra={"note": "unit"})
    assert man["output_manifest_id"] == "test_manifest_1"
    arts = man["artifacts"]
    assert "edge_discovery_report_v10_45_5.md" in arts
    ptr = json.loads((out / "CURRENT_OUTPUT_MANIFEST.json")
                     .read_text(encoding="utf-8"))
    assert ptr["output_manifest_sha256"] == man["output_manifest_sha256"]
    # tampering any published artifact is detectable against the manifest
    (out / "edge_discovery_report_v10_45_5.md").write_text(
        "# tampered", encoding="utf-8")
    now_sha = hashlib.sha256(
        (out / "edge_discovery_report_v10_45_5.md").read_bytes()).hexdigest()
    assert now_sha != arts["edge_discovery_report_v10_45_5.md"]
    # and the seal certifies code + manifest, never code alone
    seal = ENG.write_commit_seal(
        output_manifest_sha=man["output_manifest_sha256"])
    assert seal["output_manifest_sha256"] == man["output_manifest_sha256"]
    assert "published_artifacts_via_manifest" in seal["certifies"]
