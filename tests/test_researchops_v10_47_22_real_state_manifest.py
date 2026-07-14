"""V10.47.22 adversarial real-state manifest and deterministic seal contract."""

from __future__ import annotations

import json
import os
import subprocess
import copy
import hashlib
from pathlib import Path

import pytest


def _git(root: Path, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def _repo(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.invalid"],
                   cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=root, check=True)
    paths = {
        "dataset": "external_data/dataset.csv",
        "dataset_manifest": "external_data/manifest.json",
        "spec": "specs/strategy.json",
        "registry": "registry/closed.json",
        "holdout": "sealed_holdout/commitment.json",
        "policy": "app/policy.py",
        "ledger": "outputs/ledger.json",
        "tournament": "outputs/tournament.json",
        "report": "reports/report.md",
        "dashboard": "reports/dashboard.html",
        "audit": ".ai_coordination/reviews/audit.md",
        "hub": ".ai_coordination/CURRENT_STATE.md",
        "collection_log": "logs/collection.txt",
        "execution_log": "logs/execution.log",
        "test_nodeids": "logs/nodeids.txt",
    }
    for category, rel in paths.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if category == "execution_log":
            path.write_text("placeholder", encoding="utf-8")
        else:
            path.write_text(f"{category}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
                   cwd=root, check=True)
    coverage = {key: [rel] for key, rel in paths.items()}
    return root, coverage


def _manifest(root, coverage):
    from app.labs.v10_46 import manifest_seal as MZ

    return MZ.build_manifest(root=str(root), coverage=coverage)


def _certify_execution(root, coverage, **overrides):
    execution_rel = coverage["execution_log"][0]
    raw_execution_rel = "logs/pytest_execution.log"
    collection_record_rel = "logs/collection_record.json"
    subprocess.run(["git", "rm", "--cached", "-q", execution_rel], cwd=root,
                   check=True)
    (root / ".gitignore").write_text(
        f"/{execution_rel}\n/{raw_execution_rel}\n/{collection_record_rel}\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", ".gitignore"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "ignore certified execution"],
                   cwd=root, check=True)
    raw_execution = root / raw_execution_rel
    raw_execution.write_text("10 passed in 0.01s\n", encoding="utf-8")
    collection_record = root / collection_record_rel
    collection_record.write_text('{"unique_nodeids":10}\n', encoding="utf-8")
    sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    record = {
        "schema": "v10_47_22_certified_execution",
        "branch": _git(root, "branch", "--show-current"),
        "head": _git(root, "rev-parse", "HEAD"),
        "tree": _git(root, "rev-parse", "HEAD^{tree}"),
        "exit_code": 0,
        "collected": 10,
        "unique_nodeids": 10,
        "passed": 10,
        "failed": 0,
        "duplicate_nodeids": [],
        "research_only": True,
        "can_send_real_orders": False,
        "final_recommendation": "NO LIVE",
        "raw_log_sha256": sha(raw_execution),
        "nodeids_sha256": sha(root / coverage["test_nodeids"][0]),
        "collection_record_sha256": sha(collection_record),
    }
    record.update(overrides)
    (root / execution_rel).write_text(json.dumps(record), encoding="utf-8")
    coverage["execution_log"].append(raw_execution_rel)
    coverage["collection_log"].append(collection_record_rel)
    return record


def test_manifest_is_deterministic_without_generated_timestamp(tmp_path):
    root, coverage = _repo(tmp_path)
    a = _manifest(root, coverage)
    b = _manifest(root, coverage)
    assert "generated_utc" not in a["payload"]
    assert a["manifest_payload_sha256"] == b["manifest_payload_sha256"]
    assert a["seal_sha256"] == b["seal_sha256"]


