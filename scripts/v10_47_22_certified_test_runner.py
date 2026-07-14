"""Run one certified pytest collection and one certified full suite.

Research-only evidence tooling.  It never reads trading configuration, a DB,
credentials or exchange APIs.  Output is restricted to the V10.47.22 reports
tree and is created atomically in a new run directory.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ALLOWED_ROOT = ROOT / "reports" / "research" / "v10_47_22_real_state_certification"
SUMMARY_NAMES = ("passed", "failed", "skipped", "xfailed", "xpassed", "deselected")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=ROOT, text=True, encoding="utf-8",
        errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def safe_new_run_dir(requested: str | None) -> Path:
    allowed = ALLOWED_ROOT.resolve()
    if ALLOWED_ROOT.is_symlink() or any(parent.is_symlink() for parent in ALLOWED_ROOT.parents):
        raise RuntimeError("certified evidence root may not be a symlink")
    if requested:
        target = Path(requested)
        if not target.is_absolute():
            target = ROOT / target
    else:
        run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        run_id += "_" + uuid.uuid4().hex[:8]
        target = ALLOWED_ROOT / "certified_tests" / run_id
    target = target.resolve()
    try:
        target.relative_to(allowed)
    except ValueError as exc:
        raise RuntimeError("output must remain below V10.47.22 reports root") from exc
    cursor = target
    while cursor != allowed:
        if cursor.exists() and cursor.is_symlink():
            raise RuntimeError("output path may not traverse a symlink")
        cursor = cursor.parent
    if target.exists():
        raise RuntimeError("certified run directory must not already exist")
    target.mkdir(parents=True, exist_ok=False)
    return target


def atomic_text(path: Path, value: str) -> None:
    temporary = path.with_name(path.name + f".tmp.{uuid.uuid4().hex}")
    temporary.write_text(value, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def atomic_json(path: Path, value: dict) -> None:
    atomic_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def parse_nodeids(output: str) -> list[str]:
    nodeids = []
    for raw in output.splitlines():
        line = raw.strip()
        if "::" not in line or line.startswith(("<", "=", "ERROR", "WARNING")):
            continue
        if line.endswith(" selected") or line.endswith(" collected"):
            continue
        nodeids.append(line)
    return nodeids


def parse_pytest_summary(output: str) -> dict[str, int]:
    counts = {name: 0 for name in SUMMARY_NAMES}
    for name in SUMMARY_NAMES:
        matches = re.findall(rf"(?<!\d)(\d+)\s+{name}\b", output)
        if matches:
            counts[name] = int(matches[-1])
    return counts


def run_capture(command: list[str], *, stream: bool) -> tuple[int, str, float]:
    started = time.monotonic()
    process = subprocess.Popen(
        command, cwd=ROOT, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    lines: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        lines.append(line)
        if stream:
            print(line, end="", flush=True)
    return process.wait(), "".join(lines), time.monotonic() - started


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir")
    args = parser.parse_args(argv)

    tracked = git("status", "--porcelain=v1", "--untracked-files=no")
    if tracked:
        raise RuntimeError("tracked worktree must be clean before certified tests")
    branch = git("branch", "--show-current")
    head = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    output = safe_new_run_dir(args.output_dir)
    python = str(Path(sys.executable).resolve())
    collection_command = [python, "-m", "pytest", "--collect-only", "-q"]
    execution_command = [python, "-m", "pytest", "-q"]

    collection_start = utc_now()
    collection_exit, collection_text, collection_duration = run_capture(
        collection_command, stream=False
    )
    collection_end = utc_now()
    atomic_text(output / "pytest_collection.log", collection_text)
    nodeids = parse_nodeids(collection_text)
    counts = Counter(nodeids)
    duplicates = sorted(nodeid for nodeid, count in counts.items() if count > 1)
    unique_nodeids = sorted(counts)
    atomic_text(output / "pytest_nodeids.txt", "\n".join(unique_nodeids) + "\n")
    collection_record = {
        "schema": "v10_47_22_certified_collection",
        "branch": branch,
        "head": head,
        "tree": tree,
        "started_utc": collection_start,
        "ended_utc": collection_end,
        "duration_seconds": round(collection_duration, 6),
        "command": collection_command,
        "exit_code": collection_exit,
        "collected_invocations": len(nodeids),
        "unique_nodeids": len(unique_nodeids),
        "duplicate_nodeids": duplicates,
        "raw_log_sha256": sha256_file(output / "pytest_collection.log"),
        "nodeids_sha256": sha256_file(output / "pytest_nodeids.txt"),
    }
    atomic_json(output / "collection_record.json", collection_record)
    if collection_exit != 0 or not unique_nodeids or duplicates:
        print(json.dumps(collection_record, indent=2), flush=True)
        return 2

    execution_start = utc_now()
    execution_exit, execution_text, execution_duration = run_capture(
        execution_command, stream=True
    )
    execution_end = utc_now()
    atomic_text(output / "pytest_execution.log", execution_text)
    summary = parse_pytest_summary(execution_text)
    execution_record = {
        "schema": "v10_47_22_certified_execution",
        "branch": branch,
        "head": head,
        "tree": tree,
        "started_utc": execution_start,
        "ended_utc": execution_end,
        "duration_seconds": round(execution_duration, 6),
        "collection_command": collection_command,
        "execution_command": execution_command,
        "collected": len(unique_nodeids),
        "unique_nodeids": len(unique_nodeids),
        "duplicate_nodeids": duplicates,
        **summary,
        "exit_code": execution_exit,
        "raw_log_sha256": sha256_file(output / "pytest_execution.log"),
        "nodeids_sha256": collection_record["nodeids_sha256"],
        "collection_record_sha256": sha256_file(output / "collection_record.json"),
        "research_only": True,
        "can_send_real_orders": False,
        "final_recommendation": "NO LIVE",
    }
    atomic_json(output / "execution_record.json", execution_record)
    print(json.dumps(execution_record, indent=2), flush=True)
    return execution_exit


if __name__ == "__main__":
    raise SystemExit(main())
