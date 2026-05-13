from __future__ import annotations

import urllib.error

from app.config import BotConfig
from app.telegram_notifier import TelegramNotifier


class DummyLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message, *args):
        self.warnings.append(str(message) % args if args else str(message))


def test_telegram_notifier_default_disabled():
    notifier = TelegramNotifier(BotConfig(), DummyLogger())
    assert notifier.enabled() is False
    assert notifier.send_message("hello") is False


def test_safe_truncate_respects_limit():
    notifier = TelegramNotifier(BotConfig(), DummyLogger())
    text = notifier.safe_truncate("x" * 500, 120)
    assert len(text) <= 120
    assert "truncated" in text


def test_sanitize_text_hides_secrets():
    notifier = TelegramNotifier(BotConfig(), DummyLogger())
    text = notifier.sanitize_text("API_KEY=abc SECRET:super TOKEN=mytoken PASSWORD=pw PASSPHRASE=pass PRIVATE_KEY=key")
    assert "abc" not in text
    assert "super" not in text
    assert "mytoken" not in text
    assert "pw" not in text
    assert "pass" not in text
    assert "key" not in text
    assert "***" in text


def test_status_dict_does_not_expose_token():
    config = BotConfig(enable_telegram_notifier=True, telegram_bot_token="secret-token", telegram_chat_id="123")
    status = TelegramNotifier(config, DummyLogger()).status_dict()
    assert status["enabled"] is True
    assert status["configured"] is True
    assert "secret-token" not in str(status)


def test_telegram_failure_does_not_raise(monkeypatch):
    def fail(*args, **kwargs):
        raise urllib.error.URLError("network down TOKEN=should_hide")

    monkeypatch.setattr("urllib.request.urlopen", fail)
    logger = DummyLogger()
    config = BotConfig(enable_telegram_notifier=True, telegram_bot_token="secret-token", telegram_chat_id="123")
    notifier = TelegramNotifier(config, logger)
    assert notifier.send_message("hello") is False
    assert "should_hide" not in notifier.status_dict()["last_error"]
