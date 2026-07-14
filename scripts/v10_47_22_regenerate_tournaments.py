"""Orchestrate the twelve isolated V10.47.22 tournaments with resume/timeout."""

from __future__ import annotations

import argparse
import json
import os
import re
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORT_ROOT = ROOT / "reports" / "research" / "v10_47_22_real_state_certification"
RUN_ONE_SCRIPT = ROOT / "scripts" / "v10_47_22_run_one_tournament.py"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "XRPUSDT", "DOGEUSDT")
TIMEFRAMES = ("1m", "5m", "15m")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + f".tmp.{uuid.uuid4().hex}")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8", newline="\n",
    )
    os.replace(temporary, path)


def safe_run_root(label: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", label):
        raise RuntimeError("unsafe run label")
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    run_root = (REPORT_ROOT / "tournaments" / label).resolve()
    run_root.relative_to(REPORT_ROOT.resolve())
    if run_root.is_symlink() or any(parent.is_symlink() for parent in run_root.parents):
        raise RuntimeError("run root may not traverse a symlink")
    run_root.mkdir(parents=True, exist_ok=True)
    return run_root


def valid_output(path: Path, symbol: str, timeframe: str) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(
        value.get("symbol") == symbol
        and value.get("timeframe") == timeframe
        and value.get("holdout", {}).get("state") == "SEALED"
        and value.get("execution_provenance", {}).get("holdout_data_loaded") is False
        and value.get("final_recommendation") == "NO LIVE"
    )


def summarize(value: dict) -> dict:
    gates = [
        row["gate"] for row in value.get("results", {}).values()
        if isinstance(row, dict) and isinstance(row.get("gate"), dict)
    ]
    classifications: dict[str, int] = {}
    for row in value.get("results", {}).values():
        classification = row.get("metrics", {}).get("classification", "UNKNOWN")
        classifications[classification] = classifications.get(classification, 0) + 1
    return {
        "status": "COMPLETE",
        "symbol": value["symbol"],
        "timeframe": value["timeframe"],
        "classifications": classifications,
        "n_net_positive": value.get("n_net_positive", 0),
        "shadow_candidates": value.get("shadow_candidates", []),
        "validation_admitted": value.get("validation_admitted_candidates", []),
        "validation_rejected": value.get("validation_rejected_candidates", []),
        "walk_forward_called": sum(bool(gate.get("walk_forward_called")) for gate in gates),
        "pairs_requested": sum(
            int(gate.get("matched_random_paired", {}).get("pairs_requested", 0))
            for gate in gates
        ),
        "pairs_found": sum(
            int(gate.get("matched_random_paired", {}).get("pairs_found", 0))
            for gate in gates
        ),
        "pairs_incompatible": sum(
            int(gate.get("matched_random_paired", {}).get("pairs_incompatible", 0))
            for gate in gates
        ),
        "minimum_baseline_coverage": min(
            (float(gate.get("matched_random_paired", {}).get("coverage", 0.0))
             for gate in gates), default=None,
        ),
        "minimum_corrected_p_value": min(
            (float(gate.get("matched_random_paired", {}).get("corrected_p_value", 1.0))
             for gate in gates), default=None,
        ),
        "maximum_n_eff": max(
            (float(row.get("metrics", {}).get("n_eff_final", 0.0))
             for row in value.get("results", {}).values()), default=0.0,
        ),
        "holdout_state": value.get("holdout", {}).get("state"),
        "holdout_physically_loaded": value.get("holdout", {}).get("physically_loaded"),
        "final_recommendation": "NO LIVE",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-label", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--symbols", default=",".join(SYMBOLS))
    parser.add_argument("--timeframes", default=",".join(TIMEFRAMES))
    args = parser.parse_args(argv)
    symbols = tuple(item.strip().upper() for item in args.symbols.split(",") if item.strip())
    timeframes = tuple(item.strip() for item in args.timeframes.split(",") if item.strip())
    if any(item not in SYMBOLS for item in symbols) \
            or any(item not in TIMEFRAMES for item in timeframes):
        raise RuntimeError("unsupported symbol/timeframe")
    run_root = safe_run_root(args.run_label)
    summary_path = run_root / "tournament_summary.json"
    summary = {}
    if args.resume and summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    for symbol in symbols:
        for timeframe in timeframes:
            key = f"{symbol}:{timeframe}"
            output = run_root / f"{symbol}_{timeframe}.json"
            log_path = run_root / f"{symbol}_{timeframe}.log"
            if args.resume and output.exists() and valid_output(output, symbol, timeframe):
                summary[key] = summarize(json.loads(output.read_text(encoding="utf-8")))
                print(f"{key}: RESUME existing COMPLETE", flush=True)
                continue
            if output.exists():
                raise RuntimeError(f"existing invalid output requires review: {key}")
            if log_path.exists():
                if not args.resume:
                    raise RuntimeError(f"existing incomplete evidence requires review: {key}")
                retry = 1
                while run_root.joinpath(f"{symbol}_{timeframe}.retry{retry}.log").exists():
                    retry += 1
                log_path = run_root / f"{symbol}_{timeframe}.retry{retry}.log"
            command = [
                sys.executable, str(RUN_ONE_SCRIPT),
                "--symbol", symbol, "--timeframe", timeframe,
                "--output", str(output),
            ]
            started = time.monotonic()
            with log_path.open("x", encoding="utf-8", newline="\n") as log:
                process = subprocess.Popen(
                    command, cwd=ROOT, text=True, encoding="utf-8", errors="replace",
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    env={**os.environ, "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1"},
                )
                assert process.stdout is not None
                output_queue: queue.Queue[str | None] = queue.Queue()

                def read_output() -> None:
                    for output_line in process.stdout:
                        output_queue.put(output_line)
                    output_queue.put(None)

                reader = threading.Thread(target=read_output, daemon=True)
                reader.start()
                reader_finished = False
                try:
                    while not reader_finished or process.poll() is None:
                        try:
                            line = output_queue.get(timeout=0.5)
                        except queue.Empty:
                            line = ""
                        if line is None:
                            reader_finished = True
                        elif line:
                            print(f"[{key}] {line}", end="", flush=True)
                            log.write(line)
                            log.flush()
                        if time.monotonic() - started > args.timeout_seconds:
                            process.kill()
                            process.wait()
                            raise TimeoutError(f"{key} exceeded timeout")
                    reader.join(timeout=5)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
            if process.returncode != 0 or not valid_output(output, symbol, timeframe):
                raise RuntimeError(f"{key} failed with exit {process.returncode}")
            value = json.loads(output.read_text(encoding="utf-8"))
            summary[key] = summarize(value)
            summary[key]["duration_seconds"] = round(time.monotonic() - started, 3)
            atomic_json(summary_path, summary)
            print(f"{key}: COMPLETE", flush=True)
    total_shadow = sum(len(row.get("shadow_candidates", [])) for row in summary.values())
    final = {
        "schema": "v10_47_22_tournament_summary",
        "combinations_requested": len(symbols) * len(timeframes),
        "combinations_complete": sum(row.get("status") == "COMPLETE" for row in summary.values()),
        "results": summary,
        "no_confirmed_edge": total_shadow == 0,
        "shadow_candidates": total_shadow,
        "holdout_all_sealed": all(
            row.get("holdout_state") == "SEALED" for row in summary.values()
        ),
        "research_only": True,
        "can_send_real_orders": False,
        "final_recommendation": "NO LIVE",
    }
    atomic_json(run_root / "final_summary.json", final)
    print(f"NO_CONFIRMED_EDGE={str(final['no_confirmed_edge']).lower()}")
    print(f"SHADOW_CANDIDATES={total_shadow}")
    print("HOLDOUT=SEALED")
    print("FINAL_RECOMMENDATION=NO LIVE")
    return 0 if final["combinations_complete"] == final["combinations_requested"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
