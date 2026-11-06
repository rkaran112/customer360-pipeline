# Customer 360 — one trusted view of every customer

![CI](https://github.com/rkaran112/customer360-pipeline/actions/workflows/ci.yml/badge.svg)

**Live dashboard:** _link added after deployment on Streamlit Community Cloud_

An e-commerce company keeps customer data in three separate systems: **CRM** (JSON),
**Order Management** (CSV) and **Customer Support** (XML). Each system was built on its own,
so the same customer id is called `customer_id`, `cust_id` or `customerId`, and nobody can see
a customer's profile, spending and complaints together.

This project is a **data pipeline** that:
1. loads all three files,
2. gives them one common schema,
3. checks data quality (and quarantines bad records with a reason),
4. merges everything into **one row per customer** (the "Customer 360"),
5. produces business insights,
6. proves nothing was lost.

A **Streamlit dashboard** shows the results, including a live picture of the pipeline itself.

| | |
|---|---|
| Input | 1,501 records from 3 files (501 CRM + 500 orders + 500 tickets) |
| Data quality | 7 bad records caught and quarantined, 1,494 passed, **DQ score 99.53%** |
| Output | 497 customers in `customer_360` (CSV + JSON + Parquet) + 12 insight tables |
| Run time | under 1 second for the whole pipeline |
| Tests | 64 test functions = **81 test cases**, all passing, 98% code coverage, CI on Windows and Linux |

---

## 1. Run it

**Option A — one command** (needs Python 3.11+):
```bash
pip install -r requirements.txt
```
```bash
python run_pipeline.py
```
```bash
streamlit run streamlit_app.py
```

**Option B — double-click** `run.bat` on Windows. It creates its own Python environment,
installs everything, runs the pipeline and opens the dashboard.

**Option C — Docker** (same result on any machine):
```bash
docker build -t customer360 .
```
```bash
docker run -p 8501:8501 customer360
```
Then open http://localhost:8501.

**Option D — online:** open the live dashboard link at the top. The pipeline runs inside the
app, and you can **upload your own three files** from the sidebar to run it on new data.

**Run the tests:**
```bash
pip install -r requirements-dev.txt
```
```bash
pytest
```

**Command-line options:**
```bash
python run_pipeline.py --input-dir D:/new_files --output-dir D:/results --config config.json
```
The exit code is `0` when everything passed and `1` when the run failed (bad config, missing
file, schema drift or a failed check). Schedulers such as cron, Airflow and GitHub Actions use
that number to know whether a run failed.

---

## 2. How it works (the picture)

```
 crm_customers.json   orders.csv    support_tickets.xml        <- 3 systems, 3 formats
   (501 records)    (500 records)     (500 records)
          \               |               /
           v              v              v
     1. EXTRACT        read every value as text, tag each record with file + record number
           |  1501
     2. STANDARDIZE    cust_id / customerId -> customer_id, trim, lowercase email, real types,
           |  1501     schema drift guard (data contract)
     3. QUALITY GATE   5 rules in order ------------------------>  QUARANTINE (7 records + reason)
           |  1494                                                  1 duplicate, 3 invalid emails,
     4. MERGE          CRM LEFT JOIN orders LEFT JOIN tickets        1 missing field, 1 invalid id,
           |  497      = one row per customer                        1 negative revenue
     5. INSIGHTS       segments, RFM, top 10, city, support, Pareto, at-risk VIPs
           |  497
     6. CHECKS         6 reconciliation checks -> exit code 0 / 1
           |
     output/  customer_360.{csv,json,parquet}, dq_report.json, quarantine.csv,
              lineage.csv, insights/*.csv, run_metrics.json, run_history.jsonl
           |
     DASHBOARD  (streamlit_app.py)
```

| Stage | File | What happens | Rows in → out |
|---|---|---|---|
| 1 Extract | `pipeline/extract.py` | Reads JSON, CSV and XML. The reader is picked by the `format` in config. Every value is kept as **text**, so loading can never crash. `_source` and `_source_row` are added for lineage. | 1501 → 1501 |
| 2 Standardize | `pipeline/standardize.py` | Renames columns to one schema. Trims spaces, lowercases emails, title-cases cities, converts numbers and dates. Checks the columns against the data contract. **Never removes rows.** | 1501 → 1501 |
| 3 Quality gate | `pipeline/quality.py` | Runs 5 rules in order. The first rule a record breaks is its reason. Failed records go to quarantine. Builds `dq_report.json`. | 1501 → 1494 (+7 quarantined) |
| 4 Merge | `pipeline/merge.py` | CRM left join orders left join tickets on `customer_id`. Customers without orders or tickets are kept and flagged. | 1494 records → 497 customers |
| 5 Insights | `pipeline/insights.py` | Adds segment, RFM, average order value and recency. Builds 12 report tables and headline sentences. | 497 → 497 |
| 6 Checks | `pipeline/checks.py` | Proves the run is correct. Any failure gives exit code 1. | 497 → published |

`run_pipeline.py` only connects the steps, times them and writes the outputs.

---

## 3. Requirements

### Functional requirements — *what* the system does
| ID | Requirement | Implemented in |
|---|---|---|
| FR-1 | Load CRM (JSON), Orders (CSV) and Support (XML); the reader is picked by format in config | `extract.py` |
| FR-2 | Rename source columns to one schema (`cust_id`, `customerId` → `customer_id`) | `standardize.py` |
| FR-3 | Clean values: trim, lowercase email, title-case city, typed numbers and dates | `standardize.py` |
| FR-4 | Detect duplicates, invalid emails, missing mandatory fields, negative revenue and invalid customer ids | `quality.py` |
| FR-5 | Quarantine failed records with source, record number, rule and detail; never delete silently | `quality.py` |
| FR-6 | `dq_report.json` with the brief's keys, plus per-rule and per-source counts | `quality.py` |
| FR-7 | Reconcile: total = all failures + successful, otherwise the run fails | `quality.py`, `checks.py` |
| FR-8 | Customer 360: one row per customer, left join on `customer_id`; CSV + JSON + Parquet | `merge.py`, `outputs.py` |
| FR-9 | Insights: top 10 by revenue, Gold/Silver/Bronze, >5 tickets, revenue and customers by city | `insights.py` |
| FR-10 | Extra analytics: RFM, Pareto, support vs value, cohorts, city scorecard, at-risk VIPs, unhappy high-value | `insights.py` |
| FR-11 | Dashboard: live pipeline diagram, lineage tracer, analytics, data quality, customer lookup | `streamlit_app.py`, `diagram.py` |
| FR-12 | Upload your own 3 files in the dashboard and rerun the pipeline; download the results | `streamlit_app.py` |
| FR-13 | Record stage metrics (rows in/out, rejected, ms) and a run history | `run_pipeline.py`, `outputs.py` |
| FR-14 | Schema drift guard: compare arriving columns with the config "data contract" | `standardize.py` |

### Non-functional requirements — *how well* it does it
| ID | Quality | How it is met |
|---|---|---|
| NFR-1 | **Executable** | One command, `run.bat` double-click, Docker image, live URL |
| NFR-2 | **Configurable** | Sources, column mappings, mandatory fields, regex patterns, thresholds and as-of date all live in `config.json`; a new JSON/CSV/XML source needs **no code** |
| NFR-3 | **Scalable** | Every stage is a function: table in, table out, no hidden state; readers and rules are registries; see section 10 |
| NFR-4 | **Reliable** | Reconciliation, 6 checks, exit code 1, `validate="one_to_one"` joins, clear errors for a missing file or column |
| NFR-5 | **Reproducible / idempotent** | "Today" comes from the data, not the clock; duplicates resolved in a stable order; the same input gives byte-identical output (tested) |
| NFR-6 | **Observable** | Console + `logs/pipeline.log`, per-stage metrics, run history, lineage for every record |
| NFR-7 | **Testable** | 81 test cases, each with a "why", in CI on 2 operating systems × 2 Python versions |
| NFR-8 | **Fast** | Under 1 s for 1,501 records; the dashboard caches the run per set of input files |
| NFR-9 | **Maintainable** | A plain-English comment on every block, `>>> TO CHANGE:` markers, small single-purpose functions |
| NFR-10 | **Privacy** | No emails in insight reports; emails masked on screen (`am***@gmail.com`) |
| NFR-11 | **Portable** | Windows, Linux and Docker; pinned `requirements.txt` |

---

## 4. What was wrong in the data (and what happens to it)

I profiled every file before writing pipeline code. These are the actual defects:

| File | Record | Problem | Caught by | What happens |
|---|---|---|---|---|
| crm_customers.json | 501 | C0105 sent a second time (exact copy) | `duplicates` | first copy kept, second quarantined |
| crm_customers.json | 125, 250, 375 | email without `@` (`user125example.com`) | `invalid_emails` | customer quarantined (D-05) |
| orders.csv | 310 | C0310 `total_orders` is blank | `missing_fields` | order quarantined; C0310 stays in the 360 as "No order data" |
| orders.csv | 420 | C0420 `lifetime_value = -5000` | `negative_revenue` | order quarantined; C0420 shows "No order data" |
| support_tickets.xml | 450 | empty `<customerId />` | `invalid_customer_ids` | quarantined; C0450 has no ticket data |

The resulting `dq_report.json` (the first 5 keys are the brief's format):
```json
{
  "total_records": 1501,
  "duplicates": 1,
  "invalid_emails": 3,
  "missing_fields": 1,
  "successful_records": 1494,
  "invalid_customer_ids": 1,
  "negative_revenue": 1,
  "quarantined_records": 7,
  "dq_score_pct": 99.53,
  "by_source": { "crm": {"dq_score_pct": 99.2, ...}, "orders": {...}, "tickets": {...} },
  "referential_integrity": { "orders_without_customer": 3, "tickets_without_customer": 3, ... },
  "reconciled": true
}
```
*Orphans:* the orders and tickets of the 3 customers whose CRM record was quarantined have no
customer to attach to. They are **counted and reported**, never hidden.

---

## 5. Features that go beyond the brief

1. **Live pipeline diagram.** The Pipeline tab draws every stage with the real rows in/out
   and milliseconds from the last run, plus the quarantine branch. It is drawn from
   `run_metrics.json`, so it can never disagree with what actually ran.
2. **Record lineage tracer.** Pick any customer to see their journey through every file:
   *"orders.csv record 420 → extracted → standardized → QUARANTINED (negative revenue)"*.
   It answers "why is this customer missing?" in one click.
3. **Upload and run.** Upload three new files in the sidebar and the whole pipeline runs on
   them in the browser, quality report included.
4. **Schema drift guard.** An unexpected column gives a warning; a missing mandatory column stops the run
   with a clear message.
5. **DQ score per source system.** Shows which upstream team sends the dirtiest data.
6. **Run history.** Every run appends its DQ score and duration to `run_history.jsonl`; the
   dashboard charts the trend.
7. **Advanced analytics:**
   - **RFM segmentation:** Champions / Loyal / Promising / At risk / Hibernating, each with a recommended action.
   - **Pareto curve:** how concentrated revenue is.
   - **Support vs value:** a scatter plot and a correlation.
   - **Registration cohorts** and a **city scorecard**.
   - **At-risk VIPs:** Gold/Silver customers with no order for 90+ days.
   - **Unhappy high-value customers:** Gold customers with more than 5 tickets.
8. **Headline findings computed from the data**, never typed by hand. For example: *"293 Gold/Silver
   customers have not ordered for 90+ days: Rs 4.29 crore of lifetime value at risk."*
9. **Customer lookup.** Search by id or name to get one customer's 360° card, with the email masked.
10. **Downloads.** customer_360 (CSV/JSON), dq_report.json and quarantine.csv from the sidebar.

---

## 6. Decision log — every choice, and why

Each decision lists: **what** was decided, the **options** considered, **why** this option,
the **trade-off**, and **how to change it** if someone asks.

**D-01 · pandas (not DuckDB or Spark) for the transformations**
- **Options:** pandas, DuckDB SQL, PySpark.
- **Why:** 1,500 records fit in memory a thousand times over. pandas is the easiest to read line by
  line, and it reads JSON, CSV and XML directly.
- **Trade-off:** pandas uses a single machine. Every stage is a pure function, so moving to
  DuckDB or Spark only changes function bodies, not the design (section 10).
- **Change:** rewrite one stage file; `run_pipeline.py` and the tests stay.

**D-02 · Configuration-driven (`config.json`)**
- **Options:** hard-coded values, environment variables, a config file.
- **Why:** sources, column mappings, mandatory fields, regex patterns and thresholds change for business reasons.
  With a config file, those changes need no code. This is the brief's "brownie point" feature.
- **Trade-off:** one more file to read.
- **Change:** edit `config.json`. `test_changing_gold_threshold_in_config_changes_segments` proves it works.

**D-03 · Rules run in a fixed order; the first broken rule is the reason**
- **Options:** count every rule a record breaks, or count only the first.
- **Why:** each record is counted exactly once, so the report always adds up
  (total = failures + successful). The order runs from most basic to most specific:
  invalid id → duplicate → missing field → invalid email → negative revenue.
- **Trade-off:** a record that breaks two rules shows only the first reason.
- **Change:** reorder the `RULES` list in `quality.py`.

**D-04 · Quarantine bad records; never delete or silently fix them**
- **Why:** deleting hides problems from the source team, and guessing a fix (for example a missing
  `@`) can create wrong data. Quarantine keeps every record, with the rule, the detail, the file
  and the record number.
- **Change:** `quarantine.csv` can be re-fed after the source fixes it.

**D-05 · An invalid email quarantines the CRM record** *(choice agreed for this project)*
- **Options:** quarantine the record, or keep the customer with a blank email and a flag.
- **Why:** this matches the brief's report arithmetic (successful = total − duplicates −
  invalid emails − missing fields), and the email is a mandatory contact field.
- **Trade-off:** 3 customers' revenue is left out of the analytics until their emails are fixed.
- **Change:** remove `invalid_emails` from `RULES` and flag the email instead.

**D-06 · CRM is the master; LEFT JOIN orders and tickets**
- **Why:** only the CRM has name, email and city, so it defines who a customer is. A LEFT JOIN
  keeps customers who have no orders or tickets. An INNER join would silently drop them.
  `validate="one_to_one"` makes pandas fail instead of multiplying rows if a duplicate slips through.
- **Change:** `merge.py`, one line per join.

**D-07 · Customers without order data get the segment "No order data", not Bronze**
- **Why:** C0310's order says Rs 1.22 lakh but its order count is missing, so the record was
  rejected. Calling C0310 a low-value Bronze customer would be a wrong business message.
  The `has_order_data` and `has_ticket_data` flags keep "zero" and "unknown" apart.
- **Change:** `segment_customers()` in `insights.py`.

**D-08 · Zero revenue is allowed, negative revenue is rejected**
- **Why:** a newly registered customer can have 0. A negative lifetime value is impossible.
- **Change:** `rule_negative_revenue` in `quality.py`.

**D-09 · For duplicates, keep the first copy**
- **Why:** the files have no `updated_at` column, so there is no way to know which copy is newer.
  "First" is stable and repeatable. (If an `updated_at` is added, keep the newest instead.)
- **Change:** `rule_duplicates` in `quality.py`.

**D-10 · A blank customer id counts as an invalid id, not a missing field**
- **Why:** the brief lists "invalid customer IDs" as its own challenge, and the id rule runs first.
  A record without a usable id can never be joined.
- **Change:** order of `RULES`.

**D-11 · "Today" comes from the data, not the computer clock**
- **Why:** "days since last order" based on the real date would change every day, so reruns and
  tests would give different numbers. `as_of_date` in config, or the latest date in the
  data (2026-10-28), keeps every run reproducible.
- **Change:** set `"as_of_date": "2026-11-06"` in config.json.

**D-12 · Segment boundaries: Gold > 100000, Silver 50000–100000, Bronze < 50000**
- **Why:** taken from the brief. Exactly 100000 is **Silver**, because the brief says Gold is
  *more than* 100000. Boundary tests lock this in.
- **Change:** `config.json` → `segments`.

**D-13 · "More than 5 tickets" is strict (6 or more)**
- **Change:** `config.json` → `insights.high_ticket_threshold`.

**D-14 · RFM scores by thirds (tertiles) using rank-percent**
- **Why:** the scores adapt to any data size or spread, and work with even 1 customer. Fixed cut
  points would need re-tuning every time.
- **Change:** `tertile_score()` and `rfm_label()` in `insights.py`.

**D-15 · The 360 is written in three formats**
- **Why:** CSV for people and Excel, JSON for the brief's format and APIs, Parquet for analytics
  tools (compressed, typed, and fast for Spark, Power BI and warehouses).

**D-16 · The dashboard runs the pipeline itself (cached)**
- **Why:** the deployed app works from a fresh copy of the code with no pre-built files, and
  users can upload new files. A fingerprint of the input files decides when to rerun.
- **Trade-off:** the first page load runs the pipeline (under 1 second here).

**D-17 · Privacy: no emails in reports, emails masked on screen**
- **Why:** an email is personal data, and dashboards are widely shared. The full email exists
  only in the customer_360 export, for systems that need it.

**D-18 · Simple run metrics and run history instead of Airflow**
- **Why:** this is a single job on one machine. A scheduler would add setup without adding
  value. The exit code and `run_metrics.json` are exactly what Airflow would consume later.

**D-19 · Schema drift: warn on an extra column, stop on a missing mandatory column**
- **Why:** a new extra column is harmless, so the business keeps running. A missing
  `lifetime_value` would make every revenue number wrong, so it is better to stop loudly.

**D-20 · Deployed on Streamlit Community Cloud, with Docker as well**
- **Why:** Streamlit Cloud is free and deploys straight from GitHub, so every push redeploys.
  Docker covers any other server or cloud.

---

## 7. Tests — what each one checks and why

Run them with `pytest` (81 test cases from 64 test functions). Most tests use **tiny
hand-made files** in a temporary folder. That means they never depend on an earlier run,
each one shows exactly the defect it is about, and they never touch the real `output/`.

**Why these kinds of tests?**
- **Unit tests** tell you *which* rule broke.
- **Boundary tests** catch off-by-one business rules (100000 Silver or Gold?).
- **End-to-end tests** catch wiring mistakes between steps.
- **Idempotency tests** prove a rerun is safe.
- **Dashboard tests** prove the demo page opens.

| # | Test | What it checks | Why it matters |
|---|---|---|---|
| **Extract** | `tests/test_extract.py` | | |
| 1 | `test_json_loads_all_records_and_columns` | JSON rows and columns load | CRM is the master customer list |
| 2 | `test_csv_keeps_every_value_as_text` | `N/A` and `0012` stay text | a bad value must not crash the load |
| 3 | `test_xml_empty_tag_becomes_empty_text` | `<customerId />` → `''` | the exact defect in record 450 |
| 4 | `test_unknown_format_raises_clear_error` | `jsn` typo → clear error | config mistakes must be obvious |
| 5 | `test_missing_file_names_the_file` | error names `orders.csv` | the on-call person knows what to fix |
| 6–8 | `test_empty_file_gives_zero_rows_not_crash` ×3 | empty JSON/CSV/XML → 0 rows | quiet days happen |
| 9 | `test_tracking_columns_added` | `_source`, `_source_row` = 1,2,3 | powers lineage and quarantine |
| **Standardize** | `tests/test_standardize.py` | | |
| 10 | `test_different_id_names_become_customer_id` | `cust_id`/`customerId` → `customer_id` | the merge needs one key |
| 11 | `test_email_lowercased_city_titlecased_spaces_trimmed` | `" mumbai "` → `Mumbai` | otherwise city totals split |
| 12 | `test_bad_numbers_become_missing_not_crash` | `abc`, `2.5` orders → missing | step 3 reports them instead |
| 13 | `test_dates_parse_and_invalid_dates_become_missing` | `2026-13-45` → missing | date maths needs real dates |
| 14 | `test_extra_column_gives_warning_and_continues` | new column → warning | harmless changes don't stop the business |
| 15 | `test_missing_mandatory_column_stops_the_run` | no `lifetime_value` → error | revenue without revenue data is wrong |
| 16 | `test_standardize_never_removes_rows` | row counts unchanged | removing rows is step 3's job, where it's counted |
| **Quality** | `tests/test_quality.py` | | |
| 17 | `test_duplicate_keeps_first_and_quarantines_second` | first copy kept | duplicates double-count customers |
| 18–22 | `test_invalid_emails_are_rejected` ×5 | `user.com`, `a@b`, `@x.com`, spaces, the real defect | unreachable customers |
| 23–25 | `test_valid_emails_pass` ×3 | `a.b+1@x.co.in`, capitals, hyphens pass | a too-strict rule rejects good customers |
| 26 | `test_missing_mandatory_field_is_rejected` | blank `total_orders` named in detail | the brief's "missing values" |
| 27 | `test_negative_revenue_rejected_but_zero_allowed` | −5000 rejected, 0 allowed | negative LTV is impossible |
| 28–32 | `test_invalid_customer_ids_are_rejected` ×5 | blank, `C12`, `X0001`, `c0001`, `C00001` | an unjoinable id |
| 33 | `test_id_with_spaces_is_trimmed_and_passes` | `" C0001 "` passes | spaces are a slip, not a wrong id |
| 34 | `test_first_broken_rule_wins_and_counts_once` | bad id + bad email = 1 failure | the report must add up |
| 35 | `test_two_blank_ids_are_not_also_duplicates` | 2 invalid ids, 0 duplicates | proves the rule order works |
| 36 | `test_every_quarantined_row_has_reason_source_and_record` | no blank reasons | the source team must be able to fix it |
| 37 | `test_clean_and_quarantine_split_every_row_exactly_once` | no overlap, no loss | the core "nothing lost" guarantee |
| 38 | `test_report_has_every_key_from_the_brief` | 5 brief keys present | the output contract |
| 39 | `test_report_reconciles` | total = failures + successful | no record vanished |
| 40 | `test_reconcile_detects_a_broken_count` | a broken count → False | the safety check itself can fail |
| 41 | `test_dq_score_per_source` | 3/4 passed = 75% | finds the dirtiest system |
| **Merge** | `tests/test_merge.py` | | |
| 42 | `test_one_row_per_customer` | unique `customer_id` | a duplicate doubles revenue |
| 43 | `test_customer_without_orders_is_kept_and_flagged` | left join keeps them | an inner join drops customers silently |
| 44 | `test_customer_without_tickets_is_kept` | 0 tickets, flag False | no complaints is normal |
| 45 | `test_orphan_orders_are_counted_not_merged` | C0099 counted as an orphan | visible, not hidden |
| 46 | `test_output_has_the_brief_columns` | the brief's column order | the output contract |
| 47 | `test_brief_example_record_matches` | C0001 = brief's example values | proves the merge on known data |
| 48 | `test_revenue_is_conserved` | 380000 in = 380000 out | joins must not create or lose money |
| **Insights** | `tests/test_insights.py` | | |
| 49–53 | `test_segment_boundaries` ×5 | 100001 Gold, 100000 Silver, 50000 Silver, 49999 Bronze, 0 Bronze | off-by-one is the top business-rule bug |
| 54 | `test_no_order_data_segment` | unknown ≠ Bronze | don't guess value |
| 55 | `test_top_customers_sorted_and_limited` | max 10, sorted, works with 2 | the brief's revenue ranking |
| 56 | `test_more_than_5_tickets_is_strict` | 5 excluded, 6 included | "more than 5" |
| 57 | `test_city_totals_add_up` | city sums = totals | regional = overall |
| 58 | `test_rfm_scores_and_one_label_each` | scores 1–3, one label | every customer gets an action |
| 59 | `test_pareto_share_bounds` | 0–100, 100 for one customer | a percent outside 0–100 is a bug |
| 60 | `test_as_of_date_comes_from_config_not_the_clock` | 20 days exactly | reproducible results |
| 61 | `test_at_risk_vips_use_the_day_threshold` | only the old Gold order | no false alarms on a call list |
| 62 | `test_average_order_value_never_divides_by_zero` | 0 orders → empty | new customers have 0 orders |
| 63 | `test_headlines_are_computed_from_data` | sentences use data values | hand-typed findings go stale |
| 64–66 | `test_email_masking` ×3 | `am***@gmail.com`, `***` | privacy on screens |
| 67 | `test_inr_formatting` | lakh / crore | the business reads money in lakh/crore |
| **Config** | `tests/test_config.py` | | |
| 68 | `test_real_config_loads_with_all_sections` | all sections and 3 sources | broken config caught first |
| 69 | `test_config_missing_section_fails_clearly` | names `segments` | fail fast, not a KeyError later |
| 70 | `test_changing_gold_threshold_in_config_changes_segments` | 120000 → Silver at 150000 | proves "config driven" |
| **End to end** | `tests/test_end_to_end.py` (real files) | | |
| 71 | `test_exact_dq_numbers_on_real_data` | 1501 / 1 / 3 / 1 / 1 / 1 / 1494 | every planted defect caught |
| 72 | `test_all_checks_pass_on_real_data` | 6 checks green, 497 customers | release gate |
| 73 | `test_all_output_files_exist_and_are_not_empty` | 11 files written | downstream users depend on them |
| 74 | `test_rerun_gives_identical_results` | byte-identical 360 | idempotent = safe reruns |
| 75 | `test_stage_counts_flow_into_each_other` | out of one = in of next | diagram numbers are consistent |
| 76 | `test_lineage_explains_every_record` | 1501 lineage rows; C0420 reason | "why is X missing" is answerable |
| 77 | `test_cli_exit_code_zero_on_success` | exit 0 | schedulers read exit codes |
| 78 | `test_cli_exit_code_one_on_missing_file` | exit 1 + names the file | failures must be visible |
| 79 | `test_failed_check_gives_exit_code_one` | broken reconcile → 1 | wrong numbers never published as success |
| **Dashboard** | `tests/test_dashboard.py` | | |
| 80 | `test_app_runs_without_errors_and_has_all_tabs` | 7 tabs, no exception | a broken demo is the worst failure |
| 81 | `test_customer_lookup_shows_the_customer` | C0001 card, masked email | the core "360" feature |

CI (`.github/workflows/ci.yml`) runs the pipeline and every test on **Windows + Linux ×
Python 3.12 + 3.13** on every push, with a coverage report.

---

## 8. Outputs

| File | Grain (one row per…) | What it is for |
|---|---|---|
| `output/customer_360.csv/.json/.parquet` | customer | the unified dataset: brief columns + `has_order_data`, `has_ticket_data`, `segment`, `avg_order_value`, `days_since_last_order`, `days_since_last_ticket`, `high_ticket_customer`, `rfm_r/f/m`, `rfm_label` |
| `output/dq_report.json` | run | data-quality report (brief format + extras) |
| `output/quarantine.csv` | rejected record | source, record number, customer_id, rule, detail |
| `output/lineage.csv` | input record | file, record number, status (MERGED / QUARANTINED / ORPHAN) |
| `output/run_metrics.json` | stage | rows in/out/rejected, ms, checks |
| `output/run_history.jsonl` | run (appended) | DQ score and duration trend |
| `output/insights/top_customers.csv` | customer (10) | top 10 by revenue |
| `output/insights/segment_summary.csv` | segment | Gold/Silver/Bronze counts, revenue and shares |
| `output/insights/high_ticket_customers.csv` | customer | more than 5 tickets |
| `output/insights/revenue_by_city.csv`, `customers_by_city.csv`, `city_scorecard.csv` | city | regional analytics |
| `output/insights/rfm_summary.csv` | RFM group | size, revenue, action |
| `output/insights/at_risk_vips.csv`, `unhappy_high_value.csv` | customer | action lists |
| `output/insights/pareto_curve.csv`, `support_vs_value.csv`, `registration_cohorts.csv` | point / segment / month | analytics |
| `output/insights/headlines.json` | sentence | computed findings |
| `logs/pipeline.log` | log line | what happened in each run |

---

## 9. How to change things (search for `>>> TO CHANGE` in the code)

| If you're asked to… | Do this |
|---|---|
| change the Gold/Silver limits | `config.json` → `segments` (no code) |
| change "more than 5 tickets" or "90 days at risk" | `config.json` → `insights` (no code) |
| add a new **source** in JSON/CSV/XML | add an entry to `config.json` → `input_sources` with file, format, `column_mapping` and `mandatory_fields`; add its columns in `merge.py` |
| support a new **file format** (e.g. Excel) | write `read_excel()` in `extract.py` and add one line to `READERS` |
| add a **data-quality rule** (e.g. registration date in the future) | write `rule_xxx()` in `quality.py` and add one line to `RULES`; add the key to the report |
| make a field mandatory | add it to that source's `mandatory_fields` in `config.json` |
| keep customers with a bad email instead of quarantining them | remove the `invalid_emails` line from `RULES`; flag the email in `standardize.py` |
| add a **KPI / insight** | write a function in `insights.py` and add one line to `INSIGHTS` (it is exported automatically); add a chart in `streamlit_app.py` |
| add a **check** | append one tuple in `checks.py` |
| add a **segment tier** (Platinum) | one condition in `segment_customers()` + a threshold in config |
| change the email or id format | regex in `config.json` → `quality` |
| fix "today" to a date | `config.json` → `as_of_date` |

After any change, run `python run_pipeline.py` and then `pytest`.

---

## 10. Scaling: what changes at 100× and 10,000×

The **design stays the same**: extract → standardize → quality gate → merge → insights →
checks, with config, quarantine, reconciliation and lineage. Only the engine changes.

| Size | What I'd change |
|---|---|
| **Today** (~1.5 K records) | pandas, single run, under 1 s |
| **100×** (~150 K) | still pandas, or DuckDB SQL inside the same functions; Parquet input; partition outputs by date |
| **10,000×** (~15 M+) | PySpark or a cloud warehouse (BigQuery/Snowflake/Redshift) running the same steps as SQL/dbt models; files land in S3/ADLS; Airflow schedules the run and reads the exit code; incremental loads (only new or changed files, keyed by a file log); `MERGE` upserts instead of full rebuilds |
| **Real-time** | orders and tickets as Kafka events; the 360 profile updated per event; the same rules as streaming filters |

Why it scales:
- Every stage is a **pure function** (a table in, a table out, no hidden state).
- Readers and rules are **registries**, so adding one never touches existing code.
- All business values live in **config**.

---

## 11. Limitations and next steps (stated up front)

**Limitations:**
- **Full rebuild every run.** There is no incremental load. That is fine at this size; at scale, add a
  processed-file log and upserts.
- **Duplicates keep the first copy.** With an `updated_at` column the pipeline should keep the newest.
- **Customer history is not kept.** A changed city overwrites the old one. Next step: **SCD Type 2**
  (keep every version with valid-from and valid-to dates).
- **The dashboard's run history lives on the server's disk.** On Streamlit Cloud it resets when the app restarts.

**Next steps, in order of value:**
1. **Data contracts with the source teams**, so schema changes are agreed, not discovered.
2. **Alerting** on DQ score drops and volume changes. The run history already has the numbers.
3. **Fuzzy duplicate detection.** Today only an exact repeated id is caught; the same email under two ids would get through.
4. **Feed quarantine fixes back automatically.**

---

## 12. Glossary (plain words)

- **Pipeline:** a fixed series of steps that data passes through, like an assembly line.
- **ETL / ELT:** Extract, Transform, Load. This project extracts, transforms in Python, then loads (writes) the outputs.
- **Schema:** the list of columns and their types. "Standardize the schema" means everyone uses the same column names.
- **Data quality rule:** a test every record must pass, for example "the email contains @".
- **Quarantine:** a holding area for records that failed a rule, kept with the reason.
- **Reconciliation:** proving the counts add up, so no record was lost or invented.
- **Customer 360:** everything about one customer (profile, orders, support) in one row.
- **LEFT JOIN:** keep every row from the left table (CRM), and attach matching rows from the right table if any exist.
- **Orphan:** a record that points to a customer who doesn't exist in the clean customer list.
- **Idempotent:** running it twice gives the same result, so a rerun is always safe.
- **Lineage:** where a piece of data came from and what happened to it on the way.
- **Schema drift:** a source file suddenly arrives with different columns.
- **Segmentation:** splitting customers into groups (Gold/Silver/Bronze) to treat them differently.
- **RFM:** Recency (how recently they bought), Frequency (how often), Monetary (how much).
- **Pareto / 80-20:** checking whether a small share of customers brings most of the money.
- **Parquet:** a compressed, column-based file format that analytics tools read very fast.
- **Exit code:** the number a program returns when it ends. 0 means OK, anything else means failed.
- **CI:** Continuous Integration. Tests run automatically on every push.

---

## Project layout
```
config.json            every setting (sources, rules, thresholds)
run_pipeline.py        one command: runs steps 1-6, logs, exit code
streamlit_app.py       dashboard (7 tabs), runs the pipeline itself
pipeline/
  config.py            loads config.json
  logger.py            console + logs/pipeline.log
  extract.py           STEP 1
  standardize.py       STEP 2
  quality.py           STEP 3
  merge.py             STEP 4
  insights.py          STEP 5
  checks.py            STEP 6
  outputs.py           writes output/
  diagram.py           pipeline picture for the dashboard
  privacy.py           email masking
tests/                 81 test cases
data/raw/              the 3 source files from the brief
run.bat, Dockerfile, .github/workflows/ci.yml, requirements*.txt
```
