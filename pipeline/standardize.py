"""
STEP 2 - STANDARDIZE: make the 3 sources look like they came from one system.

Input : the raw text tables from step 1
Output: the same rows (none removed here!) with
            - the same column names everywhere   cust_id / customerId -> customer_id
            - tidy values                        " Mumbai " -> "Mumbai", "A@X.COM" -> "a@x.com"
            - real types                         "125000" -> 125000.0, "2024-01-05" -> a date

Also a SCHEMA DRIFT GUARD (decision D-19):
    config.json lists the columns we expect from each source (a "data contract").
    - An EXTRA column we don't know  -> warning, the run continues (harmless).
    - A MANDATORY column is missing  -> SchemaDriftError, the run stops (we cannot
      check or merge data without it, so publishing would be wrong).

Important: this step never removes a row. Values that do not fit their type
("N/A" as a number) become empty, and step 3 reports them as missing fields.
"""
import pandas as pd

from pipeline.logger import get_logger

log = get_logger()


class SchemaDriftError(Exception):
    """A source file arrived without a column the pipeline cannot work without."""


# ----------------------------------------------------------------- value cleaners
# Each cleaner takes one column (a pandas Series) and returns the cleaned column.

def clean_text(series):
    """Trim spaces and squeeze double spaces; blank text becomes missing (None)."""
    s = series.map(lambda v: " ".join(v.split()) if isinstance(v, str) else v)
    return s.map(lambda v: None if v in ("", None) else v)


def to_int(series):
    """'15' -> 15.  Not a whole number ('abc', '2.5', '') -> missing."""
    numbers = pd.to_numeric(series, errors="coerce")
    whole = numbers.where(numbers % 1 == 0)          # 2.5 is not a valid order count
    return whole.astype("Int64")                     # Int64 (capital I) = integer that allows missing


def to_number(series):
    """'125000' or '125000.50' -> 125000.0 / 125000.5.  'N/A' -> missing."""
    return pd.to_numeric(series, errors="coerce").astype("Float64")


def to_date(series):
    """'2024-01-05' -> a real date.  '2026-13-45' or '05/01/2024' -> missing."""
    # >>> TO CHANGE: if a source sends another date format, add it here (or make it a config value).
    return pd.to_datetime(series, format="%Y-%m-%d", errors="coerce")


TYPE_CONVERTERS = {"int": to_int, "number": to_number, "date": to_date}

# Columns that get special tidy-up rules on top of trimming.
# >>> TO CHANGE: a new tidy-up rule for a column = one new line here.
COLUMN_TIDY = {
    "email": str.lower,     # emails are not case sensitive: A@X.COM == a@x.com
    "city": str.title,      # " mumbai " / "MUMBAI" -> "Mumbai", so city totals don't split
}


# ----------------------------------------------------------------- schema drift guard

def check_schema(df, source):
    """Compare the columns that arrived with the data contract in config.json.

    Returns a list of warning strings (for the report). Raises SchemaDriftError when
    a mandatory column is missing.
    """
    arrived = [c for c in df.columns if not c.startswith("_")]
    expected = source.get("expected_columns", arrived)
    mapping = source.get("column_mapping", {})

    # Mandatory fields are written with STANDARD names (customer_id); translate the
    # arrived raw names (cust_id) so we can compare like with like.
    arrived_standard = {mapping.get(c, c) for c in arrived}
    missing_mandatory = [c for c in source.get("mandatory_fields", []) if c not in arrived_standard]
    if missing_mandatory:
        raise SchemaDriftError(
            f"source '{source['name']}' ({source['file']}) is missing mandatory column(s) "
            f"{missing_mandatory}. Columns received: {arrived}")

    warnings = []
    extra = [c for c in arrived if c not in expected]
    if extra:
        warnings.append(f"{source['name']}: unexpected column(s) {extra} were ignored")
    missing_optional = [c for c in expected if c not in arrived]
    if missing_optional:
        warnings.append(f"{source['name']}: optional column(s) {missing_optional} missing, filled as empty")
    for w in warnings:
        log.warning("  schema drift: %s", w)
    return warnings


# ----------------------------------------------------------------- the step itself

def standardize_source(df, source, column_types):
    """STEP 2 for one source. Returns (clean_typed_df, drift_warnings)."""
    warnings = check_schema(df, source)
    expected = source.get("expected_columns", [c for c in df.columns if not c.startswith("_")])

    # WHAT: keep only the contract columns (+ tracking), add any optional ones that are missing.
    df = df.copy()
    for col in expected:
        if col not in df.columns:
            df[col] = None
    df = df[expected + ["_source", "_source_row"]]

    # WHAT: rename to the one standard schema, e.g. cust_id -> customer_id.  (FR-2)
    df = df.rename(columns=source.get("column_mapping", {}))

    # WHAT: tidy every text column (trim spaces, blanks -> missing).  (FR-3)
    for col in df.columns:
        if not col.startswith("_"):
            df[col] = clean_text(df[col])
            if col in COLUMN_TIDY:
                tidy = COLUMN_TIDY[col]
                df[col] = df[col].map(lambda v: tidy(v) if isinstance(v, str) else v)

    # WHAT: convert to real types (numbers, dates) as listed in config.json "column_types".
    # >>> TO CHANGE: a new typed column = one line in config.json "column_types", no code.
    for col, type_name in column_types.items():
        if col in df.columns:
            df[col] = TYPE_CONVERTERS[type_name](df[col])
    return df, warnings


def standardize_all(raw_tables, config):
    """STEP 2 for every source. Returns ({name: df}, [all drift warnings])."""
    tables, all_warnings = {}, []
    for source in config["input_sources"]:
        tables[source["name"]], warnings = standardize_source(
            raw_tables[source["name"]], source, config["column_types"])
        all_warnings.extend(warnings)
        df = tables[source["name"]]
        log.info("  standardized %-8s columns -> %s", source["name"], [c for c in df.columns if not c.startswith("_")])
        # DEBUG: the type of every column and how many values are missing after conversion.
        # A jump in missing values here usually means a format changed (e.g. a new date format).
        log.debug("  %s types: %s", source["name"], {c: str(t) for c, t in df.dtypes.items()})
        log.debug("  %s missing values: %s", source["name"], {c: int(n) for c, n in df.isna().sum().items() if n})
    return tables, all_warnings
