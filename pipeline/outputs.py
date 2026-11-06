"""
OUTPUTS - write every result to the output/ folder.

    output/customer_360.csv / .json / .parquet   the unified dataset (3 formats, D-15)
    output/dq_report.json                        the data-quality report from the brief
    output/quarantine.csv                        every rejected record + reason
    output/lineage.csv                           every input record and what happened to it
    output/run_metrics.json                      per-stage counts and timings (pipeline diagram)
    output/run_history.jsonl                     one line per run, ever (DQ trend)
    output/insights/*.csv + headlines.json       the business answers

Why 3 formats for the 360?  CSV opens in Excel, JSON matches the brief's example and
feeds APIs, Parquet is compressed + typed and is what Spark/Power BI/warehouses read fast.
"""
import json
import math
from pathlib import Path

import pandas as pd


def _plain(value):
    """Turn pandas/numpy values into plain JSON values (125000.0 -> 125000, NaN -> null, date -> '2024-01-05')."""
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if hasattr(value, "item"):           # numpy number -> python number
        value = value.item()
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return value


def to_records(df):
    """DataFrame -> list of plain dicts, ready for json.dump."""
    return [{col: _plain(v) for col, v in row.items()} for row in df.to_dict("records")]


def for_csv(df):
    """Dates as YYYY-MM-DD text, so CSV files look exactly like the source files."""
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[col]):
            out[col] = out[col].dt.strftime("%Y-%m-%d")
    return out


def write_json(data, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


def write_outputs(result, output_dir):
    """Write everything in `result` (built by run_pipeline.run) to output_dir."""
    out = Path(output_dir)
    (out / "insights").mkdir(parents=True, exist_ok=True)

    c360 = result["customer_360"]
    for_csv(c360).to_csv(out / "customer_360.csv", index=False)
    write_json(to_records(c360), out / "customer_360.json")
    c360.to_parquet(out / "customer_360.parquet", index=False)

    write_json(result["dq_report"], out / "dq_report.json")
    for_csv(result["quarantine"]).to_csv(out / "quarantine.csv", index=False)
    result["lineage"].to_csv(out / "lineage.csv", index=False)

    for name, table in result["insights"].items():
        for_csv(table).to_csv(out / "insights" / f"{name}.csv", index=False)
    write_json(result["headlines"], out / "insights" / "headlines.json")

    write_json(result["run_metrics"], out / "run_metrics.json")

    # WHAT: append (not overwrite) one summary line per run.  WHY: keeps a history,
    # so the dashboard can show whether data quality is getting better or worse.
    with open(out / "run_history.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(result["run_summary"], default=str) + "\n")


def read_run_history(output_dir):
    """Read every past run summary (empty list if there is no history yet)."""
    path = Path(output_dir) / "run_history.jsonl"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
