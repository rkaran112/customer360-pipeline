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
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd

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

def extract_source(source, input_dir):
    """Read ONE source and return it as a DataFrame of text values + tracking columns."""
    fmt = source["format"].lower()
    if fmt not in READERS:
        raise ValueError(f"source '{source['name']}': unknown format '{fmt}'. Supported: {sorted(READERS)}")

    path = Path(input_dir) / source["file"]
    if not path.exists():
        raise FileNotFoundError(f"source '{source['name']}': input file not found: {path}")

    records = READERS[fmt](path, source)

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
    log.info("  extracted %-8s %5d records from %s (%s)", source["name"], len(df), path.name, fmt)
    return df


def extract_all(config, input_dir=None):
    """STEP 1 for every source in config.json. Returns {source name: DataFrame}."""
    input_dir = input_dir or config["input_dir"]
    return {source["name"]: extract_source(source, input_dir) for source in config["input_sources"]}
