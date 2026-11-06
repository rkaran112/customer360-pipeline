"""END-TO-END tests - the whole pipeline on the REAL files from the brief."""
import json
import shutil
import subprocess
import sys

import pytest

import run_pipeline
from pipeline import quality
from pipeline.config import PROJECT_ROOT

REAL_DATA = PROJECT_ROOT / "data" / "raw"


@pytest.fixture(scope="module")
def real_run(tmp_path_factory):
    """Run the full pipeline once on the real data into a temp folder (shared by the tests below)."""
    out = tmp_path_factory.mktemp("e2e") / "output"
    return run_pipeline.run(input_dir=REAL_DATA, output_dir=out), out


def test_exact_dq_numbers_on_real_data(real_run):
    """The real files give exactly the defects found when profiling them. Why: proves every planted defect is caught."""
    report = real_run[0]["dq_report"]
    assert {k: report[k] for k in ["total_records", "duplicates", "invalid_emails", "missing_fields",
                                   "invalid_customer_ids", "negative_revenue", "successful_records"]} == {
        "total_records": 1501, "duplicates": 1, "invalid_emails": 3, "missing_fields": 1,
        "invalid_customer_ids": 1, "negative_revenue": 1, "successful_records": 1494}


def test_all_checks_pass_on_real_data(real_run):
    """All 6 step-6 checks pass. Why: a release is only trusted when every check is green."""
    result = real_run[0]
    assert result["ok"] and all(ok for _, ok, _ in result["checks"])
    assert len(result["customer_360"]) == 497


def test_all_output_files_exist_and_are_not_empty(real_run):
    """Every promised output file is written and non-empty. Why: downstream users depend on these files."""
    out = real_run[1]
    files = ["customer_360.csv", "customer_360.json", "customer_360.parquet", "dq_report.json", "quarantine.csv",
             "lineage.csv", "run_metrics.json", "run_history.jsonl", "insights/top_customers.csv",
             "insights/segment_summary.csv", "insights/headlines.json"]
    for name in files:
        assert (out / name).exists() and (out / name).stat().st_size > 0, name


def test_rerun_gives_identical_results(real_run, tmp_path):
    """Running twice on the same data gives the same 360 and DQ report. Why: idempotent = safe to rerun after a failure."""
    second_out = tmp_path / "again"
    run_pipeline.run(input_dir=REAL_DATA, output_dir=second_out)
    first_out = real_run[1]
    assert (first_out / "customer_360.csv").read_bytes() == (second_out / "customer_360.csv").read_bytes()
    assert json.loads((first_out / "dq_report.json").read_text()) == json.loads((second_out / "dq_report.json").read_text())


def test_stage_counts_flow_into_each_other(real_run):
    """Rows out of each stage = rows in of the next. Why: the pipeline diagram's numbers must be consistent."""
    stages = real_run[0]["run_metrics"]["stages"]
    for before, after in zip(stages, stages[1:]):
        assert before["rows_out"] == after["rows_in"], (before["stage"], after["stage"])
    q = next(s for s in stages if s["stage"] == "Quality gate")
    assert q["rows_in"] == q["rows_out"] + q["rows_rejected"]


def test_lineage_explains_every_record(real_run):
    """Every input record has a lineage line; C0420's order shows the reason. Why: any 'missing customer' question is answerable."""
    lineage = real_run[0]["lineage"]
    assert len(lineage) == 1501
    c0420 = lineage[(lineage["customer_id"] == "C0420") & (lineage["_source"] == "orders")].iloc[0]
    assert c0420["status"] == "QUARANTINED" and c0420["reason"] == "negative_revenue"


def test_cli_exit_code_zero_on_success(tmp_path):
    """`python run_pipeline.py` exits 0 on good data. Why: schedulers (cron/Airflow/CI) read the exit code."""
    done = subprocess.run([sys.executable, "run_pipeline.py", "--output-dir", str(tmp_path / "o")],
                          cwd=PROJECT_ROOT, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_cli_exit_code_one_on_missing_file(tmp_path):
    """A missing input file -> exit code 1 and a clear message. Why: a failed run must be visible to the scheduler."""
    shutil.copy(REAL_DATA / "crm_customers.json", tmp_path)
    done = subprocess.run([sys.executable, "run_pipeline.py", "--input-dir", str(tmp_path),
                           "--output-dir", str(tmp_path / "o")], cwd=PROJECT_ROOT, capture_output=True, text=True)
    assert done.returncode == 1 and "orders.csv" in done.stderr


def test_failed_check_gives_exit_code_one(monkeypatch, tmp_path):
    """If reconciliation fails, main() returns 1. Why: wrong numbers must never be published as a success."""
    monkeypatch.setattr(quality, "reconcile", lambda report: False)
    assert run_pipeline.main(["--output-dir", str(tmp_path / "o")]) == 1
