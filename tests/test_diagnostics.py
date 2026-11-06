"""CRASH + LOGGING + RECOVERY tests - when something breaks, the developer gets what they need, and good data is kept."""
import json
import logging
from logging.handlers import RotatingFileHandler

import pytest

import run_pipeline
from pipeline import extract, merge
from pipeline.diagnostics import SourceFileError, hint_for
from pipeline.logger import LOGGER_NAME
from pipeline.outputs import read_run_history
from tests.conftest import source_cfg, write_sources


def execute(config):
    return run_pipeline.execute(input_dir=config["input_dir"], output_dir=config["output_dir"],
                                log_dir=config["log_dir"])


def test_code_crash_gives_exit_2_and_full_crash_report(config, monkeypatch):
    """A bug in merge -> exit 2 + crash report with stage, traceback, input fingerprints, reproduce command.
    Why: the developer can debug from the report alone, without re-running."""
    write_sources(config["input_dir"])
    monkeypatch.setattr(merge, "build_customer_360", lambda clean: 1 / 0)
    outcome = execute(config)
    assert outcome["exit_code"] == 2 and outcome["stage"] == "Merge"
    report = json.loads(open(outcome["crash_report"], encoding="utf-8").read())
    assert report["kind"] == "code_error" and report["error_type"] == "ZeroDivisionError"
    assert any("ZeroDivisionError" in line for line in report["traceback"])
    assert report["input_files"]["orders.csv"]["exists"] and len(report["input_files"]["orders.csv"]["md5"]) == 32
    assert "--log-level DEBUG" in report["reproduce_with"]


def test_broken_json_gives_exit_1_naming_file_and_line(config):
    """Truncated JSON -> exit 1, message names crm_customers.json and the line. Why: the fix is in the data, not the code."""
    write_sources(config["input_dir"])
    path = config["input_dir"] / "crm_customers.json"
    path.write_text(path.read_text()[:-20])
    outcome = execute(config)
    assert outcome["exit_code"] == 1 and outcome["stage"] == "Extract"
    assert isinstance(outcome["error"], SourceFileError)
    assert "crm_customers.json" in str(outcome["error"]) and "line" in str(outcome["error"])


def test_crash_report_contains_no_personal_data(config, monkeypatch):
    """The crash report has file fingerprints, never file contents. Why: crash reports get shared in tickets and chats."""
    write_sources(config["input_dir"])
    monkeypatch.setattr(merge, "build_customer_360", lambda clean: 1 / 0)
    text = open(execute(config)["crash_report"], encoding="utf-8").read()
    assert "amit.sharma@gmail.com" not in text and "Amit Sharma" not in text


def test_failed_runs_are_recorded_in_history(config):
    """A failed run appends a history line with ok=False and its error. Why: the health panel and trends need failures too."""
    config["input_dir"].mkdir(parents=True)          # empty folder: every file missing
    outcome = execute(config)
    last = read_run_history(config["output_dir"])[-1]
    assert last["run_id"] == outcome["run_id"] and last["ok"] is False and last["status"] == "FAILED_DATA"


def test_last_good_outputs_survive_a_failed_run(config):
    """Good run, then a broken file: the published customer_360.csv is unchanged. Why: a bad day must not wipe good data."""
    write_sources(config["input_dir"])
    assert execute(config)["exit_code"] == 0
    good = (config["output_dir"] / "customer_360.csv").read_bytes()
    (config["input_dir"] / "orders.csv").write_text("cust_id,total_orders\nC0001,1\n")     # mandatory column gone
    assert execute(config)["exit_code"] == 1
    assert (config["output_dir"] / "customer_360.csv").read_bytes() == good


def test_json_log_lines_have_run_id(config):
    """Every line in pipeline.jsonl is valid JSON with the run id. Why: log tools filter by run id."""
    write_sources(config["input_dir"])
    outcome = execute(config)
    lines = [json.loads(line) for line in open(config["log_dir"] / "pipeline.jsonl", encoding="utf-8")]
    assert lines and all(line["run_id"] == outcome["run_id"] for line in lines)
    assert any(line.get("stage") == "Merge" for line in lines)


def test_log_files_rotate(config):
    """Log files use size-based rotation. Why: logs must never fill up a server's disk."""
    handlers = logging.getLogger(LOGGER_NAME).handlers
    rotating = [h for h in handlers if isinstance(h, RotatingFileHandler)]
    assert len(rotating) == 2 and all(h.maxBytes > 0 and h.backupCount > 0 for h in rotating)


def test_debug_level_logs_extra_detail(config):
    """--log-level DEBUG adds column types and per-rule examples. Why: faster bug hunting without code changes."""
    write_sources(config["input_dir"])
    run_pipeline.execute(input_dir=config["input_dir"], output_dir=config["output_dir"],
                         log_dir=config["log_dir"], log_level="DEBUG")
    text = (config["log_dir"] / "pipeline.log").read_text(encoding="utf-8")
    assert "DEBUG" in text and "types:" in text


def test_transient_lock_is_retried_and_succeeds(config, monkeypatch):
    """A file locked for 2 reads succeeds on the 3rd. Why: short hiccups should heal without a human."""
    write_sources(config["input_dir"])
    real, calls = extract.READERS["csv"], {"n": 0}

    def flaky(path, source):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError("locked")
        return real(path, source)
    monkeypatch.setitem(extract.READERS, "csv", flaky)
    df = extract.extract_source(source_cfg(config, "orders"), config["input_dir"], {"attempts": 3, "backoff_seconds": 0})
    assert len(df) == 3 and calls["n"] == 3


def test_retry_gives_up_with_clear_error(config, monkeypatch):
    """Still locked after all attempts -> a clear SourceFileError with a hint. Why: don't retry forever."""
    write_sources(config["input_dir"])

    def always_locked(path, source):
        raise PermissionError("locked")
    monkeypatch.setitem(extract.READERS, "csv", always_locked)
    with pytest.raises(SourceFileError, match="unavailable after 2 attempts") as caught:
        extract.extract_source(source_cfg(config, "orders"), config["input_dir"], {"attempts": 2, "backoff_seconds": 0})
    assert "another program" in caught.value.hint


@pytest.mark.parametrize("error, words", [
    (FileNotFoundError("x"), "input_dir"), (KeyError("x"), "column"), (RuntimeError("x"), "bug")])
def test_every_error_gets_a_hint(error, words):
    """Known errors get specific advice; unknown ones are flagged as a likely bug. Why: tell on-call what to do next."""
    assert words in hint_for(error)