def test_manifest_verifier_compares_current_git_head_and_tree(tmp_path):
    from app.labs.v10_46 import manifest_seal as MZ

    root, coverage = _repo(tmp_path)
    manifest = _manifest(root, coverage)
    (root / "new.txt").write_text("new", encoding="utf-8")
    subprocess.run(["git", "add", "new.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "new head"], cwd=root, check=True)
    result = MZ.verify_manifest(manifest, root=str(root))
    assert result["ok"] is False
    assert "git:head" in result["problems"]
    assert "git:tree" in result["problems"]


@pytest.mark.parametrize("category", [
    "dataset", "dataset_manifest", "spec", "registry", "holdout", "policy", "ledger",
    "tournament", "report", "dashboard", "audit", "hub", "collection_log",
    "execution_log", "test_nodeids",
])
def test_mutating_covered_real_state_breaks_verify(tmp_path, category):
    from app.labs.v10_46 import manifest_seal as MZ

    root, coverage = _repo(tmp_path)
    manifest = _manifest(root, coverage)
    target = root / coverage[category][0]
    target.write_text(target.read_text(encoding="utf-8") + "mutated\n",
                      encoding="utf-8")
    result = MZ.verify_manifest(manifest, root=str(root))
    assert result["ok"] is False
    assert any(category in problem for problem in result["problems"])


def test_dirty_tracked_state_breaks_verify(tmp_path):
    from app.labs.v10_46 import manifest_seal as MZ

    root, coverage = _repo(tmp_path)
    manifest = _manifest(root, coverage)
    (root / coverage["policy"][0]).write_text("dirty", encoding="utf-8")
    result = MZ.verify_manifest(manifest, root=str(root))
    assert result["ok"] is False
    assert "git:dirty_tracked" in result["problems"]


def test_collection_and_execution_logs_bind_head_tree(tmp_path):
    root, coverage = _repo(tmp_path)
    head, tree = _git(root, "rev-parse", "HEAD"), _git(root, "rev-parse", "HEAD^{tree}")
    execution = root / coverage["execution_log"][0]
    execution.write_text(json.dumps({
        "schema": "v10_47_22_certified_execution",
        "head": head, "tree": tree, "exit_code": 0,
    }),
                         encoding="utf-8")
    manifest = _manifest(root, coverage)
    assert manifest["payload"]["certified_execution"]["head"] == head
    assert manifest["payload"]["certified_execution"]["tree"] == tree
    assert manifest["payload"]["certified_execution"]["exit_code"] == 0


def test_seal_text_contains_required_roots_and_self_hash(tmp_path):
    from app.labs.v10_46 import manifest_seal as MZ

    root, coverage = _repo(tmp_path)
    manifest = _manifest(root, coverage)
    seal_path = tmp_path / "SEAL.txt"
    MZ.write_seal_text(manifest, seal_path)
    parsed = MZ.read_seal_text(seal_path)
    for field in (
        "schema", "branch", "head", "tree", "origin_main",
        "manifest_payload_sha256", "dataset_root_hash", "spec_root_hash",
        "registry_hash", "holdout_commitment_hash", "policy_root_hash",
        "ledger_root_hash", "output_root_hash", "test_root_hash",
        "audit_root_hash", "hub_root_hash", "seal_sha256",
    ):
        assert field in parsed
    assert MZ.verify_seal_text(manifest, seal_path)["ok"] is True


def test_manifest_rejects_missing_required_coverage_category(tmp_path):
    from app.labs.v10_46 import manifest_seal as MZ

    root, coverage = _repo(tmp_path)
    coverage.pop("policy")
    with pytest.raises(MZ.ManifestCoverageError):
        MZ.build_manifest(root=str(root), coverage=coverage)


def test_clean_certified_execution_produces_verifiable_real_state_manifest(tmp_path):
    from app.labs.v10_46 import manifest_seal as MZ

    root, coverage = _repo(tmp_path)
    _certify_execution(root, coverage)
    manifest = _manifest(root, coverage)
    assert manifest["payload"]["certification_ready"] is True
    assert MZ.verify_manifest(manifest, root=str(root))["ok"] is True


def test_unstructured_execution_log_cannot_certify(tmp_path):
    from app.labs.v10_46 import manifest_seal as MZ

    root, coverage = _repo(tmp_path)
    manifest = _manifest(root, coverage)
    result = MZ.verify_manifest(manifest, root=str(root))
    assert manifest["payload"]["certification_ready"] is False
    assert result["ok"] is False
    assert "execution_log:not_certified" in result["problems"]


@pytest.mark.parametrize("override,problem", [
    ({"exit_code": 1, "failed": 1}, "execution_log:exit_code"),
    ({"duplicate_nodeids": ["tests/test_x.py::test_x"]},
     "execution_log:duplicate_nodeids"),
    ({"head": "0" * 40}, "execution_log:head"),
    ({"tree": "1" * 40}, "execution_log:tree"),
    ({"unique_nodeids": 9}, "execution_log:unique_nodeids_mismatch"),
])
def test_bad_certified_execution_fails_closed(tmp_path, override, problem):
    from app.labs.v10_46 import manifest_seal as MZ

    root, coverage = _repo(tmp_path)
    _certify_execution(root, coverage, **override)
    manifest = _manifest(root, coverage)
    result = MZ.verify_manifest(manifest, root=str(root))
    assert manifest["payload"]["certification_ready"] is False
    assert result["ok"] is False
    assert problem in result["problems"]


@pytest.mark.parametrize("unsafe", ["../outside.txt", "C:/outside.txt"])
def test_coverage_rejects_traversal_and_absolute_paths(tmp_path, unsafe):
    from app.labs.v10_46 import manifest_seal as MZ

    root, coverage = _repo(tmp_path)
    coverage["policy"] = [unsafe]
    with pytest.raises(MZ.ManifestCoverageError):
        _manifest(root, coverage)


def test_coverage_rejects_hardlinked_identity_across_categories(tmp_path):
    from app.labs.v10_46 import manifest_seal as MZ

    root, coverage = _repo(tmp_path)
    hardlink = root / "hardlinked-policy.py"
    try:
        os.link(root / coverage["dataset"][0], hardlink)
    except OSError as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")
    coverage["policy"] = [hardlink.relative_to(root).as_posix()]
    with pytest.raises(MZ.ManifestCoverageError):
        _manifest(root, coverage)


def test_coverage_rejects_symlink_file(tmp_path):
    from app.labs.v10_46 import manifest_seal as MZ

    root, coverage = _repo(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("outside", encoding="utf-8")
    link = root / "linked-policy.py"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    coverage["policy"] = [link.relative_to(root).as_posix()]
    with pytest.raises(MZ.ManifestCoverageError):
        _manifest(root, coverage)


def test_manifest_payload_or_readability_alias_tamper_fails(tmp_path):
    from app.labs.v10_46 import manifest_seal as MZ

    root, coverage = _repo(tmp_path)
    _certify_execution(root, coverage)
    manifest = _manifest(root, coverage)
    payload_tamper = copy.deepcopy(manifest)
    payload_tamper["payload"]["safety"]["live_trading"] = True
    assert MZ.verify_manifest(payload_tamper, root=str(root))["ok"] is False
    alias_tamper = copy.deepcopy(manifest)
    alias_tamper["dataset_root_hash"] = "0" * 64
    result = MZ.verify_manifest(alias_tamper, root=str(root))
    assert result["ok"] is False
    assert "manifest:root_alias:dataset_root_hash" in result["problems"]


@pytest.mark.parametrize("mutation,problem", [
    (lambda payload: payload["coverage"].__setitem__("policy", []),
     "policy:empty_coverage"),
    (lambda payload: payload["coverage"].__setitem__("policy", ["bad-entry"]),
     "policy:malformed_entry"),
    (lambda payload: payload.__setitem__("git", ["bad-git"]),
     "git:malformed"),
])
def test_malformed_self_consistent_manifest_fails_closed_without_exception(
        tmp_path, mutation, problem):
    from app.labs.v10_46 import manifest_seal as MZ

    root, coverage = _repo(tmp_path)
    _certify_execution(root, coverage)
    payload = copy.deepcopy(_manifest(root, coverage)["payload"])
    mutation(payload)
    malformed = MZ._assemble_manifest(payload)
    result = MZ.verify_manifest(malformed, root=str(root))
    assert result["ok"] is False
    assert problem in result["problems"]


def test_tournament_ledger_index_is_hash_bound_and_consistent():
    from app.labs.v10_46 import causal_ledger as CL
    from app.labs.v10_46 import causal_tournament as CT
    from app.labs.v10_46 import contracts as C

    ledger = CL.ImmutableLedger()
    ledger.append("SIGNAL", trade_id="T1")
    ledger.append("ENTRY", trade_id="T1")
    ledger.append("POSITION", trade_id="T1")
    ledger.append("CLOSE", trade_id="T1")
    trades = [{"trade_id": "T1"}]
    audit = CT._ledger_integrity(ledger, trades)
    assert audit["sequence_contiguous"] is True
    assert audit["close_matches_trade_count"] is True
    assert audit["unique_trade_ids"] == 1
    assert audit["ledger_sha256"] == C.canonical_hash(ledger.records())


def test_holdout_capability_cannot_be_reactivated_or_reused(tmp_path):
    from dataclasses import FrozenInstanceError

    from app.labs.v10_46 import holdout_loader as HL
    from tests.test_researchops_v10_47_20_validation_holdout import _make_isolated_tree

    _, sealed, secret = _make_isolated_tree(tmp_path)
    authority = HL.ExternalHoldoutAuthority(sealed, secret=secret)
    capability = authority.issue_capability(reason="synthetic", audit_ref="TEST")
    authority.load_once(capability)
    with pytest.raises(FrozenInstanceError):
        capability.capability_id = "reactivated"
    with pytest.raises(HL.HoldoutAccessDenied):
        authority.load_once(capability)
    restarted = HL.ExternalHoldoutAuthority(sealed, secret=secret)
    with pytest.raises(HL.HoldoutAccessDenied):
        restarted.load_once(capability)


def test_commitment_metadata_import_does_not_import_holdout_loader():
    code = (
        "import sys; import app.labs.v10_46.sealed_holdout; "
        "raise SystemExit(int('app.labs.v10_46.holdout_loader' in sys.modules))"
    )
    completed = subprocess.run([os.sys.executable, "-c", code], check=False)
    assert completed.returncode == 0


def test_holdout_commitment_metadata_tamper_is_rejected(tmp_path):
    from app.labs.v10_46 import sealed_holdout as SH

    path = tmp_path / "commitment.json"
    document = SH.commitment_document(
        symbol="BTCUSDT", timeframe="1m", data_file="bars.json",
        data_sha256="a" * 64, authority_key_sha256="b" * 64,
        n_bars=2, index_range=(8, 10),
    )
    document["n_bars"] = 3
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SH.HoldoutAccessDenied, match="row count|metadata hash"):
        SH.load_commitment(path)


def test_tournament_holdout_touch_flag_is_derived_from_discovery_contract():
    source = Path("app/labs/v10_46/causal_tournament.py").read_text(encoding="utf-8")
    assert '"holdout_touched": False' not in source
    assert 'hasattr(discovery_partitions, "holdout")' in source


def test_certified_runner_parsers_count_unique_nodeids_and_outcomes():
    from scripts import v10_47_22_certified_test_runner as runner

    collection = "tests/test_a.py::test_one\ntests/test_b.py::test_two\n2 tests collected\n"
    assert runner.parse_nodeids(collection) == [
        "tests/test_a.py::test_one", "tests/test_b.py::test_two"
    ]
    assert runner.parse_pytest_summary(
        "3000 passed, 2 skipped, 1 xfailed in 12.34s"
    ) == {
        "passed": 3000, "failed": 0, "skipped": 2,
        "xfailed": 1, "xpassed": 0, "deselected": 0,
    }
