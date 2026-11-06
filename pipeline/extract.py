"""
STEP 1 - EXTRACT: read every source file into a table (a pandas DataFrame).

Input : the 3 raw files listed under "input_sources" in config.json
Output: one DataFrame per source, every value kept as TEXT, plus 2 tracking columns:
            _source      which source the row came from ("crm", "orders", "tickets")
            _source_row  the record number inside that file (1 = first record)

Why read everything as text?
    If "lifetime_value" contains "N/A" and we ask pandas for numbers, the load
    crashes and we lose the whole file. Reading text first means loading NEVER
    fails; step 2 converts the types and step 3 reports anything that did not fit.

Why the tracking columns?
    They power the "record lineage" feature: for any customer we can show exactly
    which file and which record their data came from, and where it was rejected.
"""
import json
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd

from pipeline.diagnostics import HINTS, PipelineDataError, SourceFileError
from pipeline.logger import get_logger

log = get_logger()


# ----------------------------------------------------------------- one reader per file format
# Every reader has the same shape:  (path, source_config) -> list of dicts (one dict per record).
# Keeping them the same shape is what lets us pick a reader just by its name (see READERS).

def read_json(path, source):
    """JSON: the file must be a list of objects, e.g. [{"customer_id": "C0001", ...}, ...]."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path.name}: expected a JSON list of records, got {type(data).__name__}")
    return data


def read_csv(path, source):
    """CSV: dtype=str keeps every value as text, keep_default_na=False keeps blanks as ''."""
    # WHY keep_default_na=False: otherwise pandas silently turns the text "NA" into a missing value.
    return pd.read_csv(path, dtype=str, keep_default_na=False).to_dict("records")


def read_xml(path, source):
    """XML: every <customer> element becomes one record; its child tags become columns.

    Example:  <customer><customerId>C0001</customerId><tickets>4</tickets></customer>
          ->  {"customerId": "C0001", "tickets": "4"}
    An empty tag like <customerId /> becomes "" (empty text), never a crash.
    """
    record_tag = source.get("xml_record_tag", "record")  # >>> TO CHANGE: set per source in config.json
    root = ET.parse(path).getroot()
    return [{child.tag: (child.text or "") for child in element} for element in root.iter(record_tag)]


# Format name (from config.json) -> the function that reads it.
# >>> TO CHANGE: a new file FORMAT (e.g. parquet, excel) = write one read_xxx function
#     above and add one line here. A new SOURCE in an existing format needs NO code,
#     just a new entry in config.json "input_sources".
READERS = {
    "json": read_json,
    "csv": read_csv,
    "xml": read_xml,
}


# ----------------------------------------------------------------- the step itself

# How many retries happened in the current run (reported in run_history -> health panel).
RETRY_COUNT = {"value": 0}


def read_with_retry(reader, path, source, attempts=3, backoff_seconds=0.2):
    """Call the reader; if the file is TEMPORARILY unavailable, wait and try again.

    Example: the source system is still writing the file, or Excel has it open for a moment.
    Waits 0.2s, then 0.4s, then gives up (exponential backoff). Broken CONTENT is not retried:
    reading a broken JSON file again gives the same error, so that fails immediately.
    """
    for attempt in range(1, attempts + 1):
        try:
            return reader(path, source)
        except (PermissionError, BlockingIOError, TimeoutError) as error:
            if attempt == attempts:
                raise SourceFileError(f"source '{source['name']}': {path.name} still unavailable after "
                                      f"{attempts} attempts: {error}", hint=HINTS["PermissionError"]) from error
            RETRY_COUNT["value"] += 1
            wait = backoff_seconds * 2 ** (attempt - 1)
            log.warning("  %s temporarily unavailable (%s) - retry %d/%d in %.1fs",
                        path.name, type(error).__name__, attempt, attempts - 1, wait)
            time.sleep(wait)


def extract_source(source, input_dir, retry=None):
    """Read ONE source and return it as a DataFrame of text values + tracking columns."""
    fmt = source["format"].lower()
    if fmt not in READERS:
        raise PipelineDataError(f"source '{source['name']}': unknown format '{fmt}'. Supported: {sorted(READERS)}",
                                hint="Fix the 'format' of this source in config.json.")

    path = Path(input_dir) / source["file"]
    if not path.exists():
        raise FileNotFoundError(f"source '{source['name']}': input file not found: {path}")

    # WHAT: read the file (retrying short hiccups); if it is broken, raise ONE clear error
    #       naming the file and the line.
    # WHY: a raw "Expecting ',' delimiter: line 1 column 812" doesn't say WHICH of 3 files broke.
    # >>> TO CHANGE: retry attempts / wait time live in config.json "retry".
    retry = retry or {}
    try:
        records = read_with_retry(READERS[fmt], path, source,
                                  retry.get("attempts", 3), retry.get("backoff_seconds", 0.2))
    except (json.JSONDecodeError, ET.ParseError, UnicodeDecodeError, pd.errors.ParserError) as error:
        raise SourceFileError(
            f"source '{source['name']}': cannot read {path.name} as {fmt.upper()}: {error}",
            hint=HINTS.get(type(error).__name__, "")) from error

    # WHAT: build the table. An empty file still gets the expected columns.
    # WHY: so later steps see a normal (just empty) table instead of crashing on missing columns.
    if records:
        df = pd.DataFrame(records)
    else:
        df = pd.DataFrame(columns=source.get("expected_columns", []))

    # WHAT: make every value text; missing values stay missing (not the word "nan").
    df = df.astype("object").where(df.notna(), None)
    df = df.apply(lambda col: col.map(lambda v: None if v is None else str(v)))

    # WHAT: add the two tracking columns used by lineage and quarantine.
    df["_source"] = source["name"]
    df["_source_row"] = range(1, len(df) + 1)
    log.info("  extracted %-8s %5d records from %s (%s)", source["name"], len(df), path.name, fmt,
             extra={"source": source["name"], "records": len(df)})
    log.debug("  %s columns as received: %s", source["name"], [c for c in df.columns if not c.startswith("_")])
    return df


def extract_all(config, input_dir=None):
    """STEP 1 for every source in config.json. Returns {source name: DataFrame}."""
    input_dir = input_dir or config["input_dir"]
    RETRY_COUNT["value"] = 0
    return {source["name"]: extract_source(source, input_dir, config.get("retry"))
            for source in config["input_sources"]}
