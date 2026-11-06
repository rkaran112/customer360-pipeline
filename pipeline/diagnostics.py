"""
DIAGNOSTICS - help the developer when a run fails.

When the pipeline fails it does 3 things (see run_pipeline.main):
    1. prints ONE clear line: what broke, in which stage, and a HINT how to fix it
    2. writes logs/crash_<run_id>.json - a "black box recorder" with everything needed to
       debug without re-running: stage, error, full traceback, config used, a fingerprint
       of every input file, library versions, and the exact command to reproduce it
    3. sends a CRITICAL alert (console, log, and the webhook if one is configured)

Two kinds of failure, two exit codes:
    exit 1 = DATA problem   (missing file, broken JSON/XML, schema drift): fix the INPUT
    exit 2 = CODE problem   (an unexpected Python error, i.e. a bug): fix the CODE
    That difference tells the on-call person who to call: the source team or the developers.
"""
import hashlib
import json
import platform
import sys
import traceback
from datetime import datetime
from pathlib import Path


class PipelineDataError(ValueError):
    """A problem with the INPUT data or config (not a bug). `hint` says how to fix it."""

    def __init__(self, message, hint=""):
        super().__init__(message)
        self.hint = hint


class SourceFileError(PipelineDataError):
    """A source file exists but cannot be read (broken JSON/XML/CSV, wrong encoding)."""


# Advice for common errors, by error type name.
# >>> TO CHANGE: add a hint for a new kind of error here.
HINTS = {
    "FileNotFoundError": "Check input_dir in config.json (or --input-dir) and the file names under input_sources.",
    "SchemaDriftError": "A source changed its columns. Compare the file's header with expected_columns in "
                        "config.json, then either ask the source team to fix it or update the data contract.",
    "JSONDecodeError": "The JSON file is broken (often a missing comma or bracket). Open it at the line shown.",
    "ParseError": "The XML file is malformed (often an unclosed tag). Open it at the line shown.",
    "UnicodeDecodeError": "The file is not UTF-8. Re-save it as UTF-8, or add an encoding option to the reader.",
    "KeyError": "A column or config key is missing. Check config.json against the file's columns.",
    "PermissionError": "The file is open in another program (e.g. Excel) or the folder is read-only.",
}


def hint_for(error):
    """The best hint for this error: its own hint if it has one, else the HINTS table."""
    return getattr(error, "hint", "") or HINTS.get(type(error).__name__, "This looks like a bug in the code: "
                                                   "read the traceback in the crash report and rerun with --log-level DEBUG.")


def is_data_error(error):
    """True if the INPUT is at fault (exit 1), False if it is probably a bug (exit 2)."""
    from pipeline.standardize import SchemaDriftError    # local import avoids a circular import
    return isinstance(error, (PipelineDataError, SchemaDriftError, FileNotFoundError))


def fingerprint_inputs(config):
    """For every input file: does it exist, size, md5, line count, last modified.

    WHY: "it worked yesterday" - the md5 proves whether the file changed since the last good
    run, without copying (possibly personal) data into the crash report.
    """
    prints = {}
    for source in config.get("input_sources", []):
        path = Path(config["input_dir"]) / source["file"]
        if not path.exists():
            prints[source["file"]] = {"exists": False}
            continue
        data = path.read_bytes()
        prints[source["file"]] = {
            "exists": True,
            "bytes": len(data),
            "md5": hashlib.md5(data).hexdigest(),
            "lines": data.count(b"\n"),
            "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
        }
    return prints


def _versions():
    versions = {"python": platform.python_version(), "platform": platform.platform()}
    for package in ["pandas", "pyarrow", "streamlit"]:
        module = sys.modules.get(package)
        if module is not None:
            versions[package] = getattr(module, "__version__", "?")
    return versions


def write_crash_report(error, stage, config, run_id, argv=None):
    """Write logs/crash_<run_id>.json and return its path."""
    log_dir = Path(config.get("log_dir", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    safe_config = json.loads(json.dumps(config, default=str))       # Paths -> text
    command = "python run_pipeline.py " + " ".join(argv or []) + " --log-level DEBUG"
    report = {
        "run_id": run_id,
        "crashed_at": datetime.now().isoformat(timespec="seconds"),
        "kind": "data_error" if is_data_error(error) else "code_error",
        "exit_code": 1 if is_data_error(error) else 2,
        "stage": stage,
        "error_type": type(error).__name__,
        "error": str(error),
        "hint": hint_for(error),
        "reproduce_with": command.replace("  ", " ").strip(),
        "traceback": traceback.format_exception(type(error), error, error.__traceback__),
        "input_files": fingerprint_inputs(config),
        "versions": _versions(),
        "config": safe_config,
    }
    path = log_dir / f"crash_{run_id}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return path
