"""
STEP 3 - QUALITY GATE: test every record, let good ones through, quarantine bad ones.

Input : the standardized tables from step 2
Output: - clean tables (only records that passed every rule)
        - quarantine table (every failed record + WHICH rule + WHY, e.g. "bad email: user125example.com")
        - dq_report (the JSON the brief asks for, plus per-source scores)
        - lineage (one line per input record: passed or quarantined, and why)

How the rules work (decision D-03):
    RULES below is an ordered list. Each record is checked rule by rule, and the
    FIRST rule it breaks is its reason. So a record is counted exactly once, and
    the report always adds up:  total = sum of all failures + successful.

Why quarantine and not delete?  (decision D-04)
    Deleting bad rows hides problems. Quarantined rows are kept with a reason, so
    the source team can fix them and nothing is ever silently lost.
"""
import re

import pandas as pd

from pipeline.logger import get_logger

log = get_logger()


# ----------------------------------------------------------------- the rules
# Every rule has the same shape:  (table, source config, full config) -> a Series of text.
# For each row: None = the row PASSES this rule;  some text = the row FAILS, text says why.
# A rule only runs if the column it needs exists in that source (no email in orders -> skip).

def rule_invalid_customer_id(df, source, config):
    """customer_id must match the pattern in config, e.g. C0001. Blank also counts as invalid."""
    pattern = re.compile(config["quality"]["customer_id_pattern"])
    def check(value):
        if isinstance(value, str) and pattern.fullmatch(value):
            return None                                   # valid -> passes
        shown = "" if pd.isna(value) else value           # show blank ids as '' in the report
        return f"bad customer_id: '{shown}'"
    return df["customer_id"].map(check)


def rule_duplicates(df, source, config):
    """The same customer_id appears again in the same source: keep the FIRST, reject the rest."""
    # WHY keep the first (D-09): the source files have no 'updated_at' column, so we cannot
    # tell which copy is newer; the first one is the stable, repeatable choice.
    repeated = df["customer_id"].duplicated(keep="first")
    return df["customer_id"].where(repeated).map(
        lambda v: None if pd.isna(v) else f"customer_id {v} already seen in this source")


def rule_missing_fields(df, source, config):
    """Every mandatory field (listed per source in config.json) must have a value."""
    mandatory = [c for c in source.get("mandatory_fields", []) if c in df.columns]

    def missing_for(row):
        empty = [c for c in mandatory if pd.isna(row[c])]
        return f"missing: {', '.join(empty)}" if empty else None
    return df.apply(missing_for, axis=1) if len(df) else pd.Series(dtype="object")


def rule_invalid_emails(df, source, config):
    """email must look like name@domain.tld (pattern in config.json)."""
    pattern = re.compile(config["quality"]["email_pattern"])
    return df["email"].map(lambda v: None if pattern.fullmatch(v) else f"bad email: '{v}'")


def rule_negative_revenue(df, source, config):
    """lifetime_value below zero is impossible. Zero is allowed (a customer who never paid yet, D-08)."""
    return df["lifetime_value"].map(lambda v: f"negative lifetime_value: {v}" if v < 0 else None)


# The ORDER matters: first broken rule wins. (rule name, function, column it needs)
# The rule names are also the keys in dq_report.json.
# >>> TO CHANGE: a new data-quality rule = write one rule_xxx function above and add one line here.
#     Example: ("future_dates", rule_future_dates, "registration_date")
RULES = [
    ("invalid_customer_ids", rule_invalid_customer_id, "customer_id"),
    ("duplicates", rule_duplicates, "customer_id"),
    ("missing_fields", rule_missing_fields, "customer_id"),
    ("invalid_emails", rule_invalid_emails, "email"),
    ("negative_revenue", rule_negative_revenue, "lifetime_value"),
]


# ----------------------------------------------------------------- applying the rules

