"""
RUN THE WHOLE PIPELINE with one command.

    python run_pipeline.py                         use config.json and data/raw/
    python run_pipeline.py --config other.json     use another config
    python run_pipeline.py --input-dir D:/new_files --output-dir D:/results
    python run_pipeline.py --log-level DEBUG       extra detail for finding a bug

EXIT CODES (schedulers like cron, Airflow or GitHub Actions read these):
    0  success - outputs published
    1  DATA problem - bad/missing input, schema drift, a CRITICAL alert or a failed check.
       Fix the input; the last good outputs are kept untouched.
    2  CODE problem - an unexpected error (a bug). Fix the code; see logs/crash_<run_id>.json.

This file only CONNECTS the steps, measures them, and handles failure. The real work is in pipeline/.

    STEP 1 extract -> 2 standardize -> 3 quality -> 4 merge -> 5 insights -> 6 checks
        -> alerts -> publish (only if healthy) -> run history
"""
import argparse
import shutil
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from pipeline import alerts, checks, extract, insights, merge, quality, standardize
from pipeline.config import load_config
from pipeline.diagnostics import hint_for, is_data_error, write_crash_report
from pipeline.logger import setup_logging
from pipeline.outputs import append_history, read_run_history, write_json, write_outputs


def _metric(stage, description, rows_in, rows_out, started, rejected=0, ok=True, details=None):
    """One line of the run metrics: what a stage got, what it produced, how long it took."""
    return {"stage": stage, "description": description, "rows_in": int(rows_in), "rows_out": int(rows_out),
            "rows_rejected": int(rejected), "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "ok": ok, "details": details or {}}


def prepare_config(config_path=None, input_dir=None, output_dir=None, log_dir=None):
    """Load config.json and apply the command-line / dashboard / chaos-test overrides."""
    config = load_config(config_path)
    for key, value in [("input_dir", input_dir), ("output_dir", output_dir), ("log_dir", log_dir)]:
        if value:
            config[key] = Path(value)
    return config


def run(config_path=None, input_dir=None, output_dir=None, write=True, log_level="INFO", run_id=None,
        config=None):
    """Run steps 1-6 + alerts and return everything in one dict (the dashboard calls this too).

    If a step raises, the error is tagged with the stage it happened in (error.pipeline_stage)
    and re-raised; execute() below turns it into a crash report.
    """
    config = config or prepare_config(config_path, input_dir, output_dir)
    run_id = run_id or uuid.uuid4().hex[:8]
    log = setup_logging(config["log_dir"], run_id, log_level)
    run_started = time.perf_counter()
    stages = []
    stage = "Setup"          # WHAT: the step we are in right now - reported if anything crashes
    log.info("=== Customer 360 pipeline run %s: %s -> %s", run_id, config["input_dir"], config["output_dir"])

    try:
        # ---- STEP 1: EXTRACT - read the JSON, CSV and XML files as text.
        stage = "Extract"
        log.info("STEP 1  EXTRACT", extra={"stage": stage})
        t = time.perf_counter()
        raw = extract.extract_all(config)
        total = sum(len(df) for df in raw.values())
        retries = extract.RETRY_COUNT["value"]
        stages.append(_metric(stage, "read JSON / CSV / XML as text", total, total, t,
                              details={**{k: len(v) for k, v in raw.items()}, "retries": retries}))

        # ---- STEP 2: STANDARDIZE - one schema, tidy values, real types, schema drift guard.
        stage = "Standardize"
        log.info("STEP 2  STANDARDIZE", extra={"stage": stage})
        t = time.perf_counter()
        tables, drift = standardize.standardize_all(raw, config)
        stages.append(_metric(stage, "rename columns, trim, fix types", total, total, t,
                              details={"schema_drift_warnings": len(drift)}))

        # ---- STEP 3: QUALITY GATE - rules -> clean + quarantine, DQ report.
        stage = "Quality gate"
        log.info("STEP 3  QUALITY GATE", extra={"stage": stage})
        t = time.perf_counter()
        clean, quarantine, lineage = quality.check_all(tables, config)
        dq_report = quality.build_dq_report(tables, clean, quarantine, drift)
        passed = dq_report["successful_records"]
        stages.append(_metric(stage, "5 rules, bad rows to quarantine", total, passed, t,
                              rejected=len(quarantine), ok=dq_report["reconciled"], details=dq_report["by_source"]))

        # ---- STEP 4: MERGE - one row per customer (CRM left join orders left join tickets).
        stage = "Merge"
        log.info("STEP 4  MERGE (Customer 360)", extra={"stage": stage})
        t = time.perf_counter()
        c360 = merge.build_customer_360(clean)
        integrity = merge.referential_integrity(clean, c360)
        dq_report["referential_integrity"] = integrity
        lineage = merge.finish_lineage(lineage, c360)
        orphans = integrity["orders_without_customer"] + integrity["tickets_without_customer"]
        stages.append(_metric(stage, "join on customer_id -> 1 row/customer", passed, len(c360), t,
                              rejected=orphans, details=integrity))

        # ---- STEP 5: INSIGHTS - segments, RFM, top customers, city and support analytics.
        stage = "Insights"
        log.info("STEP 5  INSIGHTS", extra={"stage": stage})
        t = time.perf_counter()
        c360, as_of = insights.enrich(c360, config)
        insight_tables, headlines = insights.build_insights(c360, config)
        dq_report["as_of_date"] = None if as_of is None else as_of.strftime("%Y-%m-%d")
        stages.append(_metric(stage, f"{len(insight_tables)} reports + segments", len(c360), len(c360), t))

        # ---- STEP 6: CHECKS - prove nothing was lost before publishing.
        stage = "Checks"
        log.info("STEP 6  CHECKS", extra={"stage": stage})
        t = time.perf_counter()
        check_results = checks.run_checks(dq_report, clean, c360)
        for name, ok, details in check_results:
            (log.info if ok else log.error)("  %s  %-30s %s", "PASS" if ok else "FAIL", name, details)
        checks_ok = all(ok for _, ok, _ in check_results)
        stages.append(_metric(stage, f"{len(check_results)} reconciliation checks", len(c360), len(c360), t,
                              ok=checks_ok))

        # ---- ALERTS - compare against limits (and the previous run), decide if the run is healthy.
        stage = "Alerts"
        log.info("ALERTS", extra={"stage": stage})
        history = read_run_history(config["output_dir"])
        previous = next((h for h in reversed(history) if h.get("ok")), None)   # last GOOD run
        run_alerts = alerts.evaluate_alerts(dq_report, check_results, config, previous)
        fail_on = config.get("alerts", {}).get("fail_on", "CRITICAL")
        all_ok = checks_ok and not alerts.at_least(run_alerts, fail_on)

        duration = round(time.perf_counter() - run_started, 3)
        counts = {s: sum(a["severity"] == s for a in run_alerts) for s in alerts.SEVERITY_ORDER}
        run_metrics = {"run_id": run_id, "stages": stages, "duration_seconds": duration,
                       "checks": [{"name": n, "ok": o, "details": d} for n, o, d in check_results]}
        run_summary = {"run_id": run_id, "finished_at": datetime.now().isoformat(timespec="seconds"),
                       "ok": all_ok, "status": "SUCCESS" if all_ok else "FAILED_QUALITY",
                       "exit_code": 0 if all_ok else 1, "duration_seconds": duration,
                       "total_records": dq_report["total_records"], "successful_records": passed,
                       "quarantined_records": len(quarantine), "dq_score_pct": dq_report["dq_score_pct"],
                       "customers": len(c360), "alerts": counts, "retries": retries,
                       "records_by_source": {k: v["total"] for k, v in dq_report["by_source"].items()}}
        run_summary["notification"] = alerts.notify(run_alerts, run_summary, config)

        result = {"config": config, "customer_360": c360, "dq_report": dq_report, "quarantine": quarantine,
                  "lineage": lineage, "insights": insight_tables, "headlines": headlines, "alerts": run_alerts,
                  "checks": check_results, "run_metrics": run_metrics, "run_summary": run_summary, "ok": all_ok}

        # ---- PUBLISH - only a healthy run replaces the outputs (last-known-good, D-21).
        stage = "Publish"
        if write:
            publish(result, Path(config["output_dir"]), all_ok, log)
    except Exception as error:
        # WHAT: remember WHERE it broke, then let execute() write the crash report.
        error.pipeline_stage = stage
        error.run_id = run_id
        log.error("STAGE FAILED: %s - %s: %s", stage, type(error).__name__, error,
                  extra={"stage": stage, "error_type": type(error).__name__})
        raise

    log.info("=== %s in %.2fs | %d records -> %d passed, %d quarantined | %d customers | DQ %.2f%% | alerts %s",
             "FINISHED OK" if all_ok else "FAILED - outputs NOT published", duration, dq_report["total_records"],
             passed, len(quarantine), len(c360), dq_report["dq_score_pct"], counts)
    return result


def publish(result, output_dir, healthy, log):
    """Last-known-good publishing.

    Healthy run:  write everything into output/.staging first, then move each file into
                  output/. Moving a file is instant, so readers never see half-written files.
    Failed run:   write into output/failed_runs/<run_id>/ for inspection, and leave the
                  previously published (good) outputs exactly as they were.
    """
    run_id = result["run_summary"]["run_id"]
    if not healthy:
        target = output_dir / "failed_runs" / run_id
        write_outputs(result, target)
        log.error("Run is NOT healthy: outputs kept for inspection in %s; published outputs untouched", target)
        return target

    staging = output_dir / ".staging"
    shutil.rmtree(staging, ignore_errors=True)
    write_outputs(result, staging)
    for file in sorted(staging.rglob("*")):
        if file.is_file():
            destination = output_dir / file.relative_to(staging)
            destination.parent.mkdir(parents=True, exist_ok=True)
            file.replace(destination)          # atomic rename on the same disk
    shutil.rmtree(staging, ignore_errors=True)
    log.info("Outputs published to %s", output_dir)
    return output_dir


def execute(config_path=None, input_dir=None, output_dir=None, log_level="INFO", argv=None, log_dir=None):
    """Run the pipeline and NEVER raise: always return an outcome dict with an exit code.

    This is the safety net used by the command line, the dashboard and the chaos tests.
    """
    run_id = uuid.uuid4().hex[:8]
    config, stage = None, "Setup"
    try:
        config = prepare_config(config_path, input_dir, output_dir, log_dir)
        result = run(config=config, log_level=log_level, run_id=run_id)
        summary = result["run_summary"]
        append_history(config["output_dir"], summary)
        return {"exit_code": summary["exit_code"], "ok": result["ok"], "run_id": run_id, "result": result,
                "summary": summary, "alerts": result["alerts"]}
    except Exception as error:
        stage = getattr(error, "pipeline_stage", stage)
        exit_code = 1 if is_data_error(error) else 2
        fallback = {"log_dir": log_dir or "logs", "input_dir": input_dir or "data/raw", "input_sources": []}
        cfg = config or fallback
        crash_path = write_crash_report(error, stage, cfg, run_id, argv)
        summary = {"run_id": run_id, "finished_at": datetime.now().isoformat(timespec="seconds"), "ok": False,
                   "status": "FAILED_DATA" if exit_code == 1 else "CRASHED", "exit_code": exit_code,
                   "stage": stage, "error": f"{type(error).__name__}: {error}", "hint": hint_for(error),
                   "crash_report": str(crash_path), "alerts": {"INFO": 0, "WARNING": 0, "CRITICAL": 1}}
        crash_alert = [{"severity": "CRITICAL", "code": "PIPELINE_FAILED", "source": None,
                        "message": f"run {run_id} failed in stage '{stage}': {type(error).__name__}: {error}",
                        "value": exit_code, "threshold": None, "advice": hint_for(error)}]
        if config is not None:
            setup_logging(config["log_dir"], run_id, log_level)
            summary["notification"] = alerts.notify(crash_alert, summary, config)
            append_history(config["output_dir"], summary)
        return {"exit_code": exit_code, "ok": False, "run_id": run_id, "result": None, "summary": summary,
                "alerts": crash_alert, "error": error, "stage": stage, "crash_report": str(crash_path)}


def main(argv=None):
    """Command-line entry point. Returns the exit code (0 ok, 1 data problem, 2 bug)."""
    parser = argparse.ArgumentParser(description="Customer 360 data pipeline")
    parser.add_argument("--config", help="path to a config JSON (default: config.json)")
    parser.add_argument("--input-dir", help="folder with the source files (overrides config)")
    parser.add_argument("--output-dir", help="folder for the results (overrides config)")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="DEBUG shows column types, per-rule counts and examples")
    args = parser.parse_args(argv)
    outcome = execute(args.config, args.input_dir, args.output_dir, args.log_level, argv=argv)

    if "error" in outcome:
        # WHY a short message: the person on call needs WHAT broke, WHERE, and HOW TO FIX - fast.
        kind = "DATA PROBLEM" if outcome["exit_code"] == 1 else "BUG (unexpected error)"
        print(f"\nPIPELINE FAILED ({kind}) in stage '{outcome['stage']}': {outcome['error']}\n"
              f"  hint:         {outcome['summary']['hint']}\n"
              f"  crash report: {outcome['crash_report']}\n"
              f"  exit code:    {outcome['exit_code']}", file=sys.stderr)
    elif not outcome["ok"]:
        print(f"\nPIPELINE FAILED its quality gate (run {outcome['run_id']}): outputs NOT published; "
              f"see the CRITICAL alerts above. Exit code 1.", file=sys.stderr)
    return outcome["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
