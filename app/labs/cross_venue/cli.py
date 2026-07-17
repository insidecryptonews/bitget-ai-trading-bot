"""Standalone CLI for the isolated Cross-Venue research subsystem."""

from __future__ import annotations

import argparse
import json
from typing import Any

from . import safety_envelope
from .api import dashboard_snapshot, health_payload
from .collector import run_collector
from .providers import load_config, load_inventory, providers_payload
from .service import run_service


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cross-venue", description="Public cross-venue research; no execution")
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect", help="Run one persistent public-feed collector")
    collect.add_argument("--venue", required=True, choices=["bitget", "binance", "bybit", "okx", "hyperliquid"])
    collect.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    collect.add_argument("--max-sessions", type=int)
    collect.add_argument("--max-messages", type=int)
    collect.add_argument("--max-seconds", type=float)
    collect.add_argument("--stop-file")
    engine = sub.add_parser("engine", help="Run causal lead-lag and isolated paper simulation")
    engine.add_argument("--interval-seconds", type=float, default=0.25)
    engine.add_argument("--max-cycles", type=int)
    engine.add_argument("--stop-file")
    sub.add_parser("status"); sub.add_parser("providers"); sub.add_parser("verify-inventory")
    sub.add_parser("snapshot")
    return parser


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps({**payload, **safety_envelope()}, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "collect":
        payload = run_collector(args.venue, symbols=[item.strip().upper() for item in args.symbols.split(",") if item.strip()],
                                max_sessions=args.max_sessions, max_messages=args.max_messages,
                                max_seconds=args.max_seconds, stop_file=args.stop_file)
    elif args.command == "engine":
        payload = run_service(interval_seconds=args.interval_seconds, max_cycles=args.max_cycles, stop_file=args.stop_file)
    elif args.command == "status": payload = health_payload()
    elif args.command == "providers": payload = providers_payload()
    elif args.command == "verify-inventory":
        inventory = load_inventory(); config = load_config()
        payload = {"status": "PASS", "provider_count": len(inventory["providers"]),
                   "active_venues": config["active_venues"], "official_sources_only": True}
    else: payload = dashboard_snapshot()
    _print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
