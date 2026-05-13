from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .config import BotConfig
from .utils import iso_utc


SECRET_PATTERNS = [
    re.compile(r"(?i)\b(API[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSPHRASE|PRIVATE[_-]?KEY)\s*[:=]\s*([^\s,;]+)"),
]


class TelegramNotifier:
    """Small optional notifier for compact training telemetry only."""

    def __init__(self, config: BotConfig, logger) -> None:
        self.config = config
        self.logger = logger
        self.last_sent_at: str = ""
        self.last_error: str = ""
        self.sent_count = 0
        self._last_alert_monotonic = 0.0

    def enabled(self) -> bool:
        return bool(self.config.enable_telegram_notifier)

    def configured(self) -> bool:
        return bool(self.config.telegram_bot_token and self.config.telegram_chat_id)

    def send_training_pulse(self, text: str) -> bool:
        return self.send_message(text)

    def send_alert(self, title: str, text: str) -> bool:
        if not self.config.telegram_alerts_enabled:
            return False
        now = time.monotonic()
        min_interval = max(1, int(self.config.telegram_min_alert_interval_seconds or 120))
        if self._last_alert_monotonic and now - self._last_alert_monotonic < min_interval:
            return False
        self._last_alert_monotonic = now
        return self.send_message(f"{title}\n{text}")

    def send_message(self, text: str) -> bool:
        if not self.enabled() or not self.configured():
            return False
        safe_text = self.safe_truncate(self.sanitize_text(text), self.config.telegram_max_message_chars)
        payload = {
            "chat_id": self.config.telegram_chat_id,
            "text": safe_text,
            "disable_web_page_preview": True,
        }
        url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
        return self._post_json(url, payload)

    def send_document(self, path: str | Path, caption: str = "") -> bool:
        if not self.enabled() or not self.configured() or not self.config.telegram_send_files:
            return False
        # Files are disabled by default. Keep this conservative: only signal unsupported
        # instead of adding multipart helpers to the lightweight worker.
        self.last_error = "send_document disabled in lightweight notifier"
        return False

    def safe_truncate(self, text: str, max_chars: int | None = None) -> str:
        limit = max(20, int(max_chars or self.config.telegram_max_message_chars or 3500))
        safe_text = str(text or "")
        if len(safe_text) <= limit:
            return safe_text
        return safe_text[: max(0, limit - 20)] + "\n... [truncated]"

    def sanitize_text(self, text: str) -> str:
        safe_text = str(text or "")
        for pattern in SECRET_PATTERNS:
            safe_text = pattern.sub(lambda match: f"{match.group(1)}=***", safe_text)
        return safe_text

    def status_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled(),
            "configured": self.configured(),
            "last_sent_at": self.last_sent_at,
            "last_error": self.sanitize_text(self.last_error),
            "sent_count": self.sent_count,
        }

    def _post_json(self, url: str, payload: dict[str, Any]) -> bool:
        try:
            data = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=8) as response:  # noqa: S310 - URL is Telegram API from config token
                status = int(getattr(response, "status", 200) or 200)
            if status >= 400:
                self.last_error = f"telegram_http_{status}"
                self.logger.warning("Telegram notifier rechazo mensaje: HTTP %s", status)
                return False
            self.sent_count += 1
            self.last_sent_at = iso_utc()
            self.last_error = ""
            return True
        except Exception as exc:
            self.last_error = self.sanitize_text(str(exc))[:300]
            self.logger.warning("Telegram notifier fallo sin detener el bot: %s", self.last_error)
            return False


def url_encode_token(token: str) -> str:
    return urllib.parse.quote(token or "", safe="")
