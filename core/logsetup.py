"""日志初始化。"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "wpbackup"


def setup_logging(base_dir: Path) -> logging.Logger:
    log_dir = Path(base_dir) / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%Y-%m-%d %H:%M:%S")
        try:
            fh = RotatingFileHandler(
                log_dir / "app.log", maxBytes=5 * 1024 * 1024,
                backupCount=5, encoding="utf-8",
            )
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except OSError:
            pass

    return logger
