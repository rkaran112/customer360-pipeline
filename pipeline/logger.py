"""
LOGGING - every step writes what it did, in TWO forms:

    console + logs/pipeline.log     for people:   2026-11-06 14:02:11 | INFO | a1b2c3d4 | STEP 1 ...
    logs/pipeline.jsonl             for machines: {"time": "...", "level": "INFO", "run_id": "a1b2c3d4", ...}

Why both?  (NFR-6 "observable")
    - A person on call reads pipeline.log.
    - A log tool (Splunk, ELK, CloudWatch, Datadog) reads pipeline.jsonl: one JSON object per
      line can be searched and filtered ("show every ERROR for run a1b2c3d4").

Every line carries the RUN ID, so when 10 runs are in one file you can pick out exactly one.

Files ROTATE: when a log reaches 5 MB it is renamed pipeline.log.1 and a fresh file starts
(5 old files are kept). So logs can never fill up the disk on a server.

Debug mode:  python run_pipeline.py --log-level DEBUG
    prints extra detail (column types, per-rule counts, row samples) to find a bug faster.
"""
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "customer360"

# >>> TO CHANGE: log file size limit and how many old files to keep.
MAX_LOG_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5


class RunIdFilter(logging.Filter):
    """Stamps the current run id onto every log record (so formatters can print it)."""

    def __init__(self, run_id):
        super().__init__()
        self.run_id = run_id

    def filter(self, record):
        record.run_id = self.run_id
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Extra fields passed with log.info(..., extra={...}) are kept."""

    STANDARD = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "run_id"}

    def format(self, record):
        entry = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "run_id": getattr(record, "run_id", "-"),
            "module": record.module,
            "message": record.getMessage().strip(),
        }
        # WHAT: keep any structured extras, e.g. extra={"stage": "Merge", "duration_ms": 12.3}
        entry.update({k: v for k, v in vars(record).items() if k not in self.STANDARD})
        if record.exc_info:
            entry["traceback"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def setup_logging(log_dir, run_id="-", level="INFO"):
    """Create the logger with 3 outputs: console, pipeline.log (text), pipeline.jsonl (JSON)."""
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level if isinstance(level, int) else level.upper())
    logger.propagate = False

    # WHAT: remove handlers from an earlier run in the same Python process.
    # WHY: the dashboard runs the pipeline many times; without this every line would repeat.
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    for f in list(logger.filters):
        logger.removeFilter(f)
    logger.addFilter(RunIdFilter(run_id))

    # >>> TO CHANGE: edit this string to change what a human log line looks like.
    text = logging.Formatter("%(asctime)s | %(levelname)-7s | %(run_id)s | %(message)s", "%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler()
    console.setFormatter(text)
    logger.addHandler(console)

    text_file = RotatingFileHandler(Path(log_dir) / "pipeline.log", maxBytes=MAX_LOG_BYTES,
                                    backupCount=BACKUP_COUNT, encoding="utf-8")
    text_file.setFormatter(text)
    logger.addHandler(text_file)

    json_file = RotatingFileHandler(Path(log_dir) / "pipeline.jsonl", maxBytes=MAX_LOG_BYTES,
                                    backupCount=BACKUP_COUNT, encoding="utf-8")
    json_file.setFormatter(JsonFormatter())
    logger.addHandler(json_file)
    return logger


def get_logger():
    """Every module calls this to get the same shared logger."""
    return logging.getLogger(LOGGER_NAME)
