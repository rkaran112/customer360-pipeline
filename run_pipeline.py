"""
RUN THE WHOLE PIPELINE with one command.

    python run_pipeline.py                         use config.json and data/raw/
    python run_pipeline.py --config other.json     use another config
    python run_pipeline.py --input-dir D:/new_files --output-dir D:/results

Exit code 0 = success, 1 = the run failed (bad config, missing file, schema drift,
or a check failed). Schedulers like cron, Airflow or GitHub Actions read this code.

This file only CONNECTS the steps and measures them. The real work is in pipeline/.

    STEP 1 extract  ->  STEP 2 standardize  ->  STEP 3 quality  ->  STEP 4 merge
         ->  STEP 5 insights  ->  STEP 6 checks  ->  write output/
"""
import argparse
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from pipeline import checks, extract, insights, merge, quality, standardize
from pipeline.config import load_config
from pipeline.logger import setup_logging
from pipeline.outputs import write_outputs


def _metric(stage, description, rows_in, rows_out, started, rejected=0, ok=True, details=None):
    """One line of the run metrics: what a stage got, what it produced, how long it took."""
    return {"stage": stage, "description": description, "rows_in": int(rows_in), "rows_out": int(rows_out),
            "rows_rejected": int(rejected), "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "ok": ok, "details": details or {}}


def run(config_path=None, input_dir=None, output_dir=None, write=True):
    """Run steps 1-6 and return everything in one dict (the dashboard calls this too).

    input_dir / output_dir override config.json (the dashboard uses this for uploaded files).
    """
    config = load_config(config_path)
    if input_dir:
        config["input_dir"] = Path(input_dir)
    if output_dir:
        config["output_dir"] = Path(output_dir)
    log = setup_logging(config["log_dir"])
    run_started = time.perf_counter()
    stages = []
    log.info("=== Customer 360 pipeline: %s -> %s", config["input_dir"], config["output_dir"])

    # ---- STEP 1: EXTRACT - read the JSON, CSV and XML files as text.
    log.info("STEP 1  EXTRACT")
    t = time.perf_counter()
    raw = extract.extract_all(config)
    total = sum(len(df) for df in raw.values())
    stages.append(_metric("Extract", "read JSON / CSV / XML as text", total, total, t,
                          details={k: len(v) for k, v in raw.items()}))

    # ---- STEP 2: STANDARDIZE - one schema, tidy values, real types, schema drift guard.
    log.info("STEP 2  STANDARDIZE")
    t = time.perf_counter()
    tables, drift = standardize.standardize_all(raw, config)
    stages.append(_metric("Standardize", "rename columns, trim, fix types", total, total, t,
                          details={"schema_drift_warnings": len(drift)}))

    # ---- STEP 3: QUALITY GATE - rules -> clean + quarantine, DQ report.
    log.info("STEP 3  QUALITY GATE")
    t = time.perf_counter()
    clean, quarantine, lineage = quality.check_all(tables, config)
    dq_report = quality.build_dq_report(tables, clean, quarantine, drift)
    passed = dq_report["successful_records"]
    stages.append(_metric("Quality gate", "5 rules, bad rows to quarantine", total, passed, t,
                          rejected=len(quarantine), ok=dq_report["reconciled"],
                          details=dq_report["by_source"]))

    # ---- STEP 4: MERGE - one row per customer (CRM left join orders left join tickets).
    log.info("STEP 4  MERGE (Customer 360)")
    t = time.perf_counter()
    c360 = merge.build_customer_360(clean)
    integrity = merge.referential_integrity(clean, c360)
    dq_report["referential_integrity"] = integrity
    lineage = merge.finish_lineage(lineage, c360)
    orphans = integrity["orders_without_customer"] + integrity["tickets_without_customer"]
    stages.append(_metric("Merge", "join on customer_id -> 1 row/customer", passed, len(c360), t,
                          rejected=orphans, details=integrity))

    # ---- STEP 5: INSIGHTS - segments, RFM, top customers, city and support analytics.
    log.info("STEP 5  INSIGHTS")
    t = time.perf_counter()
    c360, as_of = insights.enrich(c360, config)
    insight_tables, headlines = insights.build_insights(c360, config)
    dq_report["as_of_date"] = None if as_of is None else as_of.strftime("%Y-%m-%d")
    stages.append(_metric("Insights", f"{len(insight_tables)} reports + segments", len(c360), len(c360), t))

    # ---- STEP 6: CHECKS - prove nothing was lost before publishing.
    log.info("STEP 6  CHECKS")
    t = time.perf_counter()
    check_results = checks.run_checks(dq_report, clean, c360)
    for name, ok, details in check_results:
        (log.info if ok else log.error)("  %s  %-30s %s", "PASS" if ok else "FAIL", name, details)
    all_ok = all(ok for _, ok, _ in check_results)
    stages.append(_metric("Checks", f"{len(check_results)} reconciliation checks", len(c360), len(c360), t, ok=all_ok))

    duration = round(time.perf_counter() - run_started, 3)
    run_metrics = {"stages": stages, "checks": [{"name": n, "ok": o, "details": d} for n, o, d in check_results],
                   "duration_seconds": duration}
    run_summary = {"run_id": uuid.uuid4().hex[:8], "finished_at": datetime.now().isoformat(timespec="seconds"),
                   "ok": all_ok, "duration_seconds": duration, "total_records": dq_report["total_records"],
                   "successful_records": passed, "quarantined_records": len(quarantine),
                   "dq_score_pct": dq_report["dq_score_pct"], "customers": len(c360)}

    result = {"config": config, "customer_360": c360, "dq_report": dq_report, "quarantine": quarantine,
              "lineage": lineage, "insights": insight_tables, "headlines": headlines,
              "checks": check_results, "run_metrics": run_metrics, "run_summary": run_summary, "ok": all_ok}

    if write:
        write_outputs(result, config["output_dir"])
        log.info("Outputs written to %s", config["output_dir"])
    log.info("=== %s in %.2fs | %d records -> %d passed, %d quarantined | %d customers | DQ score %.2f%%",
             "FINISHED OK" if all_ok else "FAILED CHECKS", duration, dq_report["total_records"], passed,
             len(quarantine), len(c360), dq_report["dq_score_pct"])
    return result


def main(argv=None):
    """Command-line entry point. Returns the exit code (0 ok, 1 failed)."""
    parser = argparse.ArgumentParser(description="Customer 360 data pipeline")
    parser.add_argument("--config", help="path to a config JSON (default: config.json)")
    parser.add_argument("--input-dir", help="folder with the source files (overrides config)")
    parser.add_argument("--output-dir", help="folder for the results (overrides config)")
    args = parser.parse_args(argv)
    try:
        result = run(args.config, args.input_dir, args.output_dir)
    except (FileNotFoundError, ValueError, standardize.SchemaDriftError) as error:
        # WHY catch these: a clear one-line message is better than a long traceback
        # for the person on call. Any OTHER error is a bug and shows the full traceback.
        print(f"PIPELINE FAILED: {error}", file=sys.stderr)
        return 1
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
