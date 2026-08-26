"""Tests for the tick logging setup: correct handler configuration, actual
writes reaching the log file, and that retention (rotate daily, keep ~2
days) actually enforces itself rather than growing without bound.
"""

import logging
from logging.handlers import TimedRotatingFileHandler

from ragra.logging_setup import configure_logging


def _fresh_logger():
    # configure_logging short-circuits if "ragra" already has handlers
    # (real code reuses the same process-wide logger across calls); tests
    # need a clean slate each time.
    logger = logging.getLogger("ragra")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def test_configure_logging_uses_time_based_rotation_with_two_day_retention(tmp_path):
    _fresh_logger()
    logger = configure_logging(tmp_path)

    file_handlers = [h for h in logger.handlers if isinstance(h, TimedRotatingFileHandler)]
    assert len(file_handlers) == 1
    handler = file_handlers[0]
    assert handler.when == "D"
    assert handler.interval == 86400  # TimedRotatingFileHandler normalizes "D" to seconds
    assert handler.backupCount == 2


def test_configure_logging_writes_real_log_lines(tmp_path):
    _fresh_logger()
    logger = configure_logging(tmp_path)
    logger.info("tick start")
    logger.info("Timetable sync: 3 class(es) found, 0 new, 0 updated, 3 unchanged, 0 cancelled")

    log_file = tmp_path / "logs" / "ragra.log"
    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "tick start" in content
    assert "Timetable sync: 3 class(es) found" in content


def test_configure_logging_is_idempotent_within_a_process(tmp_path):
    _fresh_logger()
    logger1 = configure_logging(tmp_path)
    logger2 = configure_logging(tmp_path)
    assert logger1 is logger2
    assert len(logger1.handlers) == 2  # file + console, not doubled


def test_rotation_enforces_two_day_retention(tmp_path):
    _fresh_logger()
    logger = configure_logging(tmp_path)
    logger.info("day 0 entry")

    handler = next(h for h in logger.handlers if isinstance(h, TimedRotatingFileHandler))
    log_dir = tmp_path / "logs"

    # Simulate several real daily rollovers without waiting real time -
    # doRollover() is the same code a real day boundary triggers.
    for _ in range(5):
        handler.doRollover()
        logger.info("subsequent day entry")

    rotated_files = sorted(p for p in log_dir.iterdir() if p.name != "ragra.log")
    # backupCount=2 means at most 2 old rotated files are ever kept, no
    # matter how many rollovers have happened - older ones are removed.
    assert len(rotated_files) <= 2


def test_never_touches_application_data(tmp_path):
    # The retention policy is scoped to the log file only - confirm the
    # database file (if present in the same home dir) is never created,
    # opened, or touched by configure_logging/rotation at all.
    _fresh_logger()
    db_path = tmp_path / "ragra.db"
    db_path.write_bytes(b"not a real db, just a sentinel")
    original_bytes = db_path.read_bytes()

    logger = configure_logging(tmp_path)
    handler = next(h for h in logger.handlers if isinstance(h, TimedRotatingFileHandler))
    for _ in range(3):
        handler.doRollover()

    assert db_path.read_bytes() == original_bytes
