from __future__ import annotations

import logging
import sys
from logging import LogRecord
from pathlib import Path

from .utils import sanitize


class SecretFilter(logging.Filter):
    def filter(self, record: LogRecord) -> bool:
        if isinstance(record.args, dict):
            record.args = sanitize(record.args)
        return True


def setup_logger(name: str = "bitget_bot", log_dir: str | Path = "logs") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    secret_filter = SecretFilter()

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(secret_filter)
    logger.addHandler(stream_handler)

    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(Path(log_dir) / "bot.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.addFilter(secret_filter)
        logger.addHandler(file_handler)
    except OSError:
        logger.warning("No se pudo crear archivo de log; usando solo consola.")

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return logger

