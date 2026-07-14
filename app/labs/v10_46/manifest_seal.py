"""Real-state provenance manifest and deterministic seal (research only).

The V10.47.22 manifest is an independent description of the current repository
and evidence files.  It never trusts precomputed file hashes: every covered path
is resolved below the repository root and hashed from disk when the manifest is
built and whenever it is verified.  Git HEAD, tree, branch, origin and the exact
tracked-worktree status are also read again by the verifier.

The old ``out_dir=`` interface remains available as ``legacy_output`` so archived
V10.47.18 tooling can still verify its own output files.  It is deliberately not
labelled as real-state certification.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "v10_47_22_real_state_manifest"
LEGACY_SCHEMA_VERSION = "v10_47_22_legacy_output_manifest"
REQUIRED_COVERAGE_CATEGORIES = (
    "dataset",
    "dataset_manifest",
    "spec",
    "registry",
    "holdout",
    "policy",
    "ledger",
    "tournament",
    "report",
    "dashboard",
    "audit",
    "hub",
    "collection_log",
    "execution_log",
    "test_nodeids",
)


class ManifestCoverageError(ValueError):
    """The requested certification coverage is incomplete or unsafe."""


def _sha_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha_str(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _root_hash(pairs: dict[str, str]) -> str:
    """Deterministic root over a path/hash (or name/hash) mapping."""
    return _sha_str("\n".join(f"{key}:{value}" for key, value in sorted(pairs.items())))


def _git(root: str | Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=str(root), text=True, encoding="utf-8",
        errors="replace", stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "UNAVAILABLE"


def _git_state(root: str | Path) -> dict:
    tracked_status = _git(root, "status", "--porcelain=v1", "--untracked-files=no")
    return {
        "branch": _git(root, "branch", "--show-current"),
        "head": _git(root, "rev-parse", "HEAD"),
        "tree": _git(root, "rev-parse", "HEAD^{tree}"),
        "origin_main": _git(root, "rev-parse", "origin/main"),
        "dirty_tracked": tracked_status not in ("", "UNAVAILABLE"),
        "tracked_status_porcelain": tracked_status,
    }


def _resolve_root(root: str | Path) -> Path:
    raw = Path(root).absolute()
    chain = list(reversed(raw.parents)) + [raw]
    if any(path.is_symlink() for path in chain):
        raise ManifestCoverageError("repository root or ancestor may not be a symlink")
    try:
        return raw.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ManifestCoverageError("repository root does not exist") from exc


def _safe_covered_file(root: Path, relative: str) -> tuple[Path, str, tuple[int, int]]:
    raw = Path(str(relative))
    if raw.is_absolute():
        raise ManifestCoverageError(f"absolute covered path forbidden: {relative}")
    if not relative or any(part in ("", ".", "..") for part in raw.parts):
        raise ManifestCoverageError(f"unsafe covered path: {relative}")
    root_resolved = root.resolve(strict=True)
    candidate = root.joinpath(raw)
    cursor = root
    for part in raw.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ManifestCoverageError(f"symlink covered path forbidden: {relative}")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root_resolved)
    except (FileNotFoundError, ValueError) as exc:
        raise ManifestCoverageError(f"covered file unavailable/outside root: {relative}") from exc
    if not resolved.is_file():
        raise ManifestCoverageError(f"covered path is not a file: {relative}")
    stat = resolved.stat()
    normalized = resolved.relative_to(root_resolved).as_posix()
    return resolved, normalized, (int(stat.st_dev), int(stat.st_ino))


def _normalize_coverage(root: Path, coverage: dict[str, list[str]]) -> dict:
    missing = [
        category for category in REQUIRED_COVERAGE_CATEGORIES
        if category not in coverage or not coverage[category]
    ]
    if missing:
        raise ManifestCoverageError(
            "missing required coverage categories: " + ",".join(missing)
        )
    unexpected = sorted(set(coverage) - set(REQUIRED_COVERAGE_CATEGORIES))
    if unexpected:
        raise ManifestCoverageError(
            "unknown coverage categories: " + ",".join(unexpected)
        )
    seen_paths: dict[str, str] = {}
    seen_identities: dict[tuple[int, int], tuple[str, str]] = {}
    normalized: dict[str, list[dict]] = {}
    for category in REQUIRED_COVERAGE_CATEGORIES:
        entries: list[dict] = []
        for raw_path in sorted(set(str(item) for item in coverage[category])):
            full, relative, identity = _safe_covered_file(root, raw_path)
            if relative in seen_paths:
                raise ManifestCoverageError(
                    f"covered path reused by {seen_paths[relative]} and {category}: {relative}"
                )
            if identity in seen_identities:
                previous_category, previous_path = seen_identities[identity]
                raise ManifestCoverageError(
                    "covered file identity reused by "
                    f"{previous_category}:{previous_path} and {category}:{relative}"
                )
            seen_paths[relative] = category
            seen_identities[identity] = (category, relative)
            entries.append({
                "path": relative,
                "sha256": _sha_file(full),
                "size_bytes": full.stat().st_size,
            })
        normalized[category] = entries
    return normalized


def _category_roots(normalized: dict[str, list[dict]]) -> dict[str, str]:
    return {
        category: _root_hash({row["path"]: row["sha256"] for row in rows})
        for category, rows in sorted(normalized.items())
    }


def _combined_root(category_roots: dict[str, str], categories: tuple[str, ...]) -> str:
    return _root_hash({category: category_roots[category] for category in categories})


def _derive_roots(category_roots: dict[str, str]) -> dict[str, str]:
    return {
        "dataset_root_hash": _combined_root(
            category_roots, ("dataset", "dataset_manifest")
        ),
        "spec_root_hash": category_roots["spec"],
        "registry_hash": category_roots["registry"],
        "holdout_commitment_hash": category_roots["holdout"],
        "policy_root_hash": category_roots["policy"],
        "ledger_root_hash": category_roots["ledger"],
        "output_root_hash": _combined_root(
            category_roots, ("tournament", "report", "dashboard")
        ),
        "test_root_hash": _combined_root(
            category_roots, ("test_nodeids", "execution_log")
        ),
        "audit_root_hash": category_roots["audit"],
        "hub_root_hash": category_roots["hub"],
        "collection_log_root_hash": category_roots["collection_log"],
    }


def _read_certified_execution(root: Path, entries: list[dict]) -> dict:
    structured: list[tuple[str, dict]] = []
    for entry in entries:
        path = root / entry["path"]
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(raw, dict) \
                and raw.get("schema") == "v10_47_22_certified_execution" \
                and all(key in raw for key in ("head", "tree", "exit_code")):
            structured.append((entry["path"], raw))
    if len(structured) != 1:
        return {
            "status": "UNSTRUCTURED_EXECUTION_LOG"
            if not structured else "AMBIGUOUS_EXECUTION_LOG",
            "structured_records": len(structured),
        }
    record_path, raw = structured[0]
    allowed = (
        "head", "tree", "branch", "exit_code", "started_utc", "ended_utc",
        "duration_seconds", "collected", "passed", "failed", "skipped",
        "xfailed", "xpassed", "deselected", "unique_nodeids",
        "nodeids_sha256", "duplicate_nodeids", "raw_log_sha256",
        "collection_record_sha256", "collection_command", "execution_command",
        "schema", "research_only", "can_send_real_orders", "final_recommendation",
    )
    result = {key: raw[key] for key in allowed if key in raw}
    result["record_path"] = record_path
    result["status"] = "CERTIFIED_EXECUTION"
    return result


def _execution_ready(execution: dict, git_state: dict) -> bool:
    try:
        collected = int(execution.get("collected", 0))
        unique = int(execution.get("unique_nodeids", -1))
        accounted = sum(
            int(execution.get(name, 0))
            for name in ("passed", "skipped", "xfailed")
        )
    except (TypeError, ValueError):
        return False
    required_git = ("branch", "head", "tree", "origin_main")
    if any(git_state.get(name) in (None, "", "UNAVAILABLE") for name in required_git):
        return False
    return bool(
        execution.get("status") == "CERTIFIED_EXECUTION"
        and execution.get("schema") == "v10_47_22_certified_execution"
        and execution.get("branch") == git_state.get("branch")
        and execution.get("head") == git_state.get("head")
        and execution.get("tree") == git_state.get("tree")
        and execution.get("exit_code") == 0
        and execution.get("failed", 0) == 0
        and execution.get("xpassed", 0) == 0
        and not execution.get("duplicate_nodeids", [])
        and collected > 0
        and unique == collected
        and accounted == collected
        and execution.get("research_only") is True
        and execution.get("can_send_real_orders") is False
        and execution.get("final_recommendation") == "NO LIVE"
    )


def _seal_hash(payload_sha256: str, payload: dict) -> str:
    roots = payload.get("roots", {})
    basis = {
        "manifest_payload_sha256": payload_sha256,
        "git": payload.get("git", {}),
        "roots": roots,
        "schema": payload.get("schema"),
        "mode": payload.get("mode"),
    }
    return _sha_str(_canonical(basis))


def _assemble_manifest(payload: dict) -> dict:
    payload_sha = _sha_str(_canonical(payload))
    manifest = {
        "payload": payload,
        "manifest_payload_sha256": payload_sha,
        "seal_sha256": _seal_hash(payload_sha, payload),
    }
    # Compatibility/readability aliases.  Payload remains the source of truth.
    manifest.update(payload.get("roots", {}))
    manifest["git"] = payload.get("git", {})
    manifest["schema_version"] = payload.get("schema")
    return manifest


def _build_real_state(root: Path, coverage: dict[str, list[str]]) -> dict:
    normalized = _normalize_coverage(root, coverage)
    category_roots = _category_roots(normalized)
    git_state = _git_state(root)
    certified_execution = _read_certified_execution(
        root, normalized["execution_log"]
    )
    certification_ready = bool(
        not git_state["dirty_tracked"]
        and _execution_ready(certified_execution, git_state)
    )
    payload = {
        "schema": SCHEMA_VERSION,
        "mode": "real_state",
        "git": git_state,
        "safety": {
            "paper_trading": True,
            "live_trading": False,
            "dry_run": True,
            "paper_filter_enabled": False,
            "can_send_real_orders": False,
            "final_recommendation": "NO LIVE",
        },
        "coverage": normalized,
        "category_roots": category_roots,
        "roots": _derive_roots(category_roots),
        "certified_execution": certified_execution,
        "certification_ready": certification_ready,
        "work_certification": "PENDING_WORK_REAUDIT",
        "scientifically_certified": False,
    }
    return _assemble_manifest(payload)


def _legacy_files(root: Path, out_dir: str | Path) -> dict[str, str]:
    root_resolved = root.resolve(strict=True)
    output = Path(out_dir).resolve(strict=True)
    try:
        output.relative_to(root_resolved)
    except ValueError as exc:
        raise ManifestCoverageError("legacy out_dir must be inside root") from exc
    files: dict[str, str] = {}
    for path in sorted(output.rglob("*")):
        if not path.is_file() or "manifests" in path.relative_to(output).parts:
            continue
        if path.suffix.lower() not in (".json", ".md", ".html", ".log"):
            continue
        relative = path.relative_to(root_resolved).as_posix()
        files[relative] = _sha_file(path)
    return files


def _build_legacy(*, root: Path, out_dir: str | Path,
                  dataset_hashes: dict | None, spec_hashes: dict | None,
                  registry_hash: str | None, split_spec_hash: str | None,
                  holdout_commitment_hash: str | None,
                  extra_provenance: dict | None) -> dict:
    files = _legacy_files(root, out_dir)
    dataset_hashes = dict(sorted((dataset_hashes or {}).items()))
    spec_hashes = dict(sorted((spec_hashes or {}).items()))
    payload = {
        "schema": LEGACY_SCHEMA_VERSION,
        "mode": "legacy_output",
        "git": _git_state(root),
        "safety": {
            "paper_trading": True, "live_trading": False, "dry_run": True,
            "paper_filter_enabled": False, "can_send_real_orders": False,
            "final_recommendation": "NO LIVE",
        },
        "legacy_files": files,
        "legacy_inputs": {
            "dataset_hashes": dataset_hashes,
            "spec_hashes": spec_hashes,
            "registry_hash": registry_hash or "",
            "split_spec_hash": split_spec_hash or "",
            "holdout_commitment_hash": holdout_commitment_hash or "",
            "extra_provenance": extra_provenance or {},
        },
        "roots": {
            "output_root_hash": _root_hash(files),
            "dataset_root_hash": _root_hash(dataset_hashes),
            "spec_root_hash": _root_hash(spec_hashes),
            "registry_hash": registry_hash or "",
            "holdout_commitment_hash": holdout_commitment_hash or "",
        },
    }
    manifest = _assemble_manifest(payload)
    manifest["files_sha256"] = files
    manifest["dataset_hashes"] = dataset_hashes
    manifest["spec_hashes"] = spec_hashes
    return manifest


def build_manifest(*, root: str, coverage: dict[str, list[str]] | None = None,
                   out_dir: str | None = None,
                   dataset_hashes: dict | None = None,
                   spec_hashes: dict | None = None,
                   registry_hash: str | None = None,
                   split_spec_hash: str | None = None,
                   holdout_commitment_hash: str | None = None,
                   extra_provenance: dict | None = None) -> dict:
    """Build either a strict real-state manifest or a legacy output manifest."""
    root_path = _resolve_root(root)
    if coverage is not None:
        if out_dir is not None:
            raise ManifestCoverageError("coverage and out_dir modes are exclusive")
        return _build_real_state(root_path, coverage)
    if out_dir is None:
        raise ManifestCoverageError("coverage is required for real-state certification")
    return _build_legacy(
        root=root_path, out_dir=out_dir, dataset_hashes=dataset_hashes,
        spec_hashes=spec_hashes, registry_hash=registry_hash,
        split_spec_hash=split_spec_hash,
        holdout_commitment_hash=holdout_commitment_hash,
        extra_provenance=extra_provenance,
    )


def _compare_git(recorded: dict, current: dict, problems: list[str]) -> None:
    if not isinstance(recorded, dict):
        problems.append("git:malformed")
        return
    for field in (
        "branch", "head", "tree", "origin_main", "dirty_tracked",
        "tracked_status_porcelain",
    ):
        if recorded.get(field) != current.get(field):
            label = "dirty_tracked" if field == "tracked_status_porcelain" else field
            problem = f"git:{label}"
            if problem not in problems:
                problems.append(problem)


def _verify_real_state(payload: dict, root: Path, problems: list[str]) -> None:
    coverage = payload.get("coverage", {})
    if not isinstance(coverage, dict):
        problems.append("coverage:malformed")
        return
    if set(coverage) != set(REQUIRED_COVERAGE_CATEGORIES):
        problems.append("coverage:required_categories")
        return
    current_roots: dict[str, str] = {}
    seen_identity: dict[tuple[int, int], str] = {}
    recorded_category_roots = payload.get("category_roots", {})
    if not isinstance(recorded_category_roots, dict):
        recorded_category_roots = {}
        problems.append("category_roots:malformed")
    recorded_roots = payload.get("roots", {})
    if not isinstance(recorded_roots, dict):
        recorded_roots = {}
        problems.append("roots:malformed")
    for category in REQUIRED_COVERAGE_CATEGORIES:
        rows = coverage.get(category)
        if not isinstance(rows, list) or not rows:
            problems.append(f"{category}:empty_coverage")
            rows = []
        pairs: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                problems.append(f"{category}:malformed_entry")
                continue
            relative = row.get("path", "")
            try:
                full, normalized, identity = _safe_covered_file(root, relative)
            except ManifestCoverageError:
                problems.append(f"{category}:unsafe_or_missing:{relative}")
                continue
            if identity in seen_identity:
                problems.append(
                    f"{category}:duplicate_file_identity:{normalized}"
                )
            seen_identity[identity] = normalized
            current_hash = _sha_file(full)
            pairs[normalized] = current_hash
            if normalized != relative or current_hash != row.get("sha256") \
                    or full.stat().st_size != row.get("size_bytes"):
                problems.append(f"{category}:stale:{relative}")
        current_roots[category] = _root_hash(pairs)
        if current_roots[category] != recorded_category_roots.get(category):
            problem = f"{category}:root_hash"
            if problem not in problems:
                problems.append(problem)
    if not problems or all(not item.endswith("required_categories") for item in problems):
        derived = _derive_roots(current_roots)
        for name, value in derived.items():
            if recorded_roots.get(name) != value:
                problems.append(f"roots:{name}")
    execution = payload.get("certified_execution", {})
    if not isinstance(execution, dict):
        execution = {}
        problems.append("execution_log:malformed")
    if execution.get("status") == "CERTIFIED_EXECUTION":
        git_state = payload.get("git", {})
        if not isinstance(git_state, dict):
            git_state = {}
        if execution.get("head") != git_state.get("head"):
            problems.append("execution_log:head")
        if execution.get("tree") != git_state.get("tree"):
            problems.append("execution_log:tree")
        if execution.get("branch") != git_state.get("branch"):
            problems.append("execution_log:branch")
        if execution.get("exit_code") != 0:
            problems.append("execution_log:exit_code")
        if execution.get("failed", 0) != 0:
            problems.append("execution_log:failed_tests")
        if execution.get("duplicate_nodeids", []):
            problems.append("execution_log:duplicate_nodeids")
        if execution.get("xpassed", 0) != 0:
            problems.append("execution_log:unexpected_xpass")
        try:
            accounted = sum(
                int(execution.get(name, 0))
                for name in ("passed", "skipped", "xfailed")
            )
            if int(execution.get("collected", 0)) <= 0 \
                    or accounted != int(execution.get("collected", 0)):
                problems.append("execution_log:count_mismatch")
            if int(execution.get("unique_nodeids", -1)) != int(
                    execution.get("collected", 0)):
                problems.append("execution_log:unique_nodeids_mismatch")
        except (TypeError, ValueError):
            problems.append("execution_log:count_mismatch")
        execution_entries = coverage.get("execution_log", [])
        nodeid_entries = coverage.get("test_nodeids", [])
        collection_entries = coverage.get("collection_log", [])
        if execution.get("raw_log_sha256") not in {
            row.get("sha256") for row in execution_entries
        }:
            problems.append("execution_log:raw_log_sha256")
        if execution.get("nodeids_sha256") not in {
            row.get("sha256") for row in nodeid_entries
        }:
            problems.append("execution_log:nodeids_sha256")
        if execution.get("collection_record_sha256") not in {
            row.get("sha256") for row in collection_entries
        }:
            problems.append("execution_log:collection_record_sha256")
    else:
        problems.append("execution_log:not_certified")
    payload_git = payload.get("git", {})
    if not isinstance(payload_git, dict):
        payload_git = {}
    expected_ready = bool(
        not payload_git.get("dirty_tracked")
        and _execution_ready(execution, payload_git)
    )
    if payload_git.get("dirty_tracked"):
        problems.append("git:dirty_tracked_at_build")
    if payload.get("certification_ready") is not expected_ready:
        problems.append("manifest:certification_ready")
    if not expected_ready:
        problems.append("manifest:not_certification_ready")
    if payload.get("work_certification") != "PENDING_WORK_REAUDIT":
        problems.append("manifest:work_certification")
    if payload.get("scientifically_certified") is not False:
        problems.append("manifest:scientifically_certified")
    expected_safety = {
        "paper_trading": True,
        "live_trading": False,
        "dry_run": True,
        "paper_filter_enabled": False,
        "can_send_real_orders": False,
        "final_recommendation": "NO LIVE",
    }
    if payload.get("safety") != expected_safety:
        problems.append("manifest:safety_contract")


def _verify_legacy(payload: dict, root: Path, problems: list[str]) -> None:
    pairs: dict[str, str] = {}
    for relative, expected in payload.get("legacy_files", {}).items():
        try:
            full, normalized, _identity = _safe_covered_file(root, relative)
        except ManifestCoverageError:
            problems.append(f"missing:{relative}")
            continue
        current = _sha_file(full)
        pairs[normalized] = current
        if current != expected:
            problems.append(f"stale:{relative}")
    if _root_hash(pairs) != payload.get("roots", {}).get("output_root_hash"):
        problems.append("stale:output_root_hash")


def verify_manifest(manifest: dict, *, root: str) -> dict:
    """Re-read covered files and Git; no value is trusted from the manifest."""
    root_path = _resolve_root(root)
    if not isinstance(manifest, dict):
        return {
            "ok": False, "payload_ok": False, "seal_ok": False,
            "problems": ["manifest:malformed"],
            "recomputed_payload_sha256": "", "recomputed_seal_sha256": "",
        }
    payload = manifest.get("payload")
    if not isinstance(payload, dict):
        return {
            "ok": False, "payload_ok": False, "seal_ok": False,
            "problems": ["manifest:unsupported_legacy_schema"],
            "recomputed_payload_sha256": "", "recomputed_seal_sha256": "",
        }
    problems: list[str] = []
    _compare_git(payload.get("git", {}), _git_state(root_path), problems)
    if payload.get("mode") == "real_state":
        _verify_real_state(payload, root_path, problems)
    elif payload.get("mode") == "legacy_output":
        _verify_legacy(payload, root_path, problems)
    else:
        problems.append("manifest:unknown_mode")
    payload_sha = _sha_str(_canonical(payload))
    seal_sha = _seal_hash(payload_sha, payload)
    payload_ok = payload_sha == manifest.get("manifest_payload_sha256")
    seal_ok = seal_sha == manifest.get("seal_sha256")
    if not payload_ok:
        problems.append("manifest:payload_sha256")
    if not seal_ok:
        problems.append("manifest:seal_sha256")
    if manifest.get("git") != payload.get("git"):
        problems.append("manifest:git_alias")
    roots = payload.get("roots", {})
    if not isinstance(roots, dict):
        roots = {}
    for name, value in roots.items():
        if manifest.get(name) != value:
            problems.append(f"manifest:root_alias:{name}")
    return {
        "ok": bool(payload_ok and seal_ok and not problems),
        "payload_ok": payload_ok,
        "seal_ok": seal_ok,
        "problems": problems,
        "recomputed_payload_sha256": payload_sha,
        "recomputed_seal_sha256": seal_sha,
    }


def _seal_fields(manifest: dict) -> dict[str, Any]:
    payload = manifest["payload"]
    git_state = payload["git"]
    roots = payload.get("roots", {})
    fields: dict[str, Any] = {
        "schema": payload["schema"],
        "branch": git_state.get("branch"),
        "head": git_state.get("head"),
        "tree": git_state.get("tree"),
        "origin_main": git_state.get("origin_main"),
        "manifest_payload_sha256": manifest["manifest_payload_sha256"],
    }
    fields.update(roots)
    fields["seal_sha256"] = manifest["seal_sha256"]
    return fields


def write_seal_text(manifest: dict, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fields = _seal_fields(manifest)
    target.write_text(
        "".join(f"{key}={fields[key]}\n" for key in sorted(fields)),
        encoding="utf-8", newline="\n",
    )


def read_seal_text(path: str | Path) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def verify_seal_text(manifest: dict, path: str | Path) -> dict:
    expected = {key: str(value) for key, value in _seal_fields(manifest).items()}
    actual = read_seal_text(path)
    problems = [
        f"seal_text:{key}" for key, value in expected.items()
        if actual.get(key) != value
    ]
    extras = sorted(set(actual) - set(expected))
    problems.extend(f"seal_text:unexpected:{key}" for key in extras)
    return {"ok": not problems, "problems": problems,
            "expected": expected, "actual": actual}
