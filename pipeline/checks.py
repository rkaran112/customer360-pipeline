"""
STEP 6 - CHECKS: prove the results are right BEFORE anyone sees them.

Each check returns (name, passed True/False, details). If any check fails,
run_pipeline.py exits with code 1, so a scheduler (cron, Airflow) marks the run
as FAILED and nobody uses wrong numbers.
"""
from pipeline import quality


def run_checks(dq_report, clean_tables, c360):
    """Return a list of (check name, passed, details)."""
    results = []

    # 1. Every input record is accounted for: total = failures + successful.
    rule_total = dq_report["total_records"] - dq_report["successful_records"]
    results.append(("records reconciled", quality.reconcile(dq_report),
                    f"{dq_report['total_records']} = {rule_total} quarantined + {dq_report['successful_records']} passed"))

    # 2. One row per customer (the joins did not create duplicates).
    dupes = int(c360["customer_id"].duplicated().sum())
    results.append(("one row per customer", dupes == 0, f"{dupes} repeated customer_id"))

    # 3. Every clean CRM customer made it into the 360 (left join kept everyone).
    crm = len(clean_tables["crm"])
    results.append(("no customer lost in merge", len(c360) == crm, f"{crm} CRM customers -> {len(c360)} rows"))

    # 4. No money appeared or vanished in the merge.
    orders = clean_tables["orders"]
    matched = orders[orders["customer_id"].isin(set(c360["customer_id"]))]["lifetime_value"].sum()
    in_360 = c360["lifetime_value"].sum()
    results.append(("revenue conserved", abs(float(matched) - float(in_360)) < 0.01,
                    f"orders {float(matched):,.0f} = customer_360 {float(in_360):,.0f}"))

    # 5. No negative revenue reached the final data.
    negative = int((c360["lifetime_value"] < 0).sum())
    results.append(("no negative revenue published", negative == 0, f"{negative} rows"))

    # 6. Every customer has exactly one segment.
    unsegmented = int(c360["segment"].isna().sum()) if "segment" in c360 else len(c360)
    results.append(("every customer segmented", unsegmented == 0, f"{unsegmented} without segment"))

    # >>> TO CHANGE: a new check = append one more (name, passed, details) tuple here.
    return results
