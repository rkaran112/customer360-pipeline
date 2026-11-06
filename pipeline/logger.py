"""
LOGGING - every step writes what it did to the screen AND to logs/pipeline.log.

Why a log file?  (NFR-6 "observable")
    When the pipeline runs at 2 a.m. on a server, nobody is watching the screen.
    The log file is how you find out tomorrow what happened and why.
"""
import logging
from pathlib import Path

LOGGER_NAME = "customer360"


def setup_logging(log_dir, level=logging.INFO):
    """Create the logger with two outputs: the console and logs/pipeline.log."""
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)

    # WHAT: remove handlers from an earlier run in the same Python process.
    # WHY: the dashboard can run the pipeline many times; without this every log
    #      line would be printed 2, 3, 4... times.
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    # Format of one log line, e.g.  2026-11-06 14:02:11 | INFO | STEP 1 ...
    # >>> TO CHANGE: edit this string to change what a log line looks like.
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    log_file = logging.FileHandler(Path(log_dir) / "pipeline.log", encoding="utf-8")
    log_file.setFormatter(fmt)
    logger.addHandler(log_file)
    return logger


def get_logger():
    """Every module calls this to get the same shared logger."""
    return logging.getLogger(LOGGER_NAME)
