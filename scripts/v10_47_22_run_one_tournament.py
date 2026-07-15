"""Run one V10.47.22 tournament with discovery-only physical inputs."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.labs.v10_46 import causal_tournament as CT  # noqa: E402
from app.labs.v10_46.discovery_dataset import (  # noqa: E402
    DiscoveryDatasetLoader,
    audit_dataset_isolation,
)


DATA_ROOT = ROOT / "external_data" / "staging" / "v10_47_22_isolated"
REPORT_ROOT = ROOT / "reports" / "research" / "v10_47_22_real_state_certification"
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, text=True, encoding="utf-8",
        errors="replace", stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "UNAVAILABLE"


def safe_combo_root(symbol: str, timeframe: str) -> Path:
    if symbol not in ("BTCUSDT", "ETHUSDT", "XRPUSDT", "DOGEUSDT"):
        raise RuntimeError("unsupported symbol")
    if timeframe not in ("1m", "5m", "15m"):
        raise RuntimeError("unsupported timeframe")
    combo = (DATA_ROOT / symbol / timeframe).resolve(strict=True)
    combo.relative_to(DATA_ROOT.resolve(strict=True))
    return combo


def safe_output(path_value: str) -> Path:
    path = Path(path_value)
    if not path.is_absolute():
        path = ROOT / path
    resolved_parent = path.parent.resolve(strict=True)
    resolved_parent.relative_to(REPORT_ROOT.resolve(strict=True))
    if path.exists() or path.is_symlink():
        raise RuntimeError("tournament output must not already exist")
    return resolved_parent / path.name


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + f".tmp.{uuid.uuid4().hex}")
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ) + "\n"
    with temporary.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    symbol, timeframe = args.symbol.upper(), args.timeframe
    combo = safe_combo_root(symbol, timeframe)
    discovery_root = combo / "discovery"
    sealed_root = combo / "sealed_holdout"
    isolation = audit_dataset_isolation(discovery_root, sealed_root)
    if not isolation["ok"]:
        raise RuntimeError(f"dataset isolation failed: {isolation['problems']}")
    partitions = DiscoveryDatasetLoader(discovery_root).load()
    dataset_manifest = json.loads(
        (combo / "dataset_manifest.json").read_text(encoding="utf-8")
    )
    output_path = safe_output(args.output)
    result = CT.run_causal_tournament(
        partitions, symbol=symbol, venue=dataset_manifest["venue"],
        timeframe=timeframe, gen=dataset_manifest["source_generation_id"],
        log=lambda message: print(message, flush=True),
    )
    result["execution_provenance"] = {
        "branch": git("branch", "--show-current"),
        "head": git("rev-parse", "HEAD"),
        "tree": git("rev-parse", "HEAD^{tree}"),
        "tracked_status_porcelain": git(
            "status", "--porcelain=v1", "--untracked-files=no"
        ),
        "dataset_manifest": combo.joinpath("dataset_manifest.json").relative_to(ROOT).as_posix(),
        "discovery_root": discovery_root.relative_to(ROOT).as_posix(),
        "holdout_commitment": sealed_root.joinpath("commitment.json").relative_to(ROOT).as_posix(),
        "holdout_data_loaded": False,
        "holdout_loader_imported": (
            "app.labs.v10_46.holdout_loader" in sys.modules
        ),
    }
    result["dataset_isolation_audit"] = isolation
    result["research_only"] = True
    result["paper_ready"] = False
    result["live_ready"] = False
    result["can_send_real_orders"] = False
    result["final_recommendation"] = "NO LIVE"
    if result["execution_provenance"]["holdout_loader_imported"]:
        raise RuntimeError("holdout loader entered the discovery tournament process")
    atomic_json(output_path, result)
    print(f"OUTPUT={output_path.relative_to(ROOT).as_posix()}")
    print("HOLDOUT=SEALED")
    print("FINAL_RECOMMENDATION=NO LIVE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
