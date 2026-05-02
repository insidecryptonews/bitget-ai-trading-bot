from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any


@dataclass
class HealthState:
    mode: str
    started_at: float = field(default_factory=time.time)
    open_positions: int = 0
    daily_pnl: float = 0.0
    last_scan: str = ""
    circuit_breaker: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "mode": self.mode,
            "uptime": f"{int(time.time() - self.started_at)}s",
            "open_positions": self.open_positions,
            "daily_pnl": self.daily_pnl,
            "last_scan": self.last_scan,
            "circuit_breaker": self.circuit_breaker,
            **self.extra,
        }


def start_health_server(state: HealthState, port: int, logger) -> threading.Thread:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/health":
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(state.payload()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    def run() -> None:
        try:
            HTTPServer(("0.0.0.0", port), Handler).serve_forever()
        except OSError as exc:
            logger.warning("Health server no pudo iniciar en puerto %s: %s", port, exc)

    thread = threading.Thread(target=run, name="health-server", daemon=True)
    thread.start()
    logger.info("Health server listo en /health puerto %s", port)
    return thread

