from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request

from app.config import BotConfig
from app.health_server import HealthState, start_health_server
from app.telegram_notifier import TelegramNotifier
from app.training_pulse import TrainingPulse


class DummyLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - local test server
        return int(response.status), response.read().decode("utf-8")


def _start_server(config: BotConfig, pulse: TrainingPulse | None = None) -> str:
    port = _free_port()
    pulse = pulse or TrainingPulse()
    notifier = TelegramNotifier(config, DummyLogger())
    start_health_server(
        HealthState(mode=config.mode),
        port,
        DummyLogger(),
        config=config,
        training_pulse=pulse,
        telegram_notifier=notifier,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(30):
        try:
            _get(base + "/health")
            return base
        except Exception:
            time.sleep(0.05)
    raise AssertionError("health server did not start")


def test_dashboard_returns_html():
    base = _start_server(BotConfig())
    status, body = _get(base + "/dashboard")
    assert status == 200
    assert "Training Dashboard" in body


def test_training_status_returns_safe_json():
    config = BotConfig(
        telegram_bot_token="real-token-that-must-not-leak",
        telegram_chat_id="123",
    )
    pulse = TrainingPulse()
    pulse.record_cycle_ok()
    base = _start_server(config, pulse)
    status, body = _get(base + "/api/training/status")
    assert status == 200
    payload = json.loads(body)
    for key in ("safety", "health", "paper", "labels", "signals", "diagnosis", "telegram"):
        assert key in payload
    assert payload["safety"]["live_trading"] is False
    assert "real-token-that-must-not-leak" not in body
    assert "BITGET_API_KEY" not in body
    assert "BITGET_API_SECRET" not in body
    assert "PASSWORD" not in body
    assert "PASSPHRASE" not in body


def test_dashboard_auth_blocks_without_token():
    base = _start_server(BotConfig(dashboard_auth_token="dash-secret"))
    try:
        _get(base + "/api/training/status")
    except urllib.error.HTTPError as exc:
        assert exc.code == 401
    else:
        raise AssertionError("expected 401 without dashboard token")


def test_dashboard_auth_accepts_query_token():
    base = _start_server(BotConfig(dashboard_auth_token="dash-secret"))
    status, body = _get(base + "/api/training/status?token=dash-secret")
    assert status == 200
    assert json.loads(body)["final_recommendation"] == "NO LIVE"


def test_health_still_works_with_dashboard_auth():
    base = _start_server(BotConfig(dashboard_auth_token="dash-secret"))
    status, body = _get(base + "/health")
    assert status == 200
    assert json.loads(body)["status"] == "ok"
