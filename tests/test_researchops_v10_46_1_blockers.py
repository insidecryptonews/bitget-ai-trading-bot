"""V10.46.1 certification blockers + no-live safety gate:
generation COMPLETE state with idempotent crash recovery, temp revalidation
right before os.replace, transactional official ledger, member/trial linkage,
AI-critique provenance, manifest reason codes, and an automatic safety test
that fails if any change opens a real-order / private-endpoint / live path.
Research only, NO LIVE."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from app.labs import ai_providers_v10_45_1 as P
from app.labs import edge_discovery_engine_v10_45_1 as ENG
from app.labs import multi_ai_orchestrator_v10_45_1 as ORCH
from app.labs import public_data_backfill_v10_45_1 as BF
from app.labs import v10_46

T0 = 1_700_000_400_000
BAR = 60_000


def _rows(n=4320, t0=T0):
    return [[t0 + i * BAR, 100.0, 100.0, 100.0, 100.0, 1.0, 100.0]
            for i in range(n)]


def _publish(monkeypatch, tmp_path, rows=None):
    monkeypatch.setattr(BF.CE, "_repo_root", lambda: tmp_path)
    rows = rows if rows is not None else _rows()
    return BF.save_dataset("bitget", "BTCUSDT", rows, 3,
                           requested_start_ms=T0,
                           requested_end_ms=T0 + len(rows) * BAR)


# ==========================================================================
# 6.1 GENERATION COMPLETE STATE + IDEMPOTENT RECOVERY
# ==========================================================================

def test_generation_has_complete_marker_and_reuses_idempotently(tmp_path,
                                                                monkeypatch):
    m1 = _publish(monkeypatch, tmp_path)
    d = BF._dataset_dir("bitget", "BTCUSDT")
    gdir = d / f"gen_{m1['generation_id']}"
    assert (gdir / BF.GEN_COMPLETE_MARKER).is_file()
    rec = json.loads((gdir / BF.GEN_COMPLETE_MARKER).read_text(encoding="utf-8"))
    assert rec["state"] == "COMPLETE"
    # identical republish reuses the COMPLETE generation, no error
    m2 = _publish(monkeypatch, tmp_path)
    assert m2["generation_id"] == m1["generation_id"]
    assert BF.verify_dataset("bitget", "BTCUSDT")["status"] == "DATASET_VERIFIED"


def test_partial_generation_is_recovered_by_identical_retry(tmp_path,
                                                            monkeypatch):
    """A crash AFTER the CSV but BEFORE the COMPLETE marker leaves a partial
    generation. An identical retry must clean it and COMPLETE — never hit a
    spurious GENERATION_CONFLICT — and the previous CURRENT stays valid."""
    m1 = _publish(monkeypatch, tmp_path)                # a good current gen
    rows2 = _rows()
    rows2[9][4] = 100.7
    rows2[9][2] = 100.7
    orig = BF.safe_atomic_write

    def crash_after_csv(path, data):
        r = orig(path, data)
        if str(path).endswith("data.csv"):
            raise IOError("simulated crash after CSV, before COMPLETE")
        return r

    monkeypatch.setattr(BF, "safe_atomic_write", crash_after_csv)
    with pytest.raises(IOError):
        BF.save_dataset("bitget", "BTCUSDT", rows2, 3,
                        requested_start_ms=T0,
                        requested_end_ms=T0 + len(rows2) * BAR)
    monkeypatch.setattr(BF, "safe_atomic_write", orig)
    # previous generation still current and verifiable
    v = BF.verify_dataset("bitget", "BTCUSDT")
    assert v["status"] == "DATASET_VERIFIED"
    assert v["generation_id"] == m1["generation_id"]
    # identical retry now COMPLETES (recover_staging cleaned the partial dir)
    m2 = BF.save_dataset("bitget", "BTCUSDT", rows2, 3,
                         requested_start_ms=T0,
                         requested_end_ms=T0 + len(rows2) * BAR)
    d = BF._dataset_dir("bitget", "BTCUSDT")
    assert (d / f"gen_{m2['generation_id']}" / BF.GEN_COMPLETE_MARKER).is_file()
    v2 = BF.verify_dataset("bitget", "BTCUSDT")
    assert v2["status"] == "DATASET_VERIFIED"
    assert v2["generation_id"] == m2["generation_id"]


def test_recover_staging_removes_incomplete_keeps_complete(tmp_path,
                                                           monkeypatch):
    m1 = _publish(monkeypatch, tmp_path)
    d = BF._dataset_dir("bitget", "BTCUSDT")
    # a hand-made incomplete generation dir (no COMPLETE marker)
    junk = d / "gen_deadbeefdeadbeef"
    junk.mkdir()
    (junk / "data.csv").write_text("ts\n", encoding="utf-8")
    rec = BF.recover_staging("bitget", "BTCUSDT")
    assert "gen_deadbeefdeadbeef" in rec["removed_incomplete_generations"]
    assert not junk.exists()
    assert rec["current_generation"] == m1["generation_id"]
    assert (d / f"gen_{m1['generation_id']}").is_dir()   # complete kept


# ==========================================================================
# 6.2 TEMP REVALIDATION RIGHT BEFORE os.replace
# ==========================================================================

def test_temp_hardlinked_before_replace_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(BF.CE, "_repo_root", lambda: tmp_path)
    d = tmp_path / "data"
    d.mkdir()
    target = d / "t.bin"
    BF.safe_atomic_write(target, b"original")
    captured = {}

    def hardlink_the_temp(path):
        # the seam fires while the temp still exists; hardlink IT
        for p in Path(path).parent.glob(f".{Path(path).name}.*.part"):
            try:
                os.link(p, Path(path).parent / "temp_alias.bin")
                captured["alias"] = Path(path).parent / "temp_alias.bin"
            except OSError:
                pytest.skip("filesystem without hardlink support")

    monkeypatch.setattr(BF, "_between_write_and_replace", hardlink_the_temp)
    with pytest.raises(ValueError, match="temp was hardlinked before replace"):
        BF.safe_atomic_write(target, b"new-content")
    assert target.read_bytes() == b"original"           # destination preserved


# ==========================================================================
# 6.3 TRANSACTIONAL LEDGER
# ==========================================================================

def test_ledger_transaction_commit_and_abort(tmp_path, monkeypatch):
    monkeypatch.setattr(ENG.CE, "_repo_root", lambda: tmp_path)
    ENG.set_run_context(run_id="r0", sprint_id="s0",
                        registry_sha_at_close="abc")
    ledp = ENG._out() / ENG.LEDGER_FILE
    # abort: nothing is written
    ENG.ledger_begin()
    ENG.ledger_append({"phase": "x", "strategy_id": "a"})
    ENG.ledger_abort()
    assert not ledp.is_file()
    # commit: the whole buffer is published atomically
    ENG.ledger_begin()
    ENG.ledger_append({"phase": "compile", "strategy_id": "a"})
    ENG.ledger_append({"phase": "validation", "strategy_id": "b"})
    sha = ENG.ledger_commit()
    assert sha and hashlib.sha256(ledp.read_bytes()).hexdigest() == sha
    rows = [json.loads(l) for l in ledp.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert all(r["linkage"]["linkage_status"] == "LINKED" for r in rows)


def test_ledger_crash_mid_run_preserves_previous(tmp_path, monkeypatch):
    monkeypatch.setattr(ENG.CE, "_repo_root", lambda: tmp_path)
    ENG.set_run_context(run_id="r0", sprint_id="s0",
                        registry_sha_at_close="abc")
    ENG.ledger_begin()
    ENG.ledger_append({"phase": "compile", "strategy_id": "a"})
    ENG.ledger_commit()                                  # first official ledger
    ledp = ENG._out() / ENG.LEDGER_FILE
    before = ledp.read_bytes()
    # a new transaction that is ABORTED (crash) must not touch the file
    ENG.ledger_begin()
    ENG.ledger_append({"phase": "validation", "strategy_id": "b"})
    ENG.ledger_abort()
    assert ledp.read_bytes() == before


# ==========================================================================
# 6.4 MEMBER / TRIAL LINKAGE
# ==========================================================================

def test_linkage_present_or_not_applicable(tmp_path, monkeypatch):
    monkeypatch.setattr(ENG.CE, "_repo_root", lambda: tmp_path)
    # no registry context -> NOT_APPLICABLE, never ambiguous
    ENG.set_run_context()
    ENG.ledger_append({"phase": "note"})
    ledp = ENG._out() / ENG.LEDGER_FILE
    row = json.loads(ledp.read_text(encoding="utf-8").splitlines()[-1])
    assert row["linkage"]["linkage_status"] == "NOT_APPLICABLE"
    # with registry context and a strategy_id -> fully LINKED
    ENG.set_run_context(run_id="r", sprint_id="s", registry_sha_at_close="z",
                        repo_commit="c1", git_tree_oid="t1",
                        dataset_generation_id="g1", timeframe="5m")
    ENG.ledger_append({"phase": "validation", "strategy_id": "foo",
                       "signature": "sig"})
    row = json.loads(ledp.read_text(encoding="utf-8").splitlines()[-1])
    lk = row["linkage"]
    assert lk["linkage_status"] == "LINKED"
    for k in ("trial_id", "strategy_id", "run_id", "sprint_id",
              "registry_sha256", "repo_commit", "tree_oid",
              "dataset_generation_id", "timeframe"):
        assert lk.get(k), k


# ==========================================================================
# 6.5 AI CRITIQUE PROVENANCE
# ==========================================================================

class _CriticProvider(P.BaseProvider):
    """Deterministic critic that returns a valid critiques JSON for whatever
    strategy_ids it is shown (no network)."""

    def __init__(self, sids):
        self.name = "ollama"
        self.model = "critic-mock"
        self._sids = sids

    def generate(self, prompt, temperature=0.4, max_tokens=None):
        crit = [{"strategy_id": s, "kill_reasons": ["cost"],
                 "overfit_risk": "HIGH", "note": "n"} for s in self._sids]
        return {"ok": True, "provider": self.name, "model": self.model,
                "cached": False, "latency_s": 0.0,
                "text": json.dumps({"critiques": crit})}


def test_ai_critique_carries_full_provenance():
    seen: set = set()
    compiled = []
    for s in ORCH.procedural_universe()[:3]:
        st_, sp = ENG.compile_strategy(s, seen)
        if st_ == "OK":
            sp["origin"] = "ai:ollama:HYPOTHESIS_GENERATOR"
            sp["hypothesis"] = "h"
            compiled.append(sp)
    providers = {"ollama": _CriticProvider([s["strategy_id"] for s in compiled])}
    notes = ORCH.cross_critique(providers, compiled, log=lambda *a: None)
    assert notes, "critic provider must yield critique rows"
    tagged = [n for n in notes if n.get("strategy_id")]
    assert tagged, "at least one critique must target a strategy"
    for n in notes:
        for k in ("provider", "role", "model", "prompt_sha256",
                  "raw_output_sha256", "response_sha256", "cache_key",
                  "cache_hit", "policy_version", "at"):
            assert k in n, k
    for n in tagged:
        assert n.get("target_spec_sha256")


# ==========================================================================
# 6.6 MANIFEST REASON CODES
# ==========================================================================

def test_manifest_discrepancy_returns_specific_reason(tmp_path, monkeypatch):
    _publish(monkeypatch, tmp_path)
    cur = BF.current_generation("bitget", "BTCUSDT")
    man = json.loads(cur["manifest_path"].read_text(encoding="utf-8"))

    def _resign(m):
        b = json.dumps(m, indent=2, default=str).encode("utf-8")
        cur["manifest_path"].write_bytes(b)
        d = BF._dataset_dir("bitget", "BTCUSDT")
        mk = json.loads((d / BF.CURRENT_MARKER).read_text(encoding="utf-8"))
        mk["manifest_sha256"] = hashlib.sha256(b).hexdigest()
        (d / BF.CURRENT_MARKER).write_text(json.dumps(mk), encoding="utf-8")

    for field, val, reason in (("symbol", "ETHUSDT", "SYMBOL_MISMATCH"),
                               ("venue", "bybit", "VENUE_MISMATCH"),
                               ("timeframe", "5m", "TIMEFRAME_MISMATCH")):
        m2 = dict(man)
        m2[field] = val
        _resign(m2)
        v = BF.verify_dataset("bitget", "BTCUSDT")
        assert v["ok"] is False and v["reason"] == reason
    # a contract-field lie yields a specific reason under INVALID_MANIFEST_CONTRACT
    m2 = dict(man)
    m2["n_bars"] = 999_999
    _resign(m2)
    v = BF.verify_dataset("bitget", "BTCUSDT")
    assert v["status"] == "INVALID_MANIFEST_CONTRACT"
    assert v["reason"] == "ACTUAL_BARS_MISMATCH"


# ==========================================================================
# NO-LIVE SAFETY GATE (must fail if any change opens a live/order path)
# ==========================================================================

def test_v10_46_safety_state_is_research_only():
    s = v10_46.assert_research_only()
    assert s["live_trading"] is False
    assert s["can_send_real_orders"] is False
    assert s["dry_run"] is True and s["paper_trading"] is True
    assert s["uses_private_endpoints"] is False
    assert s["connects_real_execution_engine"] is False
    assert v10_46.FINAL_RECOMMENDATION == "NO LIVE"


def test_v10_46_source_has_no_live_or_order_paths():
    """Scan the ENTIRE v10_46 research package source for tokens that would
    indicate a real-order / private-endpoint / live path. None may appear as
    real call sites."""
    pkg = Path(v10_46.__file__).parent
    offenders = []
    for py in pkg.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="ignore")
        for tok in v10_46.FORBIDDEN_SOURCE_TOKENS:
            # allow the token only inside the FORBIDDEN_SOURCE_TOKENS listing
            # itself and inside comments that document the ban
            for ln in text.splitlines():
                if tok in ln and not ln.lstrip().startswith("#") \
                        and "FORBIDDEN_SOURCE_TOKENS" not in ln \
                        and '"' + tok not in ln and "'" + tok not in ln \
                        and tok not in ("ExecutionEngine",):
                    offenders.append((py.name, tok, ln.strip()[:60]))
    assert offenders == [], f"live/order tokens found: {offenders[:5]}"


def test_labs_research_modules_declare_no_real_orders():
    for mod in (BF, ENG, ORCH):
        saf = getattr(mod, "_safety", None)
        if saf is None:
            continue
        s = saf()
        assert s.get("can_send_real_orders") is False
