"""Loopback-only read-only server for the local research stack."""

from __future__ import annotations

import argparse
import logging
import signal
import time
from types import SimpleNamespace

from ...health_server import HealthState, start_health_server
from . import safety_envelope


def main() -> None:
    parser = argparse.ArgumentParser(description="ATI research dashboard server (read-only)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("ATI_RESEARCH_SERVER_LOOPBACK_ONLY")
    logger = logging.getLogger("ati-paper-server")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    state = HealthState(mode="paper", extra={**safety_envelope(), "server_scope": "LOCAL_READ_ONLY"})
    config = SimpleNamespace(
        enable_training_dashboard=True,
        dashboard_auth_token="",
        dashboard_refresh_seconds=10,
    )
    thread = start_health_server(state, args.port, logger, config=config, host=args.host)
    ready = getattr(thread, "server_ready")
    ready.wait(timeout=5)
    server = getattr(thread, "server_box").get("server")
    if server is None:
        raise SystemExit("ATI_RESEARCH_SERVER_START_FAILED")
    stop = False

    def request_stop(*_: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    logger.info("SIMULATION ONLY | NO LIVE | http://%s:%s/research-dashboard", args.host, args.port)
    while not stop:
        time.sleep(1)
    server.shutdown()
    server.server_close()


if __name__ == "__main__":
    main()
