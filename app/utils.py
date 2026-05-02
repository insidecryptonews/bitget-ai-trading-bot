from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP, InvalidOperation
from typing import Any, Iterable


SECRET_KEYS = ("api_key", "api_secret", "passphrase", "access-key", "access-sign", "secret")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc() -> str:
    return utc_now().isoformat()


def now_ms() -> int:
    return int(time.time() * 1000)


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def pct_change(old: float, new: float) -> float:
    if old == 0:
        return 0.0
    return (new - old) / old


def normalize_symbol(symbol: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", symbol.upper())


def parse_csv_symbols(raw: str) -> list[str]:
    return [normalize_symbol(s) for s in raw.split(",") if normalize_symbol(s)]


def decimal_quantize(value: float | str, step: float | str, mode: str = "down") -> str:
    try:
        d_value = Decimal(str(value))
        d_step = Decimal(str(step))
        if d_step <= 0:
            return str(d_value.normalize())
        quotient = d_value / d_step
        rounding = ROUND_UP if mode == "up" else ROUND_DOWN
        quantized = quotient.to_integral_value(rounding=rounding) * d_step
        return format(quantized.normalize(), "f")
    except (InvalidOperation, ValueError):
        return str(value)


def round_to_places(value: float | str, places: int, mode: str = "down") -> str:
    try:
        q = Decimal("1").scaleb(-max(0, places))
        rounding = ROUND_UP if mode == "up" else ROUND_DOWN
        return format(Decimal(str(value)).quantize(q, rounding=rounding), "f")
    except (InvalidOperation, ValueError):
        return str(value)


def timeframe_to_seconds(timeframe: str) -> int:
    table = {
        "1m": 60,
        "3m": 180,
        "5m": 300,
        "15m": 900,
        "30m": 1800,
        "1h": 3600,
        "1H": 3600,
        "4h": 14400,
        "4H": 14400,
        "1d": 86400,
        "1D": 86400,
    }
    return table.get(timeframe, 300)


def sanitize(data: Any) -> Any:
    if isinstance(data, dict):
        cleaned: dict[str, Any] = {}
        for key, value in data.items():
            key_lower = str(key).lower()
            if any(secret in key_lower for secret in SECRET_KEYS):
                cleaned[key] = "***REDACTED***"
            else:
                cleaned[key] = sanitize(value)
        return cleaned
    if isinstance(data, list):
        return [sanitize(item) for item in data]
    if isinstance(data, tuple):
        return tuple(sanitize(item) for item in data)
    return data


def json_dumps(data: Any) -> str:
    return json.dumps(sanitize(data), ensure_ascii=True, sort_keys=True, default=str)


def mean(values: Iterable[float]) -> float:
    vals = [v for v in values if math.isfinite(v)]
    return sum(vals) / len(vals) if vals else 0.0


class SimpleRateLimiter:
    """Small thread-safe sliding-window limiter for REST calls."""

    def __init__(self, max_calls: int, period_seconds: float) -> None:
        self.max_calls = max_calls
        self.period_seconds = period_seconds
        self.calls: deque[float] = deque()
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            while self.calls and now - self.calls[0] > self.period_seconds:
                self.calls.popleft()
            if len(self.calls) >= self.max_calls:
                sleep_for = self.period_seconds - (now - self.calls[0]) + 0.01
                time.sleep(max(0.0, sleep_for))
            self.calls.append(time.monotonic())


def env_bool(raw: str | None, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_float(raw: str | None, default: float) -> float:
    return safe_float(raw, default)


def env_decimal(raw: str | None, default: Decimal | str) -> Decimal:
    try:
        if raw is None or raw == "":
            return Decimal(str(default))
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return Decimal(str(default))


def env_int(raw: str | None, default: int) -> int:
    return safe_int(raw, default)
