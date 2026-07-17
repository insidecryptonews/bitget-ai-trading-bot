"""Local administrative CLI for ATI paper simulation.

The only mutating administration command is an explicit offline reset guarded
by an exact confirmation phrase. No web route exposes it.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import ACCOUNT_ID, DEFAULT_DB_PATH, DEFAULT_RUNTIME_DIR, safety_envelope
from .api import dashboard_snapshot
from .config import load_config
from .executor import AtiPaperExecutor, STOP_PATH, read_executor_status
from .ledger import AtiPaperLedger

RESET_PHRASE = "RESET ATI_PAPER_50 SIMULATION"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ATI paper simulation (local/research only)")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--max-cycles", type=int, default=0)
    sub.add_parser("status")
    sub.add_parser("snapshot")
    sub.add_parser("request-stop")
    reset = sub.add_parser("reset")
    reset.add_argument("--confirmation", default="")
    return parser


def _reset(confirmation: str) -> dict:
    if confirmation != RESET_PHRASE:
        raise SystemExit("RESET_BLOCKED: exact confirmation phrase required")
    if DEFAULT_DB_PATH.exists():
        archive = DEFAULT_RUNTIME_DIR / "archive"
        archive.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        for suffix in ("", "-wal", "-shm"):
            source = Path(str(DEFAULT_DB_PATH) + suffix)
            if source.exists():
                os.replace(source, archive / f"ati_paper_{stamp}.sqlite{suffix}.bak")
    init = AtiPaperLedger().initialize(load_config())
    return {"status": "RESET_COMPLETE", "account_id": ACCOUNT_ID,
            "archived_previous": True, "result": init, **safety_envelope()}


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "run":
        result = AtiPaperExecutor().run(max_cycles=max(0, int(args.max_cycles)))
    elif args.command == "status":
        result = read_executor_status()
    elif args.command == "snapshot":
        result = dashboard_snapshot()
    elif args.command == "request-stop":
        STOP_PATH.parent.mkdir(parents=True, exist_ok=True)
        STOP_PATH.write_text("controlled stop requested\n", encoding="ascii")
        result = {"status": "STOP_REQUESTED", **safety_envelope()}
    else:
        result = _reset(args.confirmation)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