def check_source(df, source, config):
    """Run every rule on one source. Returns (clean_df, quarantine_df)."""
    reason = pd.Series(None, index=df.index, dtype="object")
    detail = pd.Series(None, index=df.index, dtype="object")

    for rule_name, rule, needed_column in RULES:
        if needed_column not in df.columns:
            continue
        # WHAT: only check rows that are still passing (first broken rule wins).
        # WHY: two blank ids should count as 2 invalid ids, not also as 1 duplicate.
        #      And a row with a bad id and a bad email is counted ONCE, as a bad id.
        still_ok = reason.isna()
        if not still_ok.any():
            break
        remaining = df[still_ok]
        # Missing values cannot be "invalid" emails or "negative" revenue - they are
        # missing, and rule_missing_fields (earlier in the list) already caught them.
        if rule_name not in ("invalid_customer_ids", "duplicates", "missing_fields"):
            remaining = remaining[remaining[needed_column].notna()]
        problems = rule(remaining, source, config)
        failed = problems.dropna().index
        reason.loc[failed] = rule_name
        detail.loc[failed] = problems.loc[failed]
        # DEBUG: how many rows each rule rejected, with up to 3 examples.
        if len(failed):
            log.debug("    rule %-20s %-8s rejected %d, e.g. %s", rule_name, source["name"], len(failed),
                      list(problems.loc[failed].head(3)))

    clean = df[reason.isna()].copy()
    quarantine = df[reason.notna()].copy()
    quarantine.insert(0, "reason", reason[reason.notna()])
    quarantine.insert(1, "detail", detail[reason.notna()])
    return clean, quarantine


def check_all(tables, config):
    """STEP 3 for every source. Returns (clean tables, one quarantine table, lineage table)."""
    clean_tables, quarantines = {}, []
    for source in config["input_sources"]:
        name = source["name"]
        clean, quarantine = check_source(tables[name], source, config)
        clean_tables[name] = clean
        quarantines.append(quarantine[["_source", "_source_row", "customer_id", "reason", "detail"]])
        log.info("  quality   %-8s %5d in -> %5d passed, %d quarantined",
                 name, len(tables[name]), len(clean), len(quarantine))

    quarantine_all = pd.concat(quarantines, ignore_index=True)
    lineage = build_lineage(tables, quarantine_all, config)
    return clean_tables, quarantine_all, lineage


def build_lineage(tables, quarantine, config):
    """One line per input record: which file, which record number, passed or quarantined.

    This powers the lineage tracer on the dashboard: "C0420 -> orders.csv record 420 ->
    QUARANTINED: negative lifetime_value". Step 4 adds whether passed records were merged.
    """
    files = {s["name"]: s["file"] for s in config["input_sources"]}
    rows = []
    for name, df in tables.items():
        part = df[["_source", "_source_row", "customer_id"]].copy()
        part["file"] = files[name]
        rows.append(part)
    lineage = pd.concat(rows, ignore_index=True)
    lineage = lineage.merge(quarantine[["_source", "_source_row", "reason", "detail"]],
                            on=["_source", "_source_row"], how="left")
    lineage["status"] = lineage["reason"].map(lambda r: "PASSED" if pd.isna(r) else "QUARANTINED")
    return lineage


# ----------------------------------------------------------------- the DQ report

def build_dq_report(tables, clean_tables, quarantine, drift_warnings):
    """The data-quality report. First 5 keys are exactly the ones in the brief."""
    total = sum(len(df) for df in tables.values())
    failures = {rule_name: int((quarantine["reason"] == rule_name).sum()) for rule_name, _, _ in RULES}
    successful = sum(len(df) for df in clean_tables.values())

    by_source = {}
    for name, df in tables.items():
        q = quarantine[quarantine["_source"] == name]
        passed = len(clean_tables[name])
        by_source[name] = {
            "total": len(df),
            "passed": passed,
            "quarantined": len(q),
            # DQ score = % of records that passed. 100 = perfect source. (Empty source = 100.)
            "dq_score_pct": round(100 * passed / len(df), 2) if len(df) else 100.0,
            "failures": {k: int(v) for k, v in q["reason"].value_counts().items()},
        }

    report = {
        "total_records": total,
        "duplicates": failures["duplicates"],
        "invalid_emails": failures["invalid_emails"],
        "missing_fields": failures["missing_fields"],
        "successful_records": successful,
        # ---- extras beyond the brief
        "invalid_customer_ids": failures["invalid_customer_ids"],
        "negative_revenue": failures["negative_revenue"],
        "quarantined_records": len(quarantine),
        "dq_score_pct": round(100 * successful / total, 2) if total else 100.0,
        "by_source": by_source,
        "schema_drift_warnings": drift_warnings,
    }
    report["reconciled"] = reconcile(report)
    return report


def reconcile(report):
    """PROOF that no record was lost:  total = every kind of failure + successful."""
    rule_names = [rule_name for rule_name, _, _ in RULES]
    return report["total_records"] == sum(report[k] for k in rule_names) + report["successful_records"]
