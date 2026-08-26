"""Shared logging setup for unattended runs (the Task Scheduler entrypoint).

Interactive CLI commands (sync/reminders/serve) keep using plain print() -
that's unchanged. `ragra tick`, the thing Task Scheduler actually calls, logs
to a rotating file under RAGRA_HOME/logs since nothing is watching stdout
when it runs unattended. The console handler is still attached alongside
it - unchanged - so a manually-run `ragra tick` in an open terminal still
shows live output; the scheduled task instead runs with its own window
hidden (see scripts/install-scheduled-task.ps1), so this file never needs
to suppress stdout/stderr itself to keep a normal scheduled run silent.

Rotation is time-based (one file per day, ~2 days retained) rather than
size-based: tick's log volume is small and steady (a handful of lines every
15 minutes), so "keep the last ~2 days" is a more meaningful retention
policy here than a byte threshold, and it matches the operational need
(recent-history troubleshooting) without growing without bound. This never
touches application data (tasks/courses/timetable_events/reminders) - only
this operational log file is time-limited.
"""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


def configure_logging(ragra_home: Path) -> logging.Logger:
    log_dir = ragra_home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("ragra")
    if logger.handlers:
        return logger  # already configured in this process

    logger.setLevel(logging.INFO)

    file_handler = TimedRotatingFileHandler(
        log_dir / "ragra.log", when="D", interval=1, backupCount=2, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console_handler)

    return logger
