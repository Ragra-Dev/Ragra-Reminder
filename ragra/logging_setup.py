"""Shared logging setup for unattended runs (the Task Scheduler entrypoint).

Interactive CLI commands (sync/reminders/serve) keep using plain print() -
that's unchanged. `ragra tick`, the thing Task Scheduler actually calls, logs
to a rotating file under RAGRA_HOME/logs since nothing is watching stdout
when it runs unattended.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(ragra_home: Path) -> logging.Logger:
    log_dir = ragra_home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("ragra")
    if logger.handlers:
        return logger  # already configured in this process

    logger.setLevel(logging.INFO)

    file_handler = RotatingFileHandler(
        log_dir / "ragra.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console_handler)

    return logger
