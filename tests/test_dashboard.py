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


def _start_server(config: BotConfig, pulse: TrainingPulse | None = None, db=None) -> str:
    port = _free_port()
    pulse = pulse or TrainingPulse()
    notifier = TelegramNotifier(config, DummyLogger())
    start_health_server(
        HealthState(mode=config.mode),
        port,
        DummyLogger(),
        config=config,
        db=db,
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


def test_training_status_includes_open_paper_detail_without_secrets():
    class DummyDb:
        def get_open_paper_positions_summary(self, limit=5):
            return [{
                "symbol": "BTCUSDT",
                "side": "LONG",
                "entry_price": 100.0,
                "reason": "API_KEY=must_not_exist",
                "status": "PAPER_OPEN",
            }]

        def get_signal_label_summary_since(self, since):
            return {"total_labels": 1, "time_count": 0, "sl_count": 1, "tp1_count": 0, "tp2_count": 0, "profit_factor": 0.0}

    base = _start_server(BotConfig(), db=DummyDb())
    status, body = _get(base + "/api/training/status")
    assert status == 200
    payload = json.loads(body)
    assert payload["open_paper_positions_detail"][0]["symbol"] == "BTCUSDT"
    assert "must_not_exist" not in body


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


def test_shadow_opportunity_endpoint_returns_json():
    class DummyDb:
        def get_training_observation_summary_since(self, *args, **kwargs):
            return {"total": 10, "high_score_count": 4, "regimes": [], "top_symbols": []}

        def get_high_score_label_summary_since(self, *args, **kwargs):
            return {"total_labels": 2, "time_count": 1, "sl_count": 1, "tp1_count": 0, "tp2_count": 0, "profit_factor": 0.0}

        def get_shadow_opportunity_group_summaries_since(self, *args, **kwargs):
            return [{"group_value": "BTCUSDT", "total_labels": 2, "time_ratio": 0.5, "sl_ratio": 0.5, "tp_ratio": 0.0, "profit_factor": 0.0}]

        def get_missed_high_score_summary_since(self, *args, **kwargs):
            return {"total": 1, "by_reason": [{"reason": "slot", "count": 1}]}

    base = _start_server(BotConfig(), db=DummyDb())
    status, body = _get(base + "/api/training/shadow-opportunity?hours=24")
    assert status == 200
    payload = json.loads(body)
    assert "SHADOW OPPORTUNITY START" in payload["text"]
    assert payload["final_recommendation"] == "NO LIVE"


def test_edge_guard_and_tp_sl_endpoints_return_json():
    class DummyDb:
        def get_high_score_label_summary_since(self, *args, **kwargs):
            return {"total_labels": 1000, "time_count": 900, "sl_count": 80, "tp1_count": 20, "tp2_count": 0, "profit_factor": 1.3}

        def get_shadow_opportunity_group_summaries_since(self, *args, **kwargs):
            return [{
                "group_value": "ETHUSDT",
                "total_labels": 1000,
                "profit_factor": 1.3,
                "time_ratio": 0.90,
                "sl_ratio": 0.08,
                "tp_ratio": 0.02,
            }]

        def get_signal_path_metrics_summary_since(self, *args, **kwargs):
            return {"total": 1, "active_count": 0, "matured_count": 1, "insufficient_count": 0, "coverage_pct": 1.0}

        def fetch_signal_path_metrics_since(self, *args, **kwargs):
            return [{
                "symbol": "ETHUSDT",
                "market_regime": "TREND_DOWN",
                "score_bucket": "80-89",
                "max_favorable_pct": 1.0,
                "max_adverse_pct": 0.2,
                "final_return_pct": 0.4,
                "bars_tracked": 20,
                "status": "matured",
            }] * 30

        def get_score_calibration_summaries_since(self, *args, **kwargs):
            return [{
                "group_value": "80-89",
                "total_labels": 1000,
                "profit_factor": 1.3,
                "time_ratio": 0.90,
                "sl_ratio": 0.08,
                "tp_ratio": 0.02,
            }]

    base = _start_server(BotConfig(), db=DummyDb())
    status, body = _get(base + "/api/training/edge-guard?hours=24")
    assert status == 200
    assert "EDGE GUARD START" in json.loads(body)["text"]
    status, body = _get(base + "/api/training/tp-sl-lab?hours=24")
    assert status == 200
    assert "TP SL HORIZON LAB START" in json.loads(body)["text"]
    for path, marker in (
        ("/api/training/exit-simulation?hours=24", "EXIT SIMULATION START"),
        ("/api/training/score-calibration?hours=24", "SCORE CALIBRATION START"),
        ("/api/training/shadow-experiments?hours=24", "SHADOW EXPERIMENTS START"),
        ("/api/training/evolution-score?hours=24", "EVOLUTION SCORE START"),
    ):
        status, body = _get(base + path)
        assert status == 200
        assert marker in json.loads(body)["text"]
