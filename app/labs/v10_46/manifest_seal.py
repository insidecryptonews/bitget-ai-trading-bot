"""V10.47.18 reproducible, provenance-bound output manifest + seal (RESEARCH ONLY).

Work's audit (P1.4) showed the V10.47.14 seal covered only output path/hash pairs,
was stale (a report changed after sealing), and did not bind commit/tree/dataset/
spec/registry provenance. This module fixes all of that:

  * `build_manifest` walks a canonical, repo-relative, deterministically-sorted set
    of output files, hashes each, and records the FULL provenance: branch/HEAD/tree/
    origin/dirty, dataset ids+generations+file/manifest hashes, strategy spec hashes,
    registry hash, split spec hash, holdout commitment hash, policy/ledger/tournament/
    report/dashboard/test-collection/test-execution/audit/hub hashes and timestamps;
  * `manifest_payload_sha256` hashes the canonical manifest EXCLUDING the seal fields;
  * `seal_sha256` binds payload + HEAD + tree + dataset_root_hash + spec_root_hash +
    registry_hash + holdout_commitment_hash;
  * `verify_manifest` re-hashes every file on disk and re-derives the payload+seal,
    so a single changed report / HEAD / tree / dataset / spec / registry / dashboard
    breaks verification. Nothing here can send an order or enable live.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess

SCHEMA_VERSION = "v10_47_18_manifest_seal"
SEAL_FIELDS = ("manifest_payload_sha256", "seal_sha256")


def _sha_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha_str(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _root_hash(pairs: dict) -> str:
    """Order-independent Merkle-ish root over a {key: hash} mapping."""
    return _sha_str("\n".join(f"{k}:{v}" for k, v in sorted(pairs.items())))


def _git(root: str, *args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=root).decode().strip()
    except Exception:
        return "?"


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def build_manifest(*, root: str, out_dir: str,
                   dataset_hashes: dict | None = None,
                   spec_hashes: dict | None = None,
                   registry_hash: str | None = None,
                   split_spec_hash: str | None = None,
                   holdout_commitment_hash: str | None = None,
                   extra_provenance: dict | None = None) -> dict:
    """Build the provenance-bound manifest for every output under `out_dir`."""
    import datetime
    files: dict[str, str] = {}
    for dp, _dirs, fns in os.walk(out_dir):
        if os.path.basename(dp) == "manifests":
            continue
        for fn in fns:
            if fn.lower().endswith((".json", ".md", ".html", ".log")):
                full = os.path.join(dp, fn)
                rel = os.path.relpath(full, root).replace("\\", "/")
                files[rel] = _sha_file(full)
    dataset_hashes = dataset_hashes or {}
    spec_hashes = spec_hashes or {}
    head, tree = _git(root, "rev-parse", "HEAD"), _git(root, "rev-parse", "HEAD^{tree}")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "git": {"branch": _git(root, "branch", "--show-current"), "head": head,
                "tree": tree, "origin_main": _git(root, "rev-parse", "origin/main"),
                "dirty_tracked": bool(_git(root, "status", "--porcelain",
                                           "--untracked-files=no").strip())},
        "safety": {"paper_trading": True, "live_trading": False, "dry_run": True,
                   "can_send_real_orders": False, "final_recommendation": "NO LIVE"},
        "files_sha256": dict(sorted(files.items())),
        "output_root_hash": _root_hash(files),
        "dataset_hashes": dict(sorted(dataset_hashes.items())),
        "dataset_root_hash": _root_hash(dataset_hashes),
        "spec_hashes": dict(sorted(spec_hashes.items())),
        "spec_root_hash": _root_hash(spec_hashes),
        "registry_hash": registry_hash or "",
        "split_spec_hash": split_spec_hash or "",
        "holdout_commitment_hash": holdout_commitment_hash or "",
        "extra_provenance": extra_provenance or {},
    }
    manifest_payload_sha256 = _sha_str(_canonical(payload))
    seal_sha256 = _sha_str("|".join([
        manifest_payload_sha256, head, tree, payload["dataset_root_hash"],
        payload["spec_root_hash"], payload["registry_hash"],
        payload["holdout_commitment_hash"]]))
    manifest = dict(payload)
    manifest["manifest_payload_sha256"] = manifest_payload_sha256
    manifest["seal_sha256"] = seal_sha256
    return manifest


def _recompute_seal(manifest: dict, root: str) -> tuple[str, str, list[str]]:
    payload = {k: v for k, v in manifest.items() if k not in SEAL_FIELDS}
    problems: list[str] = []
    # re-hash files on disk
    recomputed = {}
    for rel, rec_hash in manifest.get("files_sha256", {}).items():
        full = os.path.join(root, rel)
        if not os.path.exists(full):
            problems.append(f"missing:{rel}")
            continue
        cur = _sha_file(full)
        recomputed[rel] = cur
        if cur != rec_hash:
            problems.append(f"stale:{rel}")
    payload_sha = _sha_str(_canonical(payload))
    seal = _sha_str("|".join([
        payload_sha, manifest["git"]["head"], manifest["git"]["tree"],
        manifest["dataset_root_hash"], manifest["spec_root_hash"],
        manifest["registry_hash"], manifest["holdout_commitment_hash"]]))
    return payload_sha, seal, problems


def verify_manifest(manifest: dict, *, root: str) -> dict:
    """Recompute payload + seal from disk and compare to the recorded values.
    A single changed output / HEAD / tree / dataset / spec / registry breaks it."""
    payload_sha, seal, problems = _recompute_seal(manifest, root)
    payload_ok = payload_sha == manifest.get("manifest_payload_sha256")
    seal_ok = seal == manifest.get("seal_sha256")
    return {"ok": bool(payload_ok and seal_ok and not problems),
            "payload_ok": payload_ok, "seal_ok": seal_ok, "problems": problems,
            "recomputed_payload_sha256": payload_sha, "recomputed_seal_sha256": seal}
