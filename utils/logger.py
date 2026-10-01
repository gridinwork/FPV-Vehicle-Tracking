"""Application log. Frame-level events are intentionally not written here."""

from __future__ import annotations

import logging
import sys

from utils.paths import LOG_PATH, ensure_dirs


_CONFIGURED = False


def get_logger(name: str = "vtol") -> logging.Logger:
    global _CONFIGURED
    ensure_dirs()
    logger = logging.getLogger(name)
    if _CONFIGURED:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s  %(levelname)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    _CONFIGURED = True
    return logger
