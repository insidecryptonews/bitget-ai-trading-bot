"""Build and independently verify the final V10.47.22 real-state seal.

This script is research-only evidence plumbing. It accepts only already-generated
files below the repository, requires a clean tracked tree, and never reads config,
credentials, databases, exchange APIs, or holdout rows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.labs.v10_46 import manifest_seal as MZ  # noqa: E402


REPORT_ROOT = ROOT / "reports" / "research" / "v10_47_22_real_state_certification"
DATA_ROOT = ROOT / "external_data" / "staging" / "v10_47_22_isolated"


def git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=ROOT, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if completed.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rel(path: Path) -> str:
    resolved = path.resolve(strict=True)
    return resolved.relative_to(ROOT.resolve(strict=True)).as_posix()


def safe_label(value: str) -> str:
    if not value or not value.replace("_", "").replace("-", "").isalnum():
        raise RuntimeError("unsafe evidence label")
    return value


def safe_existing_dir(path: Path) -> Path:
    root = ROOT.resolve(strict=True)
    if path.is_symlink():
        raise RuntimeError(f"symlink directory forbidden: {path}")
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    cursor = path.absolute()
    while cursor != root:
        if cursor.is_symlink():
            raise RuntimeError(f"symlink ancestor forbidden: {path}")
        cursor = cursor.parent
    return resolved


def repo_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def atomic_text(path: Path, value: str) -> None:
    temporary = path.with_name(path.name + f".tmp.{uuid.uuid4().hex}")
    temporary.write_text(value, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def atomic_json(path: Path, value: dict) -> None:
    atomic_text(
        path, json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    )


def require_certified_execution(certified_root: Path) -> dict:
    record_path = certified_root / "execution_record.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    expected_head = git("rev-parse", "HEAD")
    expected_tree = git("rev-parse", "HEAD^{tree}")
    if record.get("schema") != "v10_47_22_certified_execution":
        raise RuntimeError("unexpected certified execution schema")
    if record.get("head") != expected_head or record.get("tree") != expected_tree:
        raise RuntimeError("certified execution does not match current HEAD/tree")
    if record.get("exit_code") != 0 or record.get("failed") != 0:
        raise RuntimeError("certified suite did not pass")
    if record.get("unique_nodeids") != record.get("collected"):
        raise RuntimeError("certified suite nodeid count is not unique")
    if record.get("duplicate_nodeids"):
        raise RuntimeError("certified suite contains duplicate nodeids")
    raw_log = certified_root / "pytest_execution.log"
    nodeids = certified_root / "pytest_nodeids.txt"
    collection = certified_root / "collection_record.json"
    if sha256(raw_log) != record.get("raw_log_sha256"):
        raise RuntimeError("certified execution log hash mismatch")
    if sha256(nodeids) != record.get("nodeids_sha256"):
        raise RuntimeError("certified nodeid hash mismatch")
    if sha256(collection) != record.get("collection_record_sha256"):
        raise RuntimeError("certified collection record hash mismatch")
    return record


def preseal_audit(coverage: dict[str, list[str]], execution: dict) -> dict:
    tournament_files = coverage.get("tournament", [])
    return {
        "schema": "v10_47_22_preseal_audit",
        "head": git("rev-parse", "HEAD"),
        "tree": git("rev-parse", "HEAD^{tree}"),
        "tracked_clean": not bool(git("status", "--porcelain=v1", "--untracked-files=no")),
        "all_required_categories_present": set(coverage) == set(
            MZ.REQUIRED_COVERAGE_CATEGORIES
        ),
        "all_categories_nonempty": all(coverage.get(name) for name in MZ.REQUIRED_COVERAGE_CATEGORIES),
        "tournament_json_files": len([p for p in tournament_files if p.endswith(".json")]),
        "certified_collected": execution.get("collected"),
        "certified_unique_nodeids": execution.get("unique_nodeids"),
        "certified_failed": execution.get("failed"),
        "work_certification": "PENDING_WORK_REAUDIT",
        "scientifically_certified": False,
        "no_confirmed_edge": True,
        "shadow_candidates": 0,
        "holdout_state": "SEALED",
        "research_only": True,
        "can_send_real_orders": False,
        "final_recommendation": "NO LIVE",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-label", required=True)
    parser.add_argument("--tournament-label", required=True)
    parser.add_argument("--certified-test-dir", required=True)
    parser.add_argument("--security-audit-log", required=True)
    parser.add_argument("--reproduction-audit", required=True)
    args = parser.parse_args(argv)

    evidence_label = safe_label(args.evidence_label)
    tournament_label = safe_label(args.tournament_label)
    if git("status", "--porcelain=v1", "--untracked-files=no"):
        raise RuntimeError("tracked worktree must be clean before sealing")

    evidence_root = safe_existing_dir(REPORT_ROOT / "evidence" / evidence_label)
    tournament_root = safe_existing_dir(REPORT_ROOT / "tournaments" / tournament_label)
    certified_root = safe_existing_dir(repo_path(args.certified_test_dir))
    security_log = repo_path(args.security_audit_log)
    reproduction_audit = repo_path(args.reproduction_audit)
    for path in (security_log, reproduction_audit):
        path.resolve(strict=True).relative_to(ROOT.resolve(strict=True))
        if path.is_symlink():
            raise RuntimeError(f"symlink evidence forbidden: {path}")

    coverage = json.loads(
        (evidence_root / "coverage_seed.json").read_text(encoding="utf-8")
    )
    execution = require_certified_execution(certified_root)
    coverage["collection_log"] = sorted({
        rel(certified_root / "pytest_collection.log"),
        rel(certified_root / "collection_record.json"),
        *(rel(path) for path in tournament_root.glob("*.log")),
    })
    coverage["execution_log"] = sorted({
        rel(certified_root / "pytest_execution.log"),
        rel(certified_root / "execution_record.json"),
    })
    coverage["test_nodeids"] = [rel(certified_root / "pytest_nodeids.txt")]
    coverage["dataset_manifest"] = sorted({
        *coverage["dataset_manifest"], rel(DATA_ROOT / "preparation_summary.json")
    })
    coverage["audit"] = sorted({
        *coverage["audit"], rel(security_log), rel(reproduction_audit)
    })

    manifest_root = REPORT_ROOT / "manifests" / evidence_label
    manifest_root_resolved = manifest_root.resolve()
    manifest_root_resolved.relative_to(REPORT_ROOT.resolve(strict=True))
    if manifest_root.exists() or manifest_root.is_symlink():
        raise RuntimeError("manifest output already exists")
    manifest_root.mkdir(parents=True, exist_ok=False)
    preseal_path = manifest_root / "preseal_audit.json"
    audit = preseal_audit(coverage, execution)
    if not audit["tracked_clean"] or not audit["all_required_categories_present"] \
            or not audit["all_categories_nonempty"]:
        raise RuntimeError("preseal audit failed closed")
    atomic_json(preseal_path, audit)
    coverage["audit"] = sorted({*coverage["audit"], rel(preseal_path)})

    first = MZ.build_manifest(root=str(ROOT), coverage=coverage)
    second = MZ.build_manifest(root=str(ROOT), coverage=coverage)
    if first["manifest_payload_sha256"] != second["manifest_payload_sha256"] \
            or first["seal_sha256"] != second["seal_sha256"]:
        raise RuntimeError("manifest/seal is not deterministic")
    manifest_path = manifest_root / "output_manifest.json"
    seal_path = manifest_root / "SEAL.txt"
    atomic_json(manifest_path, first)
    temporary_seal = seal_path.with_name(seal_path.name + f".tmp.{uuid.uuid4().hex}")
    MZ.write_seal_text(first, temporary_seal)
    os.replace(temporary_seal, seal_path)

    loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_verify = MZ.verify_manifest(loaded, root=str(ROOT))
    seal_verify = MZ.verify_seal_text(loaded, seal_path)
    if not manifest_verify["ok"] or not seal_verify["ok"]:
        raise RuntimeError(
            f"independent verification failed: {manifest_verify}; {seal_verify}"
        )
    verification = {
        "schema": "v10_47_22_real_state_verification",
        "manifest_ok": True,
        "seal_ok": True,
        "manifest_payload_sha256": loaded["manifest_payload_sha256"],
        "seal_sha256": loaded["seal_sha256"],
        "head": loaded["payload"]["git"]["head"],
        "tree": loaded["payload"]["git"]["tree"],
        "work_certification": "PENDING_WORK_REAUDIT",
        "scientifically_certified": False,
        "final_recommendation": "NO LIVE",
    }
    atomic_json(manifest_root / "verification.json", verification)
    print(f"MANIFEST={rel(manifest_path)}")
    print(f"MANIFEST_PAYLOAD_SHA256={loaded['manifest_payload_sha256']}")
    print(f"SEAL_SHA256={loaded['seal_sha256']}")
    print("MANIFEST_VERIFY=OK")
    print("SEAL_VERIFY=OK")
    print("CERTIFICATION=PENDING_WORK_REAUDIT")
    print("FINAL_RECOMMENDATION=NO LIVE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
