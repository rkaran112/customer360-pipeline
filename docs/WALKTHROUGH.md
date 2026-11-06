# Walkthrough — learn the project in 45 minutes, then present it in 5

## Read the code in this order (about 45 minutes)

| # | File | Time | What to understand |
|---|---|---|---|
| 1 | `config.json` | 3 min | Every setting lives here. Look at `input_sources`: the file, format, `column_mapping` and `mandatory_fields` for each source. |
| 2 | `run_pipeline.py` → `run()` | 5 min | The 6 steps called in order, each one timed into `stages`. `main()` turns the result into exit code 0 or 1. |
| 3 | `pipeline/extract.py` | 5 min | `READERS` maps a format to a reader function. Every value is read as text. `_source` and `_source_row` are added for lineage. |
| 4 | `pipeline/standardize.py` | 7 min | `check_schema` (drift guard), rename via `column_mapping`, `clean_text`, `COLUMN_TIDY`, `TYPE_CONVERTERS`. |
| 5 | `pipeline/quality.py` | 10 min | **The heart of the project.** The `RULES` list; `check_source` (first broken rule wins, only still-passing rows are checked); `build_dq_report`; `reconcile`. |
| 6 | `pipeline/merge.py` | 5 min | Two LEFT JOINs with `validate="one_to_one"`, the `has_*_data` flags, orphans. |
| 7 | `pipeline/insights.py` | 7 min | `enrich` (segment, AOV, recency, RFM), the `INSIGHTS` registry, and `headlines` built from the numbers. |
| 8 | `pipeline/checks.py` | 2 min | 6 checks; any failure means exit code 1. |
| 9 | `streamlit_app.py` | skim | `run_cached` + `folder_signature` (why the cloud app works), then one block per tab. |

Run these three commands and watch the log while you read:
```bash
python run_pipeline.py
```
```bash
pytest
```
```bash
streamlit run streamlit_app.py
```

## The 5-minute demo

1. **Problem (30 s):** "Three systems, three formats, three names for the same id. The business can't
   see one customer's profile, spend and support history together."
2. **Pipeline tab (90 s):** walk the diagram top to bottom with the real numbers:
   - 1501 records come in from 3 files.
   - Standardize renames to one schema.
   - The quality gate quarantines 7 records, each with a reason.
   - The merge produces 497 customers.
   - All 6 checks are green.
   Then show the **lineage tracer** on C0420: "this customer's order had −5000 revenue, so it was
   quarantined, and the dashboard shows exactly why they have no order data."
3. **Data Quality tab (45 s):** the brief's JSON report, the reconciliation equation, the DQ score
   per source system ("orders is our dirtiest source"), and quarantine with reasons.
4. **Overview → Revenue → Analytics (75 s):** the top 10, Gold/Silver/Bronze, city revenue. Then the
   extras: RFM with actions, the Pareto curve, and at-risk VIPs ("Rs 4.29 crore at risk, here is the call list").
5. **Executable (30 s):** one command, `run.bat`, Docker, the live URL, CI green, and
   **upload your own files** in the sidebar.
6. **Close (30 s):** "Every setting is in config.json, every rule is one line, 81 tests. At scale, the
   same stages move to Spark or a warehouse; the design doesn't change."

## Questions you are likely to get (short answers)

- **Why pandas and not Spark?** It's 1,500 rows. Spark would add a cluster and add nothing else. Each stage is a pure
  function, so switching engines only changes function bodies (README D-01, section 10).
- **Why quarantine instead of fixing emails?** Guessing a fix creates wrong data. Quarantine keeps the record and the
  reason, so the source team can fix it at the source.
- **Why LEFT JOIN?** CRM is the master list. An inner join would silently drop C0310, C0420 and C0450.
- **How do you know nothing was lost?** By reconciliation: 1501 = 1 + 3 + 1 + 1 + 1 + 1494. Six checks run every time, and a failure gives exit code 1.
- **What if the file format changes?** The schema drift guard warns on an extra column and stops on a missing mandatory one.
- **Is it idempotent?** Yes. "Today" comes from the data, duplicates are resolved in a stable order, and a test proves two
  runs produce byte-identical output.
- **How would you schedule it?** With cron or Airflow calling `python run_pipeline.py`. The exit code tells the scheduler
  whether the run failed.
- **What would you add next?** Incremental loads, SCD Type 2 history, alerting on DQ drops, and fuzzy duplicate detection.

## Live-change practice (each takes 1–3 minutes)

1. "Gold should start at 1.5 lakh." Edit `config.json` → `segments.gold_above: 150000`, then rerun.
2. "Reject registrations in the future." Add `rule_future_registration` to `RULES` in `quality.py`.
3. "Add a KPI: revenue per registration year." Add a function to `INSIGHTS` in `insights.py`.
4. "More than 8 tickets is high." Edit `config.json` → `insights.high_ticket_threshold: 8`.
5. "Make city mandatory." Add `"city"` to CRM `mandatory_fields` in `config.json`.
