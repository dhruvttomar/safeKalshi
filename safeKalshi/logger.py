"""
Structured logging: writes to both console and a rotating file.
Every log record includes a timestamp, level, and module name.
"""
import logging
import sys
from logging.handlers import RotatingFileHandler

import config


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)

    if logger.handlers:
        # Already configured — return as-is (avoid duplicate handlers)
        return logger

    level = getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)
    logger.setLevel(level)

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    # Rotating file handler (10 MB per file, keep 5 backups)
    try:
        file_handler = RotatingFileHandler(
            config.LOG_FILE,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError as exc:
        logger.warning("Could not open log file %s: %s", config.LOG_FILE, exc)

    # Prevent log records from bubbling up to the root logger
    logger.propagate = False

    return logger
